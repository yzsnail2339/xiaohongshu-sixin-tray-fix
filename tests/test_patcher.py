"""Dependency-free tests using synthetic ASAR files; no client is redistributed."""

import contextlib
import copy
import io
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import patcher


def synthetic_archive(eol="\r\r\n"):
    header = {"files": {}}
    payload = bytearray()
    contents = {path: ("\n".join(old for old, _ in replacements) + "\n").replace("\n", eol).encode("utf-8")
                for path, replacements in patcher.REPLACEMENTS.items()}
    contents["unrelated.bin"] = b"\x00\xffunchanged\x00"
    for path, content in contents.items():
        node = header
        parts = path.split("/")
        for part in parts[:-1]:
            node = node["files"].setdefault(part, {"files": {}})
        node["files"][parts[-1]] = {
            "size": len(content), "offset": str(len(payload)),
            "integrity": patcher.integrity_for(content, 97),
        }
        payload.extend(content)
    header["files"]["external.bin"] = {"size": 3, "unpacked": True}
    header["files"]["shortcut"] = {"link": "unrelated.bin"}
    return patcher.pack_asar(header, payload)


class KnownArchiveFixture:
    def setUp(self):
        self.original = synthetic_archive()
        self.patched = patcher._build_patch(self.original)
        self.constants = mock.patch.multiple(patcher, ORIGINAL_SHA256=patcher.sha256(self.original),
                                             PATCHED_SHA256=patcher.sha256(self.patched))
        self.constants.start()
        self.addCleanup(self.constants.stop)


class ArchiveTests(KnownArchiveFixture, unittest.TestCase):
    def test_exact_known_input_and_deterministic_output(self):
        self.assertEqual(patcher.build_patch(self.original), self.patched)
        self.assertEqual(patcher.build_patch(self.patched), self.patched)
        self.assertEqual(patcher.package_status(self.original), "original")
        self.assertEqual(patcher.package_status(self.patched), "patched")

    def test_unknown_input_refused(self):
        with self.assertRaisesRegex(patcher.PatchError, "Unsupported package"):
            patcher.build_patch(self.original + b"changed")

    def test_unexpected_output_hash_refused(self):
        with mock.patch.object(patcher, "PATCHED_SHA256", "0" * 64):
            with self.assertRaisesRegex(patcher.PatchError, "differs from the verified"):
                patcher.build_patch(self.original)

    def test_only_two_file_contents_change_and_metadata_survives(self):
        old_header, old_payload = patcher.parse_asar(self.original)
        new_header, new_payload = patcher.parse_asar(self.patched)
        self.assertEqual(patcher.validate_asar(new_header, new_payload), 3)
        self.assertEqual(set(dict(patcher.entries(old_header))), set(dict(patcher.entries(new_header))))
        for name, old in patcher.entries(old_header):
            new = patcher.entry(new_header, name)
            if name not in patcher.REPLACEMENTS:
                self.assertEqual(old, new)
                if "offset" in old:
                    self.assertEqual(patcher.read_entry(old_header, old_payload, name),
                                     patcher.read_entry(new_header, new_payload, name))
        windows = patcher.read_entry(new_header, new_payload, "main/windows.js")
        tray = patcher.read_entry(new_header, new_payload, "main/tray/tray.js")
        self.assertIn(b"setSkipTaskbar(false)", windows)
        self.assertIn(b"isMinimized()", windows)
        self.assertIn(b"global.willQuitApp", windows)
        self.assertIn(b"'double-click'", tray)
        self.assertIn("显示窗口".encode(), tray)
        self.assertNotIn(b"\n", windows.replace(b"\r\r\n", b""))

    def test_source_replacement_handles_all_supported_newlines(self):
        for eol in ("\n", "\r\n", "\r\r\n"):
            result = patcher._build_patch(synthetic_archive(eol))
            header, data = patcher.parse_asar(result)
            for name in patcher.REPLACEMENTS:
                changed = patcher.read_entry(header, data, name)
                self.assertNotIn(b"\n", changed.replace(eol.encode(), b""))

    def test_missing_or_duplicate_pattern_refused(self):
        name = "main/windows.js"
        raw = "\n".join(old for old, _ in patcher.REPLACEMENTS[name]).encode()
        with self.assertRaisesRegex(patcher.PatchError, "missing or ambiguous"):
            patcher.patch_source(name, raw + b"\n" + raw)
        with self.assertRaisesRegex(patcher.PatchError, "missing or ambiguous"):
            patcher.patch_source(name, b"unrelated source")

    def test_truncated_and_invalid_pickle_headers_refused(self):
        for blob in (b"", self.original[:15], self.original[:30]):
            with self.assertRaises(patcher.PatchError):
                patcher.parse_asar(blob)
        broken = bytearray(self.original)
        struct.pack_into("<I", broken, 0, 8)
        with self.assertRaises(patcher.PatchError):
            patcher.parse_asar(broken)

    def test_bounds_and_integrity_checked(self):
        header, payload = patcher.parse_asar(self.original)
        bad_header = copy.deepcopy(header)
        patcher.entry(bad_header, "unrelated.bin")["offset"] = str(len(payload) + 1)
        with self.assertRaisesRegex(patcher.PatchError, "exceeds"):
            patcher.validate_asar(bad_header, payload)
        with self.assertRaisesRegex(patcher.PatchError, "integrity mismatch"):
            patcher.validate_asar(header, payload[:-1] + b"X")

    def test_default_check_is_read_only_and_build_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "app.asar"
            archive.write_bytes(self.original)
            output = Path(temp) / "built.asar"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(patcher.main(["--asar", str(archive)]), 0)
                self.assertEqual(set(Path(temp).iterdir()), {archive})
                self.assertEqual(patcher.main(["build", "--asar", str(archive), "--output", str(archive)]), 1)
                self.assertEqual(patcher.main(["build", "--asar", str(archive), "--output", str(output)]), 0)
                self.assertEqual(output.read_bytes(), self.patched)
                self.assertEqual(patcher.main(["build", "--asar", str(archive), "--output", str(output)]), 1)
            self.assertEqual(archive.read_bytes(), self.original)


