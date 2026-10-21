"""System DLL loading policy, exercised entirely with synthetic native loaders."""

from __future__ import annotations

import ast
import ctypes
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from zd_app import system_dll


class SystemDllLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        system_dll._system_directory.cache_clear()
        system_dll._load_native.cache_clear()
        self.addCleanup(system_dll._system_directory.cache_clear)
        self.addCleanup(system_dll._load_native.cache_clear)

    @staticmethod
    def _kernel(directory: str):
        def get_directory(buffer, size):
            if size <= len(directory):
                return len(directory) + 1
            buffer.value = directory
            return len(directory)
        return SimpleNamespace(GetSystemDirectoryW=Mock(side_effect=get_directory))

    def test_native_paths_flags_and_nameless_wrapping_ignore_frozen_and_environment(self):
        kernel = self._kernel(r"D:\ActualOS\System32")
        target = object()
        native = Mock(side_effect=[101, 202])

        def frozen_constructor(name, *, handle, winmode, use_last_error):
            # A named constructor would enter PyInstaller's _frozen_name,
            # including its missing-absolute-path fallback into _MEIPASS.
            self.assertIsNone(name)
            self.assertEqual(winmode, 0x800)
            return kernel if handle == 101 else target

        with patch.object(system_dll, "_native_load", native), patch.object(
            ctypes, "WinDLL", side_effect=frozen_constructor, create=True
        ) as constructor, patch.dict(os.environ, {"SystemRoot": r"Z:\attacker"}), patch.object(
            sys, "_MEIPASS", r"Z:\bundle", create=True
        ):
            self.assertIs(system_dll.load_system_dll("HID", use_last_error=True), target)
            self.assertIs(system_dll.load_system_dll("HID", use_last_error=True), target)
        self.assertEqual(native.call_args_list, [
            unittest.mock.call("kernel32.dll", 0x800),
            unittest.mock.call(r"D:\ActualOS\System32\hid.dll", 0x800),
        ])
        self.assertTrue(constructor.call_args.kwargs["use_last_error"])

    def test_missing_absolute_system_dll_never_attempts_bundle_or_bare_fallback(self):
        native = Mock(side_effect=[11, OSError("missing system DLL")])
        with patch.object(system_dll, "_native_load", native), patch.object(
            system_dll, "_wrap_handle", return_value=self._kernel(r"D:\OS\System32")
        ) as wrap:
            with self.assertRaisesRegex(OSError, "missing system DLL"):
                system_dll.load_system_dll("xinput1_3.dll")
        self.assertEqual(native.call_args_list[-1].args, (r"D:\OS\System32\xinput1_3.dll", 0x800))
        self.assertEqual(native.call_count, 2)
        self.assertEqual(wrap.call_count, 1)

    def test_bootstrap_failure_has_no_environment_or_bundle_fallback(self):
        native = Mock(side_effect=OSError("bootstrap unavailable"))
        with patch.object(system_dll, "_native_load", native):
            with self.assertRaisesRegex(OSError, "bootstrap unavailable"):
                system_dll.load_system_dll("hid")
        native.assert_called_once_with("kernel32.dll", 0x800)

    def test_non_windows_loader_failure_is_oserror(self):
        with patch.object(system_dll, "_native_load", None):
            with self.assertRaises(OSError):
                system_dll.load_system_dll("hid")

    def test_system_directory_buffer_grows_for_long_os_path(self):
        directory = "D:\\" + "Windows" * 45 + "\\System32"
        kernel = self._kernel(directory)
        with patch.object(system_dll, "_native_load", return_value=11), patch.object(
            system_dll, "_wrap_handle", return_value=kernel
        ):
            self.assertEqual(system_dll._system_directory(), directory)
        self.assertEqual(kernel.GetSystemDirectoryW.call_count, 2)
        self.assertEqual(kernel.GetSystemDirectoryW.call_args.args[1], len(directory) + 2)

    def test_failed_malformed_and_oversized_system_directories_fail_closed(self):
        for directory in ("", "relative", r"\rooted", r"\\host\share", r"D:\OS\..\evil"):
            with self.subTest(directory=directory), patch.object(
                system_dll, "_native_load", return_value=11
            ), patch.object(system_dll, "_wrap_handle", return_value=self._kernel(directory)):
                system_dll._system_directory.cache_clear()
                with self.assertRaises(OSError):
                    system_dll._system_directory()
        kernel = SimpleNamespace(GetSystemDirectoryW=Mock(return_value=40000))
        with patch.object(system_dll, "_native_load", return_value=11), patch.object(
            system_dll, "_wrap_handle", return_value=kernel
        ):
            with self.assertRaises(OSError):
                system_dll._system_directory()

    def test_unknown_or_path_qualified_names_are_rejected_before_native_loading(self):
        with patch.object(system_dll, "_native_load") as native:
            for name in (r"D:\evil\hid.dll", "../hid", "hid.dll:payload", "vendor.dll", "hid\0"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    system_dll.load_system_dll(name)
            native.assert_not_called()

    def test_callers_get_private_wrappers_over_one_pinned_native_reference(self):
        kernel = self._kernel(r"D:\OS\System32")
        first, second = SimpleNamespace(), SimpleNamespace()
        with patch.object(system_dll, "_native_load", side_effect=[11, 22]) as native, patch.object(
            ctypes, "WinDLL", side_effect=[kernel, first, second], create=True
        ):
            one = system_dll.load_system_dll("setupapi")
            two = system_dll.load_system_dll("setupapi")
        self.assertIs(one, first)
        self.assertIs(two, second)
        one.SetupDiGetClassDevsW = Mock()
        self.assertFalse(hasattr(two, "SetupDiGetClassDevsW"))
        self.assertEqual(native.call_count, 2)  # kernel bootstrap + one SetupAPI load


class SystemDllFallbackTests(unittest.TestCase):
    def test_polling_fallback_keeps_order_and_skips_missing_symbol(self):
        from zd_app.services import xinput_poll_service as service
        target = SimpleNamespace(XInputGetState=Mock())
        with patch.object(ctypes, "WinDLL", object(), create=True), patch.object(
            service, "load_system_dll", side_effect=[OSError("missing"), object(), target]
        ) as load:
            self.assertIs(service._load_dll(), target)
        self.assertEqual([call.args[0] for call in load.call_args_list], list(service._DLL_FALLBACK_ORDER))

    def test_polling_all_missing_stays_unavailable(self):
        from zd_app.services import xinput_poll_service as service
        with patch.object(ctypes, "WinDLL", object(), create=True), patch.object(
            service, "load_system_dll", side_effect=OSError("missing")
        ) as load:
            self.assertIsNone(service._load_dll())
        self.assertEqual(load.call_count, 3)

    def test_legacy_xinput_keeps_order_and_absent_sentinel(self):
        # Execute only this loader function; importing the legacy module would
        # otherwise run its native loader and change its process-wide sentinel.
        root = Path(__file__).resolve().parents[1]
        path = root / "src" / "xinput.py"
        if not path.exists():
            path = root / "zd_app" / "services" / "xinput.py"  # public cut
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_load_xinput")
        load = Mock()
        namespace = {"ctypes": SimpleNamespace(windll=object()), "load_system_dll": load}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "<synthetic-xinput-loader>", "exec"), namespace)
        target = object()
        load.side_effect = [OSError("missing"), target]
        self.assertIs(namespace["_load_xinput"](), target)
        self.assertEqual([call.args[0] for call in load.call_args_list], ["xinput1_4", "xinput1_3"])
        load.side_effect = OSError("missing")
        self.assertIsNone(namespace["_load_xinput"]())


