"""Directory discovery pins the same object it checks and bounds the listing."""

from __future__ import annotations

import ctypes
import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from zd_app.storage import _import_guards as guards


class DirectoryGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "reports"
        self.directory.mkdir()

    def test_empty_and_exact_limit_listings_return_all_names_without_path_probes(self) -> None:
        self.assertEqual(guards.list_guarded_directory(self.directory, max_entries=0), [])
        expected = {"report.md", "ordinary.json", "\u03b1-\U0001f4dd.txt"}
        for name in expected:
            (self.directory / name).write_text("fixture", encoding="utf-8")
        with (
            patch.object(Path, "stat", side_effect=AssertionError("path stat")),
            patch.object(Path, "glob", side_effect=AssertionError("path glob")),
        ):
            self.assertEqual(
                {path.name for path in guards.list_guarded_directory(self.directory, max_entries=3)},
                expected,
            )

    def test_entry_and_name_byte_overflow_reject_entire_listing(self) -> None:
        (self.directory / "report.md").touch()
        (self.directory / "subdirectory").mkdir()
        with self.assertRaises(guards.DirectoryListingTooLargeError):
            guards.list_guarded_directory(self.directory, max_entries=1)
        with patch.object(guards, "MAX_DIRECTORY_NAME_BYTES", 1):
            with self.assertRaises(guards.DirectoryListingTooLargeError):
                guards.list_guarded_directory(self.directory)

    def test_file_and_missing_directory_are_rejected(self) -> None:
        source = self.root / "file.json"
        source.write_text("{}", encoding="utf-8")
        for path in (source, self.root / "missing"):
            with self.subTest(path=path.name):
                with self.assertRaises(OSError):
                    guards.list_guarded_directory(path)

    @unittest.skipUnless(os.name == "nt", "Windows native directory handles")
    def test_network_and_mapped_roots_fail_before_native_open(self) -> None:
        with patch.object(guards, "_open_windows_local_no_reparse") as native_open:
            with self.assertRaises(guards.UnsafeImportPathError):
                guards.list_guarded_directory(r"\\fixture.invalid\share\reports")
            with patch.object(guards, "_get_windows_drive_type", return_value=4):
                with self.assertRaises(guards.UnsafeImportPathError):
                    guards.list_guarded_directory(r"Z:\reports")
            native_open.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows native directory handles")
    def test_junction_substituted_after_lexical_check_is_rejected_before_query(self) -> None:
        import _winapi

        real_check = guards.require_local_import_path

        def check_then_swap(path):
            checked = real_check(path)
            moved = self.root / "moved"
            self.directory.rename(moved)
            _winapi.CreateJunction(str(moved), str(self.directory))
            self.addCleanup(os.rmdir, self.directory)
            return checked

        with (
            patch.object(guards, "require_local_import_path", side_effect=check_then_swap),
            patch.object(guards, "_query_windows_directory_names") as query,
        ):
            with self.assertRaises(guards.UnsafeImportPathError):
                guards.list_guarded_directory(self.directory)
            query.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows native directory handles")
    def test_root_and_ancestor_swaps_after_open_do_not_enumerate_foreign_names(self) -> None:
        import _winapi

        for ancestor in (False, True):
            with self.subTest(ancestor=ancestor):
                parent = self.root / f"parent_{ancestor}"
                directory = parent / "reports"
                directory.mkdir(parents=True)
                (directory / "ordinary.md").touch()
                foreign_parent = self.root / f"foreign_{ancestor}"
                foreign_reports = foreign_parent / "reports"
                foreign_reports.mkdir(parents=True)
                (foreign_reports / "FOREIGN_NAME_DO_NOT_ENUMERATE.md").touch()
                replace = parent if ancestor else directory
                target = foreign_parent if ancestor else foreign_reports
                original_open = guards._open_windows_local_no_reparse
                replaced = False

                def open_then_swap(path, **kwargs):
                    nonlocal replaced
                    handle = original_open(path, **kwargs)
                    try:
                        try:
                            replace.rename(self.root / f"moved_{ancestor}")
                        except PermissionError:
                            # Windows can block an ancestor rename while its
                            # descendant handle is open, even with delete
                            # sharing. Verify fixture rights after close below.
                            if not ancestor:
                                raise
                            return handle
                        _winapi.CreateJunction(str(target), str(replace))
                        self.addCleanup(os.rmdir, replace)
                        replaced = True
                    except BaseException:
                        ctypes.windll.kernel32.CloseHandle(handle)
                        raise
                    return handle

                with patch.object(
                    guards, "_open_windows_local_no_reparse", side_effect=open_then_swap
                ):
                    self.assertEqual(
                        [path.name for path in guards.list_guarded_directory(directory)],
                        ["ordinary.md"],
                    )
                if not replaced:
                    replace.rename(self.root / f"moved_{ancestor}")
                    _winapi.CreateJunction(str(target), str(replace))
                    self.addCleanup(os.rmdir, replace)
                # The spelling now really resolves to the foreign fixture.
                self.assertEqual(
                    [path.name for path in directory.iterdir()],
                    ["FOREIGN_NAME_DO_NOT_ENUMERATE.md"],
                )

    @unittest.skipUnless(os.name == "nt", "Windows native directory handles")
    def test_query_failure_closes_native_handle(self) -> None:
        original_open = guards._open_windows_local_no_reparse
        opened = []

        def capture_open(*args, **kwargs):
            handle = original_open(*args, **kwargs)
            opened.append(handle)
            return handle

        with (
            patch.object(guards, "_open_windows_local_no_reparse", side_effect=capture_open),
            patch.object(guards, "_query_windows_directory_names", side_effect=OSError("fixture")),
        ):
            with self.assertRaises(OSError):
                guards.list_guarded_directory(self.directory)
        flags = ctypes.c_ulong()
        self.assertEqual(
            ctypes.windll.kernel32.GetHandleInformation(opened[0], ctypes.byref(flags)), 0
        )
        self.assertEqual(ctypes.windll.kernel32.GetLastError(), 6)  # ERROR_INVALID_HANDLE


