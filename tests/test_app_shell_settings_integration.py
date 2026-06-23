"""Hardware-free tests for AppShell v2 SettingsService integration."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.r2_shell_test_helpers import empty_snapshot
from zd_app import i18n
from zd_app.models import AppSettings, DeviceState, WrapperProfile
from zd_app.services.device_service import DeviceService, LogEntry
from zd_app.services.settings_service import (
    AxisInversion,
    BackPaddleBinding,
    ButtonMapping,
    ButtonSlot,
    ControllerButtonTarget,
    ControllerSnapshot,
    INVERSION_STICK_LEFT,
    INVERSION_STICK_RIGHT,
    LightingMode,
    LightingSettings,
    LightingZone,
    MacroSlot,
    PollingRate,
    RgbColor,
    SensitivityAnchor,
    SetAxisInversionOutcome,
    SetBackPaddleBindingOutcome,
    SetButtonBindingOutcome,
    SetDeadzoneOutcome,
    SetLightingOutcome,
    SetPollingRateOutcome,
    SetSensitivityCurveOutcome,
    SetStepSizeOutcome,
    SetTriggerSettingsOutcome,
    SetVibrationOutcome,
    SettingsServiceError,
    StickDeadzones,
    TRIGGER_SELECTOR_LEFT,
    TRIGGER_SELECTOR_RIGHT,
    TriggerMode,
    TriggerSettings as ServiceTriggerSettings,
    TriggerVibrationMode,
    VibrationSettings,
    build_all_deadzones_payload,
    build_axis_inversion_payload,
    build_button_binding_payload,
    build_lighting_payload,
    build_left_stick_sensitivity_curve_payload,
    build_right_stick_sensitivity_curve_payload,
    build_trigger_settings_payload,
    build_vibration_payload,
)
from zd_app.storage.wrapper_profile_store import WrapperProfileError
from zd_app.ui.app_shell import (
    APPLY_DEVICE_CONFIRM_MODAL,
    POST_APPLY_READ_SETTLE_S,
    READ_TIMEOUT_RETRY_SETTLE_S,
    SAVE_AS_INCLUDE_DEVICE_CHECKBOX,
    AppShell,
    ApplyFailure,
    SENSITIVITY_PRESETS,
    SENSITIVITY_PRESETS_8POINT,
    _GEOMETRY_LOG_SETTLE_FRAMES,
    _format_apply_failure_row,
)


def _make_shell(settings_service=None, wrapper_profile_store=None) -> AppShell:
    settings_store = MagicMock()
    settings_store.load.return_value = AppSettings()
    device_service = MagicMock()
    device_service.state = DeviceState()
    device_service.recent_events.return_value = []
    device_service.summary_source_summary.return_value = "Not verified"
    device_service.last_read_duration_ms = None
    device_service.last_write_duration_ms = None
    profile_service = MagicMock()
    profile_service.pending_changes_count.return_value = 0
    profile_service.last_switch_result = None
    profile_service.switch_transport_candidate_source_label = "None"
    profile_service.switch_transport_status_label = "No Current Candidate"
    profile_service.last_verified_change_label = "No Verified Change Yet"
    profile_service.last_switch_official_sync_label = "Not Checked Yet"
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
    if wrapper_profile_store is None:
        wrapper_profile_store = MagicMock()
        wrapper_profile_store.list_profiles.return_value = []
    shell = AppShell(
        device_service=device_service,
        profile_service=profile_service,
        diagnostics_service=diagnostics_service,
        settings_store=settings_store,
        preflight_service=MagicMock(),
        settings_service=settings_service,
        wrapper_profile_store=wrapper_profile_store,
    )
    shell.refresh_shell = lambda: None
    shell.rebuild_current_screen = lambda: None
    return shell


def _controller_button_mapping(target: ControllerButtonTarget) -> ButtonMapping:
    return ButtonMapping.controller_button(target)


_DEFAULT = object()


def _full_snapshot(
    *,
    polling_rate: PollingRate | None = PollingRate.HZ_8000,
    vibration=_DEFAULT,
    deadzones=_DEFAULT,
    axis_inversion_left=_DEFAULT,
    axis_inversion_right=_DEFAULT,
    sensitivity_left=_DEFAULT,
    sensitivity_right=_DEFAULT,
    sensitivity_left_8point=None,
    sensitivity_right_8point=None,
    trigger_left=_DEFAULT,
    trigger_right=_DEFAULT,
    button_bindings: dict[ButtonSlot, ButtonMapping] | None = None,
    lighting_zones: dict[LightingZone, LightingSettings] | None = None,
    back_paddle_bindings: dict[MacroSlot, BackPaddleBinding] | None = None,
    step_size: int | None = 146,
) -> ControllerSnapshot:
    if vibration is _DEFAULT:
        vibration = VibrationSettings(11, 22, 33, 44, TriggerVibrationMode.TRIGGER_VIBRATION)
    if deadzones is _DEFAULT:
        deadzones = StickDeadzones(1, 2, 3, 4)
    if axis_inversion_left is _DEFAULT:
        axis_inversion_left = AxisInversion(True, False)
    if axis_inversion_right is _DEFAULT:
        axis_inversion_right = AxisInversion(False, True)
    if sensitivity_left is _DEFAULT:
        sensitivity_left = (
            SensitivityAnchor(1, 2),
            SensitivityAnchor(3, 4),
            SensitivityAnchor(5, 6),
        )
    if sensitivity_right is _DEFAULT:
        sensitivity_right = (
            SensitivityAnchor(7, 8),
            SensitivityAnchor(9, 10),
            SensitivityAnchor(11, 12),
        )
    if trigger_left is _DEFAULT:
        trigger_left = ServiceTriggerSettings(13, 77, TriggerMode.LONG)
    if trigger_right is _DEFAULT:
        trigger_right = ServiceTriggerSettings(14, 88, TriggerMode.SHORT)
    if button_bindings is None:
        button_bindings = {
            slot: _controller_button_mapping(ControllerButtonTarget.A)
            for slot in ButtonSlot
        }
        button_bindings[ButtonSlot.A] = _controller_button_mapping(ControllerButtonTarget.B)
    if lighting_zones is None:
        lighting_zones = {
            LightingZone.HOME: LightingSettings(
                light_on=True,
                mode=LightingMode.FLOW,
                brightness_byte=128,
                color=RgbColor(1, 2, 3),
            ),
            LightingZone.LEFT_LIGHT: LightingSettings(
                light_on=False,
                mode=LightingMode.BREATH,
                brightness_byte=64,
                color=RgbColor(4, 5, 6),
            ),
            LightingZone.RIGHT_LIGHT: LightingSettings(
                light_on=True,
                mode=LightingMode.FADE,
                brightness_byte=200,
                color=RgbColor(7, 8, 9),
            ),
        }
    if back_paddle_bindings is None:
        back_paddle_bindings = {}
    return ControllerSnapshot(
        polling_rate=polling_rate,
        vibration=vibration,
        deadzones=deadzones,
        axis_inversion_left=axis_inversion_left,
        axis_inversion_right=axis_inversion_right,
        sensitivity_left=sensitivity_left,
        sensitivity_right=sensitivity_right,
        trigger_left=trigger_left,
        trigger_right=trigger_right,
        button_bindings=button_bindings,
        lighting_zones=lighting_zones,
        back_paddle_bindings=back_paddle_bindings,
        step_size=step_size,
        sensitivity_left_8point=sensitivity_left_8point,
        sensitivity_right_8point=sensitivity_right_8point,
    )


def _run_with_widget_capture(callback):
    values, _config = _capture_widget_state(callback)
    return values


def _capture_widget_state(callback):
    """Run callback under patched DPG, capturing set_value AND configure_item.

    Returns (values, config): ``values`` maps tag -> last set_value; ``config``
    maps tag -> merged configure_item kwargs (e.g. ``enabled`` / ``show``).
    """

    values = {}
    config = {}

    def set_value(tag, value):
        values[tag] = value

    def configure_item(tag, **kwargs):
        config.setdefault(tag, {}).update(kwargs)

    with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True), patch(
        "zd_app.ui.app_shell.dpg.set_value",
        side_effect=set_value,
    ), patch(
        "zd_app.ui.app_shell.dpg.configure_item",
        side_effect=configure_item,
    ):
        callback()
    return values, config


def _ok_profile_write_results(settings_service: MagicMock) -> None:
    settings_service.set_polling_rate.return_value = SimpleNamespace(
        outcome=SetPollingRateOutcome.OK,
        error_code=None,
    )
    settings_service.set_step_size.return_value = SimpleNamespace(
        outcome=SetStepSizeOutcome.OK,
        error_code=None,
    )
    settings_service.set_vibration.return_value = SimpleNamespace(
        outcome=SetVibrationOutcome.OK,
        error_code=None,
    )
    settings_service.set_all_deadzones.return_value = SimpleNamespace(
        outcome=SetDeadzoneOutcome.OK,
        error_code=None,
    )
    settings_service.set_left_stick_inversion.return_value = SimpleNamespace(
        outcome=SetAxisInversionOutcome.OK,
        error_code=None,
    )
    settings_service.set_right_stick_inversion.return_value = SimpleNamespace(
        outcome=SetAxisInversionOutcome.OK,
        error_code=None,
    )
    settings_service.set_left_stick_sensitivity_curve.return_value = SimpleNamespace(
        outcome=SetSensitivityCurveOutcome.OK,
        error_code=None,
    )
    settings_service.set_right_stick_sensitivity_curve.return_value = SimpleNamespace(
        outcome=SetSensitivityCurveOutcome.OK,
        error_code=None,
    )
    settings_service.set_left_trigger_settings.return_value = SimpleNamespace(
        outcome=SetTriggerSettingsOutcome.OK,
        error_code=None,
    )
    settings_service.set_right_trigger_settings.return_value = SimpleNamespace(
        outcome=SetTriggerSettingsOutcome.OK,
        error_code=None,
    )
    settings_service.set_button_binding.return_value = SimpleNamespace(
        outcome=SetButtonBindingOutcome.OK,
        error_code=None,
    )
    settings_service.set_zone_lighting.return_value = SimpleNamespace(
        outcome=SetLightingOutcome.OK,
        error_code=None,
    )
    settings_service.set_back_paddle_binding.return_value = SimpleNamespace(
        outcome=SetBackPaddleBindingOutcome.OK,
        error_code=None,
    )


class TestAppShellSettingsIntegration(unittest.TestCase):
    def test_app_shell_accepts_settings_service_parameter(self) -> None:
        settings_service = MagicMock()

        shell = _make_shell(settings_service)

        self.assertIs(shell.settings_service, settings_service)

    def test_list_wrapper_profiles_tolerates_store_error(self) -> None:
        store = MagicMock()
        store.list_profiles.side_effect = RuntimeError("disk unavailable")
        shell = _make_shell(MagicMock(), store)

        with self.assertLogs("zd_app.ui.app_shell", level="WARNING"):
            profiles = shell.list_wrapper_profiles()

        self.assertEqual(profiles, [])

    def test_list_wrapper_profiles_tracks_skipped_profiles(self) -> None:
        store = MagicMock()
        profile = WrapperProfile(name="Apex", snapshot=_full_snapshot())
        skipped = [object()]
        store.list_profiles.return_value = ([profile], skipped)
        shell = _make_shell(MagicMock(), store)

        profiles = shell.list_wrapper_profiles()

        self.assertEqual(profiles, [profile])
        self.assertEqual(shell.wrapper_profiles_skipped_count(), 1)

    def test_save_current_as_wrapper_profile_persists_snapshot(self) -> None:
        store = MagicMock()
        shell = _make_shell(MagicMock(), store)
        snapshot = _full_snapshot()
        shell.last_controller_snapshot = snapshot

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value="Apex"):
            shell.save_current_as_wrapper_profile()

        saved_profile = store.save.call_args.args[0]
        self.assertIsInstance(saved_profile, WrapperProfile)
        self.assertEqual(saved_profile.name, "Apex")
        # Device settings are excluded by default (checkbox off / no UI).
        self.assertIsNone(saved_profile.snapshot.polling_rate)
        self.assertIsNone(saved_profile.snapshot.step_size)
        # Non-Device settings are still persisted as-is.
        self.assertEqual(saved_profile.snapshot.vibration, snapshot.vibration)
        self.assertEqual(saved_profile.snapshot.button_bindings, snapshot.button_bindings)

    def test_save_current_as_wrapper_profile_rejects_no_snapshot(self) -> None:
        store = MagicMock()
        shell = _make_shell(MagicMock(), store)
        shell.last_controller_snapshot = None

        shell.save_current_as_wrapper_profile()

        store.save.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once()
        self.assertIn(
            "click Refresh from Controller first",
            shell.device_service.record_apply_result.call_args.args[1],
        )

    def test_save_current_as_wrapper_profile_rejects_empty_name(self) -> None:
        store = MagicMock()
        shell = _make_shell(MagicMock(), store)
        shell.last_controller_snapshot = _full_snapshot()

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value="   "):
            shell.save_current_as_wrapper_profile()

        store.save.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Save failed: profile name is required.",
        )

    def test_save_current_as_wrapper_profile_clears_name_input_on_success(self) -> None:
        store = MagicMock()
        shell = _make_shell(MagicMock(), store)
        shell.last_controller_snapshot = _full_snapshot()
        shell._dpg_context_ready = True
        rendered = {}

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value="Apex"), patch(
            "zd_app.ui.app_shell.dpg.does_item_exist",
            return_value=True,
        ), patch(
            "zd_app.ui.app_shell.dpg.set_value",
            side_effect=lambda tag, value: rendered.__setitem__(tag, value),
        ):
            shell.save_current_as_wrapper_profile()

        self.assertEqual(rendered["settings_v2_status_text"], "OK: Saved profile 'Apex'.")
        self.assertEqual(rendered["wrapper_profile_name_input"], "")

    def test_apply_selected_wrapper_profile_writes_all_populated_categories(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        snapshot = _full_snapshot()
        store = MagicMock()
        store.load.return_value = WrapperProfile(name="Apex", snapshot=snapshot)
        shell = _make_shell(settings_service, store)
        shell.refresh_from_controller = MagicMock()

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value="Apex"):
            shell.apply_selected_wrapper_profile()

        settings_service.set_polling_rate.assert_called_once_with(snapshot.polling_rate)
        settings_service.set_step_size.assert_called_once_with(snapshot.step_size)
        settings_service.set_vibration.assert_called_once_with(snapshot.vibration)
        settings_service.set_all_deadzones.assert_called_once_with(snapshot.deadzones)
        settings_service.set_left_stick_inversion.assert_called_once_with(
            snapshot.axis_inversion_left,
        )
        settings_service.set_right_stick_inversion.assert_called_once_with(
            snapshot.axis_inversion_right,
        )
        settings_service.set_left_stick_sensitivity_curve.assert_called_once_with(
            snapshot.sensitivity_left,
        )
        settings_service.set_right_stick_sensitivity_curve.assert_called_once_with(
            snapshot.sensitivity_right,
        )
        settings_service.set_left_trigger_settings.assert_called_once_with(
            snapshot.trigger_left,
        )
        settings_service.set_right_trigger_settings.assert_called_once_with(
            snapshot.trigger_right,
        )
        self.assertEqual(settings_service.set_button_binding.call_count, len(ButtonSlot))
        self.assertEqual(settings_service.set_zone_lighting.call_count, len(LightingZone))
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Applied profile 'Apex' (29 writes).",
        )

    def test_apply_selected_wrapper_profile_counts_back_paddles(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        back_paddles = {
            slot: BackPaddleBinding(ControllerButtonTarget.A)
            for slot in MacroSlot
        }
        snapshot = _full_snapshot(back_paddle_bindings=back_paddles)
        store = MagicMock()
        store.load.return_value = WrapperProfile(name="Apex", snapshot=snapshot)
        shell = _make_shell(settings_service, store)
        shell.refresh_from_controller = MagicMock()

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value="Apex"):
            shell.apply_selected_wrapper_profile()

        self.assertEqual(settings_service.set_back_paddle_binding.call_count, len(MacroSlot))
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Applied profile 'Apex' (37 writes).",
        )

    def test_apply_selected_wrapper_profile_skips_none_categories(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        snapshot = _full_snapshot(polling_rate=None)
        store = MagicMock()
        store.load.return_value = WrapperProfile(name="Apex", snapshot=snapshot)
        shell = _make_shell(settings_service, store)
        shell.refresh_from_controller = MagicMock()

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value="Apex"):
            shell.apply_selected_wrapper_profile()

        settings_service.set_polling_rate.assert_not_called()
        settings_service.set_vibration.assert_called_once_with(snapshot.vibration)

    def test_apply_selected_wrapper_profile_aggregates_failures(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        settings_service.set_vibration.return_value = SimpleNamespace(
            outcome=SetVibrationOutcome.WRITE_FAILED,
            error_code=995,
        )
        settings_service.set_all_deadzones.return_value = SimpleNamespace(
            outcome=SetDeadzoneOutcome.WRITE_FAILED,
            error_code=995,
        )
        store = MagicMock()
        store.load.return_value = WrapperProfile(name="Apex", snapshot=_full_snapshot())
        shell = _make_shell(settings_service, store)
        shell.refresh_from_controller = MagicMock()

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value="Apex"):
            shell.apply_selected_wrapper_profile()

        success, message = shell.device_service.record_apply_result.call_args.args
        self.assertFalse(success)
        self.assertIn("Partial: Applied profile 'Apex'", message)
        self.assertIn("2 failed", message)
        self.assertEqual(shell._last_apply_result.total_attempted, 29)
        self.assertEqual(
            [failure.setting_label for failure in shell._last_apply_result.failed],
            ["vibration", "deadzones"],
        )

    def test_apply_failure_row_label_localizes_in_zh_cn(self) -> None:
        try:
            i18n.set_locale("zh-CN")

            rows = [
                _format_apply_failure_row(ApplyFailure("polling", "boom", True)),
                _format_apply_failure_row(ApplyFailure("back_paddle_M1", "boom", False)),
                _format_apply_failure_row(ApplyFailure("binding_A", "boom", False)),
            ]

            self.assertIn("轮询速率", rows[0])
            self.assertIn("暂时性", rows[0])
            self.assertIn("后置侧键 M1", rows[1])
            self.assertIn("按键映射 A", rows[2])
        finally:
            i18n.set_locale("en")

    def test_apply_failure_unknown_slug_falls_back_to_raw(self) -> None:
        row = _format_apply_failure_row(ApplyFailure("future_slug", "boom", False))

        self.assertIn("future_slug", row)
        self.assertNotIn("[apply.failure.row_label.future_slug]", row)

    def test_apply_failure_rows_do_not_expose_representative_raw_slugs_in_zh_cn(self) -> None:
        try:
            i18n.set_locale("zh-CN")
            rows = [
                _format_apply_failure_row(ApplyFailure("back_paddle_M3", "boom", False)),
                _format_apply_failure_row(ApplyFailure("binding_A", "boom", False)),
                _format_apply_failure_row(ApplyFailure("axis_inv_left", "boom", False)),
            ]
            rendered = "\n".join(rows)

            self.assertNotIn("back_paddle_M3", rendered)
            self.assertNotIn("binding_A", rendered)
            self.assertNotIn("axis_inv_left", rendered)
        finally:
            i18n.set_locale("en")

    def test_apply_selected_wrapper_profile_message_counts_retry_recoveries(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        settings_service.set_vibration.return_value = SimpleNamespace(
            outcome=SetVibrationOutcome.OK_WITH_RETRY,
            error_code=None,
        )
        store = MagicMock()
        store.load.return_value = WrapperProfile(name="Apex", snapshot=_full_snapshot())
        shell = _make_shell(settings_service, store)
        shell.refresh_from_controller = MagicMock()

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value="Apex"):
            shell.apply_selected_wrapper_profile()

        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Applied profile 'Apex' (29 writes, 1 recovered).",
        )
        self.assertEqual(shell._last_apply_result.retry_recoveries, 1)

    def test_apply_selected_wrapper_profile_logs_recent_activity_once(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        snapshot = _full_snapshot()
        store = MagicMock()
        store.load.return_value = WrapperProfile(name="Apex", snapshot=snapshot)
        shell = _make_shell(settings_service, store)
        shell.device_service = DeviceService()
        shell.refresh_from_controller = MagicMock()

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value="Apex"):
            shell.apply_selected_wrapper_profile()

        matching_events = [
            event
            for event in shell.device_service.recent_events(10)
            if "OK: Applied profile 'Apex' (29 writes)." in event
        ]
        self.assertEqual(len(matching_events), 1)

    def test_apply_snapshot_aggregates_failures_into_apply_result(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        settings_service.set_vibration.return_value = SimpleNamespace(
            outcome=SetVibrationOutcome.WRITE_FAILED,
            error_code=995,
        )
        snapshot = _full_snapshot(
            polling_rate=None,
            deadzones=None,
            axis_inversion_left=None,
            axis_inversion_right=None,
            sensitivity_left=None,
            sensitivity_right=None,
            trigger_left=None,
            trigger_right=None,
            button_bindings={},
            lighting_zones={},
            step_size=None,
        )
        shell = _make_shell(settings_service, MagicMock())

        result = shell._apply_snapshot_to_controller(snapshot)

        self.assertEqual(result.total_attempted, 1)
        self.assertEqual(result.succeeded, 0)
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(result.failed[0].setting_label, "vibration")
        self.assertTrue(result.failed[0].is_transient)
        self.assertIn("HID write failed after retry", result.failed[0].error)

    def test_apply_snapshot_modal_shown_on_failure(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        settings_service.set_vibration.return_value = SimpleNamespace(
            outcome=SetVibrationOutcome.WRITE_FAILED,
            error_code=995,
        )
        store = MagicMock()
        # Avoid the device-override confirm interception by stripping
        # Device fields from the snapshot — this test exercises the apply
        # failure modal, not the device-confirm flow.
        store.load.return_value = WrapperProfile(
            name="Apex",
            snapshot=_full_snapshot(polling_rate=None, step_size=None),
        )
        shell = _make_shell(settings_service, store)
        shell.refresh_from_controller = MagicMock()
        shell._dpg_context_ready = True
        window_context = MagicMock()
        child_context = MagicMock()
        group_context = MagicMock()
        labels = []

        def add_button(**kwargs):
            labels.append(kwargs.get("label"))
            return kwargs.get("label")

        with (
            patch("zd_app.ui.app_shell.dpg.get_value", return_value="Apex"),
            patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=False),
            patch("zd_app.ui.app_shell.dpg.window", return_value=window_context) as window,
            patch("zd_app.ui.app_shell.dpg.child_window", return_value=child_context),
            patch("zd_app.ui.app_shell.dpg.group", return_value=group_context),
            patch("zd_app.ui.app_shell.dpg.add_text"),
            patch("zd_app.ui.app_shell.dpg.add_spacer"),
            patch("zd_app.ui.app_shell.dpg.add_button", side_effect=add_button),
        ):
            shell.apply_selected_wrapper_profile()

        window.assert_called_with(
            tag="apply_failure_modal",
            label="Profile Apply Incomplete",
            modal=True,
            no_close=False,
            no_resize=True,
            width=520,
            height=300,
        )
        self.assertIn("Retry Failed Only", labels)
        self.assertIn("OK", labels)

    def test_retry_failed_settings_only_writes_failed_set(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        settings_service.set_vibration.return_value = SimpleNamespace(
            outcome=SetVibrationOutcome.WRITE_FAILED,
            error_code=995,
        )
        settings_service.set_all_deadzones.return_value = SimpleNamespace(
            outcome=SetDeadzoneOutcome.WRITE_FAILED,
            error_code=995,
        )
        store = MagicMock()
        store.load.return_value = WrapperProfile(name="Apex", snapshot=_full_snapshot())
        shell = _make_shell(settings_service, store)
        shell.refresh_from_controller = MagicMock()

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value="Apex"):
            shell.apply_selected_wrapper_profile()

        settings_service.set_vibration.return_value = SimpleNamespace(
            outcome=SetVibrationOutcome.OK,
            error_code=None,
        )
        settings_service.set_all_deadzones.return_value = SimpleNamespace(
            outcome=SetDeadzoneOutcome.OK,
            error_code=None,
        )
        before_polling_calls = settings_service.set_polling_rate.call_count

        retry_result = shell._retry_failed_settings()

        self.assertEqual(retry_result.succeeded, 2)
        self.assertEqual(retry_result.failed, [])
        self.assertEqual(settings_service.set_vibration.call_count, 2)
        self.assertEqual(settings_service.set_all_deadzones.call_count, 2)
        self.assertEqual(settings_service.set_polling_rate.call_count, before_polling_calls)

    def test_apply_selected_wrapper_profile_continues_on_exception(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        settings_service.set_vibration.side_effect = RuntimeError("boom")
        store = MagicMock()
        store.load.return_value = WrapperProfile(name="Apex", snapshot=_full_snapshot())
        shell = _make_shell(settings_service, store)
        shell.refresh_from_controller = MagicMock()

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value="Apex"):
            shell.apply_selected_wrapper_profile()

        settings_service.set_all_deadzones.assert_called_once()
        success, message = shell.device_service.record_apply_result.call_args.args
        self.assertFalse(success)
        self.assertIn("Partial: Applied profile 'Apex'", message)
        self.assertEqual(shell._last_apply_result.failed[0].setting_label, "vibration")
        self.assertEqual(shell._last_apply_result.failed[0].error, "boom")

    def test_apply_selected_wrapper_profile_calls_refresh_after_writes(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        store = MagicMock()
        store.load.return_value = WrapperProfile(name="Apex", snapshot=_full_snapshot())
        shell = _make_shell(settings_service, store)
        shell.refresh_from_controller = MagicMock()

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value="Apex"):
            shell.apply_selected_wrapper_profile()

        # Headless apply (no DPG context) bypasses the device-confirm modal and
        # applies the full snapshot — include_device=True is the default.
        shell.refresh_from_controller.assert_called_once_with(include_device=True)

    def test_apply_selected_wrapper_profile_no_settings_service(self) -> None:
        store = MagicMock()
        shell = _make_shell(None, store)

        shell.apply_selected_wrapper_profile()

        store.load.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Apply failed: settings service unavailable.",
        )

    def test_apply_selected_wrapper_profile_no_selection(self) -> None:
        settings_service = MagicMock()
        store = MagicMock()
        shell = _make_shell(settings_service, store)

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value=""):
            shell.apply_selected_wrapper_profile()

        store.load.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Apply failed: no profile selected.",
        )

    def test_apply_selected_wrapper_profile_load_error(self) -> None:
        settings_service = MagicMock()
        store = MagicMock()
        store.load.side_effect = WrapperProfileError("Profile not found: 'Apex'")
        shell = _make_shell(settings_service, store)

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value="Apex"):
            shell.apply_selected_wrapper_profile()

        shell.device_service.record_apply_result.assert_called_once()
        self.assertIn(
            "Profile not found",
            shell.device_service.record_apply_result.call_args.args[1],
        )

    def test_delete_wrapper_profile_confirmed_deletes_on_confirm(self) -> None:
        store = MagicMock()
        store.delete.return_value = True
        shell = _make_shell(MagicMock(), store)
        shell.rebuild_current_screen = MagicMock()

        shell._delete_wrapper_profile_confirmed("Apex")

        store.delete.assert_called_once_with("Apex")
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Deleted profile 'Apex'.",
        )
        shell.rebuild_current_screen.assert_called_once_with()

    def test_delete_wrapper_profile_confirmed_handles_missing(self) -> None:
        store = MagicMock()
        store.delete.return_value = False
        shell = _make_shell(MagicMock(), store)

        shell._delete_wrapper_profile_confirmed("Apex")

        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Delete failed: profile 'Apex' not found.",
        )

    def test_confirm_delete_wrapper_profile_handles_no_selection(self) -> None:
        store = MagicMock()
        shell = _make_shell(MagicMock(), store)

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value=""):
            shell.confirm_delete_wrapper_profile()

        store.delete.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Delete failed: no profile selected.",
        )

    def test_confirm_delete_wrapper_profile_delete_callback_uses_user_data(self) -> None:
        store = MagicMock()
        shell = _make_shell(MagicMock(), store)
        shell._delete_wrapper_profile_confirmed = MagicMock()
        captured_delete_button = {}
        window_context = MagicMock()
        group_context = MagicMock()

        def add_button(**kwargs):
            if kwargs.get("label") == "Delete":
                captured_delete_button.update(kwargs)
            return kwargs.get("label")

        with (
            patch("zd_app.ui.app_shell.dpg.get_value", return_value="Apex"),
            patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=False),
            patch("zd_app.ui.app_shell.dpg.window", return_value=window_context),
            patch("zd_app.ui.app_shell.dpg.group", return_value=group_context),
            patch("zd_app.ui.app_shell.dpg.add_text"),
            patch("zd_app.ui.app_shell.dpg.add_spacer"),
            patch("zd_app.ui.app_shell.dpg.add_button", side_effect=add_button),
        ):
            shell.confirm_delete_wrapper_profile()

        self.assertEqual(captured_delete_button["user_data"], "Apex")

        callback = captured_delete_button["callback"]
        callback(12345, None, captured_delete_button["user_data"])

        shell._delete_wrapper_profile_confirmed.assert_called_once_with("Apex")
        store.delete.assert_not_called()

    def test_apply_writes_all_button_bindings(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        bindings = {
            slot: _controller_button_mapping(ControllerButtonTarget.A)
            for slot in ButtonSlot
        }
        snapshot = _full_snapshot(
            polling_rate=None,
            vibration=None,
            deadzones=None,
            axis_inversion_left=None,
            axis_inversion_right=None,
            sensitivity_left=None,
            sensitivity_right=None,
            trigger_left=None,
            trigger_right=None,
            button_bindings=bindings,
            lighting_zones={},
            step_size=None,
        )
        shell = _make_shell(settings_service, MagicMock())

        result = shell._apply_snapshot_to_controller(snapshot)

        self.assertEqual(result.succeeded, len(ButtonSlot))
        self.assertEqual(result.failed, [])
        self.assertEqual(settings_service.set_button_binding.call_count, len(ButtonSlot))

    def test_apply_snapshot_writes_back_paddle_bindings(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        back_paddles = {
            MacroSlot.M1: BackPaddleBinding(ControllerButtonTarget.A),
            MacroSlot.LM: BackPaddleBinding(None),
        }
        snapshot = _full_snapshot(
            polling_rate=None,
            vibration=None,
            deadzones=None,
            axis_inversion_left=None,
            axis_inversion_right=None,
            sensitivity_left=None,
            sensitivity_right=None,
            trigger_left=None,
            trigger_right=None,
            button_bindings={},
            lighting_zones={},
            back_paddle_bindings=back_paddles,
            step_size=None,
        )
        shell = _make_shell(settings_service, MagicMock())

        result = shell._apply_snapshot_to_controller(snapshot)

        self.assertEqual(result.succeeded, len(back_paddles))
        self.assertEqual(result.failed, [])
        settings_service.set_back_paddle_binding.assert_any_call(
            MacroSlot.M1,
            ControllerButtonTarget.A,
        )
        settings_service.set_back_paddle_binding.assert_any_call(MacroSlot.LM, None)
        self.assertEqual(shell._last_back_paddle_bindings, back_paddles)

    def test_refresh_from_controller_calls_get_all_settings(self) -> None:
        settings_service = MagicMock()
        settings_service.get_all_settings.return_value = _full_snapshot()
        shell = _make_shell(settings_service)

        _run_with_widget_capture(shell.refresh_from_controller)

        settings_service.get_all_settings.assert_called_once_with()

    def test_refresh_from_controller_hydrates_polling_rate(self) -> None:
        settings_service = MagicMock()
        settings_service.get_all_settings.return_value = _full_snapshot(
            polling_rate=PollingRate.HZ_8000,
        )
        shell = _make_shell(settings_service)

        values = _run_with_widget_capture(shell.refresh_from_controller)

        self.assertEqual(values["usb_polling_rate_combo"], "8000Hz")

    def test_refresh_from_controller_hydrates_vibration(self) -> None:
        settings_service = MagicMock()
        settings_service.get_all_settings.return_value = _full_snapshot(
            vibration=VibrationSettings(
                left_grip_strength=12,
                right_grip_strength=34,
                left_trigger_motor_strength=56,
                right_trigger_motor_strength=78,
                mode=TriggerVibrationMode.STEREO_RESONANCE,
            ),
        )
        shell = _make_shell(settings_service)

        values = _run_with_widget_capture(shell.refresh_from_controller)

        self.assertEqual(values["vibration_lg_slider"], 12)
        self.assertEqual(values["vibration_rg_slider"], 34)
        self.assertEqual(values["vibration_lm_slider"], 56)
        self.assertEqual(values["vibration_rm_slider"], 78)
        self.assertEqual(values["vibration_mode_combo"], "Stereo Resonance")

    def test_refresh_from_controller_hydrates_triggers_both_sides(self) -> None:
        settings_service = MagicMock()
        settings_service.get_all_settings.return_value = _full_snapshot(
            trigger_left=ServiceTriggerSettings(10, 80, TriggerMode.LONG),
            trigger_right=ServiceTriggerSettings(20, 90, TriggerMode.SHORT),
        )
        shell = _make_shell(settings_service)

        values = _run_with_widget_capture(shell.refresh_from_controller)

        self.assertEqual(values["trigger_left_min_slider"], 10)
        self.assertEqual(values["trigger_left_max_slider"], 80)
        self.assertEqual(values["trigger_left_mode_combo"], "Long")
        self.assertEqual(values["trigger_right_min_slider"], 20)
        self.assertEqual(values["trigger_right_max_slider"], 90)
        self.assertEqual(values["trigger_right_mode_combo"], "Short")

    def test_refresh_from_controller_hydrates_deadzones(self) -> None:
        settings_service = MagicMock()
        settings_service.get_all_settings.return_value = _full_snapshot(
            deadzones=StickDeadzones(5, 6, 7, 8),
        )
        shell = _make_shell(settings_service)

        values = _run_with_widget_capture(shell.refresh_from_controller)

        self.assertEqual(values["deadzone_left_center_slider"], 5)
        self.assertEqual(values["deadzone_right_center_slider"], 6)
        self.assertEqual(values["deadzone_left_outer_slider"], 7)
        self.assertEqual(values["deadzone_right_outer_slider"], 8)

    def test_refresh_from_controller_hydrates_sensitivity_both_sticks(self) -> None:
        settings_service = MagicMock()
        settings_service.get_all_settings.return_value = _full_snapshot(
            sensitivity_left=(
                SensitivityAnchor(10, 11),
                SensitivityAnchor(12, 13),
                SensitivityAnchor(14, 15),
            ),
            sensitivity_right=(
                SensitivityAnchor(20, 21),
                SensitivityAnchor(22, 23),
                SensitivityAnchor(24, 25),
            ),
        )
        shell = _make_shell(settings_service)

        values = _run_with_widget_capture(shell.refresh_from_controller)

        self.assertEqual(values["sensitivity_left_a1x_slider"], 10)
        self.assertEqual(values["sensitivity_left_a1y_slider"], 11)
        self.assertEqual(values["sensitivity_left_a2x_slider"], 12)
        self.assertEqual(values["sensitivity_left_a2y_slider"], 13)
        self.assertEqual(values["sensitivity_left_a3x_slider"], 14)
        self.assertEqual(values["sensitivity_left_a3y_slider"], 15)
        self.assertEqual(values["sensitivity_right_a1x_slider"], 20)
        self.assertEqual(values["sensitivity_right_a1y_slider"], 21)
        self.assertEqual(values["sensitivity_right_a2x_slider"], 22)
        self.assertEqual(values["sensitivity_right_a2y_slider"], 23)
        self.assertEqual(values["sensitivity_right_a3x_slider"], 24)
        self.assertEqual(values["sensitivity_right_a3y_slider"], 25)

    def test_refresh_from_controller_hydrates_sensitivity_8point_both_sticks(self) -> None:
        settings_service = MagicMock()
        # Left anchors (0,1),(2,3),...,(14,15); right (20,21),...,(34,35).
        settings_service.get_all_settings.return_value = _full_snapshot(
            sensitivity_left_8point=tuple(
                SensitivityAnchor(n, n + 1) for n in range(0, 16, 2)
            ),
            sensitivity_right_8point=tuple(
                SensitivityAnchor(n, n + 1) for n in range(20, 36, 2)
            ),
        )
        shell = _make_shell(settings_service)

        values = _run_with_widget_capture(shell.refresh_from_controller)

        for index in range(1, 9):
            left_x = (index - 1) * 2
            self.assertEqual(
                values[f"sensitivity_left_a{index}x_slider_8point"], left_x
            )
            self.assertEqual(
                values[f"sensitivity_left_a{index}y_slider_8point"], left_x + 1
            )
            right_x = 20 + (index - 1) * 2
            self.assertEqual(
                values[f"sensitivity_right_a{index}x_slider_8point"], right_x
            )
            self.assertEqual(
                values[f"sensitivity_right_a{index}y_slider_8point"], right_x + 1
            )
        # The 3-point sliders are still hydrated independently on capable devices.
        self.assertIn("sensitivity_left_a1x_slider", values)

    def test_refresh_from_controller_3point_only_leaves_8point_sliders_untouched(self) -> None:
        # Legacy device: 8-point snapshot fields are None, so _hydrate_sensitivity
        # _8point must not write any ``_8point`` slider.
        settings_service = MagicMock()
        settings_service.get_all_settings.return_value = _full_snapshot()
        shell = _make_shell(settings_service)

        values = _run_with_widget_capture(shell.refresh_from_controller)

        eight_point = [tag for tag in values if tag.endswith("_slider_8point")]
        self.assertEqual(eight_point, [])

    def test_refresh_from_controller_hydrates_axis_inversion_both_sides(self) -> None:
        settings_service = MagicMock()
        settings_service.get_all_settings.return_value = _full_snapshot(
            axis_inversion_left=AxisInversion(True, True),
            axis_inversion_right=AxisInversion(False, True),
        )
        shell = _make_shell(settings_service)

        values = _run_with_widget_capture(shell.refresh_from_controller)

        self.assertIs(values["axis_inv_left_x_checkbox"], True)
        self.assertIs(values["axis_inv_left_y_checkbox"], True)
        self.assertIs(values["axis_inv_right_x_checkbox"], False)
        self.assertIs(values["axis_inv_right_y_checkbox"], True)

    def test_refresh_from_controller_hydrates_button_bindings_picks_a_or_first(self) -> None:
        settings_service = MagicMock()
        settings_service.get_all_settings.return_value = _full_snapshot()
        shell = _make_shell(settings_service)

        values = _run_with_widget_capture(shell.refresh_from_controller)

        self.assertEqual(values["binding_source_combo"], "A")
        self.assertEqual(values["binding_target_combo"], "B")

        fallback_service = MagicMock()
        fallback_service.get_all_settings.return_value = _full_snapshot(
            button_bindings={
                ButtonSlot.B: _controller_button_mapping(ControllerButtonTarget.X),
            },
        )
        fallback_shell = _make_shell(fallback_service)

        fallback_values = _run_with_widget_capture(fallback_shell.refresh_from_controller)

        self.assertEqual(fallback_values["binding_source_combo"], "B")
        self.assertEqual(fallback_values["binding_target_combo"], "X")

    def test_refresh_from_controller_hydrates_lighting_picks_home_or_first(self) -> None:
        settings_service = MagicMock()
        settings_service.get_all_settings.return_value = _full_snapshot()
        shell = _make_shell(settings_service)

        values = _run_with_widget_capture(shell.refresh_from_controller)

        self.assertEqual(values["lighting_zone_combo"], "Home")
        self.assertIs(values["lighting_on_checkbox"], True)
        self.assertEqual(values["lighting_mode_combo"], "Flow")
        self.assertEqual(values["lighting_brightness_slider"], 128)
        self.assertEqual(values["lighting_r_slider"], 1)
        self.assertEqual(values["lighting_g_slider"], 2)
        self.assertEqual(values["lighting_b_slider"], 3)

        fallback_settings = LightingSettings(
            light_on=False,
            mode=LightingMode.BREATH,
            brightness_byte=64,
            color=RgbColor(4, 5, 6),
        )
        fallback_service = MagicMock()
        fallback_service.get_all_settings.return_value = _full_snapshot(
            lighting_zones={LightingZone.LEFT_LIGHT: fallback_settings},
        )
        fallback_shell = _make_shell(fallback_service)

        fallback_values = _run_with_widget_capture(fallback_shell.refresh_from_controller)

        self.assertEqual(fallback_values["lighting_zone_combo"], "Left")
        self.assertIs(fallback_values["lighting_on_checkbox"], False)
        self.assertEqual(fallback_values["lighting_mode_combo"], "Breath")
        self.assertEqual(fallback_values["lighting_brightness_slider"], 64)
        self.assertEqual(fallback_values["lighting_r_slider"], 4)
        self.assertEqual(fallback_values["lighting_g_slider"], 5)
        self.assertEqual(fallback_values["lighting_b_slider"], 6)

    def test_refresh_from_controller_partial_snapshot_logs_missing(self) -> None:
        settings_service = MagicMock()
        settings_service.get_all_settings.return_value = _full_snapshot(
            vibration=None,
            trigger_right=None,
            button_bindings={},
        )
        shell = _make_shell(settings_service)

        values = _run_with_widget_capture(shell.refresh_from_controller)

        status = values["settings_v2_status_text"]
        self.assertIn("Read failed for:", status)
        self.assertIn("vibration", status)
        self.assertIn("trigger_right", status)
        self.assertIn("button_bindings", status)
        shell.device_service.log_i18n_event.assert_called_with(
            "log.snapshot.refreshed_missing",
            fields="vibration, trigger_right, button_bindings",
        )

    def test_refresh_from_controller_settings_service_none_logs_unavailable(self) -> None:
        shell = _make_shell(None)

        values = _run_with_widget_capture(shell.refresh_from_controller)

        self.assertEqual(values["settings_v2_status_text"], "Waiting for controller...")
        shell.device_service.log_i18n_event.assert_called_with("log.read.waiting")

    def test_appshell_tick_consumes_needs_hydration_flag(self) -> None:
        shell = _make_shell(MagicMock())
        shell.current_screen = "Settings"
        shell._needs_hydration = True
        shell.rebuild_current_screen = MagicMock()
        shell.refresh_from_controller = MagicMock()

        shell._tick_settings_service_tasks(10.0)

        self.assertFalse(shell._needs_hydration)
        shell.rebuild_current_screen.assert_called_once_with()
        shell.refresh_from_controller.assert_called_once_with()

    def test_rebuild_full_ui_called_on_locale_change(self) -> None:
        shell = _make_shell(MagicMock())
        shell.rebuild_full_ui = MagicMock()

        shell.update_language("zh-CN")

        shell.rebuild_full_ui.assert_called_once_with()
        self.assertEqual(shell.settings.language, "zh-CN")
        shell.device_service.log_i18n_event.assert_called_with(
            "log.setting.updated",
            label_key="setting.label.language",
        )

    def test_update_language_applies_locale_even_when_save_fails(self) -> None:
        # A4: a settings save OSError (disk full / locked file) must NOT abort
        # update_language before the locale is applied — otherwise in-memory
        # state, the active locale, and the visible UI desync. The save failure
        # is swallowed (logged); the locale change still happens.
        shell = _make_shell(MagicMock())
        shell.settings_store.save.side_effect = OSError("disk full")
        shell._locale_router.set_locale = MagicMock()

        shell.update_language("zh-CN")  # must not raise

        self.assertEqual(shell.settings.language, "zh-CN")
        shell._locale_router.set_locale.assert_called_once_with("zh-CN")

    def test_update_setting_log_humanizes_developer_panels_visible_in_en(self) -> None:
        i18n.set_locale("en")
        shell = _make_shell(MagicMock())
        shell.device_service = DeviceService(clock=lambda: 0.0)

        shell.update_setting("developer_panels_visible", True)

        self.assertTrue(
            shell.device_service.recent_events(1)[0].endswith(
                "Updated developer panels visibility setting."
            )
        )

    def test_update_setting_log_localizes_in_zh_cn(self) -> None:
        shell = _make_shell(MagicMock())
        shell.device_service = DeviceService(clock=lambda: 0.0)
        i18n.set_locale("zh-CN")

        shell.update_setting("developer_panels_visible", True)

        self.assertTrue(
            shell.device_service.recent_events(1)[0].endswith(
                "已更新开发者面板可见性设置。"
            )
        )
        i18n.set_locale("en")

    def test_update_setting_log_unknown_key_falls_back_to_raw_key(self) -> None:
        i18n.set_locale("en")
        shell = _make_shell(MagicMock())
        shell.device_service = DeviceService(clock=lambda: 0.0)

        shell.update_setting("future_setting", True)

        self.assertTrue(
            shell.device_service.recent_events(1)[0].endswith(
                "Updated future_setting setting."
            )
        )

    def test_tick_detects_absent_to_present_transition_and_triggers_hydration(self) -> None:
        settings_service = MagicMock()
        shell = _make_shell(settings_service)
        shell.settings.auto_read_on_connect = False
        shell._last_tick = 10.0
        states = ["no_device", "connected"]

        def refresh_state(*, background: bool = False, force_probe: bool = False, allow_probe: bool = True):
            del background, allow_probe
            shell.device_service.state.connection_state = (
                "connected" if force_probe else states.pop(0)
            )
            return shell.device_service.state

        shell.device_service.refresh_state.side_effect = refresh_state

        with patch("zd_app.ui.app_shell.time.time", return_value=10.0):
            shell._tick()
        self.assertEqual(shell._last_connection_state, "no_device")
        self.assertFalse(shell._needs_hydration)

        shell._last_tick = 13.0
        with patch("zd_app.ui.app_shell.time.time", return_value=13.0):
            shell._tick()

        shell.device_service.refresh_state.assert_any_call(
            background=False,
            force_probe=True,
        )
        self.assertTrue(shell._needs_hydration)
        settings_service.stop.assert_called_once_with()
        shell.device_service.log_event.assert_any_call(
            "Controller reconnected; refreshing Wrapper Settings."
        )
        self.assertEqual(shell._last_connection_state, "connected")

    def test_tick_steady_connected_no_rehydrate(self) -> None:
        shell = _make_shell(MagicMock())
        shell.settings.auto_read_on_connect = False
        shell.device_service.state.connection_state = "connected"
        shell._last_connection_state = "connected"
        shell._last_tick = 10.0

        def refresh_state(*, background: bool = False, force_probe: bool = False, allow_probe: bool = True):
            del background, force_probe, allow_probe
            shell.device_service.state.connection_state = "connected"
            return shell.device_service.state

        shell.device_service.refresh_state.side_effect = refresh_state

        with patch("zd_app.ui.app_shell.time.time", return_value=10.0):
            shell._tick()
        shell._last_tick = 13.0
        with patch("zd_app.ui.app_shell.time.time", return_value=13.0):
            shell._tick()

        force_probe_calls = [
            call
            for call in shell.device_service.refresh_state.call_args_list
            if call.kwargs.get("force_probe") is True
        ]
        self.assertEqual(force_probe_calls, [])
        self.assertFalse(shell._needs_hydration)
        shell.settings_service.stop.assert_not_called()
        self.assertEqual(shell._last_connection_state, "connected")

    def test_tick_present_to_absent_transition_no_rehydrate(self) -> None:
        shell = _make_shell(MagicMock())
        shell.settings.auto_read_on_connect = False
        shell.device_service.state.connection_state = "connected"
        shell._last_connection_state = "connected"
        shell._last_tick = 10.0
        states = ["connected", "no_device"]

        def refresh_state(*, background: bool = False, force_probe: bool = False, allow_probe: bool = True):
            del background, force_probe, allow_probe
            shell.device_service.state.connection_state = states.pop(0)
            return shell.device_service.state

        shell.device_service.refresh_state.side_effect = refresh_state

        with patch("zd_app.ui.app_shell.time.time", return_value=10.0):
            shell._tick()
        shell._last_tick = 13.0
        with patch("zd_app.ui.app_shell.time.time", return_value=13.0):
            shell._tick()

        force_probe_calls = [
            call
            for call in shell.device_service.refresh_state.call_args_list
            if call.kwargs.get("force_probe") is True
        ]
        self.assertEqual(force_probe_calls, [])
        self.assertFalse(shell._needs_hydration)
        shell.settings_service.stop.assert_not_called()
        self.assertEqual(shell._last_connection_state, "no_device")

    def test_on_binding_source_changed_updates_target_combo(self) -> None:
        shell = _make_shell(MagicMock())
        shell.last_controller_snapshot = _full_snapshot(
            button_bindings={
                ButtonSlot.A: _controller_button_mapping(ControllerButtonTarget.B),
            },
        )

        values = _run_with_widget_capture(lambda: shell.on_binding_source_changed("A"))

        self.assertEqual(values["binding_target_combo"], "B")

    def test_on_binding_source_changed_unknown_slot_no_widget_change(self) -> None:
        shell = _make_shell(MagicMock())
        shell.last_controller_snapshot = _full_snapshot()

        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True), patch(
            "zd_app.ui.app_shell.dpg.set_value",
        ) as set_value:
            shell.on_binding_source_changed("Guide")

        set_value.assert_not_called()

    def test_on_lighting_zone_changed_updates_zone_widgets(self) -> None:
        shell = _make_shell(MagicMock())
        shell.last_controller_snapshot = _full_snapshot()

        values = _run_with_widget_capture(lambda: shell.on_lighting_zone_changed("Right"))

        self.assertIs(values["lighting_on_checkbox"], True)
        self.assertEqual(values["lighting_mode_combo"], "Fade")
        self.assertEqual(values["lighting_brightness_slider"], 200)
        self.assertEqual(values["lighting_r_slider"], 7)
        self.assertEqual(values["lighting_g_slider"], 8)
        self.assertEqual(values["lighting_b_slider"], 9)

    def test_on_lighting_zone_changed_unknown_zone_no_widget_change(self) -> None:
        shell = _make_shell(MagicMock())
        shell.last_controller_snapshot = _full_snapshot()

        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True), patch(
            "zd_app.ui.app_shell.dpg.set_value",
        ) as set_value:
            shell.on_lighting_zone_changed("Global")

        set_value.assert_not_called()

    def test_set_widget_no_op_if_tag_missing(self) -> None:
        shell = _make_shell(MagicMock())

        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=False), patch(
            "zd_app.ui.app_shell.dpg.set_value",
        ) as set_value:
            shell._set_widget("missing_widget", 123)

        set_value.assert_not_called()

    def test_apply_polling_rate_routes_label_to_settings_service(self) -> None:
        settings_service = MagicMock()
        result = SimpleNamespace(
            outcome=SetPollingRateOutcome.OK,
            error_code=None,
        )
        settings_service.set_polling_rate.return_value = result
        shell = _make_shell(settings_service)
        shell._polling_rate_hydrated = True

        returned = shell.apply_polling_rate("1000Hz")

        self.assertIs(returned, result)
        settings_service.set_polling_rate.assert_called_once_with(PollingRate.HZ_1000)
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Polling rate 1000Hz applied.",
        )

    def test_apply_polling_rate_logs_once_with_real_device_service(self) -> None:
        settings_service = MagicMock()
        settings_service.set_polling_rate.return_value = SimpleNamespace(
            outcome=SetPollingRateOutcome.OK,
            error_code=None,
        )
        shell = _make_shell(settings_service)
        shell.device_service = DeviceService(clock=lambda: 0.0)
        shell._polling_rate_hydrated = True

        shell.apply_polling_rate("1000Hz")

        matching = [
            event
            for event in shell.device_service.recent_events(10)
            if "Polling rate 1000Hz" in event
        ]
        self.assertEqual(len(matching), 1)
        self.assertTrue(matching[0].endswith("OK: Polling rate 1000Hz applied."))

    def test_apply_polling_rate_log_retranslates_on_locale_switch(self) -> None:
        try:
            i18n.set_locale("en")
            settings_service = MagicMock()
            settings_service.set_polling_rate.return_value = SimpleNamespace(
                outcome=SetPollingRateOutcome.OK,
                error_code=None,
            )
            shell = _make_shell(settings_service)
            shell.device_service = DeviceService(clock=lambda: 0.0)
            shell._polling_rate_hydrated = True

            shell.apply_polling_rate("1000Hz")

            self.assertTrue(
                shell.device_service.recent_events(1)[0].endswith(
                    "OK: Polling rate 1000Hz applied."
                )
            )
            i18n.set_locale("zh-CN")
            self.assertIn("轮询率 1000Hz", shell.device_service.recent_events(1)[0])
        finally:
            i18n.set_locale("en")

    def test_record_apply_result_accepts_log_entry(self) -> None:
        try:
            i18n.set_locale("en")
            shell = _make_shell(MagicMock())
            shell.device_service = DeviceService(clock=lambda: 0.0)
            entry = LogEntry(
                timestamp="12:00:00",
                key="apply.polling_rate.success",
                fmt_args={"label": "1000Hz"},
            )

            shell._record_settings_apply_result(True, entry)

            self.assertEqual(
                shell.device_service.recent_events(1)[0],
                "12:00:00  OK: Polling rate 1000Hz applied.",
            )
            i18n.set_locale("zh-CN")
            self.assertIn("轮询率 1000Hz", shell.device_service.recent_events(1)[0])
        finally:
            i18n.set_locale("en")

    def test_record_apply_result_backward_compat_with_raw_string(self) -> None:
        try:
            shell = _make_shell(MagicMock())
            shell.device_service = DeviceService(clock=lambda: 0.0)

            shell._record_settings_apply_result(True, "Legacy apply result.")
            i18n.set_locale("zh-CN")

            self.assertTrue(
                shell.device_service.recent_events(1)[0].endswith("Legacy apply result.")
            )
        finally:
            i18n.set_locale("en")

    def test_apply_polling_rate_invalid_label_raises(self) -> None:
        settings_service = MagicMock()
        shell = _make_shell(settings_service)

        with self.assertRaises(ValueError):
            shell.apply_polling_rate("960Hz")

        settings_service.set_polling_rate.assert_not_called()

    def test_apply_polling_rate_without_settings_service_logs_failure(self) -> None:
        shell = _make_shell(None)

        result = shell.apply_polling_rate("1000Hz")

        self.assertIsNone(result)
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Polling rate not applied: settings service unavailable.",
        )
        shell.device_service.log_event.assert_not_called()

    def test_apply_messages_do_not_expose_settingsservice_jargon(self) -> None:
        shell = _make_shell(None)

        shell.apply_polling_rate("1000Hz")

        message = shell.device_service.record_apply_result.call_args.args[1]
        self.assertNotIn("SettingsService", message)
        self.assertIn("settings service unavailable", message)

    def test_apply_vibration_settings_reads_controls_and_routes_to_service(self) -> None:
        settings_service = MagicMock()
        result = SimpleNamespace(
            outcome=SetVibrationOutcome.OK,
            error_code=None,
            payload_hex="001055aa510c001e32465a01" + ("00" * 53),
        )
        settings_service.set_vibration.return_value = result
        shell = _make_shell(settings_service)
        values = {
            "vibration_lg_slider": 30,
            "vibration_rg_slider": 50,
            "vibration_lm_slider": 70,
            "vibration_rm_slider": 90,
            "vibration_mode_combo": "Stereo Resonance",
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            returned = shell.apply_vibration_settings()

        expected = VibrationSettings(
            left_grip_strength=30,
            right_grip_strength=50,
            left_trigger_motor_strength=70,
            right_trigger_motor_strength=90,
            mode=TriggerVibrationMode.STEREO_RESONANCE,
        )
        self.assertIs(returned, result)
        settings_service.set_vibration.assert_called_once_with(expected)
        self.assertEqual(build_vibration_payload(expected).hex(), result.payload_hex)
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Vibration settings applied.",
        )

    def test_apply_vibration_settings_maps_all_mode_labels(self) -> None:
        cases = [
            ("Native Trigger Vibration", TriggerVibrationMode.NATIVE),
            ("Stereo Resonance", TriggerVibrationMode.STEREO_RESONANCE),
            ("Trigger Vibration", TriggerVibrationMode.TRIGGER_VIBRATION),
        ]
        for label, expected_mode in cases:
            with self.subTest(label=label):
                settings_service = MagicMock()
                settings_service.set_vibration.return_value = SimpleNamespace(
                    outcome=SetVibrationOutcome.OK,
                    error_code=None,
                )
                shell = _make_shell(settings_service)
                values = {
                    "vibration_lg_slider": 15,
                    "vibration_rg_slider": 15,
                    "vibration_lm_slider": 15,
                    "vibration_rm_slider": 15,
                    "vibration_mode_combo": label,
                }

                with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
                    shell.apply_vibration_settings()

                called_settings = settings_service.set_vibration.call_args.args[0]
                self.assertEqual(called_settings.mode, expected_mode)

    def test_apply_vibration_settings_unknown_mode_logs_and_returns_early(self) -> None:
        settings_service = MagicMock()
        shell = _make_shell(settings_service)
        values = {
            "vibration_lg_slider": 15,
            "vibration_rg_slider": 15,
            "vibration_lm_slider": 15,
            "vibration_rm_slider": 15,
            "vibration_mode_combo": "Boosted Resonance",
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            with self.assertLogs("zd_app.ui.app_shell", level="WARNING") as logs:
                result = shell.apply_vibration_settings()

        self.assertIsNone(result)
        self.assertIn("unknown vibration mode 'Boosted Resonance'", logs.output[0])
        settings_service.set_vibration.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Apply failed: unknown vibration mode 'Boosted Resonance'; vibration settings not applied.",
        )

    def test_apply_vibration_settings_without_settings_service_returns_early(self) -> None:
        shell = _make_shell(None)

        with patch("zd_app.ui.app_shell.dpg.get_value") as get_value:
            result = shell.apply_vibration_settings()

        self.assertIsNone(result)
        get_value.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Vibration settings not applied: settings service unavailable.",
        )
        shell.device_service.log_event.assert_not_called()

    def test_apply_vibration_updates_status_strip_on_success(self) -> None:
        settings_service = MagicMock()
        settings_service.set_vibration.return_value = SimpleNamespace(
            outcome=SetVibrationOutcome.OK,
            error_code=None,
        )
        shell = _make_shell(settings_service)
        shell._dpg_context_ready = True
        values = {
            "vibration_lg_slider": 15,
            "vibration_rg_slider": 15,
            "vibration_lm_slider": 15,
            "vibration_rm_slider": 15,
            "vibration_mode_combo": "Native Trigger Vibration",
        }
        rendered = {}

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__), patch(
            "zd_app.ui.app_shell.dpg.does_item_exist",
            return_value=True,
        ), patch(
            "zd_app.ui.app_shell.dpg.set_value",
            side_effect=lambda tag, value: rendered.__setitem__(tag, value),
        ):
            shell.apply_vibration_settings()

        self.assertEqual(
            rendered["settings_v2_status_text"],
            "OK: Vibration settings applied.",
        )
        self.assertIsNotNone(shell._apply_status_clear_after)

    def test_apply_vibration_updates_status_strip_on_failure(self) -> None:
        settings_service = MagicMock()
        settings_service.set_vibration.return_value = SimpleNamespace(
            outcome=SetVibrationOutcome.WRITE_FAILED,
            error_code=995,
        )
        shell = _make_shell(settings_service)
        shell._dpg_context_ready = True
        values = {
            "vibration_lg_slider": 15,
            "vibration_rg_slider": 15,
            "vibration_lm_slider": 15,
            "vibration_rm_slider": 15,
            "vibration_mode_combo": "Native Trigger Vibration",
        }
        rendered = {}

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__), patch(
            "zd_app.ui.app_shell.dpg.does_item_exist",
            return_value=True,
        ), patch(
            "zd_app.ui.app_shell.dpg.set_value",
            side_effect=lambda tag, value: rendered.__setitem__(tag, value),
        ):
            shell.apply_vibration_settings()

        self.assertEqual(
            rendered["settings_v2_status_text"],
            "Apply failed: vibration settings: HID write failed after retry (error 995)",
        )
        self.assertIsNotNone(shell._apply_status_clear_after)

    def test_apply_status_auto_clears_after_timeout(self) -> None:
        shell = _make_shell(MagicMock())
        shell.last_snapshot_status = "Read OK"
        shell.last_snapshot_ts = 100.0
        shell._apply_status_clear_after = 5.0
        rendered = {}

        with patch(
            "zd_app.ui.app_shell.dpg.does_item_exist",
            return_value=True,
        ), patch(
            "zd_app.ui.app_shell.dpg.set_value",
            side_effect=lambda tag, value: rendered.__setitem__(tag, value),
        ):
            shell._tick_settings_service_tasks(6.0)

        self.assertIsNone(shell._apply_status_clear_after)
        self.assertIn("Read OK (read at", rendered["settings_v2_status_text"])

    def test_render_settings_snapshot_status_skips_when_apply_status_active(self) -> None:
        shell = _make_shell(MagicMock())
        shell.last_snapshot_status = "Read OK"
        shell.last_snapshot_ts = 100.0
        shell._apply_status_clear_after = 200.0

        with patch("zd_app.ui.app_shell.time.time", return_value=150.0), patch(
            "zd_app.ui.app_shell.dpg.does_item_exist",
            return_value=True,
        ) as does_item_exist, patch(
            "zd_app.ui.app_shell.dpg.set_value",
        ) as set_value:
            shell._render_settings_snapshot_status()

        does_item_exist.assert_not_called()
        set_value.assert_not_called()

    def test_render_settings_snapshot_status_replays_apply_status_when_active(self) -> None:
        shell = _make_shell(MagicMock())
        shell.last_snapshot_status = "Read OK"
        shell.last_snapshot_ts = 100.0
        shell._apply_status_text = "OK: Vibration settings applied."
        shell._apply_status_clear_after = 200.0
        rendered = {}

        with patch("zd_app.ui.app_shell.time.time", return_value=150.0), patch(
            "zd_app.ui.app_shell.dpg.does_item_exist",
            return_value=True,
        ), patch(
            "zd_app.ui.app_shell.dpg.set_value",
            side_effect=lambda tag, value: rendered.__setitem__(tag, value),
        ):
            shell._render_settings_snapshot_status()

        self.assertEqual(
            rendered["settings_v2_status_text"],
            "OK: Vibration settings applied.",
        )

    def test_render_settings_snapshot_status_proceeds_when_apply_timer_expired(self) -> None:
        shell = _make_shell(MagicMock())
        shell.last_snapshot_status = "Read OK"
        shell.last_snapshot_ts = 100.0
        shell._apply_status_clear_after = 100.0
        rendered = {}

        with patch("zd_app.ui.app_shell.time.time", return_value=150.0), patch(
            "zd_app.ui.app_shell.dpg.does_item_exist",
            return_value=True,
        ), patch(
            "zd_app.ui.app_shell.dpg.set_value",
            side_effect=lambda tag, value: rendered.__setitem__(tag, value),
        ):
            shell._render_settings_snapshot_status()

        self.assertIn("Read OK (read at", rendered["settings_v2_status_text"])

    def test_apply_left_trigger_writes_via_settings_service(self) -> None:
        settings_service = MagicMock()
        expected = ServiceTriggerSettings(
            range_min=10,
            range_max=80,
            mode=TriggerMode.LONG,
        )
        result = SimpleNamespace(
            outcome=SetTriggerSettingsOutcome.OK,
            error_code=None,
            payload_hex=build_trigger_settings_payload(TRIGGER_SELECTOR_LEFT, expected).hex(),
        )
        settings_service.set_left_trigger_settings.return_value = result
        shell = _make_shell(settings_service)
        values = {
            "trigger_left_min_slider": 10,
            "trigger_left_max_slider": 80,
            "trigger_left_mode_combo": "Long",
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            returned = shell.apply_left_trigger_settings()

        self.assertIs(returned, result)
        settings_service.set_left_trigger_settings.assert_called_once_with(expected)
        settings_service.set_right_trigger_settings.assert_not_called()
        self.assertEqual(
            result.payload_hex,
            "001055aa510a00000a5000" + ("00" * 54),
        )
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Left trigger settings applied.",
        )

    def test_apply_right_trigger_writes_via_settings_service(self) -> None:
        settings_service = MagicMock()
        expected = ServiceTriggerSettings(
            range_min=20,
            range_max=90,
            mode=TriggerMode.SHORT,
        )
        result = SimpleNamespace(
            outcome=SetTriggerSettingsOutcome.OK,
            error_code=None,
            payload_hex=build_trigger_settings_payload(TRIGGER_SELECTOR_RIGHT, expected).hex(),
        )
        settings_service.set_right_trigger_settings.return_value = result
        shell = _make_shell(settings_service)
        values = {
            "trigger_right_min_slider": 20,
            "trigger_right_max_slider": 90,
            "trigger_right_mode_combo": "Short",
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            returned = shell.apply_right_trigger_settings()

        self.assertIs(returned, result)
        settings_service.set_right_trigger_settings.assert_called_once_with(expected)
        settings_service.set_left_trigger_settings.assert_not_called()
        self.assertEqual(
            result.payload_hex,
            "001055aa510a0001145a01" + ("00" * 54),
        )
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Right trigger settings applied.",
        )

    def test_apply_trigger_unknown_mode_no_write(self) -> None:
        settings_service = MagicMock()
        shell = _make_shell(settings_service)
        values = {
            "trigger_left_mode_combo": "Hair",
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            with self.assertLogs("zd_app.ui.app_shell", level="WARNING") as logs:
                result = shell.apply_left_trigger_settings()

        self.assertIsNone(result)
        self.assertIn("unknown trigger mode 'Hair'", logs.output[0])
        settings_service.set_left_trigger_settings.assert_not_called()
        settings_service.set_right_trigger_settings.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Apply failed: unknown trigger mode 'Hair'; Left trigger settings not applied.",
        )

    def test_apply_trigger_min_gt_max_no_write(self) -> None:
        settings_service = MagicMock()
        shell = _make_shell(settings_service)
        values = {
            "trigger_right_mode_combo": "Short",
            "trigger_right_min_slider": 80,
            "trigger_right_max_slider": 10,
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            with self.assertLogs("zd_app.ui.app_shell", level="WARNING") as logs:
                result = shell.apply_right_trigger_settings()

        self.assertIsNone(result)
        self.assertIn("trigger min (80) > max (10)", logs.output[0])
        settings_service.set_left_trigger_settings.assert_not_called()
        settings_service.set_right_trigger_settings.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Apply failed: trigger min (80) > max (10); Right trigger settings not applied.",
        )

    def test_apply_trigger_settings_service_none_no_write(self) -> None:
        shell = _make_shell(None)

        with patch("zd_app.ui.app_shell.dpg.get_value") as get_value:
            result = shell.apply_left_trigger_settings()

        self.assertIsNone(result)
        get_value.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Left trigger settings not applied: settings service unavailable.",
        )
        shell.device_service.log_event.assert_not_called()

    def test_apply_deadzone_writes_via_settings_service(self) -> None:
        settings_service = MagicMock()
        expected = StickDeadzones(
            left_center=5,
            right_center=10,
            left_outer=3,
            right_outer=7,
        )
        result = SimpleNamespace(
            outcome=SetDeadzoneOutcome.OK,
            error_code=None,
            payload_hex=build_all_deadzones_payload(expected).hex(),
        )
        settings_service.set_all_deadzones.return_value = result
        shell = _make_shell(settings_service)
        values = {
            "deadzone_left_center_slider": 5,
            "deadzone_right_center_slider": 10,
            "deadzone_left_outer_slider": 3,
            "deadzone_right_outer_slider": 7,
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            returned = shell.apply_deadzone_settings()

        self.assertIs(returned, result)
        settings_service.set_all_deadzones.assert_called_once_with(expected)
        self.assertEqual(
            result.payload_hex,
            "001055aa510900050a0307" + ("00" * 54),
        )
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Deadzone settings applied.",
        )

    def test_apply_deadzone_settings_service_none_no_write(self) -> None:
        shell = _make_shell(None)

        with patch("zd_app.ui.app_shell.dpg.get_value") as get_value:
            result = shell.apply_deadzone_settings()

        self.assertIsNone(result)
        get_value.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Deadzone settings not applied: settings service unavailable.",
        )
        shell.device_service.log_event.assert_not_called()

    def test_apply_left_sensitivity_writes_via_settings_service(self) -> None:
        settings_service = MagicMock()
        expected = (
            SensitivityAnchor(0, 0),
            SensitivityAnchor(40, 30),
            SensitivityAnchor(100, 100),
        )
        result = SimpleNamespace(
            outcome=SetSensitivityCurveOutcome.OK,
            error_code=None,
            payload_hex=build_left_stick_sensitivity_curve_payload(expected).hex(),
        )
        settings_service.set_left_stick_sensitivity_curve.return_value = result
        shell = _make_shell(settings_service)
        values = {
            "sensitivity_left_a1x_slider": 0,
            "sensitivity_left_a1y_slider": 0,
            "sensitivity_left_a2x_slider": 40,
            "sensitivity_left_a2y_slider": 30,
            "sensitivity_left_a3x_slider": 100,
            "sensitivity_left_a3y_slider": 100,
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            returned = shell.apply_left_sensitivity_curve()

        self.assertIs(returned, result)
        settings_service.set_left_stick_sensitivity_curve.assert_called_once_with(expected)
        settings_service.set_right_stick_sensitivity_curve.assert_not_called()
        self.assertEqual(
            result.payload_hex,
            "001055aa510600000000281e6464" + ("00" * 51),
        )
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Left sensitivity curve applied.",
        )

    def test_apply_right_sensitivity_writes_via_settings_service(self) -> None:
        settings_service = MagicMock()
        expected = (
            SensitivityAnchor(10, 5),
            SensitivityAnchor(50, 60),
            SensitivityAnchor(100, 100),
        )
        result = SimpleNamespace(
            outcome=SetSensitivityCurveOutcome.OK,
            error_code=None,
            payload_hex=build_right_stick_sensitivity_curve_payload(expected).hex(),
        )
        settings_service.set_right_stick_sensitivity_curve.return_value = result
        shell = _make_shell(settings_service)
        values = {
            "sensitivity_right_a1x_slider": 10,
            "sensitivity_right_a1y_slider": 5,
            "sensitivity_right_a2x_slider": 50,
            "sensitivity_right_a2y_slider": 60,
            "sensitivity_right_a3x_slider": 100,
            "sensitivity_right_a3y_slider": 100,
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            returned = shell.apply_right_sensitivity_curve()

        self.assertIs(returned, result)
        settings_service.set_right_stick_sensitivity_curve.assert_called_once_with(expected)
        settings_service.set_left_stick_sensitivity_curve.assert_not_called()
        self.assertEqual(
            result.payload_hex,
            "001055aa510600010a05323c6464" + ("00" * 51),
        )
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Right sensitivity curve applied.",
        )

    def test_apply_left_sensitivity_8point_writes_via_settings_service(self) -> None:
        settings_service = MagicMock()
        # 8 monotonic anchors: (0,5),(12,17),...,(84,89).
        expected = tuple(
            SensitivityAnchor(n * 12, n * 12 + 5) for n in range(8)
        )
        result = SimpleNamespace(
            outcome=SetSensitivityCurveOutcome.OK,
            error_code=None,
        )
        settings_service.set_left_stick_sensitivity_curve_8point.return_value = result
        shell = _make_shell(settings_service)
        values = {}
        for index in range(1, 9):
            values[f"sensitivity_left_a{index}x_slider_8point"] = (index - 1) * 12
            values[f"sensitivity_left_a{index}y_slider_8point"] = (index - 1) * 12 + 5

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            returned = shell.apply_left_sensitivity_curve_8point()

        self.assertIs(returned, result)
        settings_service.set_left_stick_sensitivity_curve_8point.assert_called_once_with(expected)
        settings_service.set_right_stick_sensitivity_curve_8point.assert_not_called()
        # The 8-point apply must NOT touch the legacy 3-point writer.
        settings_service.set_left_stick_sensitivity_curve.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Left sensitivity curve (8-pt) applied.",
        )

    def test_apply_right_sensitivity_8point_writes_via_settings_service(self) -> None:
        settings_service = MagicMock()
        expected = tuple(
            SensitivityAnchor(n * 10 + 1, n * 10 + 2) for n in range(8)
        )
        result = SimpleNamespace(
            outcome=SetSensitivityCurveOutcome.OK,
            error_code=None,
        )
        settings_service.set_right_stick_sensitivity_curve_8point.return_value = result
        shell = _make_shell(settings_service)
        values = {}
        for index in range(1, 9):
            values[f"sensitivity_right_a{index}x_slider_8point"] = (index - 1) * 10 + 1
            values[f"sensitivity_right_a{index}y_slider_8point"] = (index - 1) * 10 + 2

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            returned = shell.apply_right_sensitivity_curve_8point()

        self.assertIs(returned, result)
        settings_service.set_right_stick_sensitivity_curve_8point.assert_called_once_with(expected)
        settings_service.set_left_stick_sensitivity_curve_8point.assert_not_called()
        settings_service.set_right_stick_sensitivity_curve.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Right sensitivity curve (8-pt) applied.",
        )

    def test_apply_sensitivity_8point_failure_surfaces_log_entry(self) -> None:
        # Service-layer monotonic validation (or any write failure) comes back as
        # a non-OK outcome; the shell surfaces it as a failed apply log entry
        # rather than pre-validating client-side (set spec, choice #4).
        settings_service = MagicMock()
        result = SimpleNamespace(
            outcome=SetSensitivityCurveOutcome.WRITE_FAILED,
            error_code=5,
        )
        settings_service.set_left_stick_sensitivity_curve_8point.return_value = result
        shell = _make_shell(settings_service)
        values = {}
        for index in range(1, 9):
            values[f"sensitivity_left_a{index}x_slider_8point"] = 0
            values[f"sensitivity_left_a{index}y_slider_8point"] = 0

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            returned = shell.apply_left_sensitivity_curve_8point()

        self.assertIs(returned, result)
        args = shell.device_service.record_apply_result.call_args[0]
        self.assertFalse(args[0])
        self.assertIn("Left sensitivity curve (8-pt)", args[1])

    def test_apply_sensitivity_8point_settings_service_none_no_write(self) -> None:
        for side in ("left", "right"):
            with self.subTest(side=side):
                shell = _make_shell(None)
                method = getattr(shell, f"apply_{side}_sensitivity_curve_8point")

                with patch("zd_app.ui.app_shell.dpg.get_value") as get_value:
                    result = method()

                self.assertIsNone(result)
                get_value.assert_not_called()
                shell.device_service.record_apply_result.assert_called_once_with(
                    False,
                    f"{side.title()} sensitivity not applied: settings service unavailable.",
                )

    def test_sensitivity_8point_presets_validate_and_are_monotonic(self) -> None:
        # Every bundled 8-point preset must satisfy the service-layer apply gate:
        # exactly 8 anchors, each axis in 0-100, non-decreasing in both X and Y,
        # on the documented standard X sample grid. If any fails, the preset
        # button would surface a failed-apply log instead of writing the curve.
        from zd_app.services.settings_service import _validate_sensitivity_anchors_8point

        self.assertEqual(
            set(SENSITIVITY_PRESETS_8POINT),
            {"Linear", "Aggressive", "Smooth", "Balanced"},
        )
        for name, anchors in SENSITIVITY_PRESETS_8POINT.items():
            with self.subTest(preset=name):
                self.assertEqual(len(anchors), 8)
                self.assertEqual(
                    tuple(a.x for a in anchors), (0, 14, 28, 42, 57, 71, 85, 100)
                )
                for index, anchor in enumerate(anchors):
                    self.assertGreaterEqual(anchor.x, 0)
                    self.assertLessEqual(anchor.x, 100)
                    self.assertGreaterEqual(anchor.y, 0)
                    self.assertLessEqual(anchor.y, 100)
                    if index > 0:
                        self.assertGreaterEqual(anchor.x, anchors[index - 1].x)
                        self.assertGreaterEqual(anchor.y, anchors[index - 1].y)
                # The real apply gate accepts it unchanged.
                self.assertEqual(_validate_sensitivity_anchors_8point(anchors), anchors)

    def test_apply_left_sensitivity_preset_8point_sets_sliders_and_applies(self) -> None:
        settings_service = MagicMock()
        result = SimpleNamespace(
            outcome=SetSensitivityCurveOutcome.OK,
            error_code=None,
        )
        settings_service.set_left_stick_sensitivity_curve_8point.return_value = result
        shell = _make_shell(settings_service)
        expected = SENSITIVITY_PRESETS_8POINT["Aggressive"]

        # Real DPG widgets aren't created here; the preset writes sliders via
        # _set_widget (guarded by does_item_exist) and the apply re-reads them.
        # Back both with a dict so the round-trip is faithful.
        values: dict = {}
        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True), \
             patch("zd_app.ui.app_shell.dpg.set_value", side_effect=values.__setitem__), \
             patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            returned = shell.apply_left_sensitivity_preset_8point("Aggressive")

        self.assertIs(returned, result)
        # The 16 sliders now hold the preset's 8 anchors.
        for index, anchor in enumerate(expected, start=1):
            self.assertEqual(values[f"sensitivity_left_a{index}x_slider_8point"], anchor.x)
            self.assertEqual(values[f"sensitivity_left_a{index}y_slider_8point"], anchor.y)
        # Apply dispatched exactly those anchors via the 8-point writer only.
        settings_service.set_left_stick_sensitivity_curve_8point.assert_called_once_with(expected)
        settings_service.set_right_stick_sensitivity_curve_8point.assert_not_called()
        settings_service.set_left_stick_sensitivity_curve.assert_not_called()

    def test_apply_right_sensitivity_preset_8point_sets_sliders_and_applies(self) -> None:
        settings_service = MagicMock()
        result = SimpleNamespace(
            outcome=SetSensitivityCurveOutcome.OK,
            error_code=None,
        )
        settings_service.set_right_stick_sensitivity_curve_8point.return_value = result
        shell = _make_shell(settings_service)
        expected = SENSITIVITY_PRESETS_8POINT["Smooth"]

        values: dict = {}
        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True), \
             patch("zd_app.ui.app_shell.dpg.set_value", side_effect=values.__setitem__), \
             patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            returned = shell.apply_right_sensitivity_preset_8point("Smooth")

        self.assertIs(returned, result)
        for index, anchor in enumerate(expected, start=1):
            self.assertEqual(values[f"sensitivity_right_a{index}x_slider_8point"], anchor.x)
            self.assertEqual(values[f"sensitivity_right_a{index}y_slider_8point"], anchor.y)
        settings_service.set_right_stick_sensitivity_curve_8point.assert_called_once_with(expected)
        settings_service.set_left_stick_sensitivity_curve_8point.assert_not_called()
        settings_service.set_right_stick_sensitivity_curve.assert_not_called()

    def test_apply_sensitivity_preset_8point_unknown_name_no_write(self) -> None:
        settings_service = MagicMock()
        shell = _make_shell(settings_service)
        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True), \
             patch("zd_app.ui.app_shell.dpg.set_value") as set_value, \
             patch("zd_app.ui.app_shell.dpg.get_value"):
            returned = shell.apply_left_sensitivity_preset_8point("Nonexistent")

        self.assertIsNone(returned)
        set_value.assert_not_called()
        settings_service.set_left_stick_sensitivity_curve_8point.assert_not_called()
        args = shell.device_service.record_apply_result.call_args[0]
        self.assertFalse(args[0])

    def test_sensitivity_8point_edit_exact_entry_sets_anchor(self) -> None:
        # Polish #2: the live-edit funnel (driven by the exact-entry input or the
        # slider) sets the edited anchor on BOTH widget twins and repaints the
        # curve series. No service call — this is a live edit, not an Apply.
        shell = _make_shell(MagicMock())
        values = {}
        for index in range(1, 9):
            values[f"sensitivity_left_a{index}x_slider_8point"] = 0
            values[f"sensitivity_left_a{index}y_slider_8point"] = 0

        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True), \
             patch("zd_app.ui.app_shell.dpg.set_value", side_effect=values.__setitem__), \
             patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            shell._on_sensitivity_8point_edit("left", 3, "x", 35)

        # Both twins for anchor 3 X now hold the typed value.
        self.assertEqual(values["sensitivity_left_a3x_slider_8point"], 35)
        self.assertEqual(values["sensitivity_left_a3x_input_8point"], 35)
        # The painted line series reflects it (X sequence index 2 == 35).
        series = values["sensitivity_left_plot_series_8point"]
        self.assertEqual(series[0][2], 35)

    def test_sensitivity_8point_edit_monotonic_assist_pushes_forward(self) -> None:
        # Polish #3: raising a middle anchor's X above its successors pushes them
        # up so the axis stays non-decreasing (the vendor's "drag pushes the
        # subsequent points").
        from zd_app.services.settings_service import _validate_sensitivity_anchors_8point

        shell = _make_shell(MagicMock())
        seed = [0, 14, 28, 42, 57, 71, 85, 100]
        values = {}
        for index in range(1, 9):
            values[f"sensitivity_left_a{index}x_slider_8point"] = seed[index - 1]
            values[f"sensitivity_left_a{index}y_slider_8point"] = seed[index - 1]

        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True), \
             patch("zd_app.ui.app_shell.dpg.set_value", side_effect=values.__setitem__), \
             patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            shell._on_sensitivity_8point_edit("left", 2, "x", 60)

        result = [values[f"sensitivity_left_a{i}x_slider_8point"] for i in range(1, 9)]
        self.assertEqual(values["sensitivity_left_a2x_slider_8point"], 60)
        # Anchors 3-5 (28/42/57) pushed up to 60; 6-8 already >= 60, untouched.
        self.assertEqual(result, [0, 60, 60, 60, 60, 71, 85, 100])
        self.assertTrue(all(result[i] >= result[i - 1] for i in range(1, 8)))
        # The input twins mirror the assisted sliders, anchor for anchor.
        for i in range(1, 9):
            self.assertEqual(
                values[f"sensitivity_left_a{i}x_input_8point"], result[i - 1]
            )
        # The assisted curve passes the real service-layer backstop unchanged.
        anchors = tuple(SensitivityAnchor(result[i], seed[i]) for i in range(8))
        self.assertEqual(_validate_sensitivity_anchors_8point(anchors), anchors)

    def test_sensitivity_8point_edit_monotonic_assist_pushes_backward(self) -> None:
        # Polish #3: dropping a later anchor's X below its predecessors pushes
        # them DOWN — the decrease case the vendor's forward-only push misses, so
        # the user still can't strand a higher earlier anchor.
        from zd_app.services.settings_service import _validate_sensitivity_anchors_8point

        shell = _make_shell(MagicMock())
        seed = [0, 14, 28, 42, 57, 71, 85, 100]
        values = {}
        for index in range(1, 9):
            values[f"sensitivity_left_a{index}x_slider_8point"] = seed[index - 1]
            values[f"sensitivity_left_a{index}y_slider_8point"] = seed[index - 1]

        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True), \
             patch("zd_app.ui.app_shell.dpg.set_value", side_effect=values.__setitem__), \
             patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            shell._on_sensitivity_8point_edit("left", 6, "x", 20)

        result = [values[f"sensitivity_left_a{i}x_slider_8point"] for i in range(1, 9)]
        self.assertEqual(values["sensitivity_left_a6x_slider_8point"], 20)
        # Anchors 3-5 (28/42/57) pushed down to 20; 1-2 already <= 20, untouched.
        self.assertEqual(result, [0, 14, 20, 20, 20, 20, 85, 100])
        self.assertTrue(all(result[i] >= result[i - 1] for i in range(1, 8)))
        anchors = tuple(SensitivityAnchor(result[i], seed[i]) for i in range(8))
        self.assertEqual(_validate_sensitivity_anchors_8point(anchors), anchors)

    def test_sensitivity_8point_edit_clamps_out_of_range(self) -> None:
        # The funnel clamps to 0-100 even if a raw value slips past the input's
        # own clamp — a second guard ahead of the service validator.
        shell = _make_shell(MagicMock())
        values = {}
        for index in range(1, 9):
            values[f"sensitivity_left_a{index}x_slider_8point"] = 0
            values[f"sensitivity_left_a{index}y_slider_8point"] = 0

        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True), \
             patch("zd_app.ui.app_shell.dpg.set_value", side_effect=values.__setitem__), \
             patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            shell._on_sensitivity_8point_edit("left", 4, "y", 250)

        self.assertEqual(values["sensitivity_left_a4y_slider_8point"], 100)
        self.assertEqual(values["sensitivity_left_a4y_input_8point"], 100)

    def test_sensitivity_8point_edit_noop_when_widgets_absent(self) -> None:
        # No 8-point editor on screen (3-point device / no DPG context): the
        # funnel must no-op without writing anything.
        shell = _make_shell(MagicMock())
        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=False), \
             patch("zd_app.ui.app_shell.dpg.set_value") as set_value, \
             patch("zd_app.ui.app_shell.dpg.get_value") as get_value:
            shell._on_sensitivity_8point_edit("left", 3, "x", 35)

        set_value.assert_not_called()
        get_value.assert_not_called()

    def test_apply_sensitivity_settings_service_none_no_write(self) -> None:
        cases = [
            ("left", "sensitivity_left_"),
            ("right", "sensitivity_right_"),
        ]
        for side, tag_prefix in cases:
            with self.subTest(side=side):
                shell = _make_shell(None)

                with patch("zd_app.ui.app_shell.dpg.get_value") as get_value:
                    result = shell._apply_sensitivity_curve(side, tag_prefix)

                self.assertIsNone(result)
                get_value.assert_not_called()
                shell.device_service.record_apply_result.assert_called_once_with(
                    False,
                    f"{side.title()} sensitivity not applied: settings service unavailable.",
                )
                shell.device_service.log_event.assert_not_called()

    def test_apply_sensitivity_presets_write_vendor_values_both_sides(self) -> None:
        cases = [
            ("left", "apply_left_sensitivity_preset", "set_left_stick_sensitivity_curve"),
            ("right", "apply_right_sensitivity_preset", "set_right_stick_sensitivity_curve"),
        ]
        for side, method_name, setter_name in cases:
            for preset_name, anchors in SENSITIVITY_PRESETS.items():
                with self.subTest(side=side, preset=preset_name):
                    settings_service = MagicMock()
                    result = SimpleNamespace(
                        outcome=SetSensitivityCurveOutcome.OK,
                        error_code=None,
                    )
                    getattr(settings_service, setter_name).return_value = result
                    shell = _make_shell(settings_service)
                    values = {}

                    def set_value(tag, value):
                        values[tag] = value

                    def get_value(tag):
                        return values[tag]

                    with patch(
                        "zd_app.ui.app_shell.dpg.does_item_exist",
                        return_value=True,
                    ), patch(
                        "zd_app.ui.app_shell.dpg.set_value",
                        side_effect=set_value,
                    ), patch(
                        "zd_app.ui.app_shell.dpg.get_value",
                        side_effect=get_value,
                    ):
                        returned = getattr(shell, method_name)(preset_name)

                    self.assertIs(returned, result)
                    getattr(settings_service, setter_name).assert_called_once_with(anchors)
                    opposite_setter = (
                        "set_right_stick_sensitivity_curve"
                        if side == "left"
                        else "set_left_stick_sensitivity_curve"
                    )
                    getattr(settings_service, opposite_setter).assert_not_called()
                    tag_prefix = f"sensitivity_{side}_"
                    self.assertEqual(values[f"{tag_prefix}a1x_slider"], anchors[0].x)
                    self.assertEqual(values[f"{tag_prefix}a1y_slider"], anchors[0].y)
                    self.assertEqual(values[f"{tag_prefix}a2x_slider"], anchors[1].x)
                    self.assertEqual(values[f"{tag_prefix}a2y_slider"], anchors[1].y)
                    self.assertEqual(values[f"{tag_prefix}a3x_slider"], anchors[2].x)
                    self.assertEqual(values[f"{tag_prefix}a3y_slider"], anchors[2].y)

    def test_apply_sensitivity_preset_unknown_name_no_write(self) -> None:
        settings_service = MagicMock()
        shell = _make_shell(settings_service)
        shell._dpg_context_ready = True
        values = {}

        with patch(
            "zd_app.ui.app_shell.dpg.does_item_exist",
            return_value=True,
        ), patch(
            "zd_app.ui.app_shell.dpg.set_value",
            side_effect=lambda tag, value: values.__setitem__(tag, value),
        ):
            with self.assertLogs("zd_app.ui.app_shell", level="WARNING") as logs:
                result = shell.apply_left_sensitivity_preset("Turbo")

        self.assertIsNone(result)
        self.assertIn("unknown sensitivity preset 'Turbo'", logs.output[0])
        settings_service.set_left_stick_sensitivity_curve.assert_not_called()
        settings_service.set_right_stick_sensitivity_curve.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Apply failed: unknown sensitivity preset 'Turbo'.",
        )
        self.assertEqual(
            values["settings_v2_status_text"],
            "Apply failed: unknown sensitivity preset 'Turbo'.",
        )

    def test_apply_left_axis_inversion_writes_via_settings_service(self) -> None:
        settings_service = MagicMock()
        expected = AxisInversion(x_inverted=True, y_inverted=False)
        result = SimpleNamespace(
            outcome=SetAxisInversionOutcome.OK,
            error_code=None,
            payload_hex=build_axis_inversion_payload(INVERSION_STICK_LEFT, expected).hex(),
        )
        settings_service.set_left_stick_inversion.return_value = result
        shell = _make_shell(settings_service)
        values = {
            "axis_inv_left_x_checkbox": True,
            "axis_inv_left_y_checkbox": False,
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            returned = shell.apply_left_axis_inversion()

        self.assertIs(returned, result)
        settings_service.set_left_stick_inversion.assert_called_once_with(expected)
        settings_service.set_right_stick_inversion.assert_not_called()
        self.assertEqual(
            result.payload_hex,
            "001055aa510700000100" + ("00" * 55),
        )
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Left axis inversion applied.",
        )

    def test_apply_right_axis_inversion_writes_via_settings_service(self) -> None:
        settings_service = MagicMock()
        expected = AxisInversion(x_inverted=False, y_inverted=True)
        result = SimpleNamespace(
            outcome=SetAxisInversionOutcome.OK,
            error_code=None,
            payload_hex=build_axis_inversion_payload(INVERSION_STICK_RIGHT, expected).hex(),
        )
        settings_service.set_right_stick_inversion.return_value = result
        shell = _make_shell(settings_service)
        values = {
            "axis_inv_right_x_checkbox": False,
            "axis_inv_right_y_checkbox": True,
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            returned = shell.apply_right_axis_inversion()

        self.assertIs(returned, result)
        settings_service.set_right_stick_inversion.assert_called_once_with(expected)
        settings_service.set_left_stick_inversion.assert_not_called()
        self.assertEqual(
            result.payload_hex,
            "001055aa510700010001" + ("00" * 55),
        )
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Right axis inversion applied.",
        )

    def test_apply_axis_inversion_settings_service_none_no_write(self) -> None:
        cases = [
            ("left", "axis_inv_left_x_checkbox", "axis_inv_left_y_checkbox"),
            ("right", "axis_inv_right_x_checkbox", "axis_inv_right_y_checkbox"),
        ]
        for side, x_tag, y_tag in cases:
            with self.subTest(side=side):
                shell = _make_shell(None)

                with patch("zd_app.ui.app_shell.dpg.get_value") as get_value:
                    result = shell._apply_axis_inversion(side, x_tag, y_tag)

                self.assertIsNone(result)
                get_value.assert_not_called()
                shell.device_service.record_apply_result.assert_called_once_with(
                    False,
                    f"{side.title()} axis inversion not applied: settings service unavailable.",
                )
                shell.device_service.log_event.assert_not_called()

    def test_apply_button_binding_writes_via_settings_service(self) -> None:
        settings_service = MagicMock()
        expected_mapping = ButtonMapping.controller_button(ControllerButtonTarget.B)
        result = SimpleNamespace(
            outcome=SetButtonBindingOutcome.OK,
            error_code=None,
            payload_hex=build_button_binding_payload(ButtonSlot.A, expected_mapping).hex(),
        )
        settings_service.set_button_binding.return_value = result
        shell = _make_shell(settings_service)
        values = {
            "binding_source_combo": "A",
            "binding_target_combo": "B",
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            returned = shell.apply_button_binding()

        self.assertIs(returned, result)
        settings_service.set_button_binding.assert_called_once_with(
            ButtonSlot.A,
            expected_mapping,
        )
        self.assertEqual(
            result.payload_hex,
            "001055aa5102000501001000" + ("00" * 53),
        )
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Binding A -> B applied.",
        )

    def test_apply_back_paddle_binding_writes_via_settings_service(self) -> None:
        settings_service = MagicMock()
        result = SimpleNamespace(
            outcome=SetBackPaddleBindingOutcome.OK,
            error_code=None,
        )
        settings_service.set_back_paddle_binding.return_value = result
        shell = _make_shell(settings_service)
        shell.last_controller_snapshot = _full_snapshot()

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value="A"):
            returned = shell.apply_back_paddle_binding_from_combo(MacroSlot.M1)

        self.assertIs(returned, result)
        settings_service.set_back_paddle_binding.assert_called_once_with(
            MacroSlot.M1,
            ControllerButtonTarget.A,
        )
        self.assertEqual(
            shell.last_controller_snapshot.back_paddle_bindings,
            {MacroSlot.M1: BackPaddleBinding(ControllerButtonTarget.A)},
        )
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Back paddle M1 -> A applied.",
        )

    def test_apply_back_paddle_binding_unbound(self) -> None:
        settings_service = MagicMock()
        settings_service.set_back_paddle_binding.return_value = SimpleNamespace(
            outcome=SetBackPaddleBindingOutcome.OK,
            error_code=None,
        )
        shell = _make_shell(settings_service)

        with patch("zd_app.ui.app_shell.dpg.get_value", return_value="Unbound"):
            result = shell.apply_back_paddle_binding_from_combo(MacroSlot.M2)

        self.assertIsNotNone(result)
        settings_service.set_back_paddle_binding.assert_called_once_with(MacroSlot.M2, None)
        self.assertEqual(shell._last_back_paddle_bindings, {MacroSlot.M2: BackPaddleBinding(None)})

    def test_apply_button_binding_unknown_source_no_write(self) -> None:
        settings_service = MagicMock()
        shell = _make_shell(settings_service)
        values = {
            "binding_source_combo": "Guide",
            "binding_target_combo": "A",
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            with self.assertLogs("zd_app.ui.app_shell", level="WARNING") as logs:
                result = shell.apply_button_binding()

        self.assertIsNone(result)
        self.assertIn("unknown source button 'Guide'", logs.output[0])
        settings_service.set_button_binding.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Apply failed: unknown source button 'Guide'; binding not applied.",
        )

    def test_apply_button_binding_unknown_target_no_write(self) -> None:
        settings_service = MagicMock()
        shell = _make_shell(settings_service)
        values = {
            "binding_source_combo": "A",
            "binding_target_combo": "Guide",
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            with self.assertLogs("zd_app.ui.app_shell", level="WARNING") as logs:
                result = shell.apply_button_binding()

        self.assertIsNone(result)
        self.assertIn("unknown target button 'Guide'", logs.output[0])
        settings_service.set_button_binding.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Apply failed: unknown target button 'Guide'; binding not applied.",
        )

    def test_apply_button_binding_settings_service_none_no_write(self) -> None:
        shell = _make_shell(None)

        with patch("zd_app.ui.app_shell.dpg.get_value") as get_value:
            result = shell.apply_button_binding()

        self.assertIsNone(result)
        get_value.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Button binding not applied: settings service unavailable.",
        )
        shell.device_service.log_event.assert_not_called()

    def test_apply_lighting_writes_via_settings_service(self) -> None:
        settings_service = MagicMock()
        expected = LightingSettings(
            light_on=True,
            mode=LightingMode.BREATH,
            brightness_byte=80,
            color=RgbColor(r=255, g=0, b=128),
        )
        result = SimpleNamespace(
            outcome=SetLightingOutcome.OK,
            error_code=None,
            payload_hex=build_lighting_payload(LightingZone.HOME, expected).hex(),
        )
        settings_service.set_zone_lighting.return_value = result
        shell = _make_shell(settings_service)
        values = {
            "lighting_zone_combo": "Home",
            "lighting_mode_combo": "Breath",
            "lighting_on_checkbox": True,
            "lighting_brightness_slider": 80,
            "lighting_r_slider": 255,
            "lighting_g_slider": 0,
            "lighting_b_slider": 128,
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            returned = shell.apply_lighting()

        self.assertIs(returned, result)
        settings_service.set_zone_lighting.assert_called_once_with(
            LightingZone.HOME,
            expected,
        )
        self.assertEqual(
            result.payload_hex,
            "001055aa51100000010250ff0080" + ("00" * 51),
        )
        shell.device_service.record_apply_result.assert_called_once_with(
            True,
            "OK: Lighting zone 'Home' applied.",
        )

    def test_apply_lighting_unknown_zone_no_write(self) -> None:
        settings_service = MagicMock()
        shell = _make_shell(settings_service)
        values = {
            "lighting_zone_combo": "Global",
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            with self.assertLogs("zd_app.ui.app_shell", level="WARNING") as logs:
                result = shell.apply_lighting()

        self.assertIsNone(result)
        self.assertIn("unknown lighting zone 'Global'", logs.output[0])
        settings_service.set_zone_lighting.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Apply failed: unknown lighting zone 'Global'; lighting not applied.",
        )

    def test_apply_lighting_unknown_mode_no_write(self) -> None:
        settings_service = MagicMock()
        shell = _make_shell(settings_service)
        values = {
            "lighting_zone_combo": "Home",
            "lighting_mode_combo": "Rainbow",
        }

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=values.__getitem__):
            with self.assertLogs("zd_app.ui.app_shell", level="WARNING") as logs:
                result = shell.apply_lighting()

        self.assertIsNone(result)
        self.assertIn("unknown lighting mode 'Rainbow'", logs.output[0])
        settings_service.set_zone_lighting.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Apply failed: unknown lighting mode 'Rainbow'; lighting not applied.",
        )

    def test_apply_lighting_settings_service_none_no_write(self) -> None:
        shell = _make_shell(None)

        with patch("zd_app.ui.app_shell.dpg.get_value") as get_value:
            result = shell.apply_lighting()

        self.assertIsNone(result)
        get_value.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            False,
            "Lighting not applied: settings service unavailable.",
        )
        shell.device_service.log_event.assert_not_called()


class RestoreAppDefaultsRegressionTests(unittest.TestCase):
    """Regression guard for N-C1.

    An earlier refactor moved bind_default_font + rebuild_full_ui into
    LocaleRouter's _on_locale_changed listener, which fires only on actual
    locale change. restore_app_defaults was a second caller that depended
    on the always-rebuild behavior; it now triggers rebuild_full_ui
    explicitly so settings unrelated to language (developer toggle, logging
    verbosity, etc.) reflect their new values regardless of locale change.
    """

    def setUp(self) -> None:
        i18n.set_locale("en")

    def tearDown(self) -> None:
        i18n.set_locale("en")

    def test_rebuild_full_ui_fires_when_locale_unchanged(self) -> None:
        """en -> en: LocaleRouter no-ops, but rebuild must still happen."""
        shell = _make_shell()
        shell.settings = AppSettings(language="en", developer_panels_visible=True)
        i18n.set_locale("en")

        with patch.object(shell, "rebuild_full_ui") as rebuild:
            shell.restore_app_defaults()

        self.assertEqual(shell.settings, AppSettings())
        rebuild.assert_called()
        self.assertEqual(rebuild.call_count, 1, "expected exactly one rebuild on no-op locale change")

    def test_rebuild_full_ui_fires_when_locale_changes(self) -> None:
        """zh-CN -> en: _on_locale_changed fires once, plus the new explicit call."""
        shell = _make_shell()
        shell.settings = AppSettings(language="zh-CN", developer_panels_visible=True)
        i18n.set_locale("zh-CN")

        with patch.object(shell, "rebuild_full_ui") as rebuild:
            shell.restore_app_defaults()

        self.assertEqual(shell.settings, AppSettings())
        rebuild.assert_called()
        # Two calls: one from _on_locale_changed via the LocaleRouter listener,
        # one from the explicit restore_app_defaults rebuild. Idempotent so the
        # double-call is acceptable; spec endorses this.
        self.assertGreaterEqual(rebuild.call_count, 1)


class RefreshFromControllerTimeoutRegressionTests(unittest.TestCase):
    """refresh_from_controller must degrade cleanly when the
    settings service raises TimeoutError (the _default_read_file 1500ms
    timeout for unresponsive HID). Without this, a wedged controller would
    block startup before dpg.create_viewport runs and the wrapper would
    never render — the regression we're guarding against."""

    def setUp(self) -> None:
        i18n.set_locale("en")

    def tearDown(self) -> None:
        i18n.set_locale("en")

    def test_refresh_from_controller_handles_timeout_error_gracefully(self) -> None:
        settings_service = MagicMock()
        settings_service.get_all_settings.side_effect = TimeoutError(
            "HID read timed out after 1500ms"
        )
        shell = _make_shell(settings_service=settings_service)

        # Should NOT raise — the broad except in refresh_from_controller is
        # the degrade-cleanly contract. Pre-fix this would have blocked the call
        # forever; post-fix the exception flows through and refresh sets a
        # status string before returning. Wrap in widget-capture so the
        # status-render path's dpg.* calls land on stubs instead of trying
        # to talk to an uninitialised DPG context. time.sleep is patched so
        # the retry settle (READ_TIMEOUT_RETRY_SETTLE_S, 2026-06-10
        # mitigation) doesn't really wait.
        with patch("zd_app.ui.app_shell.time.sleep") as sleep:
            _run_with_widget_capture(shell.refresh_from_controller)

        self.assertIsNotNone(shell.last_snapshot_status)
        self.assertIn("1500ms", shell.last_snapshot_status)
        self.assertIsNone(shell.last_snapshot_ts)
        # Two read attempts since the 2026-06-10 settle+retry mitigation: a
        # TimeoutError gets exactly one settle + retry before the failure
        # path — a persistently wedged controller still degrades cleanly.
        self.assertEqual(settings_service.get_all_settings.call_count, 2)
        sleep.assert_called_once_with(READ_TIMEOUT_RETRY_SETTLE_S)


