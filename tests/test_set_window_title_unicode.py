from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app.i18n import set_locale
from zd_app.models import AppSettings
from zd_app.ui import app_shell


class _Callable:
    def __init__(self, func):
        self.func = func
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.func(*args)


class SetWindowTitleUnicodeTests(unittest.TestCase):
    def test_set_window_title_unicode_finds_pid_window(self) -> None:
        fake = _FakeUser32(pid=1234)

        with patch.object(app_shell.os, "getpid", return_value=1234), patch.object(
            app_shell.ctypes,
            "windll",
            SimpleNamespace(user32=fake),
            create=True,
        ):
            count = app_shell.set_window_title_unicode("ZD 控制中心")

        self.assertEqual(count, 1)
        self.assertEqual(fake.SetWindowTextW.calls[0], (100, "ZD 控制中心"))

    def test_set_window_title_unicode_calls_set_window_text_w(self) -> None:
        fake = _FakeUser32(pid=99)

        with patch.object(app_shell.os, "getpid", return_value=99), patch.object(
            app_shell.ctypes,
            "windll",
            SimpleNamespace(user32=fake),
            create=True,
        ):
            app_shell.set_window_title_unicode("ZD Ultimate Legend 控制中心")

        self.assertEqual(fake.SetWindowTextW.calls[-1][1], "ZD Ultimate Legend 控制中心")

    def test_set_window_title_unicode_called_after_locale_change(self) -> None:
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()
            shell._dpg_viewport_title_seeded = True

            with patch.object(app_shell, "bind_default_font"), patch.object(
                shell._title_manager,
                "_schedule_reapply",
            ), patch.object(
                app_shell,
                "set_window_title_unicode",
            ) as set_unicode, patch.object(
                app_shell.dpg,
                "set_viewport_title",
            ) as set_viewport_title:
                shell.update_language("zh-CN")

            set_unicode.assert_called_with("ZD Ultimate Legend")
            self.assertNotIn(
                "ZD Ultimate Legend 控制中心",
                [call.args[0] for call in set_viewport_title.call_args_list],
            )
        finally:
            dpg.destroy_context()
            set_locale("en")

    def test_zh_cn_window_title_uses_ascii_fallback(self) -> None:
        shell = make_shell(settings_service=MagicMock(), settings=AppSettings(language="zh-CN"))
        shell._dpg_viewport_title_seeded = True

        try:
            with patch.object(shell._title_manager, "_schedule_reapply"), patch.object(
                app_shell,
                "set_window_title_unicode",
            ) as set_unicode:
                shell._set_window_title(shell.window_title())

            set_unicode.assert_called_once_with("ZD Ultimate Legend")
            self.assertEqual(shell.window_title(), "ZD Ultimate Legend")
        finally:
            set_locale("en")

    def test_main_window_label_stays_ascii_safe_in_zh_cn_locale(self) -> None:
        shell = make_shell(settings_service=MagicMock(), settings=AppSettings(language="zh-CN"))

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()

            self.assertEqual(dpg.get_item_label("main_window"), "ZD Ultimate Legend")
        finally:
            dpg.destroy_context()
            set_locale("en")


class _FakeUser32:
    def __init__(self, pid: int):
        self.pid = pid
        self.EnumWindows = _Callable(self._enum_windows)
        self.GetWindowThreadProcessId = _Callable(self._get_window_thread_process_id)
        self.IsWindowVisible = _Callable(lambda _hwnd: True)
        self.SetWindowTextW = _Callable(lambda _hwnd, _title: True)

    def _enum_windows(self, callback, lparam):
        callback(100, lparam)
        callback(200, lparam)
        return True

    def _get_window_thread_process_id(self, hwnd, pid_ptr):
        pid_ptr._obj.value = self.pid if hwnd == 100 else self.pid + 1
        return 1


if __name__ == "__main__":
    unittest.main()
