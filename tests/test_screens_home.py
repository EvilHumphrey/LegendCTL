from __future__ import annotations

import json
import tempfile
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import empty_snapshot, make_shell
from zd_app.i18n import set_locale, t
from zd_app.models import DeviceState
from zd_app.ui import trust_front_door
from zd_app.ui.fonts import bind_default_font, register_fonts
from zd_app.ui.screens import home


# A concrete ZD Ultimate Legend instance path used as the connected unit for
# the setup-drawer identity-scope tests (unit-swap honesty, fix B 2026-07-03).
_UNIT_A = r"USB\VID_413D&PID_2104&MI_02\UNIT_A"


def _all_text_values() -> list[str]:
    """Collect the rendered string of every text item in the current context."""
    values: list[str] = []
    for item in dpg.get_all_items():
        if dpg.get_item_type(item) == "mvAppItemType::mvText":
            value = dpg.get_value(item)
            if value is not None:
                values.append(value)
    return values


def _item_types() -> set[str]:
    return {dpg.get_item_type(item) for item in dpg.get_all_items()}


class HomeScreenTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_locale("en")

    def test_home_renders_connection_card_with_state_data(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.state.connection_state = "connected"
        shell.device_service.state.last_read_time = "2026-05-05T00:00:00"
        shell.device_service.recent_events.return_value = ["Controller connected"]

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")

            self.assertTrue(dpg.does_item_exist("home_recent_events"))
            self.assertEqual(dpg.get_value("home_recent_events"), "Controller connected")
        finally:
            dpg.destroy_context()

    def test_home_renders_skeleton_when_no_device(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.state.connection_state = "no_device"
        shell.device_service.state.last_read_time = None

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")

            self.assertTrue(dpg.does_item_exist("home_recent_events"))
        finally:
            dpg.destroy_context()

    def test_home_recent_activity_shows_last_5_events(self) -> None:
        # Capped at 5 (was 10) as a content-height trim — Home is a dashboard
        # glance; Diagnostics owns the full history.
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.state.connection_state = "connected"
        shell.device_service.state.last_read_time = "now"
        shell.device_service.last_apply_result = "Applied"
        shell.device_service.recent_events.return_value = [f"event {idx}" for idx in range(5)]

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")

            text = dpg.get_value("home_recent_events")
            self.assertIn("event 0", text)
            self.assertIn("event 4", text)
            shell.device_service.recent_events.assert_called_with(5)
        finally:
            dpg.destroy_context()

    def test_home_profile_card_localizes_profile_values_in_zh_cn(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.state.connection_state = "connected"
        shell.device_service.state.last_read_time = "now"
        shell.device_service.last_apply_result = "Applied"
        shell.device_service.state.summary_sources["active_profile"] = "unknown"
        shell.last_controller_snapshot = empty_snapshot()
        shell.profile_service.current_draft = SimpleNamespace(
            display_name="Draft aligned to Config 1",
            dirty=False,
        )
        set_locale("zh-CN")

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")

            active = dpg.get_value("home_profile_active")
            draft = dpg.get_value("home_profile_draft")
            self.assertIn("\u672a\u9a8c\u8bc1", active)
            self.assertIn("\u8349\u7a3f\u5bf9\u9f50\u5230\u914d\u7f6e\u6587\u4ef6 1", draft)
            self.assertNotIn("Not verified", active)
            self.assertNotIn("Draft aligned", draft)
        finally:
            dpg.destroy_context()
            set_locale("en")

    def test_home_active_shows_profile_slot_and_tooltip(self) -> None:
        # Home's Active value is the device's onboard slot by NUMBER ("Profile N",
        # relabelled from the old "Config N"); a tooltip explains the controller
        # doesn't report a custom profile name. Building the tooltip also proves
        # its wiring — a bad target tag would crash build.
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.state = DeviceState(active_onboard_profile=3)
        shell.device_service.state.summary_sources["active_profile"] = "protocol"
        shell.device_service.state.connection_state = "connected"
        shell.device_service.state.last_read_time = "now"
        shell.device_service.summary_source_label_for.return_value = "Controller Protocol"
        shell.last_controller_snapshot = empty_snapshot()

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")

            self.assertEqual(
                dpg.get_value("home_profile_active"),
                "Profile 3 - Controller Protocol",
            )
            self.assertTrue(
                any(
                    "doesn't expose a custom profile name" in value
                    for value in _all_text_values()
                )
            )
        finally:
            dpg.destroy_context()

    def test_home_card_titles_use_section_title_helper(self) -> None:
        # Each card heading flows through the section() component,
        # which renders the title via the typography type-scale helper
        # (h2 SemiBold, text.primary) -- not a bare accent-green dpg.add_text.
        # Patch the helper section() calls and assert it ran once per card.
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.recent_events.return_value = []

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            with patch("zd_app.ui.components.section_title") as mock_section_title:
                home.build(shell, "content_region")
        finally:
            dpg.destroy_context()

        titles = [call.args[0] for call in mock_section_title.call_args_list]
        self.assertIn(t("home.orientation.title"), titles)
        self.assertIn(t("home.status.title"), titles)
        self.assertIn(t("home.recent.title"), titles)
        self.assertIn(t("home.cta.title"), titles)

    def test_home_cards_use_flexible_width_not_fixed_520(self) -> None:
        # The current layout removes the fixed 520px card widths in favor of a
        # fill-the-width layout (card() defaults to width=-1). Verify the
        # rendered card child-windows fill available width and that the old
        # magic 520 is gone -- a behavioral guard, robust to comments/docs.
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.recent_events.return_value = []

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")
            widths = [
                dpg.get_item_configuration(item).get("width")
                for item in dpg.get_all_items()
                if dpg.get_item_type(item) == "mvAppItemType::mvChildWindow"
            ]
        finally:
            dpg.destroy_context()

        self.assertNotIn(520, widths)
        self.assertIn(-1, widths)

    def test_home_top_row_uses_a_stretch_table_for_responsiveness(self) -> None:
        # The two top cards live in a table so they split the available width
        # and shrink with the window instead of overflowing at a fixed size.
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.recent_events.return_value = []

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")
            self.assertIn("mvAppItemType::mvTable", _item_types())
        finally:
            dpg.destroy_context()

    def test_home_connection_card_renders_firmware_and_battery_as_metrics(self) -> None:
        # The compact device/profile status card routes firmware and battery
        # through metric(), and retained firmware includes its source.
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.state.connection_state = "connected"
        shell.device_service.state.last_read_time = "2026-05-31T00:00:00"
        shell.device_service.state.firmware_version = "1.24"
        shell.device_service.format_firmware_version.return_value = "1.24"
        shell.device_service.format_battery_level.return_value = "87%"
        shell.device_service.summary_source_label_for.return_value = "Official App UI"
        shell.device_service.recent_events.return_value = []

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")
            values = _all_text_values()
            self.assertIn("1.24 - Official App UI", values)
            self.assertIn("87%", values)
            # The metric label is rendered as its own (muted) text item, kept
            # separate from the value so each can carry its own color/tag.
            self.assertIn(t("home.connection.firmware"), values)
        finally:
            dpg.destroy_context()

    def test_home_status_keeps_unknown_firmware_bare(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.state.connection_state = "connected"
        shell.device_service.format_firmware_version.return_value = "Unknown"
        shell.device_service.summary_source_label_for.return_value = "Official App UI"

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")

            self.assertEqual(dpg.get_value("home_status_firmware"), "Unknown")
        finally:
            dpg.destroy_context()

    def test_connected_explainer_names_shipped_device_profile_card(self) -> None:
        set_locale("en")
        en_text = t("home.state_explainer.connected")
        self.assertIn("Device & profile", en_text)
        self.assertNotIn("Connection card", en_text)

        set_locale("zh-CN")
        zh_text = t("home.state_explainer.connected")
        self.assertIn("设备与配置", zh_text)
        self.assertNotIn("主页“连接”卡", zh_text)

    def test_home_status_card_marks_retained_values_as_last_read_when_disconnected(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.state.connection_state = "no_device"
        shell.device_service.state.last_read_time = "2026-05-31T00:00:00"
        shell.device_service.state.firmware_version = "1.24"
        shell.device_service.state.active_onboard_profile = 3
        shell.device_service.state.summary_sources["active_profile"] = "protocol"
        shell.device_service.format_firmware_version.return_value = "1.24"
        shell.device_service.format_battery_level.return_value = "Unknown"
        shell.device_service.summary_source_label_for.return_value = "Controller Protocol"
        shell.device_service.recent_events.return_value = []

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")

            self.assertEqual(
                dpg.get_value("home_status_firmware"),
                "1.24 (last read) - Controller Protocol",
            )
            self.assertEqual(
                dpg.get_value("home_profile_active"),
                "Profile 3 (last read) - Controller Protocol",
            )
        finally:
            dpg.destroy_context()

    def test_home_retained_qualifiers_wait_for_this_connections_fresh_source(self) -> None:
        # Audit26 HO-F3: after the same unit reconnects, a retained value stays
        # explicitly "last read" until an actual new source arrives.
        shell = make_shell(settings_service=MagicMock())
        state = shell.device_service.state
        state.firmware_version = "1.24"
        state.active_onboard_profile = 3
        state.summary_sources["firmware"] = "official_app_ui"
        state.summary_sources["active_profile"] = "protocol"
        shell.device_service.format_firmware_version.return_value = "1.24"
        shell.device_service.summary_source_label_for.return_value = "Controller Protocol"

        state.connection_state = "no_device"
        self.assertEqual(
            home._firmware_status_value(shell),
            "1.24 (last read) - Controller Protocol",
        )
        self.assertEqual(
            home._active_profile_status_value(shell),
            "Profile 3 (last read) - Controller Protocol",
        )

        state.connection_state = "connected"  # same unit, no fresh read
        shell.device_service.summary_field_refreshed_this_connection.return_value = False
        self.assertEqual(
            home._firmware_status_value(shell),
            "1.24 (last read) - Controller Protocol",
        )
        self.assertEqual(
            home._active_profile_status_value(shell),
            "Profile 3 (last read) - Controller Protocol",
        )

        shell.device_service.summary_field_refreshed_this_connection.return_value = True
        self.assertEqual(
            home._firmware_status_value(shell), "1.24 - Controller Protocol"
        )
        self.assertEqual(
            home._active_profile_status_value(shell),
            "Profile 3 - Controller Protocol",
        )


class HomeLowerDashboardTests(unittest.TestCase):
    """The lower dashboard region — the primary call-to-action card.

    The earlier device-health / restore-point summary row was dropped as a
    content-height trim (it restated firmware + last-read already shown on the
    Connection card); only the CTA remains below the dashboard cards.
    """

    def test_lower_dashboard_renders_cta_card_and_health_check_button(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.recent_events.return_value = []
        shell.device_service.format_firmware_version.return_value = "1.24"

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")

            values = _all_text_values()
            self.assertIn(t("home.cta.title"), values)
            self.assertIn(t("home.orientation.what"), values)
            self.assertIn(t("home.orientation.stance"), values)
            # The dropped summary row's titles must no longer appear on Home.
            # (Asserted as literals — the home.health.*/home.restore.* keys were
            # deleted as orphans when the summary row was removed.)
            self.assertNotIn("Device health", values)
            self.assertNotIn("Most recent restore point", values)

            button_labels = [
                dpg.get_item_configuration(item).get("label")
                for item in dpg.get_all_items()
                if dpg.get_item_type(item) == "mvAppItemType::mvButton"
            ]
            # The primary CTA routes to the existing Health Check screen.
            self.assertIn(t("nav.health_report"), button_labels)
        finally:
            dpg.destroy_context()

    def test_actions_card_preserves_every_quick_and_cta_button(self) -> None:
        # The state-branching next-step card keeps the read/verify path first
        # while preserving the major navigation exits.
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.state.connection_state = "connected"
        shell.device_service.recent_events.return_value = []

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")
            button_labels = [
                dpg.get_item_configuration(item).get("label")
                for item in dpg.get_all_items()
                if dpg.get_item_type(item) == "mvAppItemType::mvButton"
            ]
        finally:
            dpg.destroy_context()

        for key in (
            "nav.health_report",
            "home.orientation.read_settings",
            "home.orientation.open_live_verify",
            "home.quick.controller",
        ):
            with self.subTest(button=key):
                self.assertIn(t(key), button_labels)

    def test_actions_card_branches_to_connect_guidance_without_controller(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.state.connection_state = "no_device"
        shell.device_service.recent_events.return_value = []

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")
            values = _all_text_values()
        finally:
            dpg.destroy_context()

        self.assertIn(t("home.orientation.no_controller_cta"), values)
        self.assertIn(t("home.cta.no_controller_helper"), values)

    def test_home_state_explainer_renders_compact_honest_lines(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.recent_events.return_value = []
        shell.profile_service.pending_changes_count.return_value = 0

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")

            values = _all_text_values()
            self.assertTrue(dpg.does_item_exist("home_state_explainer"))
            self.assertIn(t("home.state_explainer.connected"), values)
            self.assertIn(t("home.state_explainer.firmware_unknown"), values)
            self.assertIn(t("home.state_explainer.profile_not_verified"), values)
            self.assertIn(t("home.state_explainer.pending_changes", count=0), values)
        finally:
            dpg.destroy_context()

    def test_home_trust_front_door_routes_to_diagnostics_guidance(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.recent_events.return_value = []
        shell.switch_screen = MagicMock()

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")

            values = _all_text_values()
            self.assertTrue(dpg.does_item_exist("home_trust_front_door_card"))
            self.assertIn(t("home.trust_front_door.title"), values)
            for link in trust_front_door.TRUST_FRONT_DOOR_LINKS:
                tag = trust_front_door.button_tag("home_trust_front_door", link.target)
                self.assertTrue(dpg.does_item_exist(tag), f"missing {tag}")
                label = dpg.get_item_configuration(tag)["label"]
                self.assertEqual(label, t(link.label_key))

            target = "evidence_card"
            callback = dpg.get_item_callback(
                trust_front_door.button_tag("home_trust_front_door", target)
            )
            callback("sender", None, None)
        finally:
            dpg.destroy_context()

        self.assertEqual(shell.diagnostics_active_tab, "guidance")
        self.assertEqual(
            getattr(shell, trust_front_door.TRUST_FRONT_DOOR_FOCUS_ATTR),
            target,
        )
        shell.switch_screen.assert_called_once_with("diagnostics")

    def test_home_reference_height_budget_runs_isolated(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        test_id = (
            "tests.isolated_home_reference_height."
            "IsolatedHomeReferenceHeightTest."
            "test_home_reference_height_has_no_scrollbar_with_trust_blocks"
        )
        try:
            result = subprocess.run(
                [sys.executable, "-m", "unittest", test_id],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            output_parts: list[str] = []
            for part in (exc.stdout, exc.stderr):
                if isinstance(part, bytes):
                    output_parts.append(part.decode(errors="replace"))
                elif part:
                    output_parts.append(part)
            output = "\n".join(output_parts)
            self.fail(
                "Native render hang reproduced in isolation for the first time. "
                "See zd_research/_wide_layout_hang_investigation_2026-07-02.md."
                + (f"\n\nChild output before timeout:\n{output}" if output else "")
            )

        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if any(line.strip() == "OK" for line in output.splitlines()):
            return

        self.fail(
            "Isolated Home reference-height test did not report unittest OK.\n"
            f"Return code: {result.returncode}\n"
            f"Command: {sys.executable} -m unittest {test_id}\n\n"
            f"Child output:\n{output}"
        )


class HomeSetupDrawerTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_locale("en")

    def _shell(
        self,
        *,
        connection_state: str = "connected",
        device_class: str = "zd_ultimate_legend",
        dismissed: bool = False,
    ):
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.state.connection_state = connection_state
        shell.device_service.state.device_class = device_class
        shell.device_service.state.product_name = "ZD Ultimate Legend"
        shell.device_service.state.stable_identifier = _UNIT_A
        shell.device_service.recent_events.return_value = []
        shell.settings.setup_drawer_dismissed = dismissed
        # The setup drawer scopes its completed-step flags to the connected unit
        # (unit-swap honesty). Pre-stamp both flag identities to the current unit
        # so a test that sets last_snapshot_ts / setup_drawer_restore_point_created
        # exercises the normal same-unit case; the swap case is its own test.
        shell.last_snapshot_identity = _UNIT_A
        shell.setup_drawer_restore_point_identity = _UNIT_A
        return shell

    def _build(self, shell) -> None:
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass
        home.build(shell, "content_region")

    def test_setup_drawer_visibility_requires_connected_recognized_and_not_dismissed(self) -> None:
        cases = (
            ("connected recognized", "connected", "zd_ultimate_legend", False, True),
            ("disconnected", "no_device", "zd_ultimate_legend", False, False),
            ("generic xinput", "connected", "generic_xinput", False, False),
            ("dismissed", "connected", "zd_ultimate_legend", True, False),
        )
        for name, connection_state, device_class, dismissed, expected in cases:
            with self.subTest(name=name):
                shell = self._shell(
                    connection_state=connection_state,
                    device_class=device_class,
                    dismissed=dismissed,
                )
                dpg.create_context()
                try:
                    self._build(shell)
                    self.assertEqual(
                        dpg.does_item_exist("home_setup_drawer_card"),
                        expected,
                    )
                finally:
                    dpg.destroy_context()

    def test_setup_drawer_read_step_branches_and_read_now_uses_existing_read_action(self) -> None:
        shell = self._shell()
        shell.refresh_from_controller = MagicMock()

        dpg.create_context()
        try:
            self._build(shell)
            self.assertTrue(dpg.does_item_exist("home_setup_drawer_read_now"))
            self.assertEqual(dpg.get_value("home_setup_drawer_read_marker"), "-")
            self.assertIn(
                t("home.setup_drawer.settings_read_helper"),
                _all_text_values(),
            )
            callback = dpg.get_item_callback("home_setup_drawer_read_now")
            callback()
        finally:
            dpg.destroy_context()

        shell.refresh_from_controller.assert_called_once_with()

        shell = self._shell()
        shell.last_controller_snapshot = empty_snapshot()
        shell.last_snapshot_ts = 1.0
        dpg.create_context()
        try:
            self._build(shell)
            self.assertFalse(dpg.does_item_exist("home_setup_drawer_read_now"))
            self.assertEqual(dpg.get_value("home_setup_drawer_read_marker"), "✓")
            self.assertIn(
                t("home.setup_drawer.settings_read_label"),
                _all_text_values(),
            )
        finally:
            dpg.destroy_context()

    def test_setup_drawer_restore_step_tracks_session_success_and_wires_explore_links(self) -> None:
        shell = self._shell()
        shell.manual_save_restore_point = MagicMock(return_value=SimpleNamespace(id="rp1"))
        shell.rebuild_current_screen = MagicMock()
        shell.switch_screen = MagicMock()

        dpg.create_context()
        try:
            self._build(shell)
            self.assertTrue(dpg.does_item_exist("home_setup_drawer_create_restore_point"))
            callback = dpg.get_item_callback("home_setup_drawer_create_restore_point")
            callback()
            dpg.get_item_callback("home_setup_drawer_health_check")()
            dpg.get_item_callback("home_setup_drawer_live_verify")()
        finally:
            dpg.destroy_context()

        shell.manual_save_restore_point.assert_called_once_with()
        self.assertTrue(shell.setup_drawer_restore_point_created)
        shell.rebuild_current_screen.assert_called_once_with()
        self.assertEqual(
            [call.args[0] for call in shell.switch_screen.call_args_list],
            ["health_report", "live_verify"],
        )

        shell = self._shell()
        shell.setup_drawer_restore_point_created = True
        dpg.create_context()
        try:
            self._build(shell)
            self.assertFalse(
                dpg.does_item_exist("home_setup_drawer_create_restore_point")
            )
            self.assertEqual(dpg.get_value("home_setup_drawer_backup_marker"), "✓")
            self.assertIn(
                t("home.setup_drawer.restore_point_created"),
                _all_text_values(),
            )
        finally:
            dpg.destroy_context()

    def test_setup_drawer_completion_does_not_carry_across_a_unit_swap(self) -> None:
        # Fix B: swapping two physical ZD units mid-session must not let the new
        # unit inherit the previous one's completed drawer steps. The predicates
        # are pure functions of the shell, so exercise them directly (no DPG).
        shell = self._shell()
        unit_a = shell.device_service.state.stable_identifier
        shell.last_controller_snapshot = empty_snapshot()
        shell.last_snapshot_ts = 1.0
        shell.setup_drawer_restore_point_created = True
        self.assertTrue(home._settings_read_this_session(shell))
        self.assertTrue(home._setup_drawer_restore_point_done(shell))
        self.assertTrue(home._setup_drawer_complete(shell))

        # Swap to unit B: unit A's snapshot + flags still hold their unit-A stamps.
        unit_b = r"USB\VID_413D&PID_2104&MI_02\UNIT_B"
        self.assertNotEqual(unit_a, unit_b)
        shell.device_service.state.stable_identifier = unit_b
        self.assertFalse(home._settings_read_this_session(shell))
        self.assertFalse(home._setup_drawer_restore_point_done(shell))
        self.assertFalse(home._setup_drawer_complete(shell))

        # Unit B earns the steps: a fresh read + restore point stamp B's identity.
        shell.last_snapshot_identity = unit_b
        shell.setup_drawer_restore_point_identity = unit_b
        self.assertTrue(home._settings_read_this_session(shell))
        self.assertTrue(home._setup_drawer_restore_point_done(shell))
        self.assertTrue(home._setup_drawer_complete(shell))

    def _is_shown(self, tag: str) -> bool:
        return bool(dpg.get_item_configuration(tag)["show"])

    def test_setup_drawer_read_step_flips_live_when_read_completes_after_build(self) -> None:
        # Reproduce the REAL order: Home builds FIRST with no snapshot (drawer
        # shows the pending dash + Read-now button), THEN the delayed startup
        # auto-read lands and the refresh hook must flip step 2 to ✓ live.
        shell = self._shell()

        dpg.create_context()
        try:
            self._build(shell)
            self.assertEqual(dpg.get_value("home_setup_drawer_read_marker"), "-")
            self.assertTrue(self._is_shown("home_setup_drawer_read_now"))
            self.assertTrue(self._is_shown("home_setup_drawer_read_helper"))

            shell.last_controller_snapshot = empty_snapshot()
            shell.last_snapshot_ts = 1.0
            home.refresh_setup_drawer(shell)

            self.assertEqual(dpg.get_value("home_setup_drawer_read_marker"), "✓")
            self.assertFalse(self._is_shown("home_setup_drawer_read_now"))
            self.assertFalse(self._is_shown("home_setup_drawer_read_helper"))
            self.assertEqual(
                dpg.get_value("home_setup_drawer_read_label"),
                t("home.setup_drawer.settings_read_label"),
            )
        finally:
            dpg.destroy_context()

    def test_setup_drawer_read_step_flips_live_through_refresh_shell(self) -> None:
        # Same ordering, but driven through the real AppShell.refresh_shell hook
        # (the completion path _refresh_read_on_done calls) rather than the
        # helper directly — proves the wiring, not just the function.
        shell = self._shell()

        dpg.create_context()
        try:
            self._build(shell)
            self.assertEqual(dpg.get_value("home_setup_drawer_read_marker"), "-")

            shell.last_controller_snapshot = empty_snapshot()
            shell.last_snapshot_ts = 1.0
            shell.refresh_shell()

            self.assertEqual(dpg.get_value("home_setup_drawer_read_marker"), "✓")
            self.assertFalse(self._is_shown("home_setup_drawer_read_now"))
        finally:
            dpg.destroy_context()

    def test_setup_drawer_backup_step_flips_live_when_restore_point_created_after_build(self) -> None:
        # Build FIRST with no restore point (pending dash + Create button), THEN
        # a drawer-created restore point sets the flag and the refresh hook must
        # flip step 3 live (the case the operator smoke never exercised because
        # first-connect auto-capture happened to run before Home built).
        shell = self._shell()

        dpg.create_context()
        try:
            self._build(shell)
            self.assertEqual(dpg.get_value("home_setup_drawer_backup_marker"), "-")
            self.assertTrue(self._is_shown("home_setup_drawer_create_restore_point"))

            shell.setup_drawer_restore_point_created = True
            home.refresh_setup_drawer(shell)

            self.assertEqual(dpg.get_value("home_setup_drawer_backup_marker"), "✓")
            self.assertFalse(self._is_shown("home_setup_drawer_create_restore_point"))
            self.assertEqual(
                dpg.get_value("home_setup_drawer_backup_label"),
                t("home.setup_drawer.restore_point_created"),
            )
        finally:
            dpg.destroy_context()

    def test_refresh_setup_drawer_is_noop_when_drawer_absent(self) -> None:
        # Dismissed / different screen: the drawer card never built. The hook
        # must be a silent no-op — not raise — even with read + restore-point
        # state set, and it must not conjure the drawer card.
        shell = self._shell(dismissed=True)
        shell.last_controller_snapshot = empty_snapshot()
        shell.last_snapshot_ts = 1.0
        shell.setup_drawer_restore_point_created = True

        dpg.create_context()
        try:
            self._build(shell)
            self.assertFalse(dpg.does_item_exist("home_setup_drawer_card"))
            home.refresh_setup_drawer(shell)  # must not raise
            self.assertFalse(dpg.does_item_exist("home_setup_drawer_card"))
        finally:
            dpg.destroy_context()

    def _all_done_shell(self):
        shell = self._shell()
        shell.last_controller_snapshot = empty_snapshot()
        shell.last_snapshot_ts = 1.0
        shell.setup_drawer_restore_point_created = True
        return shell

    def test_setup_drawer_builds_compact_variant_when_all_steps_done(self) -> None:
        # The common fresh-build path: connect auto-read + first-connect
        # auto-backup both landed before Home built -> the drawer renders its
        # compact single-row variant directly, with the full step list hidden.
        shell = self._all_done_shell()

        dpg.create_context()
        try:
            self._build(shell)
            self.assertTrue(dpg.does_item_exist("home_setup_drawer_card"))
            self.assertFalse(self._is_shown("home_setup_drawer_full"))
            self.assertTrue(self._is_shown("home_setup_drawer_compact"))
            self.assertEqual(
                dpg.get_value("home_setup_drawer_compact_marker"), "✓"
            )
            self.assertEqual(
                dpg.get_value("home_setup_drawer_compact_label"),
                t("home.setup_drawer.complete"),
            )
            self.assertTrue(dpg.does_item_exist("home_setup_drawer_compact_dismiss"))
        finally:
            dpg.destroy_context()

    def test_setup_drawer_collapses_live_when_last_step_completes(self) -> None:
        # Build pending, complete the steps one at a time through the refresh
        # hook. After the FIRST completion the drawer must stay full (partial
        # progress is not "complete"); after the LAST it must swap to the
        # compact row in place — both orderings (read-last and backup-last).
        orderings = (
            ("backup last", ("read", "backup")),
            ("read last", ("backup", "read")),
        )
        for name, order in orderings:
            with self.subTest(name=name):
                shell = self._shell()
                dpg.create_context()
                try:
                    self._build(shell)
                    self.assertTrue(self._is_shown("home_setup_drawer_full"))
                    self.assertFalse(self._is_shown("home_setup_drawer_compact"))

                    first, last = order
                    if first == "read":
                        shell.last_controller_snapshot = empty_snapshot()
                        shell.last_snapshot_ts = 1.0
                    else:
                        shell.setup_drawer_restore_point_created = True
                    home.refresh_setup_drawer(shell)
                    self.assertTrue(self._is_shown("home_setup_drawer_full"))
                    self.assertFalse(self._is_shown("home_setup_drawer_compact"))

                    if last == "read":
                        shell.last_controller_snapshot = empty_snapshot()
                        shell.last_snapshot_ts = 1.0
                    else:
                        shell.setup_drawer_restore_point_created = True
                    home.refresh_setup_drawer(shell)
                    self.assertFalse(self._is_shown("home_setup_drawer_full"))
                    self.assertTrue(self._is_shown("home_setup_drawer_compact"))
                    self.assertEqual(
                        dpg.get_value("home_setup_drawer_compact_label"),
                        t("home.setup_drawer.complete"),
                    )
                finally:
                    dpg.destroy_context()

    def test_setup_drawer_collapses_live_through_refresh_shell(self) -> None:
        # The real wiring: the last pending step (the startup auto-read)
        # completes and AppShell.refresh_shell — already called from the
        # read-done path — performs the collapse.
        shell = self._shell()
        shell.setup_drawer_restore_point_created = True

        dpg.create_context()
        try:
            self._build(shell)
            self.assertTrue(self._is_shown("home_setup_drawer_full"))

            shell.last_controller_snapshot = empty_snapshot()
            shell.last_snapshot_ts = 1.0
            shell.refresh_shell()

            self.assertFalse(self._is_shown("home_setup_drawer_full"))
            self.assertTrue(self._is_shown("home_setup_drawer_compact"))
        finally:
            dpg.destroy_context()

    def test_setup_drawer_compact_dismiss_persists_through_settings_store(self) -> None:
        # The compact row's dismiss must ride the exact same persistence path
        # as the full variant's button.
        shell = self._all_done_shell()
        shell.rebuild_current_screen = MagicMock()

        dpg.create_context()
        try:
            self._build(shell)
            callback = dpg.get_item_callback("home_setup_drawer_compact_dismiss")
            callback()
        finally:
            dpg.destroy_context()

        self.assertTrue(shell.settings.setup_drawer_dismissed)
        shell.settings_store.set_setup_drawer_dismissed.assert_called_once_with(True)
        shell.rebuild_current_screen.assert_called_once_with()

    def test_setup_drawer_session_flag_flips_on_existing_restore_create_action(self) -> None:
        shell = self._shell()

        self.assertFalse(shell.setup_drawer_restore_point_created)
        rp = shell.manual_save_restore_point()

        self.assertIsNotNone(rp)
        self.assertTrue(shell.setup_drawer_restore_point_created)

    def test_setup_drawer_dismiss_persists_through_settings_store_accessor(self) -> None:
        shell = self._shell()
        shell.rebuild_current_screen = MagicMock()

        dpg.create_context()
        try:
            self._build(shell)
            callback = dpg.get_item_callback("home_setup_drawer_dismiss")
            callback()
        finally:
            dpg.destroy_context()

        self.assertTrue(shell.settings.setup_drawer_dismissed)
        shell.settings_store.set_setup_drawer_dismissed.assert_called_once_with(True)
        shell.rebuild_current_screen.assert_called_once_with()


class HomeSetupDrawerLocaleTests(unittest.TestCase):
    def _load_locale(self, name: str) -> dict[str, str]:
        return json.loads(
            Path("zd_app/i18n/locales", f"{name}.json").read_text(
                encoding="utf-8"
            )
        )

    def test_setup_drawer_locale_keys_have_parity_and_exact_en_copy(self) -> None:
        en = self._load_locale("en")
        zh = self._load_locale("zh-CN")
        expected_en = {
            "home.setup_drawer.title": "First steps with this controller",
            "home.setup_drawer.detected_label": "Detected",
            "home.setup_drawer.settings_read_label": "Settings read from controller",
            "home.setup_drawer.settings_read_helper": "Read the current on-device settings first — nothing is changed by reading.",
            "home.setup_drawer.read_now": "Read now",
            "home.setup_drawer.back_up_label": "Back up before changing anything",
            "home.setup_drawer.back_up_helper": "A restore point saves the controller's current settings so any later change can be undone.",
            "home.setup_drawer.create_restore_point": "Create restore point",
            "home.setup_drawer.restore_point_created": "Restore point created",
            "home.setup_drawer.complete": "First steps complete",
            "home.setup_drawer.explore_label": "Explore",
            "home.setup_drawer.health_check": "Health Check",
            "home.setup_drawer.live_verify": "Live Verify",
            "home.setup_drawer.dismiss": "Don't show these steps again",
        }
        self.assertEqual({key: en[key] for key in expected_en}, expected_en)
        en_keys = {key for key in en if key.startswith("home.setup_drawer.")}
        zh_keys = {key for key in zh if key.startswith("home.setup_drawer.")}
        self.assertEqual(en_keys, zh_keys)
        for key in expected_en:
            self.assertTrue(zh[key].strip(), f"empty zh-CN value for {key}")

    def test_setup_drawer_complete_zh_copy_is_exact(self) -> None:
        # Researcher-authored zh-CN copy for the compact "First steps complete"
        # row, byte-verified via escapes (never re-typed as literal CJK).
        # First word revised \u65b0\u624b -> \u5165\u95e8 in the 2026-07-03
        # naturalness pass; this assertion tracks the locale file exactly.
        zh = self._load_locale("zh-CN")
        self.assertEqual(
            zh["home.setup_drawer.complete"],
            "\u5165\u95e8\u6b65\u9aa4\u5df2\u5b8c\u6210",
        )


class HomeDisclaimerTests(unittest.TestCase):
    """The ZD-required disclaimer is surfaced on the Home landing screen.

    Reuses the same verbatim ``about.zd_disclaimer`` i18n value as the About
    screen (single source of truth), so it is seen on launch without
    navigating away.
    """

    def test_home_renders_zd_disclaimer_banner(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.recent_events.return_value = []

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")

            self.assertTrue(dpg.does_item_exist("home_zd_disclaimer"))
            self.assertEqual(
                dpg.get_value("home_zd_disclaimer"), t("about.zd_disclaimer")
            )
        finally:
            dpg.destroy_context()


class HomeFormatHelperTests(unittest.TestCase):
    """Unit coverage for the Home cosmetic helpers (no DPG context needed)."""

    def test_actions_card_is_content_fit_not_fixed_height(self) -> None:
        # The former fixed-height "Quick Actions" (120) and "Next step" (the old
        # _CTA_CARD_HEIGHT=180) cards were merged into ONE content-fit actions
        # card so it can never grow an inner scrollbar and the magic heights are
        # gone — this is part of what keeps Home under the un-maximized clip.
        import inspect

        src = inspect.getsource(home._actions_card)
        self.assertIn("card(fit=True)", src)
        self.assertNotRegex(src, r"card\(height=")
        # The old CTA-card height constant is retired.
        self.assertFalse(hasattr(home, "_CTA_CARD_HEIGHT"))

    def test_format_last_read_renders_iso_as_date_and_minute(self) -> None:
        self.assertEqual(
            home._format_last_read("2026-06-01T14:30:00"), "2026-06-01 14:30"
        )

    def test_format_last_read_empty_falls_back_to_never(self) -> None:
        self.assertEqual(home._format_last_read(None), t("common.never"))
        self.assertEqual(home._format_last_read(""), t("common.never"))

    def test_format_last_read_unparseable_returned_as_is(self) -> None:
        # A non-ISO string is shown verbatim rather than hidden.
        self.assertEqual(home._format_last_read("just now"), "just now")


class HomeTrustFrontDoorRowFitTests(unittest.TestCase):
    """Rail/wide Home halves the trust card's width, so the four front-door
    buttons no longer fit one row (the 4th clipped mid-label — visual review
    2026-07-06, 02_home_maximized.png). Wide state must wrap to 2x2; windowed
    keeps the single row so the pinned Home height budget is untouched."""

    _LINKS_TAG = "home_trust_front_door_links"

    def tearDown(self) -> None:
        set_locale("en")

    def _shell(self, *, wide: bool):
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.recent_events.return_value = []
        if wide:
            # Same idiom as the diagnostics wide-grid test: overriding the
            # bound getter makes right_rail see a 2560px viewport (>= the
            # 1600 threshold, content width >= work+gap+rail).
            shell._viewport_client_width = lambda: 2560
        return shell

    def _build(self, shell) -> None:
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass
        home.build(shell, "content_region")

    def _button_tags(self) -> list[str]:
        return [
            trust_front_door.button_tag("home_trust_front_door", link.target)
            for link in trust_front_door.TRUST_FRONT_DOOR_LINKS
        ]

    def test_wide_state_wraps_to_two_rows_of_two_with_none_omitted(self) -> None:
        shell = self._shell(wide=True)
        dpg.create_context()
        try:
            self._build(shell)
            # Wrapped structure: a vertical links group holding two horizontal
            # rows of two buttons each.
            self.assertTrue(dpg.does_item_exist(self._LINKS_TAG))
            self.assertFalse(dpg.get_item_configuration(self._LINKS_TAG)["horizontal"])
            for row_index, expected_links in enumerate(
                (
                    trust_front_door.TRUST_FRONT_DOOR_LINKS[:2],
                    trust_front_door.TRUST_FRONT_DOOR_LINKS[2:],
                )
            ):
                row_tag = f"{self._LINKS_TAG}_row_{row_index}"
                self.assertTrue(dpg.does_item_exist(row_tag), f"missing {row_tag}")
                self.assertTrue(dpg.get_item_configuration(row_tag)["horizontal"])
                children = dpg.get_item_children(row_tag, 1) or []
                self.assertEqual(
                    [dpg.get_item_alias(child) for child in children],
                    [
                        trust_front_door.button_tag(
                            "home_trust_front_door", link.target
                        )
                        for link in expected_links
                    ],
                )
            # All four buttons render with their labels (nothing omitted).
            for link, tag in zip(
                trust_front_door.TRUST_FRONT_DOOR_LINKS, self._button_tags()
            ):
                self.assertTrue(dpg.does_item_exist(tag), f"missing {tag}")
                self.assertEqual(
                    dpg.get_item_configuration(tag)["label"], t(link.label_key)
                )
        finally:
            dpg.destroy_context()

    def test_windowed_state_keeps_the_single_row(self) -> None:
        shell = self._shell(wide=False)
        dpg.create_context()
        try:
            self._build(shell)
            self.assertTrue(dpg.does_item_exist(self._LINKS_TAG))
            self.assertTrue(dpg.get_item_configuration(self._LINKS_TAG)["horizontal"])
            self.assertFalse(dpg.does_item_exist(f"{self._LINKS_TAG}_row_0"))
            children = dpg.get_item_children(self._LINKS_TAG, 1) or []
            self.assertEqual(
                [dpg.get_item_alias(child) for child in children],
                self._button_tags(),
            )
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