class StepSizeAndPollingReadMissGuardTests(unittest.TestCase):
    """Read-miss display/clobber fix (2026-05-22).

    The live-write widgets (step-size slider, polling-rate combo) must stay
    disabled + unhydrated on a read-miss, never present a writable stale
    value, and never write a value the user did not choose. Direct regression
    for the operator's bug: set step_size=255, reopen with a missed read, and
    the slider showed a writable default 146 that would clobber the real 255
    on the first touch.
    """

    def setUp(self) -> None:
        i18n.set_locale("en")

    def tearDown(self) -> None:
        i18n.set_locale("en")

    # ---- step_size ----

    def test_step_size_read_miss_disables_slider_shows_hint_no_write(self) -> None:
        settings_service = MagicMock()
        settings_service.get_all_settings.return_value = _full_snapshot(step_size=None)
        shell = _make_shell(settings_service)

        values, config = _capture_widget_state(shell.refresh_from_controller)

        self.assertFalse(shell._step_size_hydrated)
        self.assertIs(config["step_size_slider"]["enabled"], False)
        self.assertIs(config["step_size_unread_hint"]["show"], True)
        # A read-miss must not set a concrete, writable value on the slider.
        self.assertNotIn("step_size_slider", values)
        settings_service.set_step_size.assert_not_called()

    def test_step_size_successful_read_hydrates_and_enables(self) -> None:
        settings_service = MagicMock()
        settings_service.get_all_settings.return_value = _full_snapshot(step_size=200)
        shell = _make_shell(settings_service)

        values, config = _capture_widget_state(shell.refresh_from_controller)

        self.assertTrue(shell._step_size_hydrated)
        self.assertEqual(values["step_size_slider"], 200)
        self.assertIs(config["step_size_slider"]["enabled"], True)
        self.assertIs(config["step_size_unread_hint"]["show"], False)

    def test_step_size_write_blocked_until_hydrated(self) -> None:
        settings_service = MagicMock()
        shell = _make_shell(settings_service)
        # Default unhydrated state: a stray callback (e.g. a DPG set_value that
        # fires the callback) must not write.
        self.assertFalse(shell._step_size_hydrated)

        result = shell.apply_step_size(146)

        self.assertIsNone(result)
        settings_service.set_step_size.assert_not_called()

    def test_step_size_write_routes_after_hydrate(self) -> None:
        settings_service = MagicMock()
        settings_service.set_step_size.return_value = SimpleNamespace(
            outcome=SetStepSizeOutcome.OK,
            error_code=None,
        )
        shell = _make_shell(settings_service)
        shell._step_size_hydrated = True

        shell.apply_step_size(200)

        settings_service.set_step_size.assert_called_once_with(200)

    def test_step_size_read_miss_then_no_interaction_never_writes(self) -> None:
        # The operator's exact repro: a missed startup read must leave the
        # controller's real value untouched — zero writes, even from a stray
        # callback, with no deliberate user interaction.
        settings_service = MagicMock()
        settings_service.get_all_settings.return_value = _full_snapshot(step_size=None)
        shell = _make_shell(settings_service)

        _capture_widget_state(shell.refresh_from_controller)
        stray = shell.apply_step_size(146)

        self.assertIsNone(stray)
        settings_service.set_step_size.assert_not_called()

    # ---- polling_rate (same live-write defect) ----

    def test_polling_rate_read_miss_disables_combo_shows_hint(self) -> None:
        settings_service = MagicMock()
        settings_service.get_all_settings.return_value = _full_snapshot(polling_rate=None)
        shell = _make_shell(settings_service)

        values, config = _capture_widget_state(shell.refresh_from_controller)

        self.assertFalse(shell._polling_rate_hydrated)
        self.assertIs(config["usb_polling_rate_combo"]["enabled"], False)
        self.assertIs(config["usb_polling_rate_unread_hint"]["show"], True)
        self.assertNotIn("usb_polling_rate_combo", values)

    def test_polling_rate_successful_read_hydrates_and_enables(self) -> None:
        settings_service = MagicMock()
        settings_service.get_all_settings.return_value = _full_snapshot(
            polling_rate=PollingRate.HZ_1000,
        )
        shell = _make_shell(settings_service)

        values, config = _capture_widget_state(shell.refresh_from_controller)

        self.assertTrue(shell._polling_rate_hydrated)
        self.assertEqual(values["usb_polling_rate_combo"], "1000Hz")
        self.assertIs(config["usb_polling_rate_combo"]["enabled"], True)
        self.assertIs(config["usb_polling_rate_unread_hint"]["show"], False)

    def test_polling_rate_write_blocked_until_hydrated(self) -> None:
        settings_service = MagicMock()
        shell = _make_shell(settings_service)
        self.assertFalse(shell._polling_rate_hydrated)

        result = shell.apply_polling_rate("1000Hz")

        self.assertIsNone(result)
        settings_service.set_polling_rate.assert_not_called()


