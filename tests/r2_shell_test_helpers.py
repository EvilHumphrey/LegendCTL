from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from zd_app.models import AppSettings, DeviceState
from zd_app.services.settings_service import ControllerSnapshot
from zd_app.ui.app_shell import AppShell


def make_shell(
    *,
    settings_service=None,
    settings: AppSettings | None = None,
    profiles: list | None = None,
    restore_point_service=None,
) -> AppShell:
    settings_store = MagicMock()
    settings_store.load.return_value = settings or AppSettings()
    device_service = MagicMock()
    device_service.state = DeviceState()
    device_service.recent_events.return_value = []
    device_service.summary_source_summary.return_value = "Not verified"
    device_service.summary_source_label_for.return_value = "Not verified"
    device_service.last_read_duration_ms = None
    device_service.last_write_duration_ms = None
    device_service.last_apply_result = None
    profile_service = MagicMock()
    profile_service.pending_changes_count.return_value = 0
    profile_service.current_draft = SimpleNamespace(display_name="Unsaved Draft", origin="draft")
    profile_service.last_switch_result = None
    profile_service.switch_transport_candidate_source_label = "None"
    profile_service.switch_transport_status_label = "No Current Candidate"
    profile_service.last_verified_change_label = "No Verified Change Yet"
    profile_service.last_switch_official_sync_label = "Not Checked Yet"
    diagnostics_service = MagicMock()
    diagnostics_service.logging_state = "Idle"
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
    wrapper_store = MagicMock()
    wrapper_store.list_profiles.return_value = profiles or []
    # Default restore_point_service to a MagicMock unless the caller
    # injects a real one. Otherwise the shell's __init__ would build a
    # real RestorePointService against the user's actual user_data_dir
    # and try to JSON-serialize MagicMock-typed snapshot values. The mock
    # makes capture()/list_with_skipped()/restore() return MagicMock so
    # the hooks fire (recorded for assertion) without touching disk.
    if restore_point_service is None:
        restore_point_service = MagicMock()
        # capture() returns a fake RP-like with a string ``title`` attribute
        # so the Safe Import path's audit.restore_point_name assertion still
        # gets a non-None value out of the box.
        restore_point_service.capture.return_value = SimpleNamespace(
            id="rp_test_000000",
            title="Test restore point",
        )
        restore_point_service.list_with_skipped.return_value = ([], [])
    shell = AppShell(
        device_service=device_service,
        profile_service=profile_service,
        diagnostics_service=diagnostics_service,
        settings_store=settings_store,
        preflight_service=MagicMock(),
        settings_service=settings_service,
        wrapper_profile_store=wrapper_store,
        restore_point_service=restore_point_service,
    )
    return shell


def alias_of(item):
    """Resolve a DPG item handle (or pre-resolved alias string) to its alias.

    Tests often build widgets with `tag="foo"` and later collect them via
    helpers that may return either the alias string or the runtime item id.
    """
    if isinstance(item, str):
        return item
    import dearpygui.dearpygui as dpg
    return dpg.get_item_alias(item)


def empty_snapshot(**overrides) -> ControllerSnapshot:
    payload = {
        "polling_rate": None,
        "vibration": None,
        "deadzones": None,
        "axis_inversion_left": None,
        "axis_inversion_right": None,
        "sensitivity_left": None,
        "sensitivity_right": None,
        "trigger_left": None,
        "trigger_right": None,
        "button_bindings": {},
        "lighting_zones": {},
        "motion_settings": None,
        "back_paddle_bindings": {},
    }
    payload.update(overrides)
    return ControllerSnapshot(**payload)
