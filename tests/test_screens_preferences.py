"""Tests for the preferences screen (UX cleanup: Developer section)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n
from zd_app.models import AppSettings
from zd_app.ui import fonts
from zd_app.ui.screens import preferences


def _build_in_fresh_context(shell) -> None:
    # FONT_HANDLES persists across DPG contexts as a module-level cache. If a
    # prior test loaded fonts (e.g. test_fonts), the cached IDs reference the
    # destroyed context and bind_item_font raises in this fresh context.
    # Clearing forces font_for() to return None, which preferences.build
    # correctly handles by skipping bind_item_font.
    fonts.FONT_HANDLES.clear()
    with dpg.window():
        with dpg.child_window(tag="content_region"):
            pass
    preferences.build(shell, "content_region")


class PreferencesDeveloperSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_developer_section_renders(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)

            self.assertTrue(dpg.does_item_exist("preferences_developer_section_title"))
            self.assertTrue(dpg.does_item_exist("preferences_developer_panels_toggle"))
            self.assertTrue(dpg.does_item_exist("preferences_developer_panels_helper"))
        finally:
            dpg.destroy_context()

    def test_developer_toggle_default_reflects_settings_default(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        # AppSettings default has developer_panels_visible=False
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertFalse(dpg.get_value("preferences_developer_panels_toggle"))
        finally:
            dpg.destroy_context()

    def test_developer_toggle_default_reflects_persisted_true(self) -> None:
        settings = AppSettings(developer_panels_visible=True)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertTrue(dpg.get_value("preferences_developer_panels_toggle"))
        finally:
            dpg.destroy_context()

    def test_legacy_screens_toggle_renders_in_developer_section(self) -> None:
        # Relocated here from the Diagnostics screen (it's a navigation
        # preference, not a diagnostic). Lives beside the dev-panels toggle.
        shell = make_shell(settings_service=MagicMock())
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertTrue(dpg.does_item_exist("preferences_show_legacy_screens_toggle"))
            self.assertTrue(dpg.does_item_exist("preferences_show_legacy_screens_helper"))
        finally:
            dpg.destroy_context()

    def test_legacy_screens_toggle_reflects_persisted_true(self) -> None:
        settings = AppSettings(show_legacy_screens=True)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertTrue(dpg.get_value("preferences_show_legacy_screens_toggle"))
        finally:
            dpg.destroy_context()

    def test_legacy_screens_toggle_invokes_shell_handler(self) -> None:
        # The checkbox delegates straight to AppShell._toggle_legacy_screens
        # (which persists + rebuilds the full UI). Reassign the bound method to
        # a mock after build so the captured lambda routes through it.
        shell = make_shell(settings_service=MagicMock())
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            shell._toggle_legacy_screens = MagicMock()
            callback = dpg.get_item_callback("preferences_show_legacy_screens_toggle")
            self.assertIsNotNone(callback)
            callback("preferences_show_legacy_screens_toggle", True)
            shell._toggle_legacy_screens.assert_called_once_with(True)
        finally:
            dpg.destroy_context()


class PreferencesToggleHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_dev_toggle_persists_via_update_setting(self) -> None:
        shell = MagicMock()
        preferences._on_developer_panels_toggle(shell, True)
        shell.update_setting.assert_called_once_with("developer_panels_visible", True)

    def test_dev_toggle_triggers_rebuild_full_ui(self) -> None:
        shell = MagicMock()
        preferences._on_developer_panels_toggle(shell, False)
        shell.rebuild_full_ui.assert_called_once()

    def test_dev_toggle_coerces_truthy_value_to_bool(self) -> None:
        shell = MagicMock()
        preferences._on_developer_panels_toggle(shell, 1)
        shell.update_setting.assert_called_once_with("developer_panels_visible", True)


if __name__ == "__main__":
    unittest.main()