class PollingRate8000FirmwareHonestyTests(unittest.TestCase):
    """8000 Hz needs controller fw v1.18+ — honest non-commit handling.

    The manual polling-rate combo write ACKs on a pre-1.18 device but the
    firmware silently keeps its prior (lower) rate. On an 8000 Hz selection
    *only*, ``_do_write_polling_rate`` reads the rate back once (reusing the
    existing ``get_polling_rate``) and, when the device kept a lower rate,
    surfaces the firmware-capability message and reconciles the combo to the
    device's real rate. Capable hardware (read-back == 8000) and unverifiable
    read-backs fall through to the normal success path — never a false block.
    """

    @staticmethod
    def _ok_8000(settings_service):
        result = SimpleNamespace(outcome=SetPollingRateOutcome.OK, error_code=None)
        settings_service.set_polling_rate.return_value = result
        return result

    @staticmethod
    def _hydrated_shell(settings_service):
        shell = _make_shell(settings_service)
        shell._polling_rate_hydrated = True  # past the stale-combo write guard
        # Disable the before-write Restore-Point capture so the only
        # get_polling_rate() caller is the read-back under test (the RP hook does
        # its own pre-write device read). Mirrors SliderWriteThrottleTests.
        shell.restore_point_service = None
        return shell

    def test_8000_confirmed_commit_records_success(self) -> None:
        # Capable device (fw 1.18+) honours 8K: read-back == HZ_8000 → normal
        # success, original result returned, combo NOT reconciled away from 8K.
        settings_service = MagicMock()
        result = self._ok_8000(settings_service)
        settings_service.get_polling_rate.return_value = PollingRate.HZ_8000
        shell = self._hydrated_shell(settings_service)

        returned = shell.apply_polling_rate("8000Hz")

        self.assertIs(returned, result)
        settings_service.set_polling_rate.assert_called_once_with(PollingRate.HZ_8000)
        settings_service.get_polling_rate.assert_called_once_with()
        shell.device_service.record_apply_result.assert_called_once_with(
            True, "OK: Polling rate 8000Hz applied."
        )

    def test_8000_non_commit_warns_and_reconciles_combo(self) -> None:
        # Pre-1.18 device: read-back shows a lower rate → recorded as a
        # non-success carrying the firmware-capability message, and the combo is
        # reconciled to the device's real rate (still enabled + hydrated).
        settings_service = MagicMock()
        self._ok_8000(settings_service)
        settings_service.get_polling_rate.return_value = PollingRate.HZ_1000
        shell = self._hydrated_shell(settings_service)

        values, config = _capture_widget_state(
            lambda: shell.apply_polling_rate("8000Hz")
        )

        # Combo reconciled to the device's actual (kept) rate.
        self.assertEqual(values["usb_polling_rate_combo"], "1000Hz")
        self.assertIs(config["usb_polling_rate_combo"]["enabled"], True)
        self.assertTrue(shell._polling_rate_hydrated)
        # Recorded as a non-success carrying the honest fw-capability message.
        success, message = shell.device_service.record_apply_result.call_args.args
        self.assertFalse(success)
        self.assertIn("1.18", message)
        self.assertIn("1000Hz", message)

    def test_8000_non_commit_message_localizes_zh(self) -> None:
        settings_service = MagicMock()
        self._ok_8000(settings_service)
        settings_service.get_polling_rate.return_value = PollingRate.HZ_2000
        shell = self._hydrated_shell(settings_service)
        try:
            i18n.set_locale("zh-CN")
            _capture_widget_state(lambda: shell.apply_polling_rate("8000Hz"))
            _success, message = shell.device_service.record_apply_result.call_args.args
            self.assertIn("固件", message)  # "firmware"
            self.assertIn("2000Hz", message)
        finally:
            i18n.set_locale("en")

    def test_8000_unverifiable_readback_is_trusted(self) -> None:
        # Fail-safe: read-back returns None (couldn't read) → trust the ACK,
        # record success, do NOT cry non-commit.
        settings_service = MagicMock()
        result = self._ok_8000(settings_service)
        settings_service.get_polling_rate.return_value = None
        shell = self._hydrated_shell(settings_service)

        returned = shell.apply_polling_rate("8000Hz")

        self.assertIs(returned, result)
        shell.device_service.record_apply_result.assert_called_once_with(
            True, "OK: Polling rate 8000Hz applied."
        )

    def test_8000_readback_exception_is_trusted(self) -> None:
        # Fail-safe: a read-back that raises (e.g. TimeoutError) must not crash
        # the apply nor be treated as a non-commit.
        settings_service = MagicMock()
        result = self._ok_8000(settings_service)
        settings_service.get_polling_rate.side_effect = TimeoutError("no answer")
        shell = self._hydrated_shell(settings_service)

        returned = shell.apply_polling_rate("8000Hz")

        self.assertIs(returned, result)
        shell.device_service.record_apply_result.assert_called_once_with(
            True, "OK: Polling rate 8000Hz applied."
        )

    def test_sub_8000_selection_never_reads_back(self) -> None:
        # Only 8000 Hz pays the confirmation read — every other rate is
        # unchanged (zero extra round-trips, no behaviour change).
        settings_service = MagicMock()
        result = SimpleNamespace(outcome=SetPollingRateOutcome.OK, error_code=None)
        settings_service.set_polling_rate.return_value = result
        shell = self._hydrated_shell(settings_service)

        returned = shell.apply_polling_rate("4000Hz")

        self.assertIs(returned, result)
        settings_service.get_polling_rate.assert_not_called()
        shell.device_service.record_apply_result.assert_called_once_with(
            True, "OK: Polling rate 4000Hz applied."
        )

    def test_8000_write_failure_skips_readback(self) -> None:
        # If the WriteFile itself failed there is nothing to confirm — no
        # read-back, normal failure handling.
        settings_service = MagicMock()
        result = SimpleNamespace(
            outcome=SetPollingRateOutcome.WRITE_FAILED, error_code=5
        )
        settings_service.set_polling_rate.return_value = result
        shell = self._hydrated_shell(settings_service)

        returned = shell.apply_polling_rate("8000Hz")

        self.assertIs(returned, result)
        settings_service.get_polling_rate.assert_not_called()
        success, _message = shell.device_service.record_apply_result.call_args.args
        self.assertFalse(success)


