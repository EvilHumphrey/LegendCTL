from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app.models import AppSettings


class LegacyStashTests(unittest.TestCase):
    def test_legacy_screens_hidden_by_default(self) -> None:
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()

            self.assertFalse(dpg.does_item_exist("nav_legacy_buttons"))
            self.assertFalse(dpg.does_item_exist("nav_legacy_sticks"))
            self.assertFalse(dpg.does_item_exist("nav_legacy_triggers"))
            self.assertFalse(dpg.does_item_exist("nav_legacy_lighting"))
        finally:
            dpg.destroy_context()

    def test_legacy_screens_shown_when_settings_enable(self) -> None:
        shell = make_shell(
            settings_service=MagicMock(),
            settings=AppSettings(show_legacy_screens=True),
        )

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()

            self.assertTrue(dpg.does_item_exist("nav_legacy_buttons"))
            self.assertTrue(dpg.does_item_exist("nav_legacy_sticks"))
            self.assertTrue(dpg.does_item_exist("nav_legacy_triggers"))
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
