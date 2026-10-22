"""Tests for the preferences screen (UX cleanup: Developer section)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n
from zd_app.models import AppSettings, DeviceState
from zd_app.services.diagnostics_service import DiagnosticsService
from zd_app.storage import _output_guards
from zd_app.ui.app_shell import AppShell
from zd_app.ui import fonts
from zd_app.ui.screens import diagnostics, preferences


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


class DiagnosticOutputCallbackTests(unittest.TestCase):
    """Callback tests stub every UI and shell action; no DPG context is built."""

    def _shell(self, path):
        shell = AppShell.__new__(AppShell)
        shell.settings = AppSettings(diagnostics_bundle_dir=path)
        shell.diagnostics_service = DiagnosticsService()
        shell.device_service = MagicMock()
        shell.device_service.state = DeviceState()
        shell.device_service.last_read_duration_ms = None
        shell.device_service.last_write_duration_ms = None
        shell.refresh_shell = MagicMock()
        shell.refresh_current_screen = MagicMock()
        return shell

    def test_preferences_display_is_lexical_for_all_settings_and_roots(self):
        root = Path.cwd() / "synthetic" / "Roaming" / "ZDUltimateLegend"
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"APPDATA": str(root.parent)}))
            for name in ("resolve", "stat", "open", "mkdir"):
                stack.enter_context(patch.object(Path, name, side_effect=AssertionError("display I/O")))
            stack.enter_context(patch.object(_output_guards, "_get_windows_drive_type", side_effect=AssertionError("display drive probe")))
            for default in (root, Path(r"\\server\share\app")):
                with patch.object(preferences, "_default_user_data_dir", return_value=default):
                    for raw in (r"\\server\share\out", r"\\?\C:\out", "Q:out", r"Z:\out"):
                        with self.subTest(default=default, raw=raw):
                            self.assertEqual(preferences.diagnostics_bundle_dir_display_value(raw), raw)
            with patch.object(preferences, "_default_user_data_dir", return_value=root):
                self.assertEqual(
                    preferences.diagnostics_bundle_dir_display_value(str(root / "diagnostics")),
                    r"%APPDATA%\ZDUltimateLegend\diagnostics",
                )
                with patch.dict(os.environ, {"APPDATA": str(root / "other"), "LOCALAPPDATA": str(root.parent)}):
                    self.assertEqual(
                        preferences.diagnostics_bundle_dir_display_value(str(root / "diagnostics")),
                        r"%LOCALAPPDATA%\ZDUltimateLegend\diagnostics",
                    )

    def test_all_output_callbacks_handle_rejections_without_lookup_or_shell_use(self):
        root = str(Path.cwd() / "synthetic-root")
        variable = "%OUTPUT_GUARD_TEST%/out" if os.name == "nt" else "$OUTPUT_GUARD_TEST/out"
        hostile = (r"\\server\share\out", r"\\?\C:\out", r"\??\C:\out", "Q:out", variable, "~/out")
        for configured in hostile:
            with self.subTest(configured=configured), ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {
                    "ZDUL_DATA_DIR": root, "OUTPUT_GUARD_TEST": r"\\server\share",
                    "USERPROFILE": r"\\server\home", "HOME": "//server/home",
                }))
                for name in ("resolve", "stat", "open", "mkdir"):
                    stack.enter_context(patch.object(Path, name, side_effect=AssertionError("unsafe lookup")))
                native = stack.enter_context(patch.object(_output_guards, "_windows_open", side_effect=AssertionError("unsafe native open")))
                start = stack.enter_context(patch("zd_app.ui.app_shell.os.startfile", create=True))
                card = stack.enter_context(patch.object(diagnostics, "_share_card_result", side_effect=AssertionError("card before admission")))
                stack.enter_context(patch.object(diagnostics.dpg, "does_item_exist", return_value=False))
                stack.enter_context(patch("zd_app.ui.app_shell.logger"))
                stack.enter_context(patch.object(diagnostics, "logger"))
                shell = self._shell(configured)
                shell.export_diagnostics_bundle()
                shell.open_diagnostics_bundle_folder()
                diagnostics._save_share_card(shell)
                native.assert_not_called()
                start.assert_not_called()
                card.assert_not_called()
                shell.device_service.log_i18n_event.assert_any_call("log.diagnostics.bundle_failed")
                shell.device_service.log_i18n_event.assert_any_call("log.diagnostics.share_card_failed")

    def test_unsafe_default_fails_in_every_output_callback(self):
        shell = self._shell(str(Path.cwd() / "synthetic-root"))
        with patch.dict(os.environ, {"ZDUL_DATA_DIR": r"\\server\share\app"}), patch(
            "zd_app.ui.app_shell.os.startfile", create=True
        ) as start, patch.object(diagnostics.dpg, "does_item_exist", return_value=False), patch.object(
            _output_guards, "_windows_open", side_effect=AssertionError("unsafe default lookup")
        ), patch("zd_app.ui.app_shell.logger"), patch.object(diagnostics, "logger"):
            shell.export_diagnostics_bundle()
            shell.open_diagnostics_bundle_folder()
            diagnostics._save_share_card(shell)
        start.assert_not_called()
        shell.device_service.log_i18n_event.assert_any_call("log.diagnostics.open_folder_failed")

    def test_save_card_uses_guarded_files_and_open_folder_dispatch_holds_pins(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = self._shell(str(root / "custom"))
            card = MagicMock()
            card.to_html.return_value = "<html>evidence</html>"
            card.to_markdown.return_value = "# Evidence\n"
            with patch.dict(os.environ, {"ZDUL_DATA_DIR": temp}), patch.object(
                diagnostics, "_share_card_result", return_value=card
            ), patch.object(diagnostics.dpg, "does_item_exist", return_value=False):
                diagnostics._save_share_card(shell)
                self.assertEqual(shell._last_share_card_path.read_text(), card.to_html.return_value)
                markdown = list((root / "custom").glob("*.md"))
                self.assertEqual(len(markdown), 1)
                self.assertEqual(markdown[0].read_text(), card.to_markdown.return_value)
                self.assertEqual(markdown[0].stem, shell._last_share_card_path.stem)

                def dispatch(path):
                    self.assertEqual(Path(path), root / "custom")
                    if os.name == "nt":
                        with self.assertRaises(OSError):
                            (root / "custom").rename(root / "moved")

                with patch("zd_app.ui.app_shell.os.startfile", side_effect=dispatch, create=True) as start:
                    shell.open_diagnostics_bundle_folder()
                start.assert_called_once()

    def test_same_second_card_saves_have_matching_unique_pair_stems(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = self._shell(temp)
            card = MagicMock()
            card.to_html.return_value = "<html>evidence</html>"
            card.to_markdown.return_value = "# Evidence\n"
            with patch.dict(os.environ, {"ZDUL_DATA_DIR": temp}), patch.object(
                diagnostics, "_share_card_result", return_value=card
            ), patch.object(diagnostics.dpg, "does_item_exist", return_value=False), patch.object(
                diagnostics.time, "strftime", return_value="2026-10-21_120000"
            ):
                diagnostics._save_share_card(shell)
                first = shell._last_share_card_path
                diagnostics._save_share_card(shell)
                second = shell._last_share_card_path
            self.assertNotEqual(first, second)
            self.assertEqual(len(list(root.iterdir())), 4)
            for html in (first, second):
                self.assertEqual(html.read_text(), card.to_html.return_value)
                self.assertEqual(html.with_suffix(".md").read_text(), card.to_markdown.return_value)

    def test_card_pair_collision_on_either_suffix_reports_failure_and_preserves_existing(self):
        stamp = "2026-10-21_120000"
        token = "0123456789abcdef0123456789abcdef"
        stem = f"legendctl_evidence_card_{stamp}_{token}"
        for suffix in (".html", ".md"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                original = root / f"{stem}{suffix}"
                original.write_text("existing sentinel")
                shell = self._shell(temp)
                previous = root / "previous.html"
                shell._last_share_card_path = previous
                card = MagicMock()
                card.to_html.return_value = "<html>new evidence</html>"
                card.to_markdown.return_value = "# New evidence\n"
                with patch.dict(os.environ, {"ZDUL_DATA_DIR": temp}), patch.object(
                    diagnostics, "_share_card_result", return_value=card
                ), patch.object(diagnostics.dpg, "does_item_exist", return_value=False), patch.object(
                    diagnostics.time, "strftime", return_value=stamp
                ), patch.object(diagnostics, "uuid4", return_value=SimpleNamespace(hex=token)), patch.object(diagnostics, "logger"):
                    diagnostics._save_share_card(shell)
                self.assertEqual(original.read_text(), "existing sentinel")
                self.assertEqual(shell._last_share_card_path, previous)
                shell.device_service.log_i18n_event.assert_called_once_with("log.diagnostics.share_card_failed")
                # A second-file collision may leave the newly created HTML,
                # but must not rename it or report a successful paired save.
                self.assertEqual(
                    {item.name for item in root.iterdir()},
                    {f"{stem}.html"} if suffix == ".html" else {f"{stem}.html", f"{stem}.md"},
                )


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