class DeviceSettingsTests(unittest.TestCase):
    """Device settings: Profile vs Device persistence model (D3, D5, D6)."""

    # --- Save As (D3) -------------------------------------------------------

    def test_save_named_default_excludes_device(self) -> None:
        store = MagicMock()
        shell = _make_shell(MagicMock(), store)
        snapshot = _full_snapshot()  # polling + step_size set
        shell.last_controller_snapshot = snapshot

        shell.save_current_as_named_wrapper_profile("Apex")

        saved = store.save.call_args.args[0]
        self.assertIsNone(saved.snapshot.polling_rate)
        self.assertIsNone(saved.snapshot.step_size)
        # Non-Device fields preserved.
        self.assertEqual(saved.snapshot.vibration, snapshot.vibration)

    def test_save_named_include_device_keeps_device(self) -> None:
        store = MagicMock()
        shell = _make_shell(MagicMock(), store)
        snapshot = _full_snapshot()
        shell.last_controller_snapshot = snapshot

        shell.save_current_as_named_wrapper_profile("Apex", include_device=True)

        saved = store.save.call_args.args[0]
        self.assertEqual(saved.snapshot.polling_rate, snapshot.polling_rate)
        self.assertEqual(saved.snapshot.step_size, snapshot.step_size)

    def test_read_save_as_include_device_off_when_dpg_not_ready(self) -> None:
        shell = _make_shell(MagicMock(), MagicMock())
        self.assertFalse(shell._read_save_as_include_device())

    def test_read_save_as_include_device_reads_checkbox_when_ready(self) -> None:
        shell = _make_shell(MagicMock(), MagicMock())
        shell._dpg_context_ready = True
        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True), \
             patch("zd_app.ui.app_shell.dpg.get_value", return_value=True):
            self.assertTrue(shell._read_save_as_include_device())

    def test_save_current_as_wrapper_profile_propagates_checkbox_off(self) -> None:
        store = MagicMock()
        shell = _make_shell(MagicMock(), store)
        snapshot = _full_snapshot()
        shell.last_controller_snapshot = snapshot
        shell._dpg_context_ready = True

        def get_value(tag):
            if tag == "wrapper_profile_name_input":
                return "Apex"
            if tag == SAVE_AS_INCLUDE_DEVICE_CHECKBOX:
                return False
            return ""

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=get_value), \
             patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True), \
             patch("zd_app.ui.app_shell.dpg.set_value"):
            shell.save_current_as_wrapper_profile()

        saved = store.save.call_args.args[0]
        self.assertIsNone(saved.snapshot.polling_rate)
        self.assertIsNone(saved.snapshot.step_size)

    def test_save_current_as_wrapper_profile_propagates_checkbox_on(self) -> None:
        store = MagicMock()
        shell = _make_shell(MagicMock(), store)
        snapshot = _full_snapshot()
        shell.last_controller_snapshot = snapshot
        shell._dpg_context_ready = True

        def get_value(tag):
            if tag == "wrapper_profile_name_input":
                return "Apex"
            if tag == SAVE_AS_INCLUDE_DEVICE_CHECKBOX:
                return True
            return ""

        with patch("zd_app.ui.app_shell.dpg.get_value", side_effect=get_value), \
             patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True), \
             patch("zd_app.ui.app_shell.dpg.set_value"):
            shell.save_current_as_wrapper_profile()

        saved = store.save.call_args.args[0]
        self.assertEqual(saved.snapshot.polling_rate, snapshot.polling_rate)
        self.assertEqual(saved.snapshot.step_size, snapshot.step_size)

    # --- Apply confirm modal (D5) -------------------------------------------

    def test_apply_named_opens_device_confirm_for_device_override(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        store = MagicMock()
        store.load.return_value = WrapperProfile(name="Apex", snapshot=_full_snapshot())
        shell = _make_shell(settings_service, store)
        shell._dpg_context_ready = True
        window_kwargs: dict = {}

        def window(**kwargs):
            window_kwargs.update(kwargs)
            return MagicMock()

        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=False), \
             patch("zd_app.ui.app_shell.dpg.window", side_effect=window), \
             patch("zd_app.ui.app_shell.dpg.group"), \
             patch("zd_app.ui.app_shell.dpg.add_text"), \
             patch("zd_app.ui.app_shell.dpg.add_spacer"), \
             patch("zd_app.ui.app_shell.dpg.add_button"):
            shell.apply_named_wrapper_profile("Apex")

        self.assertEqual(window_kwargs.get("tag"), APPLY_DEVICE_CONFIRM_MODAL)
        # No writes happen until the user picks an option.
        settings_service.set_polling_rate.assert_not_called()
        settings_service.set_step_size.assert_not_called()
        settings_service.set_vibration.assert_not_called()

    def test_apply_named_plain_profile_bypasses_device_confirm(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        snapshot = _full_snapshot(polling_rate=None, step_size=None)
        store = MagicMock()
        store.load.return_value = WrapperProfile(name="Plain", snapshot=snapshot)
        shell = _make_shell(settings_service, store)
        shell._dpg_context_ready = True
        shell.refresh_from_controller = MagicMock()
        window_kwargs: dict = {}

        def window(**kwargs):
            window_kwargs.update(kwargs)
            return MagicMock()

        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=False), \
             patch("zd_app.ui.app_shell.dpg.window", side_effect=window), \
             patch("zd_app.ui.app_shell.dpg.group"), \
             patch("zd_app.ui.app_shell.dpg.add_text"), \
             patch("zd_app.ui.app_shell.dpg.add_spacer"), \
             patch("zd_app.ui.app_shell.dpg.add_button"):
            shell.apply_named_wrapper_profile("Plain")

        # No device confirm — apply went straight through.
        self.assertNotEqual(window_kwargs.get("tag"), APPLY_DEVICE_CONFIRM_MODAL)
        settings_service.set_vibration.assert_called_once_with(snapshot.vibration)
        settings_service.set_polling_rate.assert_not_called()
        settings_service.set_step_size.assert_not_called()

    def test_apply_wrapper_profile_resolved_profile_only_skips_device(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        profile = WrapperProfile(name="Apex", snapshot=_full_snapshot())
        shell = _make_shell(settings_service, MagicMock())
        shell.refresh_from_controller = MagicMock()

        shell._apply_wrapper_profile_resolved("Apex", profile, include_device=False)

        settings_service.set_polling_rate.assert_not_called()
        settings_service.set_step_size.assert_not_called()
        # Non-Device writes still happen.
        settings_service.set_vibration.assert_called_once_with(profile.snapshot.vibration)

    def test_apply_wrapper_profile_resolved_with_device_writes_both(self) -> None:
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        profile = WrapperProfile(name="Apex", snapshot=_full_snapshot())
        shell = _make_shell(settings_service, MagicMock())
        shell.refresh_from_controller = MagicMock()

        shell._apply_wrapper_profile_resolved("Apex", profile, include_device=True)

        settings_service.set_polling_rate.assert_called_once_with(profile.snapshot.polling_rate)
        settings_service.set_step_size.assert_called_once_with(profile.snapshot.step_size)
        settings_service.set_vibration.assert_called_once_with(profile.snapshot.vibration)

    # --- Migration (D6) -----------------------------------------------------

    def test_legacy_profile_polling_only_triggers_device_confirm(self) -> None:
        """Profile saved before step_size existed (polling set, step_size never
        serialized) still flags as a device override on apply and routes through
        the confirm."""
        from dataclasses import replace as _replace

        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        # Mimic a pre-step_size profile: polling set, step_size dropped.
        legacy_snapshot = _replace(_full_snapshot(), step_size=None)
        store = MagicMock()
        store.load.return_value = WrapperProfile(name="Old", snapshot=legacy_snapshot)
        shell = _make_shell(settings_service, store)
        shell._dpg_context_ready = True
        window_kwargs: dict = {}

        def window(**kwargs):
            window_kwargs.update(kwargs)
            return MagicMock()

        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=False), \
             patch("zd_app.ui.app_shell.dpg.window", side_effect=window), \
             patch("zd_app.ui.app_shell.dpg.group"), \
             patch("zd_app.ui.app_shell.dpg.add_text"), \
             patch("zd_app.ui.app_shell.dpg.add_spacer"), \
             patch("zd_app.ui.app_shell.dpg.add_button"):
            shell.apply_named_wrapper_profile("Old")

        # Device confirm opens; polling not written without explicit consent.
        self.assertEqual(window_kwargs.get("tag"), APPLY_DEVICE_CONFIRM_MODAL)
        settings_service.set_polling_rate.assert_not_called()


