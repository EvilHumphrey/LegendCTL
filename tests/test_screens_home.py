from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import empty_snapshot, make_shell
from zd_app.i18n import set_locale, t
from zd_app.models import DeviceState
from zd_app.ui.screens import home


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
        shell.last_controller_snapshot = empty_snapshot()

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")

            self.assertEqual(dpg.get_value("home_profile_active"), "Profile 3")
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
        self.assertIn(t("home.connection.title"), titles)
        self.assertIn(t("home.profile.title"), titles)
        self.assertIn(t("home.recent.title"), titles)
        # The former separate "Quick Actions" + "Next step" cards were merged
        # into one content-fit actions card titled "Next step" (home.cta.title)
        # with Health Check emphasized — see home._actions_card.
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
        # The connection stats route through the metric() component; the values
        # the device service reports should appear verbatim somewhere on screen.
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.state.connection_state = "connected"
        shell.device_service.state.last_read_time = "2026-05-31T00:00:00"
        shell.device_service.format_firmware_version.return_value = "1.24"
        shell.device_service.format_battery_level.return_value = "87%"
        shell.device_service.recent_events.return_value = []

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            home.build(shell, "content_region")
            values = _all_text_values()
            self.assertIn("1.24", values)
            self.assertIn("87%", values)
            # The metric label is rendered as its own (muted) text item, kept
            # separate from the value so each can carry its own color/tag.
            self.assertIn(t("home.connection.firmware"), values)
        finally:
            dpg.destroy_context()


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
        # Merging "Quick Actions" + "Next step" must not drop any action. Every
        # button + target from BOTH former cards survives: Health Check, Read
        # controller state, Open Controller settings, View diagnostics, About.
        # ("Read controller state" was doubled across the two former cards; the
        # merge dedupes it to one, but it must still be present.)
        shell = make_shell(settings_service=MagicMock())
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
            "home.quick.read",
            "home.quick.controller",
            "home.quick.diagnostics",
            "home.quick.about",
        ):
            with self.subTest(button=key):
                self.assertIn(t(key), button_labels)


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


if __name__ == "__main__":
    unittest.main()