@unittest.skipUnless(os.name == "nt", "Windows native query contract")
class DirectoryQueryContractTests(unittest.TestCase):
    def _query_with_result(self, *, status=0, payload=b"", information=None, max_entries=3):
        def query(_handle, _event, _apc, _context, io_status, buffer, *_args):
            ctypes.memmove(buffer, payload, len(payload))
            ctypes.cast(io_status, ctypes.POINTER(guards._IO_STATUS_BLOCK)).contents.Information = (
                len(payload) if information is None else information
            )
            return status

        native_query = Mock(side_effect=query)
        with patch.object(
            guards, "load_system_dll", return_value=SimpleNamespace(NtQueryDirectoryFile=native_query)
        ):
            return guards._query_windows_directory_names(ctypes.c_void_p(1), max_entries)

    def test_buffer_overflow_and_incomplete_success_fail_closed(self) -> None:
        cases = [
            {"status": ctypes.c_long(0x80000005).value},  # STATUS_BUFFER_OVERFLOW
            {"information": 0},
            {"information": 65537},
            {"payload": struct.pack("<III", 0, 0, 2)},  # missing filename bytes
            {"payload": struct.pack("<III", 0, 0, 1) + b"x"},  # odd UTF-16 length
            {"payload": struct.pack("<III", 16, 0, 2) + b"x\0"},  # multiple entries
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(OSError):
                    self._query_with_result(**case)

    def test_query_calls_are_bounded_even_if_only_dot_entries_repeat(self) -> None:
        payload = struct.pack("<III", 0, 0, 2) + b".\0"
        with self.assertRaises(guards.DirectoryListingTooLargeError):
            self._query_with_result(payload=payload, max_entries=0)

    def test_non_leaf_native_names_are_rejected(self) -> None:
        for name in ("../foreign.md", r"..\foreign.md", "file:stream", "nul\0suffix"):
            encoded = name.encode("utf-16-le")
            payload = struct.pack("<III", 0, 0, len(encoded)) + encoded
            with self.subTest(name=name):
                with self.assertRaises(guards.UnsafeImportPathError):
                    self._query_with_result(payload=payload)


if __name__ == "__main__":
    unittest.main()
