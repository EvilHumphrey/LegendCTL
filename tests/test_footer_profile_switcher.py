from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import empty_snapshot, make_shell


class FooterProfileSwitcherTests(unittest.TestCase):
    def test_footer_persists_across_screens(self) -> None:
        profile = SimpleNamespace(name="Apex", last_modified_at="2026-05-05T00:00:00Z")
        shell = make_shell(settings_service=MagicMock(), profiles=[profile])

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()
            shell.switch_screen("diagnostics")

            self.assertTrue(dpg.does_item_exist("footer_profile_bar"))
            self.assertEqual(dpg.get_value("wrapper_profile_combo"), "Apex")
        finally:
            dpg.destroy_context()

    def test_footer_save_as_opens_modal(self) -> None:
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            with dpg.window():
                shell._build_footer()
            shell._open_save_as_modal()

            self.assertTrue(dpg.does_item_exist("wrapper_profile_save_as_modal"))
            self.assertTrue(dpg.does_item_exist("wrapper_profile_name_input"))
        finally:
            dpg.destroy_context()

    def test_footer_apply_calls_apply_selected(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.apply_selected_wrapper_profile = MagicMock()

        dpg.create_context()
        try:
            with dpg.window():
                shell._build_footer()
            callback = dpg.get_item_callback("footer_apply_button")
            callback()

            shell.apply_selected_wrapper_profile.assert_called_once_with()
        finally:
            dpg.destroy_context()

    def test_footer_combo_refreshes_after_save(self) -> None:
        saved = SimpleNamespace(name="Test Profile", last_modified_at="2026-05-05T00:00:00Z")
        shell = make_shell(settings_service=MagicMock())
        shell.wrapper_profile_store.list_profiles.side_effect = [[], [saved]]
        shell.last_controller_snapshot = empty_snapshot()

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()

            shell.save_current_as_named_wrapper_profile("Test Profile")

            config = dpg.get_item_configuration("wrapper_profile_combo")
            self.assertIn("Test Profile", config["items"])
            self.assertEqual(dpg.get_value("wrapper_profile_combo"), "Test Profile")
        finally:
            dpg.destroy_context()

    def test_footer_combo_refreshes_after_delete(self) -> None:
        profile = SimpleNamespace(name="Test Profile", last_modified_at="2026-05-05T00:00:00Z")
        shell = make_shell(settings_service=MagicMock())
        shell.wrapper_profile_store.list_profiles.side_effect = [[profile], []]
        shell.wrapper_profile_store.delete.return_value = True

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()

            shell._delete_wrapper_profile_confirmed("Test Profile")

            config = dpg.get_item_configuration("wrapper_profile_combo")
            self.assertNotIn("Test Profile", config["items"])
            self.assertEqual(dpg.get_value("wrapper_profile_combo"), "")
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
