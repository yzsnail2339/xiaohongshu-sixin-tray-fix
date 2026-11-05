#!/usr/bin/env python3
"""Offline, version-locked repair for the Windows SixinTong 1.0.11 tray.

Only small replacement fragments are included, never the client package.
Check and build do not modify the installed client. Apply/restore require Windows,
the exact verified executable, and a fully exited client. No network is used.
"""

import argparse
import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile


ORIGINAL_SHA256 = "9b7ecb2d9f504150ba9bf24cab0429052b398087eeec52fa959cd28656df5bd9"
PATCHED_SHA256 = "5d4494c807d8eab04376d1339e73a912252f864eaa604083ca16bbd1c805a41d"
EXE_SHA256 = "8ea8920336b42e8fab6f323d54dad67535d8e852e34d3f3c5c5393749677e65f"
EXE_NAME = "小红书私信通.exe"
BACKUP_SUFFIX = ".tray-restore.original"
FUSE_SENTINEL = b"dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX"

# Minimal context needed to identify and repair the two original handlers.
REPLACEMENTS = {
    "main/windows.js": (
        (
            """const showAllWindow = () => {
  windows.forEach((win) => {
    win.show()
    win.focus()
  })
}""",
            """const restoreWindow = (win) => {
  if (!win || win.isDestroyed() || global.willQuitApp) return
  win.setSkipTaskbar(false)
  if (win.isMinimized()) win.restore()
  win.show()
  win.focus()
}

exports.restoreWindow = restoreWindow

const showAllWindow = () => {
  Array.from(windows).forEach(restoreWindow)
}""",
        ),
        (
            "exports.getAllWindows = () => Array.from(windows)",
            "exports.getAllWindows = () => Array.from(windows).filter((win) => !win.isDestroyed())",
        ),
    ),
    "main/tray/tray.js": (
        (
            "const { getAllWindows, createWindow, showAboutWindow, clearEndpoint } = require('../windows')",
            "const { getAllWindows, createWindow, showAboutWindow, clearEndpoint, restoreWindow, showAllWindow } = require('../windows')",
        ),
        (
            """    this.appTray.on('click', () => {
      if (this.timer) {
        this.stopFlash()
      }
    })""",
            """    const restoreWindows = () => {
      if (global.willQuitApp) return
      if (this.timer) {
        this.stopFlash()
      }
      showAllWindow()
    }
    this.appTray.on('click', restoreWindows)
    this.appTray.on('double-click', restoreWindows)""",
        ),
        (
            """        submenu: [
          {
            label: '退出窗口',
            click: () => {
              win.destroy()
            },
          },
        ],
        click: () => {
          win.show()
          win.focus()
        },""",
            """        submenu: [
          {
            label: '显示窗口',
            click: () => restoreWindow(win),
          },
          {
            label: '退出窗口',
            click: () => {
              if (!win.isDestroyed()) win.destroy()
            },
          },
        ],""",
        ),
    ),
}


class PatchError(Exception):
    """A failed precondition; the tool must not proceed with installation."""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise PatchError(message)


