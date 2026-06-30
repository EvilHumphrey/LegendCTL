from __future__ import annotations

import re
import unittest
from types import SimpleNamespace

from zd_app import i18n
from zd_app.models import DeviceState
from zd_app.ui import trust_labels
from zd_app.ui.screens import diagnostics, home


class HomeDiagnosticsCopyTests(unittest.TestCase):
    def test_home_stale_warning_copy_is_correct(self) -> None:
        self.assertEqual(
            home.STALE_WARNING_HEADLINE,
            "Controller state may have changed outside the app.",
        )
        self.assertEqual(
            home.STALE_WARNING_HELPER,
            "Read the controller again before trusting current editors or summary fields.",
        )

    def test_home_connection_state_labels_stay_human_readable(self) -> None:
        self.assertEqual(home._connection_state_label("connected"), "Connected")
        self.assertEqual(home._connection_state_label("no_device"), "No Device")
        self.assertEqual(home._connection_state_label("unsupported_firmware"), "Unsupported Firmware")
        self.assertEqual(home._connection_state_label("wrong_mode"), "Wrong Mode")
        self.assertEqual(home._connection_state_label("device_error"), "Device Error")
        self.assertEqual(trust_labels.UNKNOWN_CONNECTION_STATE_LABEL, "Unknown")

    def test_unknown_connection_state_falls_back_to_unknown_and_logs_warning(self) -> None:
        with self.assertLogs("zd_app.ui.trust_labels", level="WARNING") as captured:
            label = home._connection_state_label("reconnecting_wireless")

        self.assertEqual(label, "Unknown")
        self.assertIn("Unmapped connection state label requested: reconnecting_wireless", captured.output[0])

    def test_home_active_config_label_stays_honest_before_source_is_known(self) -> None:
        unknown = DeviceState(active_onboard_profile=1)
        known = DeviceState(active_onboard_profile=3)
        known.summary_sources["active_profile"] = "official_app_ui"

        self.assertEqual(home._active_config_label(unknown), "Not verified")
        self.assertEqual(home._active_config_label(known), "Config 3")

    def test_active_config_label_localizes_to_profile_slot(self) -> None:
        # The device labels its 4 onboard slots positionally "Profile 1-4";
        # LegendCTL now matches that instead of the old "Config N". The internal
        # trust-label token stays "Config N" (asserted above); only the
        # user-facing localized value changes.
        known = DeviceState(active_onboard_profile=2)
        known.summary_sources["active_profile"] = "protocol"
        self.assertEqual(home._localized_active_config_label(known), "Profile 2")

    def test_draft_aligned_label_localizes_to_profile_slot(self) -> None:
        draft = SimpleNamespace(display_name="Draft aligned to Config 2", dirty=False)
        self.assertEqual(home._localized_draft_label(draft), "Draft aligned to Profile 2")

    def test_diagnostics_firmware_target_split_localizes_in_both_locales(self) -> None:
        # An earlier change moved the previously-mixed-language constant
        # FIRMWARE_TARGET_SPLIT_TEXT to an explicit i18n key so the
        # auto-hash translation layer's constant-indirection blind spot
        # stops rendering it raw in both locales.
        from zd_app import i18n

        try:
            i18n.set_locale("en")
            i18n._loaded.clear()
            i18n._reverse_en.clear()
            en_value = i18n.t("ui.diagnostics.firmware_target_split")
            self.assertIn("controller", en_value.lower())
            self.assertIn("receiver", en_value.lower())
            # No CJK characters in the en value (ensures it actually
            # translates rather than falling back to the bilingual literal).
            self.assertNotIn("\u624b\u67c4", en_value)

            i18n.set_locale("zh-CN")
            zh_value = i18n.t("ui.diagnostics.firmware_target_split")
            # zh-CN rendering carries the controller / receiver / sticks list.
            self.assertIn("\u624b\u67c4", zh_value)  # \u624b\u67c4
            self.assertIn("\u63a5\u6536\u5668", zh_value)  # \u63a5\u6536\u5668
            self.assertIn("\u5de6\u6447\u6746", zh_value)  # \u5de6\u6447\u6746
            self.assertIn("\u53f3\u6447\u6746", zh_value)  # \u53f3\u6447\u6746
            self.assertNotEqual(en_value, zh_value)
        finally:
            i18n.set_locale("en")

        self.assertEqual(
            diagnostics.STALE_WARNING_HEADLINE,
            "Controller state may have changed outside the app.",
        )
        self.assertEqual(
            diagnostics.STALE_WARNING_HELPER,
            "Read the controller again before trusting summary fields or write context as current.",
        )

    def test_diagnostics_health_summary_surfaces_stale_state(self) -> None:
        connected_stale = DeviceState(connection_state="connected", data_freshness="stale")
        connected_fresh = DeviceState(connection_state="connected", data_freshness="fresh")
        disconnected = DeviceState(connection_state="no_device", data_freshness="never_read")

        self.assertEqual(
            diagnostics.health_summary_text(connected_stale, None),
            "Stale\nController state may be out of date until the next read.",
        )
        self.assertEqual(
            diagnostics.health_summary_text(connected_fresh, "2026-04-22T12:00:00Z"),
            "Healthy\nLive packets are arriving and diagnostics polling is active.",
        )
        self.assertEqual(
            diagnostics.health_summary_text(disconnected, None),
            "Disconnected\nNo supported controller is currently active.",
        )

    def test_diagnostics_health_summary_localizes_in_zh_cn(self) -> None:
        cases = [
            (
                DeviceState(connection_state="no_device", data_freshness="never_read"),
                None,
                "diagnostics.health.state.disconnected",
                "diagnostics.health.body.disconnected",
            ),
            (
                DeviceState(connection_state="connected", data_freshness="stale"),
                None,
                "diagnostics.health.state.stale",
                "diagnostics.health.body.stale",
            ),
            (
                DeviceState(connection_state="connected", data_freshness="fresh"),
                "2026-05-07T06:02:44+00:00",
                "diagnostics.health.state.healthy",
                "diagnostics.health.body.healthy",
            ),
            (
                DeviceState(
                    connection_state="connected",
                    data_freshness="fresh",
                    last_read_time="2026-05-07T06:02:44+00:00",
                ),
                None,
                "diagnostics.health.state.waiting",
                "diagnostics.health.body.waiting",
            ),
            (
                DeviceState(connection_state="connected", data_freshness="fresh"),
                None,
                "diagnostics.health.state.never_read",
                "diagnostics.health.body.never_read",
            ),
        ]
        i18n.set_locale("zh-CN")
        try:
            for state, packet_ts, state_key, body_key in cases:
                with self.subTest(state_key=state_key):
                    text = diagnostics.health_summary_text(state, packet_ts)
                    self.assertEqual(text, f"{i18n.t(state_key)}\n{i18n.t(body_key)}")
                    self.assertEqual(re.findall(r"[A-Za-z]{4,}", text), [])
        finally:
            i18n.set_locale("en")

    def test_diagnostics_connection_details_distinguish_missing_durations_from_zero(self) -> None:
        state = DeviceState()
        missing_snapshot = SimpleNamespace(
            connection_mode="USB",
            device_id="usb-test",
            firmware_version="Unknown",
            last_packet_timestamp=None,
            last_read_duration_ms=None,
            last_write_duration_ms=None,
        )
        measured_snapshot = SimpleNamespace(
            connection_mode="USB",
            device_id="usb-test",
            firmware_version="Unknown",
            last_packet_timestamp=None,
            last_read_duration_ms=0.0,
            last_write_duration_ms=1.5,
        )

        missing_text = diagnostics.connection_details_text(state, missing_snapshot, "Not verified")
        measured_text = diagnostics.connection_details_text(state, measured_snapshot, "Not verified")

        self.assertIn(i18n.t("diagnostics.connection.value.no_read_recorded"), missing_text)
        self.assertIn(i18n.t("diagnostics.connection.value.no_write_recorded"), missing_text)
        self.assertNotIn("0.00ms", missing_text)
        self.assertIn("0.00ms", measured_text)
        self.assertIn("1.50ms", measured_text)

    def test_diagnostics_connection_details_localize_in_zh_cn(self) -> None:
        state = DeviceState(
            connection_mode="USB",
            stable_identifier="USB\\VID_413D&PID_2104\\ABC123",
            firmware_version="Unknown",
            sleep_setting="Unknown",
            active_onboard_profile=1,
        )
        snapshot = SimpleNamespace(
            connection_mode="USB",
            device_id=state.stable_identifier,
            firmware_version="Unknown",
            last_packet_timestamp="2026-05-07T06:02:44+00:00",
            last_read_duration_ms=135.5,
            last_write_duration_ms=0.0,
        )
        i18n.set_locale("zh-CN")
        try:
            text = diagnostics.connection_details_text(state, snapshot, "XInput (Battery)")
            self.assertIn(f"{i18n.t('diagnostics.connection.field.transport')}: USB", text)
            self.assertIn(
                f"{i18n.t('diagnostics.connection.field.device_id')}: USB\\VID_413D&PID_2104",
                text,
            )
            self.assertNotIn("ABC123", text)
            self.assertIn(f"{i18n.t('diagnostics.connection.field.firmware')}: {i18n.t('common.unknown')}", text)
            self.assertIn(f"{i18n.t('diagnostics.connection.field.sleep')}: {i18n.t('common.unknown')}", text)
            self.assertIn(
                f"{i18n.t('diagnostics.connection.field.active_config')}: {i18n.t('profile.config_state.not_verified')}",
                text,
            )
            self.assertIn(
                f"{i18n.t('diagnostics.connection.field.summary_source')}: {i18n.t('diagnostics.connection.value.xinput_battery')}",
                text,
            )
            self.assertNotIn("XInput（电池）", text)
            self.assertIn("2026-05-07T06:02:44+00:00", text)
            self.assertIn("135.50ms", text)
            self.assertNotIn("Active Config:", text)
            self.assertNotIn("Controller Summary Source:", text)
        finally:
            i18n.set_locale("en")

    def test_diagnostics_active_config_status_waits_for_real_source(self) -> None:
        unknown = DeviceState(active_onboard_profile=1)
        known = DeviceState(active_onboard_profile=2)
        known.summary_sources["active_profile"] = "protocol"

        self.assertEqual(diagnostics.active_config_status_text(unknown), "Not verified")
        self.assertEqual(diagnostics.active_config_status_text(known), "Profile 2")

    def test_freshness_status_labels_stay_human_readable(self) -> None:
        self.assertEqual(
            diagnostics.freshness_status_text(DeviceState(data_freshness="fresh")),
            "Current",
        )
        self.assertEqual(
            diagnostics.freshness_status_text(DeviceState(data_freshness="stale")),
            "Stale",
        )
        self.assertEqual(
            diagnostics.freshness_status_text(DeviceState(data_freshness="never_read")),
            "Never Read",
        )
        self.assertEqual(
            diagnostics.freshness_status_text(DeviceState(data_freshness="write_success")),
            "Write Succeeded",
        )
        self.assertEqual(diagnostics.UNKNOWN_FRESHNESS_STATUS_TEXT, "Unknown")

    def test_unknown_freshness_status_falls_back_to_unknown_and_logs_warning(self) -> None:
        state = DeviceState(data_freshness="syncing_in_background")

        with self.assertLogs("zd_app.ui.screens.diagnostics", level="WARNING") as captured:
            label = diagnostics.freshness_status_text(state)

        self.assertEqual(label, "Unknown")
        self.assertIn("Unmapped freshness status label requested: syncing_in_background", captured.output[0])


if __name__ == "__main__":
    unittest.main()