class ApplyProfileOnlyDeviceWidgetSnapbackTests(unittest.TestCase):
    """Step-size apply-snapback diagnostic (2026-05-24).

    "Apply profile only" must not touch the step-size slider or polling-rate
    combo — those widgets reflect the user's live-write value, and the device
    state was deliberately left alone by the filtered apply. Hydrating from the
    post-write read (which can race, return stale data, or carry the profile's
    saved-but-filtered values) caused the slider to visually revert and made
    the UI lie about device state.
    """

    def _profile_with_device(self) -> WrapperProfile:
        return WrapperProfile(name="Apex", snapshot=_full_snapshot())

    def test_apply_profile_only_does_not_revert_step_size_slider(self) -> None:
        # Device was just live-written to 255; the profile carries the legacy
        # step_size=146. After "Apply profile only" the slider must NOT snap to
        # 146 — the hydrate that would have written the slider is skipped.
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        settings_service.get_all_settings.return_value = _full_snapshot(step_size=255)
        shell = _make_shell(settings_service, MagicMock())

        values, _config = _capture_widget_state(
            lambda: shell._apply_wrapper_profile_resolved(
                "Apex", self._profile_with_device(), include_device=False
            )
        )

        settings_service.set_step_size.assert_not_called()
        self.assertNotIn("step_size_slider", values)
        self.assertFalse(shell._step_size_hydrated)

    def test_apply_profile_plus_device_does_revert_step_size_slider(self) -> None:
        # include_device=True means the profile's step_size IS written, then
        # read back, and the slider hydrates to the profile's value.
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        settings_service.get_all_settings.return_value = _full_snapshot(step_size=146)
        shell = _make_shell(settings_service, MagicMock())

        values, config = _capture_widget_state(
            lambda: shell._apply_wrapper_profile_resolved(
                "Apex", self._profile_with_device(), include_device=True
            )
        )

        settings_service.set_step_size.assert_called_once_with(146)
        self.assertEqual(values["step_size_slider"], 146)
        self.assertIs(config["step_size_slider"]["enabled"], True)
        self.assertTrue(shell._step_size_hydrated)

    def test_apply_profile_only_does_not_revert_polling_rate(self) -> None:
        # Polling combo behaves symmetrically with the step-size slider —
        # "Apply profile only" leaves it at whatever the user just set.
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        settings_service.get_all_settings.return_value = _full_snapshot(
            polling_rate=PollingRate.HZ_8000,
        )
        shell = _make_shell(settings_service, MagicMock())

        values, _config = _capture_widget_state(
            lambda: shell._apply_wrapper_profile_resolved(
                "Apex", self._profile_with_device(), include_device=False
            )
        )

        settings_service.set_polling_rate.assert_not_called()
        self.assertNotIn("usb_polling_rate_combo", values)
        self.assertFalse(shell._polling_rate_hydrated)

    def test_apply_profile_plus_device_does_revert_polling_rate(self) -> None:
        # include_device=True writes the profile's polling rate and hydrates
        # the combo to the profile's value.
        settings_service = MagicMock()
        _ok_profile_write_results(settings_service)
        settings_service.get_all_settings.return_value = _full_snapshot(
            polling_rate=PollingRate.HZ_8000,
        )
        shell = _make_shell(settings_service, MagicMock())

        values, config = _capture_widget_state(
            lambda: shell._apply_wrapper_profile_resolved(
                "Apex", self._profile_with_device(), include_device=True
            )
        )

        settings_service.set_polling_rate.assert_called_once_with(PollingRate.HZ_8000)
        self.assertEqual(values["usb_polling_rate_combo"], "8000Hz")
        self.assertIs(config["usb_polling_rate_combo"]["enabled"], True)
        self.assertTrue(shell._polling_rate_hydrated)