def _direct_system_loads(source: str) -> list[int]:
    """Recognize ctypes aliases and both attribute/getattr loader spelling.

    Explicitly inventoried third-party DLL names keep their native loading
    policy. New or dynamic native loader calls require review; system calls
    belong in the shared helper. This is a regression guard, not a sandbox.
    """
    tree = ast.parse(source)
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("ctypes", "_ctypes"):
                    names[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in ("ctypes", "_ctypes"):
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def qualified(node):
        if isinstance(node, ast.Name):
            return names.get(node.id, "")
        if isinstance(node, ast.Attribute):
            base = qualified(node.value)
            return base + "." + node.attr if base else ""
        if (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "getattr" and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
        ):
            base = qualified(node.args[0])
            return base + "." + node.args[1].value if base else ""
        return ""

    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            for target in node.targets:
                value = qualified(node.value)
                if isinstance(target, ast.Name) and value and names.get(target.id) != value:
                    names[target.id] = value
                    changed = True
        if not changed:
            break

    # These are third-party library basenames, never routed through the system
    # resolver. Add an intentional bundled-library exception here when needed.
    bundled_libraries = {"hidapi.dll", "libusb-1.0.dll"}

    def requires_system_helper(name):
        return name.lower().removesuffix(".dll") + ".dll" not in bundled_libraries

    violations = []
    namespaces = ("ctypes.windll", "ctypes.cdll", "ctypes.oledll")
    loaders = {"ctypes.WinDLL", "ctypes.CDLL", "ctypes.OleDLL", "_ctypes.LoadLibrary"}
    loaders.update(namespace + ".LoadLibrary" for namespace in namespaces)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            base = qualified(node.value)
            key = node.attr if isinstance(node, ast.Attribute) else (
                node.slice.value if isinstance(node.slice, ast.Constant) else None
            )
            if base in namespaces and key != "LoadLibrary" and (not isinstance(key, str) or requires_system_helper(key)):
                violations.append(node.lineno)
        if not isinstance(node, ast.Call):
            continue
        if qualified(node.func) == "ctypes.LibraryLoader":
            violations.append(node.lineno)
        if qualified(node.func) in loaders:
            name = node.args[0] if node.args else None
            if not isinstance(name, ast.Constant) or not isinstance(name.value, str) or requires_system_helper(name.value):
                violations.append(node.lineno)
        elif isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
            if qualified(node.args[0]) in namespaces:
                key = node.args[1]
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str) or requires_system_helper(key.value):
                    violations.append(node.lineno)
    return sorted(set(violations))