class MutationTests(KnownArchiveFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        (root / "resources").mkdir()
        self.asar = root / "resources" / "app.asar"
        self.asar.write_bytes(self.original)
        self.exe = root / patcher.EXE_NAME
        self.exe.write_bytes(b"test executable")
        self.guard_patch = mock.patch.object(patcher, "require_stopped_installation")
        self.guard = self.guard_patch.start()
        self.addCleanup(self.guard_patch.stop)

    def test_apply_backup_repeat_restore_round_trip(self):
        patcher.apply_patch(self.asar, self.exe)
        backup = patcher.backup_path(self.asar)
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(self.asar.read_bytes(), self.patched)
        backup_stat = backup.stat().st_mtime_ns
        self.assertIn("Already patched", patcher.apply_patch(self.asar, self.exe))
        self.assertEqual(backup.stat().st_mtime_ns, backup_stat)
        patcher.restore_original(self.asar, self.exe)
        self.assertEqual(self.asar.read_bytes(), self.original)
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertIn("Already original", patcher.restore_original(self.asar, self.exe))
        self.assertGreaterEqual(self.guard.call_count, 6)

    def test_different_existing_backup_is_never_overwritten(self):
        backup = patcher.backup_path(self.asar)
        backup.write_bytes(b"different")
        with self.assertRaisesRegex(patcher.PatchError, "will not be overwritten"):
            patcher.apply_patch(self.asar, self.exe)
        self.assertEqual(backup.read_bytes(), b"different")
        self.assertEqual(self.asar.read_bytes(), self.original)

    def test_restore_rejects_unknown_upgrade_and_bad_backup(self):
        patcher.apply_patch(self.asar, self.exe)
        self.asar.write_bytes(b"a newer client package")
        with self.assertRaisesRegex(patcher.PatchError, "Unsupported package"):
            patcher.restore_original(self.asar, self.exe)
        self.assertEqual(self.asar.read_bytes(), b"a newer client package")
        self.asar.write_bytes(self.patched)
        patcher.backup_path(self.asar).write_bytes(b"bad backup")
        with self.assertRaisesRegex(patcher.PatchError, "Backup hash mismatch"):
            patcher.restore_original(self.asar, self.exe)
        self.assertEqual(self.asar.read_bytes(), self.patched)

    def test_running_client_refused_without_any_write(self):
        self.guard.side_effect = patcher.PatchError("The client is running")
        with self.assertRaisesRegex(patcher.PatchError, "running"):
            patcher.apply_patch(self.asar, self.exe)
        self.assertEqual(self.asar.read_bytes(), self.original)
        self.assertFalse(patcher.backup_path(self.asar).exists())

    def test_client_reappearing_before_replace_keeps_original(self):
        self.guard.side_effect = [None, patcher.PatchError("The client is running")]
        with self.assertRaisesRegex(patcher.PatchError, "running"):
            patcher.apply_patch(self.asar, self.exe)
        self.assertEqual(self.asar.read_bytes(), self.original)
        self.assertEqual(patcher.backup_path(self.asar).read_bytes(), self.original)
        self.assertEqual(list(self.asar.parent.glob(".tray-restore-*.tmp")), [])

    def test_external_package_change_before_replace_is_not_overwritten(self):
        def change_on_second_check(*_):
            if self.guard.call_count == 2:
                self.asar.write_bytes(b"external update")
        self.guard.side_effect = change_on_second_check
        with self.assertRaisesRegex(patcher.PatchError, "changed during"):
            patcher.apply_patch(self.asar, self.exe)
        self.assertEqual(self.asar.read_bytes(), b"external update")

    def test_failed_atomic_replace_preserves_original_and_backup(self):
        with mock.patch.object(patcher.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                patcher.apply_patch(self.asar, self.exe)
        self.assertEqual(self.asar.read_bytes(), self.original)
        self.assertEqual(patcher.backup_path(self.asar).read_bytes(), self.original)
        self.assertEqual(list(self.asar.parent.glob(".tray-restore-*.tmp")), [])


class ExecutableTests(unittest.TestCase):
    def test_executable_and_disabled_integrity_fuse_required(self):
        verified = b"synthetic-exe" + patcher.FUSE_SENTINEL + b"\x01\x071011000"
        with mock.patch.object(patcher, "EXE_SHA256", patcher.sha256(verified)):
            patcher.verify_executable(verified)
            with self.assertRaisesRegex(patcher.PatchError, "Unsupported client executable"):
                patcher.verify_executable(verified + b"changed")
        protected = b"synthetic-exe" + patcher.FUSE_SENTINEL + b"\x01\x071011100"
        with mock.patch.object(patcher, "EXE_SHA256", patcher.sha256(protected)):
            with self.assertRaisesRegex(patcher.PatchError, "integrity validation"):
                patcher.verify_executable(protected)

    def test_missing_fuse_fails_closed(self):
        blob = b"no fuse marker"
        with mock.patch.object(patcher, "EXE_SHA256", patcher.sha256(blob)):
            with self.assertRaisesRegex(patcher.PatchError, "Unknown Electron fuse"):
                patcher.verify_executable(blob)

    @unittest.skipUnless(os.name == "nt", "Windows process enumeration smoke test")
    def test_windows_process_reader_finds_this_python_process(self):
        self.assertIn(os.getpid(), patcher.running_client_pids(Path(sys.executable).resolve()))


if __name__ == "__main__":
    unittest.main()
