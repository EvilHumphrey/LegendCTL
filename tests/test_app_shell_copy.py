from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import dearpygui.dearpygui as dpg

from zd_app.i18n import set_locale, t
from zd_app.models import AppSettings, DeviceState
from zd_app.ui.app_shell import AppShell, _top_profile_label
from zd_app.ui.screens import diagnostics
from zd_app.ui.themes import COLORS


class AppShellCopyTests(unittest.TestCase):
    def make_shell(self) -> AppShell:
        settings_store = MagicMock()
        # Dev-scaffold sections are gated behind developer_panels_visible
        # in the UX-cleanup pass. These tests assert the dev sections
        # render, so opt the test shell into the developer-visible state.
        settings_store.load.return_value = AppSettings(developer_panels_visible=True)
        device_service = MagicMock()
        device_service.state = DeviceState()
        device_service.recent_events.return_value = []
        device_service.summary_source_summary.return_value = "Not verified"
        device_service.last_read_duration_ms = None
        device_service.last_write_duration_ms = None
        profile_service = MagicMock()
        profile_service.pending_changes_count.return_value = 0
        diagnostics_service = MagicMock()
        diagnostics_service.build_snapshot.return_value = SimpleNamespace(
            connection_mode="Unknown",
            device_id="unknown",
            firmware_version="Unknown",
            active_profile=1,
            last_packet_timestamp=None,
            last_read_duration_ms=None,
            last_write_duration_ms=None,
            buttons_pressed=[],
            stick_values={"lx": 0, "ly": 0, "rx": 0, "ry": 0},
            trigger_values={"lt": 0, "rt": 0},
            packet_rate_hz=0.0,
            recent_anomaly_count=0,
            event_log=[],
        )
        shell = AppShell(
            device_service=device_service,
            profile_service=profile_service,
            diagnostics_service=diagnostics_service,
            settings_store=settings_store,
            preflight_service=MagicMock(),
        )
        shell.rebuild_current_screen = lambda: None
        return shell

    def test_top_profile_label_waits_for_real_source(self) -> None:
        unknown = DeviceState(active_onboard_profile=1)
        known = DeviceState(active_onboard_profile=2)
        known.summary_sources["active_profile"] = "protocol"

        self.assertEqual(_top_profile_label(unknown), "Profile: Not verified")
        self.assertEqual(_top_profile_label(known), "Profile 2")

    def test_top_bar_does_not_boot_with_default_config_1(self) -> None:
        shell = self.make_shell()

        dpg.create_context()
        try:
            with dpg.window():
                shell._build_top_bar()
            self.assertEqual(dpg.get_value("top_profile"), "Profile: Not verified")
        finally:
            dpg.destroy_context()

    def test_top_bar_not_verified_profile_has_explanatory_tooltip(self) -> None:
        shell = self.make_shell()

        dpg.create_context()
        try:
            with dpg.window():
                shell._build_top_bar()

            tooltip = dpg.get_value("top_active_profile_tooltip_text")
            self.assertEqual(dpg.get_value("top_active_profile"), "Profile: Not verified")
            self.assertTrue(dpg.get_item_configuration("top_active_profile_tooltip")["show"])
            self.assertEqual(tooltip, t("status.config.not_verified_tooltip"))
            self.assertIn("hasn't independently confirmed", tooltip)
            self.assertIn("official app's view can lag", tooltip)
            self.assertIn("settings work normally", tooltip)
            self.assertIn("Profiles tab", tooltip)
            self.assertNotIn("security", tooltip.lower())
            self.assertNotIn("safety", tooltip.lower())
        finally:
            dpg.destroy_context()

    def test_top_bar_verified_profile_hides_not_verified_tooltip(self) -> None:
        shell = self.make_shell()
        shell.device_service.state.active_onboard_profile = 2
        shell.device_service.state.summary_sources["active_profile"] = "protocol"

        dpg.create_context()
        try:
            with dpg.window():
                shell._build_top_bar()

            self.assertEqual(dpg.get_value("top_active_profile"), "Profile 2")
            self.assertFalse(dpg.get_item_configuration("top_active_profile_tooltip")["show"])
        finally:
            dpg.destroy_context()

    def test_status_strip_renders_connected_state_in_zh_cn(self) -> None:
        shell = self.make_shell()
        shell.settings.language = "zh-CN"
        shell.device_service.state.connection_state = "connected"
        shell.device_service.state.connection_mode = "USB"
        shell.device_service.state.sync_status = "Connected"
        set_locale("zh-CN")

        dpg.create_context()
        try:
            with dpg.window():
                shell._build_top_bar()
            shell.refresh_shell()

            self.assertEqual(dpg.get_value("top_connection_mode"), "USB")
            self.assertEqual(dpg.get_value("top_sync_status"), "已连接")
            self.assertEqual(dpg.get_value("top_profile"), "配置文件：未验证")
        finally:
            set_locale("en")
            dpg.destroy_context()

    def test_top_status_dot_color_changes_with_state(self) -> None:
        shell = self.make_shell()

        def color_255(tag: str) -> tuple[int, int, int, int]:
            color = dpg.get_item_configuration(tag)["color"]
            return tuple(round(channel * 255) for channel in color)

        dpg.create_context()
        try:
            with dpg.window():
                shell._build_top_bar()

            shell.device_service.state.connection_state = "no_device"
            shell.device_service.state.sync_status = "Disconnected"
            shell.refresh_shell()
            self.assertEqual(color_255("top_connection_dot"), COLORS["text.muted"])

            shell.device_service.state.connection_state = "connected"
            shell.device_service.state.sync_status = "Connected"
            shell.refresh_shell()
            self.assertEqual(color_255("top_connection_dot"), COLORS["accent.primary"])
        finally:
            dpg.destroy_context()

    def test_footer_status_localizes_when_no_writes_yet_in_zh_cn(self) -> None:
        shell = self.make_shell()
        shell.device_service.last_apply_result = None
        shell.wrapper_profile_store = MagicMock()
        shell.wrapper_profile_store.list_profiles.return_value = []
        set_locale("zh-CN")

        dpg.create_context()
        try:
            with dpg.window():
                shell._build_footer()
            shell.refresh_shell()

            self.assertEqual(
                dpg.get_value("footer_status_text"),
                t("apply.idle_ready"),
            )
        finally:
            set_locale("en")
            dpg.destroy_context()

    def test_footer_status_retranslates_on_locale_switch_pre_apply(self) -> None:
        shell = self.make_shell()
        shell.device_service.last_apply_result = None
        shell.wrapper_profile_store = MagicMock()
        shell.wrapper_profile_store.list_profiles.return_value = []

        dpg.create_context()
        try:
            with dpg.window():
                shell._build_footer()
            set_locale("en")
            shell.refresh_shell()
            en_value = dpg.get_value("footer_status_text")

            set_locale("zh-CN")
            shell.refresh_shell()
            zh_value = dpg.get_value("footer_status_text")

            self.assertEqual(en_value, "Ready.")
            self.assertEqual(zh_value, t("apply.idle_ready"))
            self.assertNotEqual(en_value, zh_value)
        finally:
            set_locale("en")
            dpg.destroy_context()

    def test_diagnostics_connection_details_do_not_boot_with_default_config_1(self) -> None:
        shell = self.make_shell()
        shell.current_screen = "Diagnostics"

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            diagnostics.build(shell, "content_region")
            shell.refresh_current_screen()
            details = dpg.get_value("diag_connection_details")
            self.assertIn("Active Config: Not verified", details)
            self.assertIn("Controller Summary Source: Not verified", details)
            self.assertNotIn("Active Profile: 1", details)
        finally:
            dpg.destroy_context()