class SliderWriteThrottleTests(unittest.TestCase):
    """Drag-storm debounce (2026-05-24).

    These tests drive ``apply_step_size`` / ``apply_polling_rate`` through
    the inner :class:`_SliderWriteThrottle` and assert leading-edge fire +
    trailing-edge flush semantics. ``time.monotonic`` is mocked so the
    150 ms window is deterministic; the throttle's internal ``_last_write_ts``
    dict could be manipulated directly (see ``test_restore_points_hooks.py``
    for that pattern), but the explicit clock-mock here also covers the
    ``apply_*`` call sites' own ``time.monotonic()`` lookups.
    """

    def _make_shell_with_settings(self):
        settings_service = MagicMock()
        settings_service.set_step_size.return_value = SimpleNamespace(
            outcome=SetStepSizeOutcome.OK,
            error_code=None,
        )
        settings_service.set_polling_rate.return_value = SimpleNamespace(
            outcome=SetPollingRateOutcome.OK,
            error_code=None,
        )
        shell = _make_shell(settings_service)
        shell._step_size_hydrated = True
        shell._polling_rate_hydrated = True
        # Disable the RP hook by default so the throttle tests don't trip
        # on RP capture; per-test overrides bring it back when needed.
        shell.restore_point_service = None
        return shell, settings_service

    def test_slider_throttle_first_tick_writes_immediately(self) -> None:
        shell, settings_service = self._make_shell_with_settings()
        with patch("zd_app.ui.app_shell.time.monotonic", return_value=100.0):
            shell.apply_step_size(140)
        settings_service.set_step_size.assert_called_once_with(140)

    def test_slider_throttle_burst_within_window_writes_once(self) -> None:
        shell, settings_service = self._make_shell_with_settings()
        # 5 calls spaced 20 ms apart — total span 80 ms, all within the
        # 150 ms window. Only the leading-edge value (140) should reach the
        # service; the rest are stored as pending.
        clock = [100.0]
        with patch(
            "zd_app.ui.app_shell.time.monotonic",
            side_effect=lambda: clock[0],
        ):
            for value in (140, 141, 142, 143, 144):
                shell.apply_step_size(value)
                clock[0] += 0.020
        settings_service.set_step_size.assert_called_once_with(140)

    def test_slider_throttle_flush_pending_writes_trailing_value(self) -> None:
        shell, settings_service = self._make_shell_with_settings()
        clock = [100.0]
        with patch(
            "zd_app.ui.app_shell.time.monotonic",
            side_effect=lambda: clock[0],
        ):
            # Burst: leading-edge writes 140; 141..144 stored as pending
            # under the same window.
            for value in (140, 141, 142, 143, 144):
                shell.apply_step_size(value)
                clock[0] += 0.020
            # Drag has stopped. Jump past the window and flush — the last
            # pending value (144) is the trailing-edge write.
            clock[0] += 0.200
            shell._flush_slider_throttle()
        self.assertEqual(
            settings_service.set_step_size.call_args_list,
            [((140,), {}), ((144,), {})],
        )

    def test_slider_throttle_per_field_state(self) -> None:
        shell, settings_service = self._make_shell_with_settings()
        # Interleaved step_size + polling_rate at the same instant — each
        # field has its own throttle window, so both leading-edge writes
        # fire even though they're in the same global tick.
        with patch("zd_app.ui.app_shell.time.monotonic", return_value=100.0):
            shell.apply_step_size(140)
            shell.apply_polling_rate("1000Hz")
        settings_service.set_step_size.assert_called_once_with(140)
        settings_service.set_polling_rate.assert_called_once_with(PollingRate.HZ_1000)

    def test_slider_throttle_does_not_suppress_rp_hook(self) -> None:
        # The RP capture hook must fire on EVERY apply_* invocation
        # (subject to its own 7 s debounce, which is a separate concern).
        # Slider throttle suppressing the HID write does NOT suppress
        # user-intent capture.
        shell, _settings_service = self._make_shell_with_settings()
        captured: list[str] = []
        shell._maybe_capture_before_manual_device_write = (
            lambda *, field_key: captured.append(field_key)
        )
        clock = [100.0]
        with patch(
            "zd_app.ui.app_shell.time.monotonic",
            side_effect=lambda: clock[0],
        ):
            for value in (140, 141, 142, 143, 144):
                shell.apply_step_size(value)
                clock[0] += 0.020
        # All 5 callback invocations triggered the RP hook helper, even
        # though only the first one reached settings_service.set_step_size.
        self.assertEqual(captured, ["step_size"] * 5)

    def test_peek_pending_reports_without_consuming(self) -> None:
        # Item L: peek_pending() lets the render loop see queued trailing
        # writes (to defer them behind an in-flight HID job) WITHOUT discarding
        # them — they survive for a later flush once the gate clears.
        shell, _settings_service = self._make_shell_with_settings()
        clock = [100.0]
        with patch(
            "zd_app.ui.app_shell.time.monotonic",
            side_effect=lambda: clock[0],
        ):
            shell.apply_step_size(140)  # leading edge fires immediately
            clock[0] += 0.020
            shell.apply_step_size(141)  # within the window -> stored as pending
            throttle = shell._slider_throttle
            self.assertEqual(throttle.peek_pending(), ["step_size"])
            # Non-consuming: a second peek still sees it and the value survives.
            self.assertEqual(throttle.peek_pending(), ["step_size"])
            self.assertEqual(throttle._pending.get("step_size"), 141)
            # The window elapses and the flush still fires the queued value.
            clock[0] += 0.200
            flushed = throttle.flush_pending(now=clock[0])
        self.assertEqual(flushed, [("step_size", 141)])
        self.assertEqual(throttle.peek_pending(), [])  # consumed by the flush

    def test_slider_throttle_hydration_guard_still_works(self) -> None:
        # If the hydration write-guard is False, the callback returns
        # early — no write, AND no throttle state mutation (so a later
        # legitimate post-hydration callback isn't accidentally treated
        # as "throttled" from the unhydrated stray callback).
        shell, settings_service = self._make_shell_with_settings()
        shell._step_size_hydrated = False
        with patch("zd_app.ui.app_shell.time.monotonic", return_value=100.0):
            result = shell.apply_step_size(146)
        self.assertIsNone(result)
        settings_service.set_step_size.assert_not_called()
        self.assertNotIn("step_size", shell._slider_throttle._last_write_ts)
        self.assertNotIn("step_size", shell._slider_throttle._pending)