def parse_asar(blob):
    """Parse the Chromium Pickle header without extracting any files."""
    require(len(blob) >= 16, "Truncated ASAR header.")
    size_payload, header_size, payload_size, text_size = struct.unpack_from("<IIII", blob)
    require(size_payload == 4, "Unsupported ASAR size header.")
    require(header_size == payload_size + 4 and header_size % 4 == 0,
            "Invalid ASAR header size.")
    require(4 + text_size <= payload_size < 8 + text_size,
            "Invalid ASAR JSON length or padding.")
    data_start = 8 + header_size
    require(data_start <= len(blob), "Truncated ASAR archive.")
    require(not any(blob[16 + text_size:data_start]), "Nonzero ASAR header padding.")
    try:
        header = json.loads(blob[16:16 + text_size].decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise PatchError("Invalid ASAR JSON header.") from error
    require(isinstance(header, dict) and isinstance(header.get("files"), dict),
            "ASAR file tree is missing.")
    return header, blob[data_start:]


def entry(header, path):
    node = header
    try:
        for part in path.split("/"):
            node = node["files"][part]
    except (KeyError, TypeError) as error:
        raise PatchError("ASAR entry is missing: " + path) from error
    return node


def entries(header, prefix=""):
    for name, node in header["files"].items():
        require(isinstance(node, dict), "Invalid ASAR entry.")
        path = prefix + name
        if "files" in node:
            require(isinstance(node["files"], dict), "Invalid ASAR directory.")
            yield from entries(node, path + "/")
        else:
            yield path, node


def read_entry(header, payload, path):
    node = entry(header, path)
    require(not node.get("unpacked") and "link" not in node,
            "Cannot read an external ASAR entry: " + path)
    try:
        start, size = int(node["offset"]), node["size"]
    except (KeyError, TypeError, ValueError) as error:
        raise PatchError("Invalid ASAR offset: " + path) from error
    require(isinstance(size, int) and not isinstance(size, bool) and start >= 0
            and size >= 0 and start + size <= len(payload),
            "ASAR entry exceeds the archive: " + path)
    return payload[start:start + size]


def integrity_for(content, block_size):
    require(isinstance(block_size, int) and block_size > 0, "Invalid integrity block size.")
    return {
        "algorithm": "SHA256",
        "hash": sha256(content),
        "blockSize": block_size,
        "blocks": [sha256(content[i:i + block_size]) for i in range(0, len(content), block_size)],
    }


def validate_asar(header, payload):
    count = 0
    for path, node in entries(header):
        if node.get("unpacked") or "link" in node:
            continue
        content = read_entry(header, payload, path)
        if "integrity" in node:
            info = node["integrity"]
            require(isinstance(info, dict) and info.get("algorithm") == "SHA256",
                    "Unsupported ASAR integrity algorithm: " + path)
            require(info == integrity_for(content, info.get("blockSize")),
                    "ASAR entry integrity mismatch: " + path)
        count += 1
    return count


def pack_asar(header, payload):
    text = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    size = (4 + len(text) + 3) & ~3
    pickle = struct.pack("<II", size, len(text)) + text + bytes(size - 4 - len(text))
    return struct.pack("<II", 4, len(pickle)) + pickle + payload


def patch_source(path, raw):
    eol = b"\r\r\n" if b"\r\r\n" in raw else b"\r\n" if b"\r\n" in raw else b"\n"
    original = raw.decode("utf-8").replace(eol.decode(), "\n")
    require(original.replace("\n", eol.decode()).encode("utf-8") == raw,
            "Unexpected source line endings: " + path)
    text = original
    for old, new in REPLACEMENTS[path]:
        require(text.count(old) == 1, "Source pattern missing or ambiguous: " + path)
        text = text.replace(old, new, 1)
    return text.replace("\n", eol.decode()).encode("utf-8")


def _build_patch(blob):
    """Internal transformation; callers must verify the supported input hash."""
    header, payload = parse_asar(blob)
    validate_asar(header, payload)
    result_header = copy.deepcopy(header)
    result_payload = bytearray(payload)
    changed = {}
    for path in REPLACEMENTS:
        content = patch_source(path, read_entry(header, payload, path))
        changed[path] = content
        node = entry(result_header, path)
        node["offset"], node["size"] = str(len(result_payload)), len(content)
        if "integrity" in node:
            node["integrity"] = integrity_for(content, node["integrity"]["blockSize"])
        result_payload.extend(content)
    result = pack_asar(result_header, result_payload)
    checked_header, checked_payload = parse_asar(result)
    validate_asar(checked_header, checked_payload)
    require(set(dict(entries(header))) == set(dict(entries(checked_header))), "ASAR entry set changed.")
    for path, old_node in entries(header):
        new_node = entry(checked_header, path)
        if path not in changed:
            require(old_node == new_node, "Unrelated ASAR metadata changed: " + path)
        if old_node.get("unpacked") or "link" in old_node:
            continue
        expected = changed.get(path, read_entry(header, payload, path))
        require(read_entry(checked_header, checked_payload, path) == expected,
                "ASAR round-trip verification failed: " + path)
    return result


def package_status(blob):
    digest = sha256(blob)
    if digest == ORIGINAL_SHA256:
        return "original"
    if digest == PATCHED_SHA256:
        return "patched"
    raise PatchError("Unsupported package SHA-256: " + digest
                     + ". Only the verified 1.0.11 package is supported; no files were changed.")


def build_patch(blob):
    status = package_status(blob)
    if status == "patched":
        validate_asar(*parse_asar(blob))
        return blob
    result = _build_patch(blob)
    require(sha256(result) == PATCHED_SHA256, "Patch output differs from the verified build.")
    return result


def verify_executable(blob):
    require(sha256(blob) == EXE_SHA256, "Unsupported client executable; installation refused.")
    require(blob.count(FUSE_SENTINEL) == 1, "Unknown Electron fuse format; installation refused.")
    index = blob.index(FUSE_SENTINEL) + len(FUSE_SENTINEL)
    require(len(blob) >= index + 2, "Truncated Electron fuse header.")
    version, count = blob[index:index + 2]
    require(version == 1 and count >= 5 and len(blob) >= index + 2 + count,
            "Unsupported Electron fuse version.")
    require(blob[index + 2 + 4] == ord("0"),
            "Embedded ASAR integrity validation is enabled or unknown; installation refused.")


def running_client_pids(exe):
    """Enumerate exact executable paths with read-only Windows process APIs."""
    require(os.name == "nt", "Applying or restoring is supported only on Windows.")
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = kernel.Process32FirstW.argtypes
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x2, 0)
    require(snapshot not in (None, ctypes.c_void_p(-1).value), "Cannot enumerate processes.")
    matches = []
    record = PROCESSENTRY32W()
    record.dwSize = ctypes.sizeof(record)
    try:
        present = kernel.Process32FirstW(snapshot, ctypes.byref(record))
        require(present, "Cannot read the process list; installation refused.")
        while present:
            if record.szExeFile.casefold() == exe.name.casefold():
                handle = kernel.OpenProcess(0x1000, False, record.th32ProcessID)
                require(handle, "Cannot inspect a matching client process; fully exit the client first.")
                try:
                    path = ctypes.create_unicode_buffer(32768)
                    length = wintypes.DWORD(len(path))
                    require(kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)),
                            "Cannot inspect a matching client process; installation refused.")
                    if os.path.normcase(os.path.realpath(path.value)) == os.path.normcase(str(exe)):
                        matches.append(record.th32ProcessID)
                finally:
                    kernel.CloseHandle(handle)
            present = kernel.Process32NextW(snapshot, ctypes.byref(record))
        require(ctypes.get_last_error() == 18, "Process enumeration failed; installation refused.")
    finally:
        kernel.CloseHandle(snapshot)
    return matches


