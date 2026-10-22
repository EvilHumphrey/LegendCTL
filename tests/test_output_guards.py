"""Diagnostic-output admission, pinned I/O and local alias regressions."""

from __future__ import annotations

import ctypes
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from zd_app.storage import _output_guards as guards


class OutputGuardAdmissionTests(unittest.TestCase):
    def _no_filesystem(self):
        stack = ExitStack()
        for name in ("resolve", "stat", "open", "mkdir"):
            stack.enter_context(patch.object(Path, name, side_effect=AssertionError("filesystem lookup")))
        for name in ("_windows_open", "_windows_volume"):
            stack.enter_context(patch.object(guards, name, side_effect=AssertionError("native lookup")))
        stack.enter_context(patch.object(os, "open", side_effect=AssertionError("open")))
        stack.enter_context(patch.object(os, "mkdir", side_effect=AssertionError("mkdir")))
        return stack

    def test_hostile_namespaces_are_rejected_before_any_lookup(self):
        root = Path.cwd() / "synthetic-output-root"
        hostile = (
            r"\\server\share\out", "//server/share/out", r"\\?\UNC\server\share\out",
            r"\\?\C:\out", r"\\.\C:\out", r"\??\UNC\server\share\out",
            r"\Device\Mup\server\share\out", r"C:out", r"C:", "bad\0name",
        )
        with self._no_filesystem(), patch.object(
            guards, "_get_windows_drive_type", side_effect=AssertionError("drive lookup")
        ):
            for raw in hostile:
                with self.subTest(raw=raw), self.assertRaises(guards.UnsafeOutputPathError):
                    with guards.guarded_output_directory(raw, root):
                        self.fail("unsafe directory admitted")

    def test_env_and_home_are_expanded_before_namespace_admission(self):
        root = Path.cwd() / "synthetic-output-root"
        env = {"OUTPUT_GUARD_TEST": r"\\server\share", "USERPROFILE": r"\\server\home", "HOME": "//server/home"}
        variable = "%OUTPUT_GUARD_TEST%/out" if os.name == "nt" else "$OUTPUT_GUARD_TEST/out"
        with patch.dict(os.environ, env), self._no_filesystem(), patch.object(
            guards, "_get_windows_drive_type", side_effect=AssertionError("drive lookup")
        ):
            for raw in (variable, "~/out"):
                with self.subTest(raw=raw), self.assertRaises(guards.UnsafeOutputPathError):
                    with guards.guarded_output_directory(raw, root):
                        self.fail("expanded remote path admitted")

    def test_unsafe_default_never_becomes_fallback(self):
        with self._no_filesystem():
            for root in (r"\\server\share\app", r"\??\C:\app", "Q:app"):
                with self.subTest(root=root), self.assertRaises(guards.UnsafeOutputPathError):
                    with guards.guarded_output_directory("diagnostics", root):
                        self.fail("unsafe default admitted")

    @unittest.skipUnless(os.name == "nt", "Windows drive admission")
    def test_mapped_drive_rejected_before_native_or_path_lookup(self):
        def drive_type(path):
            return 4 if path.upper().startswith("Z:") else 3

        with self._no_filesystem(), patch.object(guards, "_get_windows_drive_type", side_effect=drive_type):
            for requested, root in ((r"Z:\out", r"C:\app"), (r"C:\app\out", r"Z:\app")):
                with self.subTest(requested=requested, root=root), self.assertRaises(guards.UnsafeOutputPathError):
                    with guards.guarded_output_directory(requested, root):
                        self.fail("mapped drive admitted")
            with patch.dict(os.environ, {"OUTPUT_GUARD_TEST": r"Z:\out", "USERPROFILE": r"Z:\home"}):
                for raw in ("%OUTPUT_GUARD_TEST%", "~/out"):
                    with self.subTest(raw=raw), self.assertRaises(guards.UnsafeOutputPathError):
                        with guards.guarded_output_directory(raw, r"C:\app"):
                            self.fail("expanded mapped drive admitted")

    @unittest.skipUnless(os.name == "nt", "Windows namespace admission")
    def test_dos_devices_ads_and_ambiguous_components_fail_before_lookup(self):
        with self._no_filesystem(), patch.object(
            guards, "_get_windows_drive_type", side_effect=AssertionError("drive lookup")
        ):
            for raw in (r"C:\app\NUL", r"C:\app\COM1.txt", r"C:\app\out:stream", "C:\\app\\out. ", r"\out"):
                with self.subTest(raw=raw), self.assertRaises(guards.UnsafeOutputPathError):
                    with guards.guarded_output_directory(raw, r"C:\app"):
                        self.fail("ambiguous output admitted")

    def test_ordinary_relative_and_absolute_descendants_keep_their_location(self):
        root = Path.cwd() / "synthetic-output-root"
        with self._no_filesystem(), patch.object(guards, "_get_windows_drive_type", return_value=3):
            for raw in (str(root / "custom"), str(Path("synthetic-output-root") / "custom")):
                self.assertEqual(guards.select_output_directory(raw, root), (root / "custom", False))
            self.assertEqual(
                guards.select_output_directory(root / ".." / "other", root),
                (root / "diagnostics", True),
            )

    def test_length_and_depth_overflow_rejected_before_drive_or_filesystem_calls(self):
        root = Path.cwd() / "synthetic-output-root"
        overlong = root / ("x" * guards.MAX_OUTPUT_PATH_UNITS)
        deep = root.joinpath(*(["x"] * (guards.MAX_OUTPUT_COMPONENTS + 1)))
        relative_overlong = "x" * (guards.MAX_OUTPUT_PATH_UNITS - len(str(Path.cwd())))
        env_var = "%OUTPUT_GUARD_TEST%" if os.name == "nt" else "$OUTPUT_GUARD_TEST"
        # Two references exceed our path limit without setting a Windows
        # environment variable above its own independent size ceiling.
        expanded_overflow = f"{env_var}/{env_var}"
        with self._no_filesystem(), patch.object(
            guards, "_get_windows_drive_type", side_effect=AssertionError("drive before limit")
        ), patch.dict(os.environ, {"OUTPUT_GUARD_TEST": "x" * (guards.MAX_OUTPUT_PATH_UNITS // 2)}):
            for requested, allowed in (
                (overlong, root), (deep, root), (relative_overlong, root),
                (root, overlong), (root, deep), (expanded_overflow, root),
            ):
                with self.subTest(kind="length/depth", requested_length=len(str(requested))), self.assertRaisesRegex(
                    guards.UnsafeOutputPathError, "limit"
                ):
                    with guards.guarded_output_directory(requested, allowed):
                        self.fail("unbounded directory admitted")

    def test_normalized_walk_and_fallback_each_obey_component_limit(self):
        anchor = Path(Path.cwd().anchor)
        root = anchor.joinpath(*(["x"] * guards.MAX_OUTPUT_COMPONENTS))
        with self._no_filesystem(), patch.object(guards, "_get_windows_drive_type", return_value=3):
            self.assertEqual(guards.select_output_directory(root, root), (root, False))
            # Redundant lexical traversal must not increase the native walk.
            self.assertEqual(guards.lexical_output_path(root / "unused" / ".."), root)
        with self._no_filesystem(), patch.object(
            guards, "_get_windows_drive_type", side_effect=AssertionError("drive before fallback limit")
        ), self.assertRaisesRegex(guards.UnsafeOutputPathError, "component limit"):
            with guards.guarded_output_directory(anchor / "other", root):
                self.fail("over-depth fallback admitted")

    @unittest.skipUnless(os.name == "nt", "Windows UTF-16 path budget")
    def test_windows_path_limit_counts_surrogate_pairs(self):
        root = Path.cwd() / "synthetic-output-root"
        requested = root / ("\U0001f600" * (guards.MAX_OUTPUT_PATH_UNITS // 2))
        self.assertLess(len(str(requested)), guards.MAX_OUTPUT_PATH_UNITS)
        with self._no_filesystem(), patch.object(
            guards, "_get_windows_drive_type", side_effect=AssertionError("drive before UTF-16 limit")
        ), self.assertRaisesRegex(guards.UnsafeOutputPathError, "length limit"):
            with guards.guarded_output_directory(requested, root):
                self.fail("UTF-16 overflow admitted")

    @unittest.skipUnless(os.name == "nt", "Windows drive admission")
    def test_ram_disk_type_is_explicitly_unsupported_before_lookup(self):
        with self._no_filesystem(), patch.object(guards, "_get_windows_drive_type", return_value=6):
            with self.assertRaisesRegex(guards.UnsafeOutputPathError, "supported local disk"):
                with guards.guarded_output_directory(r"R:\app\out", r"R:\app"):
                    self.fail("unsupported RAM disk admitted")


class OutputGuardNativeContractTests(unittest.TestCase):
    def test_native_open_is_no_reparse_pinned_and_exclusive_for_files(self):
        calls = []

        def create(handle_out, access, attributes_ptr, _status, _size, _attrs, share, disposition, options, *_):
            attributes = ctypes.cast(attributes_ptr, ctypes.POINTER(guards._OBJECT_ATTRIBUTES)).contents
            calls.append((access, attributes.RootDirectory, attributes.Attributes, share, disposition, options))
            ctypes.cast(handle_out, ctypes.POINTER(ctypes.c_void_p)).contents.value = 123
            return 0

        ntdll = SimpleNamespace(NtCreateFile=Mock(side_effect=create))
        with patch.object(guards, "load_system_dll", return_value=ntdll):
            self.assertEqual(guards._windows_open("child", 42, directory=True, create=True), 123)
            self.assertEqual(guards._windows_open("card.html", 123, directory=False, create=True), 123)
        self.assertEqual([call[1] for call in calls], [42, 123])
        self.assertTrue(all(call[2] & 0x1000 for call in calls))
        self.assertTrue(all(call[3] == 1 for call in calls))
        self.assertEqual([call[4] for call in calls], [3, 2])
        self.assertEqual([call[5] for call in calls], [0x21, 0x60])

    def test_native_reparse_error_is_explicit(self):
        ntdll = SimpleNamespace(NtCreateFile=Mock(return_value=ctypes.c_int32(0xC000050B).value))
        with patch.object(guards, "load_system_dll", return_value=ntdll), self.assertRaises(guards.UnsafeOutputPathError):
            guards._windows_open("alias", 42, directory=True, create=True)

    def test_only_direct_disk_mapping_is_admitted(self):
        def query_for(target):
            def query(_drive, buffer, _size):
                buffer.value = target
                return len(target) + 2
            return SimpleNamespace(QueryDosDeviceW=Mock(side_effect=query))

        for target in (
            r"\Device\Mup\server\share", r"\??\C:\alias",
            r"\Device\LanmanRedirector\server\share", r"\Device\ImDisk0",
        ):
            with self.subTest(target=target), patch.object(guards, "load_system_dll", return_value=query_for(target)):
                with self.assertRaises(guards.UnsafeOutputPathError):
                    guards._windows_volume("Z:")
        with patch.object(guards, "load_system_dll", return_value=query_for(r"\Device\HarddiskVolume3")):
            self.assertEqual(guards._windows_volume("C:"), r"\Device\HarddiskVolume3")

    @unittest.skipUnless(os.name == "nt", "Windows volume admission")
    def test_unsupported_local_volume_mapping_is_rejected_before_filesystem_open(self):
        def query(_drive, buffer, _size):
            buffer.value = r"\Device\ImDisk0"
            return len(buffer.value) + 2

        kernel32 = SimpleNamespace(QueryDosDeviceW=Mock(side_effect=query))
        with patch.object(guards, "_get_windows_drive_type", return_value=3), patch.object(
            guards, "load_system_dll", return_value=kernel32
        ), patch.object(guards, "_windows_open", side_effect=AssertionError("unsupported volume open")):
            with self.assertRaisesRegex(guards.UnsafeOutputPathError, "directly to a local disk volume"):
                with guards.guarded_output_directory(r"R:\app\out", r"R:\app"):
                    self.fail("unsupported local mapping admitted")

    @unittest.skipUnless(os.name == "nt", "Windows handle walk")
    def test_ancestor_handles_remain_pinned_through_use_and_close_on_error(self):
        events = []

        def opened(name, parent, **kwargs):
            handle = len(events) + 10
            events.append(("open", name, parent, handle))
            return handle

        with patch.object(guards, "_get_windows_drive_type", return_value=3), patch.object(
            guards, "_windows_volume", return_value=r"\Device\HarddiskVolume3"
        ), patch.object(guards, "_windows_open", side_effect=opened), patch.object(
            guards, "_close_windows_handle", side_effect=lambda h: events.append(("close", h))
        ):
            with self.assertRaisesRegex(RuntimeError, "consumer"):
                with guards.guarded_output_directory(r"C:\app\diagnostics", r"C:\app") as directory:
                    self.assertFalse(any(item[0] == "close" for item in events))
                    self.assertEqual(directory.path, Path(r"C:\app\diagnostics"))
                    raise RuntimeError("consumer")
            with self.assertRaisesRegex(ValueError, "closed"):
                directory.write_text("late.txt", "late")
        opened_handles = [item[3] for item in events if item[0] == "open"]
        self.assertEqual([item[1] for item in events if item[0] == "close"], list(reversed(opened_handles)))


class OutputGuardFilesystemTests(unittest.TestCase):
    def test_new_directories_and_unicode_files_work_without_reopening_output_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(Path, "write_text", side_effect=AssertionError("path write")), patch.object(
                Path, "resolve", side_effect=AssertionError("path resolution")
            ):
                with guards.guarded_output_directory(root / "nested" / "diagnostics", root) as directory:
                    result = directory.write_text("evidence.txt", "Evidence \u2713\n")
            self.assertEqual(result.read_text(encoding="utf-8"), "Evidence \u2713\n")

    def test_existing_file_collision_preserves_original_and_creates_another(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with guards.guarded_output_directory(root, root) as directory:
                first = directory.write_text("card.html", "first")
                second = directory.write_text("card.html", "second")
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_text(), "first")
            self.assertEqual(second.read_text(), "second")

    def test_collision_renaming_can_be_disabled_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with guards.guarded_output_directory(root, root) as directory:
                first = directory.write_text("card.html", "first", rename_on_collision=False)
                with self.assertRaises(FileExistsError):
                    directory.write_text("card.html", "second", rename_on_collision=False)
            self.assertEqual(first.read_text(), "first")
            self.assertEqual(list(root.iterdir()), [first])

    def test_existing_hardlink_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "original.txt"
            original.write_text("sentinel")
            os.link(original, root / "card.html")
            with guards.guarded_output_directory(root, root) as directory:
                output = directory.write_text("card.html", "export")
            self.assertNotEqual(output, root / "card.html")
            self.assertEqual(original.read_text(), "sentinel")
            self.assertEqual(output.read_text(), "export")

    @unittest.skipUnless(os.name == "nt", "Windows sharing pins")
    def test_directory_rename_fails_while_output_is_pinned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with guards.guarded_output_directory(root / "out", root) as directory:
                with self.assertRaises(OSError):
                    (root / "out").rename(root / "moved")
                result = directory.write_text("receipt.txt", "pinned")
            self.assertEqual(result.read_text(), "pinned")

    @unittest.skipUnless(os.name == "nt", "Windows disposable junction fixture")
    def test_junction_at_default_or_substituted_after_admission_is_rejected(self):
        import _winapi

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "foreign"
            target.mkdir()
            alias = root / "alias"
            _winapi.CreateJunction(str(target), str(alias))
            try:
                with self.assertRaises(guards.UnsafeOutputPathError):
                    with guards.guarded_output_directory(alias / "out", alias):
                        self.fail("junction default admitted")
                select = guards.select_output_directory
                output = root / "output"
                output.mkdir()

                def select_then_swap(*args):
                    result = select(*args)
                    output.rename(root / "original")
                    _winapi.CreateJunction(str(target), str(output))
                    return result

                try:
                    with patch.object(guards, "select_output_directory", side_effect=select_then_swap):
                        with self.assertRaises(guards.UnsafeOutputPathError):
                            with guards.guarded_output_directory(output, root):
                                self.fail("junction swap admitted")
                    self.assertEqual(list(target.iterdir()), [])
                finally:
                    if output.exists():
                        os.rmdir(output)
            finally:
                os.rmdir(alias)


if __name__ == "__main__":
    unittest.main()