class _FlakyReadSettingsService:
    """``get_all_settings`` raises the queued exceptions first, then succeeds.

    Plain class (not MagicMock) so the retry path sees real exception
    instances and tests can count read attempts without mock bookkeeping.
    """

    def __init__(self, exceptions, snapshot) -> None:
        self._exceptions = list(exceptions)
        self._snapshot = snapshot
        self.read_calls = 0

    def get_all_settings(self):
        self.read_calls += 1
        if self._exceptions:
            raise self._exceptions.pop(0)
        return self._snapshot


class ReadSettleRetryTests(unittest.TestCase):
    """Settle + retry-once mitigation for the transient first-read-after-burst
    HID timeout (two hardware occurrences 2026-06-10 — see
    READ_TIMEOUT_RETRY_SETTLE_S / POST_APPLY_READ_SETTLE_S in app_shell).
    """

    # R1 — first read times out, the single retry succeeds: normal success
    # path, exactly one settle sleep, exactly one retry.
    def test_refresh_retries_once_on_timeout_then_succeeds(self) -> None:
        snapshot = _full_snapshot()
        service = _FlakyReadSettingsService(
            [TimeoutError("HID read timed out after 1000ms")], snapshot
        )
        shell = _make_shell(service)

        with patch("zd_app.ui.app_shell.time.sleep") as sleep, self.assertLogs(
            "zd_app.ui.app_shell", level="INFO"
        ) as logs:
            _run_with_widget_capture(shell.refresh_from_controller)

        self.assertEqual(service.read_calls, 2)
        sleep.assert_called_once_with(READ_TIMEOUT_RETRY_SETTLE_S)
        self.assertTrue(any("retrying once" in line for line in logs.output))
        self.assertIs(shell.last_controller_snapshot, snapshot)
        self.assertIsNotNone(shell.last_snapshot_ts)
        self.assertEqual(shell.last_snapshot_status, i18n.t("apply.read.success"))
        shell.device_service.log_i18n_event.assert_called_with(
            "log.snapshot.refreshed_ok"
        )

    # R2 — the retry times out too: failure path exactly as today, and no
    # second retry.
    def test_refresh_fails_after_second_timeout_without_more_retries(self) -> None:
        retry_exc = TimeoutError("HID read timed out after 1000ms")
        service = _FlakyReadSettingsService(
            [TimeoutError("HID read timed out after 1000ms"), retry_exc],
            _full_snapshot(),
        )
        shell = _make_shell(service)

        with patch("zd_app.ui.app_shell.time.sleep") as sleep:
            values = _run_with_widget_capture(shell.refresh_from_controller)

        self.assertEqual(service.read_calls, 2)
        sleep.assert_called_once_with(READ_TIMEOUT_RETRY_SETTLE_S)
        self.assertIsNone(shell.last_controller_snapshot)
        self.assertIsNone(shell.last_snapshot_ts)
        self.assertEqual(
            shell.last_snapshot_status,
            i18n.t("apply.read.failed", reason=retry_exc),
        )
        self.assertIn("timed out", values["settings_v2_status_text"])
        shell.device_service.log_i18n_event.assert_called_with(
            "log.snapshot.refresh_failed", reason=retry_exc
        )

    # R3 — non-timeout read failures keep today's behavior: no retry, no
    # settle sleep.
    def test_refresh_non_timeout_error_fails_without_retry(self) -> None:
        for exc in (
            SettingsServiceError("get_all_settings exploded"),
            OSError("handle yanked"),
        ):
            with self.subTest(exc_type=type(exc).__name__):
                service = _FlakyReadSettingsService([exc], _full_snapshot())
                shell = _make_shell(service)

                with patch("zd_app.ui.app_shell.time.sleep") as sleep:
                    _run_with_widget_capture(shell.refresh_from_controller)

                self.assertEqual(service.read_calls, 1)
                sleep.assert_not_called()
                self.assertIsNone(shell.last_snapshot_ts)
                self.assertEqual(
                    shell.last_snapshot_status,
                    i18n.t("apply.read.failed", reason=exc),
                )
                shell.device_service.log_i18n_event.assert_called_with(
                    "log.snapshot.refresh_failed", reason=exc
                )

    # R4 — profile apply settles POST_APPLY_READ_SETTLE_S between the write
    # burst and the post-apply read: strict order write -> sleep -> read.
    def test_apply_profile_snapshot_settles_before_post_apply_read(self) -> None:
        events: list = []
        settings_service = MagicMock()
        settings_service.set_polling_rate.side_effect = lambda rate: (
            events.append("write:polling"),
            SimpleNamespace(outcome=SetPollingRateOutcome.OK, error_code=None),
        )[1]
        settings_service.get_all_settings.side_effect = lambda: (
            events.append("read:get_all_settings"),
            _full_snapshot(),
        )[1]
        shell = _make_shell(settings_service)

        with patch(
            "zd_app.ui.app_shell.time.sleep",
            side_effect=lambda seconds: events.append(("sleep", seconds)),
        ):
            _capture_widget_state(
                lambda: shell._apply_wrapper_profile_snapshot(
                    "profile-a",
                    empty_snapshot(polling_rate=PollingRate.HZ_8000),
                    include_device=True,
                )
            )

        self.assertEqual(
            events,
            [
                "write:polling",
                ("sleep", POST_APPLY_READ_SETTLE_S),
                "read:get_all_settings",
            ],
        )

    # R4 (second site) — the retry-failed-settings path settles the same way
    # before its trailing refresh.
    def test_retry_failed_settings_settles_before_post_apply_read(self) -> None:
        events: list = []
        results: list = []
        settings_service = MagicMock()
        settings_service.get_all_settings.side_effect = lambda: (
            events.append("read:get_all_settings"),
            _full_snapshot(),
        )[1]
        shell = _make_shell(settings_service)
        failure = ApplyFailure(
            setting_label="polling",
            error="transient write glitch",
            is_transient=True,
            retry_fn=lambda: (
                events.append("write:retry"),
                SimpleNamespace(outcome=SetPollingRateOutcome.OK, error_code=None),
            )[1],
        )

        with patch(
            "zd_app.ui.app_shell.time.sleep",
            side_effect=lambda seconds: events.append(("sleep", seconds)),
        ):
            _capture_widget_state(
                lambda: results.append(shell._retry_failed_settings([failure]))
            )

        self.assertEqual(results[0].succeeded, 1)
        self.assertEqual(
            events,
            [
                "write:retry",
                ("sleep", POST_APPLY_READ_SETTLE_S),
                "read:get_all_settings",
            ],
        )