def require_stopped_installation(asar, exe):
    require(os.name == "nt", "Applying or restoring is supported only on Windows.")
    require(asar.name.casefold() == "app.asar" and asar.parent == exe.parent / "resources",
            "The archive must be resources/app.asar next to the selected executable.")
    verify_executable(exe.read_bytes())
    require(not running_client_pids(exe),
            "The client is running. Fully exit it from the tray menu, then retry. No process was stopped.")


def backup_path(asar):
    return asar.with_name(asar.name + BACKUP_SUFFIX)


def ensure_original_backup(path, original):
    require(sha256(original) == ORIGINAL_SHA256, "Refusing to back up an unverified original.")
    if path.exists():
        require(path.is_file() and not path.is_symlink() and sha256(path.read_bytes()) == ORIGINAL_SHA256,
                "The existing backup differs; it will not be overwritten.")
        return
    try:
        with path.open("xb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as error:
        raise PatchError("Backup appeared during the operation; retry after checking it.") from error
    require(sha256(path.read_bytes()) == ORIGINAL_SHA256, "Original backup verification failed.")


def replace_checked(asar, content, expected_current_hash, exe):
    """Stage on the same filesystem, recheck, then atomically replace the target."""
    descriptor, name = tempfile.mkstemp(prefix=".tray-restore-", suffix=".tmp", dir=str(asar.parent))
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        require(sha256(staged.read_bytes()) == sha256(content), "Staged archive verification failed.")
        require_stopped_installation(asar, exe)
        require(sha256(asar.read_bytes()) == expected_current_hash,
                "Installed archive changed during the operation; replacement refused.")
        os.replace(str(staged), str(asar))
        require(sha256(asar.read_bytes()) == sha256(content),
                "Post-installation verification failed. Keep the original backup for recovery.")
    finally:
        if staged.exists():
            staged.unlink()


def apply_patch(asar, exe):
    require_stopped_installation(asar, exe)
    original = asar.read_bytes()
    status = package_status(original)
    backup = backup_path(asar)
    if status == "patched":
        require(backup.is_file() and sha256(backup.read_bytes()) == ORIGINAL_SHA256,
                "Already patched, but the expected original backup is missing or invalid.")
        return "Already patched; no files changed."
    patched = build_patch(original)
    ensure_original_backup(backup, original)
    replace_checked(asar, patched, ORIGINAL_SHA256, exe)
    return "Applied. Original backup: " + str(backup)


def restore_original(asar, exe):
    require_stopped_installation(asar, exe)
    current = asar.read_bytes()
    status = package_status(current)
    if status == "original":
        return "Already original; no files changed."
    backup = backup_path(asar)
    require(backup.is_file() and not backup.is_symlink(), "Original backup is missing or is a symbolic link.")
    original = backup.read_bytes()
    require(sha256(original) == ORIGINAL_SHA256, "Backup hash mismatch; restore refused.")
    replace_checked(asar, original, PATCHED_SHA256, exe)
    return "Restored the verified original. Backup retained."


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="check", choices=("check", "build", "apply", "restore"))
    parser.add_argument("--asar", required=True, type=Path, help="Path to the user's own app.asar")
    parser.add_argument("--output", type=Path, help="New output file for build; existing files are never overwritten")
    parser.add_argument("--exe", type=Path, help="Client executable for apply/restore; defaults to the standard filename next to resources")
    args = parser.parse_args(argv)
    try:
        require(not args.asar.is_symlink(), "Symbolic-link archives are not supported.")
        asar = args.asar.resolve(strict=True)
        require(asar.is_file(), "The ASAR path must be a regular file.")
        blob = asar.read_bytes()
        status = package_status(blob)
        if args.command == "check":
            count = validate_asar(*parse_asar(blob))
            print("Supported 1.0.11 package: {}. Verified {} packed files. SHA-256: {}".format(status, count, sha256(blob)))
        elif args.command == "build":
            require(args.output is not None, "build requires --output.")
            require(args.output.resolve() != asar, "Build output must not replace the input archive.")
            result = build_patch(blob)
            with args.output.open("xb") as handle:
                handle.write(result)
                handle.flush()
                os.fsync(handle.fileno())
            print("Built verified patch: " + str(args.output) + "\nSHA-256: " + sha256(result))
        else:
            exe_arg = args.exe or asar.parent.parent / EXE_NAME
            require(not exe_arg.is_symlink(), "Symbolic-link executables are not supported.")
            exe = exe_arg.resolve(strict=True)
            print(apply_patch(asar, exe) if args.command == "apply" else restore_original(asar, exe))
        return 0
    except (PatchError, OSError, ValueError, UnicodeError, struct.error) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
