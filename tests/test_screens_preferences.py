"""Tests for the preferences screen (UX cleanup: Developer section)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n
from zd_app.models import AppSettings
from zd_app.services.diagnostics_service import DiagnosticsService
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

    def test_diagnostics_bundle_dir_displays_appdata_collapsed_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"APPDATA": str(Path(tmpdir) / "Roaming")},
            clear=True,
        ), patch.object(sys, "frozen", True, create=True):
            stored = Path(os.environ["APPDATA"]) / "ZDUltimateLegend" / "diagnostics"

            self.assertEqual(
                preferences.diagnostics_bundle_dir_display_value(str(stored)),
                r"%APPDATA%\ZDUltimateLegend\diagnostics",
            )

    def test_diagnostics_bundle_dir_display_leaves_external_path_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"APPDATA": str(Path(tmpdir) / "Roaming")},
            clear=True,
        ), patch.object(sys, "frozen", True, create=True):
            external = str(Path(tmpdir) / "Other" / "diagnostics")

            self.assertEqual(
                preferences.diagnostics_bundle_dir_display_value(external),
                external,
            )

    def test_diagnostics_bundle_dir_open_target_expands_env_display_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"APPDATA": str(Path(tmpdir) / "Roaming")},
            clear=True,
        ):
            expected = Path(os.environ["APPDATA"]) / "ZDUltimateLegend" / "diagnostics"

            self.assertEqual(
                preferences.diagnostics_bundle_dir_open_target(
                    r"%APPDATA%\ZDUltimateLegend\diagnostics"
                ),
                expected,
            )

    def test_open_folder_uses_safe_default_for_out_of_root_setting(self) -> None:
        with tempfile.TemporaryDirectory() as data_root, tempfile.TemporaryDirectory() as outside:
            with patch.dict(os.environ, {"ZDUL_DATA_DIR": data_root}):
                shell = make_shell(
                    settings_service=MagicMock(),
                    settings=AppSettings(diagnostics_bundle_dir=str(outside)),
                )
                shell.diagnostics_service = DiagnosticsService()
                shell.refresh_shell = lambda: None
                shell.refresh_current_screen = lambda: None

                with patch("zd_app.ui.app_shell.os.startfile", create=True) as startfile:
                    shell.open_diagnostics_bundle_folder()

                opened = Path(startfile.call_args.args[0])
                self.assertEqual(
                    opened.resolve(),
                    (Path(data_root) / "diagnostics").resolve(),
                )
                self.assertEqual(list(Path(outside).iterdir()), [])
                shell.device_service.log_i18n_event.assert_any_call(
                    "log.diagnostics.open_folder_safe_fallback"
                )

    def test_open_folder_rejects_unc_setting_before_startfile(self) -> None:
        shell = make_shell(
            settings_service=MagicMock(),
            settings=AppSettings(
                diagnostics_bundle_dir=r"\\fileserver\share\diagnostics"
            ),
        )
        shell.diagnostics_service = DiagnosticsService()
        shell.refresh_shell = lambda: None
        shell.refresh_current_screen = lambda: None

        with patch("zd_app.ui.app_shell.os.startfile", create=True) as startfile:
            shell.open_diagnostics_bundle_folder()

        startfile.assert_not_called()
        shell.device_service.log_i18n_event.assert_any_call(
            "log.diagnostics.open_folder_unc_rejected"
        )

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