class StartupMaximizesViewportTests(unittest.TestCase):
    """The app opens maximized so it uses the full screen by default
    (2026-06-21). ``dpg.maximize_viewport`` must fire exactly once during
    ``run()``, after the viewport is shown, while the 1480x1040
    ``create_viewport`` size is kept as the (sane) restore size with a
    non-collapsing minimum.

    The restore height was raised 920 -> 1040 so that un-maximizing yields a
    window whose ~876px content clip clears the trimmed Home dashboard (~816px),
    i.e. dragging off maximized does not bring back the far-right page scrollbar.
    It stays <= 1080 so the restore size still fits common smaller monitors.
    """

    def setUp(self) -> None:
        i18n.set_locale("en")

    def tearDown(self) -> None:
        i18n.set_locale("en")

    def test_run_maximizes_viewport_after_show(self) -> None:
        shell = _make_shell()
        fake_dpg = MagicMock()
        # Skip the render loop entirely — we only care about the
        # create -> setup -> show -> maximize startup sequence.
        fake_dpg.is_dearpygui_running.return_value = False

        with patch("zd_app.ui.app_shell.dpg", fake_dpg), \
                patch("zd_app.ui.app_shell.install_dearpygui_i18n"), \
                patch("zd_app.ui.app_shell.register_fonts"), \
                patch("zd_app.ui.app_shell.bind_default_font"), \
                patch("zd_app.ui.app_shell.trust_ritual_widget.drain_pending_trust_ritual"), \
                patch.object(shell, "_setup_theme"), \
                patch.object(shell, "_build_ui"), \
                patch.object(shell, "_bind_viewport_resize_callback"), \
                patch.object(shell, "_set_window_title"), \
                patch.object(
                    shell,
                    "_show_first_run_acknowledgment_modal_if_needed",
                    return_value=True,
                ), \
                patch.object(shell, "_emit_session_start_event"):
            shell.run()

        # Maximized on startup, exactly once.
        fake_dpg.maximize_viewport.assert_called_once_with()

        # Restore size stays sane (un-maximizing yields a usable window) and
        # the layout can't collapse below a workable minimum.
        _, kwargs = fake_dpg.create_viewport.call_args
        self.assertEqual(kwargs.get("width"), 1480)
        # Restore height raised 920 -> 1040 so the un-maximized content clip
        # (~876px) clears the trimmed Home dashboard (~816px) — no far-right page
        # scrollbar when dragged off maximized.
        self.assertEqual(kwargs.get("height"), 1040)
        # ...but it must stay <= 1080 so the restore size still fits common
        # smaller monitors (a 1080-tall display).
        self.assertLessEqual(kwargs.get("height"), 1080)
        self.assertGreaterEqual(kwargs.get("min_width", 0), 1000)
        self.assertGreaterEqual(kwargs.get("min_height", 0), 700)

        # maximize_viewport must come AFTER show_viewport (maximizing an
        # unshown viewport is a no-op / undefined on some platforms).
        names = [call[0] for call in fake_dpg.method_calls]
        self.assertIn("show_viewport", names)
        self.assertIn("maximize_viewport", names)
        self.assertGreater(
            names.index("maximize_viewport"),
            names.index("show_viewport"),
            "maximize_viewport must be called after show_viewport",
        )


class GeometryLoggingTests(unittest.TestCase):
    """The permanent geometry diagnostic (PART C, 2026-06-21).

    AppShell logs one ``geometry: ...`` line at INFO after the startup maximize,
    on each screen rebuild, and (debounced) on resize, so a post-smoke read of
    zd_wrapper.log recovers the operator's REAL maximized client height + each
    screen's real root y_scroll_max — values the headless overflow probe can't
    see. The read is deferred a few frames (via the render-loop ``_tick`` drain)
    so layout has settled before it is measured.
    """

    def setUp(self) -> None:
        i18n.set_locale("en")

    def test_geometry_log_request_defers_until_countdown_elapses(self) -> None:
        shell = _make_shell()
        with patch.object(shell, "_log_geometry") as mock_log:
            shell._request_geometry_log("startup")
            # The settle countdown must elapse before anything is emitted, so a
            # newly-built (un-laid-out) screen is never measured.
            for _ in range(_GEOMETRY_LOG_SETTLE_FRAMES):
                shell._drain_geometry_log()
            mock_log.assert_not_called()
            shell._drain_geometry_log()
            mock_log.assert_called_once_with("startup")
            # Slot cleared — no repeat emit on subsequent frames.
            shell._drain_geometry_log()
            mock_log.assert_called_once_with("startup")

    def test_geometry_log_repeated_request_resets_countdown_debounce(self) -> None:
        # A continuous resize drag fires many requests; each resets the
        # countdown so no emit lands mid-drag — only the settled size logs.
        shell = _make_shell()
        with patch.object(shell, "_log_geometry") as mock_log:
            shell._request_geometry_log("resize")
            shell._drain_geometry_log()  # countdown advances one frame
            shell._request_geometry_log("resize")  # drag continues -> reset
            for _ in range(_GEOMETRY_LOG_SETTLE_FRAMES):
                shell._drain_geometry_log()
            mock_log.assert_not_called()
            shell._drain_geometry_log()
            mock_log.assert_called_once_with("resize")

    def test_log_geometry_emits_required_fields_at_info(self) -> None:
        import dearpygui.dearpygui as dpg

        shell = _make_shell()
        shell.current_screen = "home"
        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(
                    tag="content_region", height=400, no_scrollbar=True
                ):
                    # content_region's first child is the screen root — the real
                    # scroll surface the diagnostic reports on.
                    with dpg.child_window(autosize_y=True):
                        dpg.add_text("geometry probe row")
            shell._dpg_context_ready = True
            with self.assertLogs("zd_app.ui.app_shell", level="INFO") as captured:
                shell._log_geometry("startup")
        finally:
            dpg.destroy_context()

        line = "\n".join(captured.output)
        self.assertIn("geometry: screen=home", line)
        for token in (
            "viewport_client=(",
            "content_region_clip_h=",
            "root_rect_h=",
            "root_y_scroll_max=",
            "trigger=startup",
        ):
            with self.subTest(token=token):
                self.assertIn(token, line)

    def test_log_geometry_noops_without_content_region(self) -> None:
        # Guarded: before the UI exists (or after teardown) the diagnostic must
        # neither raise nor emit.
        import dearpygui.dearpygui as dpg

        shell = _make_shell()
        shell._dpg_context_ready = True
        dpg.create_context()
        try:
            with self.assertNoLogs("zd_app.ui.app_shell", level="INFO"):
                shell._log_geometry("startup")  # no content_region built
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
