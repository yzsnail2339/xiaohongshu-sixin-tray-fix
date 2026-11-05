#!/usr/bin/env python3
"""Test supported local client code without modifying the installation.

The proprietary client is never downloaded or stored in this repository.
Only three required JavaScript files are extracted into a system temporary
directory, which is removed when the test completes or raises an exception.
The Node VM uses test doubles; it is not a security sandbox. The patcher's
known-original validation runs before any extracted client code is executed.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


SOURCE_FILES = (
    "main/main.js",
    "main/windows.js",
    "main/tray/tray.js",
)


def load_patcher(repository: Path):
    """Import the adjacent patcher without creating a repository bytecode file."""
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location(
        "local_client_test_patcher", repository / "patcher.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load patcher.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def extract_test_sources(patcher, archive: bytes, destination: Path) -> None:
    header, payload = patcher.parse_asar(archive)
    for relative in SOURCE_FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(patcher.read_entry(header, payload, relative))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--asar", required=True, type=Path,
        help="Explicit path to your supported original app.asar (read only).",
    )
    parser.add_argument(
        "--node", default="node",
        help="Node.js executable or path (default: node from PATH).",
    )
    args = parser.parse_args(argv)
    node = shutil.which(args.node)
    if node is None:
        parser.error("Node.js was not found; install Node.js or supply --node.")
    archive_path = args.asar.expanduser().resolve()
    if not archive_path.is_file():
        parser.error("--asar must point to an existing original app.asar file.")

    repository = Path(__file__).resolve().parent.parent
    patcher = load_patcher(repository)
    original = archive_path.read_bytes()
    # Build performs the version/hash guard before any source is executed.
    try:
        if patcher.package_status(original) != "original":
            parser.error("These comparison tests need the original, unpatched app.asar.")
        patched = patcher.build_patch(original)
    except patcher.PatchError as error:
        parser.error(str(error))
    with tempfile.TemporaryDirectory(prefix="sxt-tray-client-test-") as temporary:
        temporary_root = Path(temporary)
        original_root = temporary_root / "original"
        patched_root = temporary_root / "patched"
        extract_test_sources(patcher, original, original_root)
        extract_test_sources(patcher, patched, patched_root)
        result = subprocess.run(
            [
                node, str(repository / "tests" / "client_regression.cjs"),
                "--original-dir", str(original_root),
                "--patched-dir", str(patched_root),
            ],
            cwd=temporary_root,
            check=False,
            timeout=60,
        )
        return result.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Tests interrupted; temporary files have been cleaned up.", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Local client tests failed: {error}", file=sys.stderr)
        raise SystemExit(1)