class SystemDllSourceGuardTests(unittest.TestCase):
    def test_shipped_python_system_loads_use_shared_helper(self):
        root = Path(__file__).resolve().parents[1]
        violations = []
        for folder in ("zd_app", "src"):
            for path in (root / folder).rglob("*.py"):
                if path == root / "zd_app" / "system_dll.py":
                    continue
                for line in _direct_system_loads(path.read_text(encoding="utf-8-sig")):
                    violations.append(f"{path.relative_to(root)}:{line}")
        self.assertEqual(violations, [], "Route system DLLs through zd_app.system_dll")

    def test_guard_recognizes_aliases_dynamic_and_loader_namespace_forms(self):
        for source in (
            'import ctypes as c; c.windll.hid',
            'from ctypes import WinDLL as load; load("setupapi")',
            'import ctypes; load = ctypes.WinDLL; load("hid.dll")',
            'import ctypes; load = getattr(ctypes, "WinDLL"); load(name)',
            'from ctypes import windll as w; getattr(w, name)',
            'import ctypes; ctypes.windll["cfgmgr32"]',
            'import ctypes; ctypes.windll.LoadLibrary("dbghelp")',
            'import _ctypes as c; c.LoadLibrary("kernel32.dll", 0x800)',
            'import ctypes; loader = ctypes.LibraryLoader(ctypes.WinDLL); loader.hid',
        ):
            with self.subTest(source=source):
                self.assertTrue(_direct_system_loads(source))

    def test_guard_leaves_intentional_non_system_library_loading_alone(self):
        self.assertEqual(_direct_system_loads('import ctypes; ctypes.CDLL("hidapi.dll")'), [])
