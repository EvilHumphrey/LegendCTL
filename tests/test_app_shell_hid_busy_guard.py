"""Worker-thread phase 1.5 — busy guard for every device-touching UI entry point.

The wrapper moved the three long HID flows (footer Read, profile
apply, RP restore) onto a worker thread, which keeps the UI LIVE during a
2-9 s job. The first pass guarded only the footer buttons + the restore confirm;
every other device-touching callback could still fire a concurrent
synchronous HID call from the render thread mid-burst. SettingsService has
no internal lock, so interleaved write bursts can trip the firmware's
in-burst rejection quirk and concurrent ``_read_response`` loops can consume
each other's frames.

These tests pin the phase-1.5 contract (refuse-with-status, never queue):

- B1 — with a threaded executor and an event-blocked job in flight, every
  guarded entry point refuses: the service stub records zero new calls, no
  widget beyond the status banner is touched, and the localized busy banner
  lands in the status strip.
- B2 — while a HID job holds the gate, the slider throttle's trailing flush
  DEFERS its pending write (debug log, no write, no status spam) and RE-QUEUES
  it, so the value still lands the moment the gate clears (item L).
- B3 — after the job's completion drains, each entry point works again.
- B4 — ``AppShell.hid_busy`` (the public accessor screens consume) tracks
  the in-flight window; sync mode (``hid_executor=None``) never reports busy.
- B5 — Device-vs-Profile ``_perform_read`` renders the read-failed banner
  path with the busy message instead of issuing the live read.
- B6 — Restore Points: row/detail Restore (``_on_open_confirm``) refuses
  while busy because the CONFIRM build's pre-restore preview is a live HID
  read; a CONFIRM build that happens while busy skips the preview call.
- B7 — Safe Import's Save + Apply refuses as a whole (no half-completed
  disk save) and works again after the drain.
- B8 — Safe Import's Save + Apply runs its device sequence (RP capture +
  write burst + settle/read-back verify) as a HID job: render thread
  returns immediately, import modals torn down at job start, all result
  UI deferred to the on_done drain; store-only Save never enqueues a job;
  sync mode keeps today's exact inline call order.

Stub services use ``threading.Event`` gates for latency control — never
sleeps-as-synchronization (same harness shape as
test_app_shell_worker_thread.py).
"""

from __future__ import annotations

import threading
import unittest
from collections import Counter
from time import monotonic
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.r2_shell_test_helpers import empty_snapshot
from tests.test_restore_points_screen import (
    _FakeService as _FakeRpScreenService,
    _PatchedScreen,
    _rp,
    _shell_with as _rp_screen_shell_with,
)
from zd_app import i18n
from zd_app.models import AppSettings, DeviceState
from zd_app.services.settings_service import MacroSlot, PollingRate
from zd_app.ui.app_shell import (
    _HID_FLOW_BUTTON_TAGS,
    BUTTON_SLOT_BY_LABEL,
    BUTTON_TARGET_BY_LABEL,
    LIGHTING_MODE_BY_LABEL,
    LIGHTING_ZONE_BY_LABEL,
    POLLING_RATE_BY_LABEL,
    SENSITIVITY_PRESETS,
    SENSITIVITY_PRESETS_8POINT,
    TRIGGER_MODE_BY_LABEL,
    VIBRATION_MODE_BY_LABEL,
    AppShell,
    threaded_hid_executor,
)
from zd_app.ui.screens import device_vs_profile as dvp_screen
from zd_app.ui.screens import restore_points as rp_screen
from zd_app.ui.screens import safe_import as safe_import_screen


_GATE_TIMEOUT_S = 5.0

# outcome_is_success() checks ``outcome.name`` — one generic OK stands in for
# every per-setter outcome enum.
_OK_OUTCOME = SimpleNamespace(name="OK", value="ok")


def _busy_banner() -> str:
    return i18n.t("apply.status.failure_prefix", message=i18n.t("apply.busy"))


def _make_shell(
    settings_service=None,
    *,
    hid_executor=None,
    restore_point_service=None,
) -> AppShell:
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
    wrapper_profile_store = MagicMock()
    wrapper_profile_store.list_profiles.return_value = []
    shell = AppShell(
        device_service=device_service,
        profile_service=profile_service,
        diagnostics_service=MagicMock(),
        settings_store=settings_store,
        preflight_service=MagicMock(),
        settings_service=settings_service,
        wrapper_profile_store=wrapper_profile_store,
        restore_point_service=restore_point_service,
        hid_executor=hid_executor,
    )
    # Suppress the default-built RestorePointService unless a test passes its
    # own — keeps the refresh job's first-connect capture a deterministic
    # no-op (no fresh-read against the gated stub, no on-disk RP writes).
    if restore_point_service is None:
        shell.restore_point_service = None
    shell.refresh_shell = lambda: None
    shell.rebuild_current_screen = lambda: None
    return shell


def _capture_widget_state(callback):
    """Run ``callback`` under patched DPG, capturing set_value + configure_item.

    Same shape as the helper in test_app_shell_worker_thread.
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


class _RecordingService:
    """Settings-service stub: every setter records a call and reports OK.

    ``get_all_settings`` (the refresh job) is gated on threading.Events so a
    test can hold a worker mid-job deterministically — signal ``started``,
    block until ``release``.
    """

    def __init__(self, snapshot, *, gate_read=False):
        self._snapshot = snapshot
        self._gate_read = gate_read
        self.started = threading.Event()
        self.release = threading.Event()
        self.read_calls = 0
        self.write_calls: Counter = Counter()

    def get_all_settings(self):
        self.read_calls += 1
        if self._gate_read:
            self.started.set()
            if not self.release.wait(timeout=_GATE_TIMEOUT_S):
                raise AssertionError("test gate never released")
        return self._snapshot

    def _record(self, name):
        self.write_calls[name] += 1
        return SimpleNamespace(outcome=_OK_OUTCOME, error_code=None)

    def set_polling_rate(self, rate):
        return self._record("set_polling_rate")

    def set_step_size(self, value):
        return self._record("set_step_size")

    def set_step_size_verified(self, value, attempts=3, settle_s=0.1):
        # The apply path uses the verified setter; the live slider path uses the
        # plain set_step_size. Delegate to the recorded set_step_size so the
        # busy-guard write counts are unchanged regardless of which path runs.
        return self.set_step_size(value)

    def set_vibration(self, settings):
        return self._record("set_vibration")

    def set_left_trigger_settings(self, settings):
        return self._record("set_left_trigger_settings")

    def set_right_trigger_settings(self, settings):
        return self._record("set_right_trigger_settings")

    def set_all_deadzones(self, deadzones):
        return self._record("set_all_deadzones")

    def set_left_stick_sensitivity_curve(self, anchors):
        return self._record("set_left_stick_sensitivity_curve")

    def set_right_stick_sensitivity_curve(self, anchors):
        return self._record("set_right_stick_sensitivity_curve")

    def set_left_stick_sensitivity_curve_8point(self, anchors):
        return self._record("set_left_stick_sensitivity_curve_8point")

    def set_right_stick_sensitivity_curve_8point(self, anchors):
        return self._record("set_right_stick_sensitivity_curve_8point")

    def set_left_stick_inversion(self, inversion):
        return self._record("set_left_stick_inversion")

    def set_right_stick_inversion(self, inversion):
        return self._record("set_right_stick_inversion")

    def set_button_binding(self, slot, mapping):
        return self._record("set_button_binding")

    def set_back_paddle_binding(self, slot, target):
        return self._record("set_back_paddle_binding")

    def set_zone_lighting(self, zone, settings):
        return self._record("set_zone_lighting")


def _drain_queued_completions(shell, expected: int):
    """Block until ``expected`` completions are queued, then re-queue them."""

    entries = [
        shell._hid_job_completions.get(timeout=_GATE_TIMEOUT_S)
        for _ in range(expected)
    ]
    for entry in entries:
        shell._hid_job_completions.put(entry)
    return entries


# ---------------------------------------------------------------------------
# Entry-point case table: name → (invoke, expected write counter, the
# dpg.get_value tag map the works-again phase needs).
# ---------------------------------------------------------------------------

_POLLING_LABEL = next(iter(POLLING_RATE_BY_LABEL))
_VIBRATION_MODE_LABEL = next(iter(VIBRATION_MODE_BY_LABEL))
_TRIGGER_MODE_LABEL = next(iter(TRIGGER_MODE_BY_LABEL))
_BINDING_SOURCE_LABEL = next(iter(BUTTON_SLOT_BY_LABEL))
_BINDING_TARGET_LABEL = next(iter(BUTTON_TARGET_BY_LABEL))
_LIGHTING_ZONE_LABEL = next(iter(LIGHTING_ZONE_BY_LABEL))
_LIGHTING_MODE_LABEL = next(iter(LIGHTING_MODE_BY_LABEL))
_PRESET_3PT = next(iter(SENSITIVITY_PRESETS))
_PRESET_8PT = next(iter(SENSITIVITY_PRESETS_8POINT))
_MACRO_SLOT = next(iter(MacroSlot))

_SENS_3PT_VALUES = {
    f"sensitivity_left_a{i}{axis}_slider": v
    for i, axis, v in (
        (1, "x", 10), (1, "y", 10),
        (2, "x", 50), (2, "y", 50),
        (3, "x", 90), (3, "y", 90),
    )
}
_SENS_8PT_VALUES = {
    f"sensitivity_left_a{i}{axis}_slider_8point": i * 10
    for i in range(1, 9)
    for axis in ("x", "y")
}

_ENTRY_POINT_CASES = [
    {
        "name": "apply_polling_rate",
        "invoke": lambda shell: shell.apply_polling_rate(_POLLING_LABEL),
        "counter": "set_polling_rate",
        "values": {},
    },
    {
        "name": "apply_step_size",
        "invoke": lambda shell: shell.apply_step_size(150),
        "counter": "set_step_size",
        "values": {},
    },
    {
        "name": "apply_vibration_settings",
        "invoke": lambda shell: shell.apply_vibration_settings(),
        "counter": "set_vibration",
        "values": {
            "vibration_lg_slider": 30,
            "vibration_rg_slider": 50,
            "vibration_lm_slider": 70,
            "vibration_rm_slider": 90,
            "vibration_mode_combo": _VIBRATION_MODE_LABEL,
        },
    },
    {
        "name": "apply_left_trigger_settings",
        "invoke": lambda shell: shell.apply_left_trigger_settings(),
        "counter": "set_left_trigger_settings",
        "values": {
            "trigger_left_min_slider": 10,
            "trigger_left_max_slider": 90,
            "trigger_left_mode_combo": _TRIGGER_MODE_LABEL,
        },
    },
    {
        "name": "apply_deadzone_settings",
        "invoke": lambda shell: shell.apply_deadzone_settings(),
        "counter": "set_all_deadzones",
        "values": {
            "deadzone_left_center_slider": 1,
            "deadzone_right_center_slider": 2,
            "deadzone_left_outer_slider": 3,
            "deadzone_right_outer_slider": 4,
        },
    },
    {
        "name": "apply_left_sensitivity_curve",
        "invoke": lambda shell: shell.apply_left_sensitivity_curve(),
        "counter": "set_left_stick_sensitivity_curve",
        "values": dict(_SENS_3PT_VALUES),
    },
    {
        "name": "apply_left_sensitivity_preset",
        "invoke": lambda shell: shell.apply_left_sensitivity_preset(_PRESET_3PT),
        "counter": "set_left_stick_sensitivity_curve",
        "values": dict(_SENS_3PT_VALUES),
    },
    {
        "name": "apply_left_sensitivity_curve_8point",
        "invoke": lambda shell: shell.apply_left_sensitivity_curve_8point(),
        "counter": "set_left_stick_sensitivity_curve_8point",
        "values": dict(_SENS_8PT_VALUES),
    },
    {
        "name": "apply_left_sensitivity_preset_8point",
        "invoke": lambda shell: shell.apply_left_sensitivity_preset_8point(
            _PRESET_8PT
        ),
        "counter": "set_left_stick_sensitivity_curve_8point",
        "values": dict(_SENS_8PT_VALUES),
    },
    {
        "name": "apply_left_axis_inversion",
        "invoke": lambda shell: shell.apply_left_axis_inversion(),
        "counter": "set_left_stick_inversion",
        "values": {
            "axis_inv_left_x_checkbox": True,
            "axis_inv_left_y_checkbox": False,
        },
    },
    {
        "name": "apply_button_binding",
        "invoke": lambda shell: shell.apply_button_binding(),
        "counter": "set_button_binding",
        "values": {
            "binding_source_combo": _BINDING_SOURCE_LABEL,
            "binding_target_combo": _BINDING_TARGET_LABEL,
        },
    },
    {
        "name": "apply_lighting",
        "invoke": lambda shell: shell.apply_lighting(),
        "counter": "set_zone_lighting",
        "values": {
            "lighting_zone_combo": _LIGHTING_ZONE_LABEL,
            "lighting_mode_combo": _LIGHTING_MODE_LABEL,
            "lighting_on_checkbox": True,
            "lighting_brightness_slider": 128,
            "lighting_r_slider": 10,
            "lighting_g_slider": 20,
            "lighting_b_slider": 30,
        },
    },
    {
        "name": "apply_back_paddle_binding_from_combo",
        "invoke": lambda shell: shell.apply_back_paddle_binding_from_combo(
            _MACRO_SLOT
        ),
        "counter": "set_back_paddle_binding",
        "values": {
            f"back_paddle_combo_{_MACRO_SLOT.name}": i18n.t(
                "controller.back_paddles.unbound"
            ),
        },
    },
]


class _BusyWindowTestCase(unittest.TestCase):
    """Shared harness: a threaded shell held mid-job on an event gate."""

    def _busy_shell(self):
        snapshot = empty_snapshot(
            polling_rate=PollingRate.HZ_8000, step_size=146
        )
        service = _RecordingService(snapshot, gate_read=True)
        shell = _make_shell(service, hid_executor=threaded_hid_executor)
        shell._dpg_context_ready = True
        shell._polling_rate_hydrated = True
        shell._step_size_hydrated = True
        _capture_widget_state(shell.refresh_from_controller)
        self.assertTrue(service.started.wait(_GATE_TIMEOUT_S))
        self.assertTrue(shell._hid_job_in_flight)
        return shell, service

    def _block_first_connect_capture(self, shell):
        """Keep the in-flight refresh job's first-connect RP capture a no-op
        for shells that attach a restore_point_service mid-test."""

        identity = shell._current_device_identity()
        shell._first_connect_captured.add(identity.product_string or "__unknown__")

    def _finish(self, shell, service):
        service.release.set()
        _drain_queued_completions(shell, 1)
        _capture_widget_state(shell._drain_hid_job_completions)
        self.assertFalse(shell._hid_job_in_flight)
        # The drained read hydrates from the stub snapshot; re-pin the
        # hydration flags so the works-again phase exercises only the guard.
        shell._polling_rate_hydrated = True
        shell._step_size_hydrated = True


# ---------------------------------------------------------------------------
# B1 + B3 — every guarded entry point refuses while busy (zero service calls,
# busy banner, no widget reads) and works again after the drain.
# ---------------------------------------------------------------------------


class EntryPointBusyRefusalTests(_BusyWindowTestCase):
    def test_each_entry_point_refuses_then_works_again(self) -> None:
        for case in _ENTRY_POINT_CASES:
            with self.subTest(entry_point=case["name"]):
                shell, service = self._busy_shell()

                with patch("zd_app.ui.app_shell.dpg.get_value") as get_value:
                    values, _ = _capture_widget_state(
                        lambda: case["invoke"](shell)
                    )

                # Refused before reading any widget or touching the service.
                get_value.assert_not_called()
                self.assertEqual(
                    sum(service.write_calls.values()), 0, case["name"]
                )
                self.assertEqual(values["footer_status_text"], _busy_banner())
                self.assertEqual(
                    values["settings_v2_status_text"], _busy_banner()
                )
                # Nothing beyond the status strip was written (the preset
                # paths must not move sliders the device never received).
                self.assertLessEqual(
                    set(values),
                    {"footer_status_text", "settings_v2_status_text"},
                    case["name"],
                )

                self._finish(shell, service)

                with patch(
                    "zd_app.ui.app_shell.dpg.get_value",
                    side_effect=case["values"].__getitem__,
                ):
                    _capture_widget_state(lambda: case["invoke"](shell))

                self.assertEqual(
                    service.write_calls[case["counter"]], 1, case["name"]
                )
                self.assertEqual(
                    sum(service.write_calls.values()), 1, case["name"]
                )

    def test_manual_save_restore_point_refuses_then_works(self) -> None:
        shell, service = self._busy_shell()
        rp_service = MagicMock()
        rp_service.capture.return_value = SimpleNamespace(
            id="rp_test", title="Manual RP"
        )
        shell.restore_point_service = rp_service
        self._block_first_connect_capture(shell)

        outcome: list = []
        values, _ = _capture_widget_state(
            lambda: outcome.append(shell.manual_save_restore_point())
        )

        self.assertIsNone(outcome[0])
        rp_service.capture.assert_not_called()
        self.assertEqual(values["footer_status_text"], _busy_banner())

        self._finish(shell, service)

        outcome.clear()
        _capture_widget_state(
            lambda: outcome.append(shell.manual_save_restore_point())
        )
        rp_service.capture.assert_called_once()
        self.assertIs(outcome[0], rp_service.capture.return_value)

    def test_safe_import_apply_refuses_whole_action_then_works(self) -> None:
        shell, service = self._busy_shell()
        shell._apply_snapshot_to_controller = MagicMock(
            return_value=SimpleNamespace(failed=[])
        )
        audit = SimpleNamespace()
        shell._safe_import_result = SimpleNamespace(
            profile=SimpleNamespace(snapshot=empty_snapshot()),
            generated_name="Imported Profile",
            audit=audit,
            categories={},
        )
        shell._safe_import_selected = set()

        with patch("zd_app.ui.app_shell.dpg.get_value") as get_value:
            values, _ = _capture_widget_state(
                lambda: shell.safe_import_apply(apply_to_controller=True)
            )

        # Refused as a whole: no half-completed action (nothing saved to
        # disk, nothing applied, audit untouched).
        get_value.assert_not_called()
        shell.wrapper_profile_store.save.assert_not_called()
        shell._apply_snapshot_to_controller.assert_not_called()
        self.assertFalse(hasattr(audit, "selected_categories"))
        self.assertEqual(values["footer_status_text"], _busy_banner())

        self._finish(shell, service)

        with patch(
            "zd_app.ui.app_shell.dpg.get_value",
            side_effect={safe_import_screen.NAME_INPUT: "Imported Profile"}.__getitem__,
        ), patch("zd_app.ui.app_shell.safe_import.open_result"), patch(
            "zd_app.ui.app_shell.time.sleep"
        ), patch("zd_app.ui.app_shell.dpg.delete_item"):
            _capture_widget_state(
                lambda: shell.safe_import_apply(apply_to_controller=True)
            )
            # Save + Apply's device sequence is itself a jobbed flow now
            # (Safe-Import jobbing work): the disk save runs synchronously
            # but the apply happens on the worker — drain its completion
            # before asserting the device work happened.
            _drain_queued_completions(shell, 1)
            _capture_widget_state(shell._drain_hid_job_completions)

        shell.wrapper_profile_store.save.assert_called_once()
        shell._apply_snapshot_to_controller.assert_called_once()

    def test_save_without_apply_stays_available_while_busy(self) -> None:
        # Plain "Save as New Profile" is store-only — the busy window must
        # not block it (refuse-only-what-touches-the-device).
        shell, service = self._busy_shell()
        shell._apply_snapshot_to_controller = MagicMock()
        shell._safe_import_result = SimpleNamespace(
            profile=SimpleNamespace(snapshot=empty_snapshot()),
            generated_name="Imported Profile",
            audit=SimpleNamespace(),
            categories={},
        )
        shell._safe_import_selected = set()

        with patch(
            "zd_app.ui.app_shell.dpg.get_value",
            side_effect={safe_import_screen.NAME_INPUT: "Imported Profile"}.__getitem__,
        ), patch("zd_app.ui.app_shell.safe_import.open_result"):
            _capture_widget_state(
                lambda: shell.safe_import_apply(apply_to_controller=False)
            )

        shell.wrapper_profile_store.save.assert_called_once()
        shell._apply_snapshot_to_controller.assert_not_called()
        self.assertEqual(
            shell._safe_import_result.audit.controller_write, "not_performed"
        )

        self._finish(shell, service)


# ---------------------------------------------------------------------------
# B2 — the slider throttle's trailing flush defers + re-queues its pending
# write while busy (item L), so it still lands once the gate clears.
# ---------------------------------------------------------------------------


class SliderTrailingFlushTests(_BusyWindowTestCase):
    def test_trailing_flush_defers_and_requeues_while_busy(self) -> None:
        # Item L: while a HID job holds the gate, an elapsed trailing write is
        # LEFT QUEUED (not consumed) — no write fires, no status spam, just a
        # debug "deferred" line — so it still lands the moment the gate clears.
        # The old behavior dropped + DISCARDED it, which stranded the inline
        # deadzone status at "sending" when the user released mid-verify (the
        # consumed pending meant no trailing flush ever ran the verify).
        shell, service = self._busy_shell()
        # Plant an elapsed pending trailing write, as a drag-storm would.
        shell._slider_throttle._pending["step_size"] = 123
        shell._slider_throttle._last_write_ts["step_size"] = monotonic() - 60.0

        with self.assertLogs("zd_app.ui.app_shell", level="DEBUG") as logs:
            values, _ = _capture_widget_state(shell._flush_slider_throttle)

        # No write fires and no status banner spams from the render tick...
        self.assertEqual(sum(service.write_calls.values()), 0)
        self.assertEqual(values, {})
        self.assertTrue(
            any("deferred" in line for line in logs.output), logs.output
        )
        # ...but the pending is RE-QUEUED, not discarded.
        self.assertEqual(shell._slider_throttle._pending.get("step_size"), 123)

        self._finish(shell, service)

        # Re-queued, not dropped: the preserved value fires on the very next
        # idle tick once the gate clears — no further user interaction needed.
        _capture_widget_state(shell._flush_slider_throttle)
        self.assertEqual(service.write_calls["set_step_size"], 1)
        self.assertEqual(sum(service.write_calls.values()), 1)
        self.assertIsNone(shell._slider_throttle._pending.get("step_size"))


# ---------------------------------------------------------------------------
# B4 — the public hid_busy accessor.
# ---------------------------------------------------------------------------


class HidBusyAccessorTests(_BusyWindowTestCase):
    def test_sync_mode_never_reports_busy(self) -> None:
        service = _RecordingService(empty_snapshot())
        shell = _make_shell(service)
        self.assertFalse(shell.hid_busy)
        _capture_widget_state(shell.refresh_from_controller)
        self.assertFalse(shell.hid_busy)
        self.assertEqual(service.read_calls, 1)

    def test_tracks_threaded_job_window(self) -> None:
        shell, service = self._busy_shell()
        self.assertTrue(shell.hid_busy)
        self._finish(shell, service)
        self.assertFalse(shell.hid_busy)


# ---------------------------------------------------------------------------
# B5 — Device-vs-Profile read path.
# ---------------------------------------------------------------------------


class DeviceVsProfileBusyTests(_BusyWindowTestCase):
    def test_perform_read_renders_busy_banner_then_reads_after(self) -> None:
        shell, service = self._busy_shell()
        rp_service = MagicMock()
        sentinel = empty_snapshot()
        rp_service.read_current_state_with_provenance.return_value = (
            sentinel,
            {},
            {},
        )
        shell.restore_point_service = rp_service
        self._block_first_connect_capture(shell)
        state = dvp_screen._ensure_state(shell)

        dvp_screen._perform_read(shell, state)

        rp_service.read_current_state_with_provenance.assert_not_called()
        self.assertTrue(state.read_attempted)
        self.assertTrue(state.read_failed)
        self.assertIsNone(state.current_snapshot)
        self.assertEqual(state.status_text, i18n.t("apply.busy"))
        self.assertEqual(state.status_kind, "bad")
        self.assertIsNone(state.last_read_ts)  # no read was issued

        self._finish(shell, service)

        state.read_attempted = False  # what the Read button does
        dvp_screen._perform_read(shell, state)

        rp_service.read_current_state_with_provenance.assert_called_once()
        self.assertFalse(state.read_failed)
        self.assertIs(state.current_snapshot, sentinel)
        self.assertIsNotNone(state.last_read_ts)

    def test_perform_read_unchanged_for_stub_shells(self) -> None:
        # Screen tests drive _perform_read with SimpleNamespace shells that
        # have no hid_busy attribute — the guard must fall through.
        rp_service = MagicMock()
        sentinel = empty_snapshot()
        rp_service.read_current_state_with_provenance.return_value = (
            sentinel,
            {},
            {},
        )
        shell = SimpleNamespace(restore_point_service=rp_service)
        state = dvp_screen.DeviceVsProfileScreenState()

        dvp_screen._perform_read(shell, state)

        rp_service.read_current_state_with_provenance.assert_called_once()
        self.assertIs(state.current_snapshot, sentinel)
        self.assertFalse(state.read_failed)


# ---------------------------------------------------------------------------
# B6 — Restore Points: confirm entry + confirm-build preview.
# ---------------------------------------------------------------------------


class RestorePointsConfirmBusyTests(_BusyWindowTestCase):
    def test_open_confirm_refuses_then_works(self) -> None:
        shell, service = self._busy_shell()
        rebuilds = MagicMock()
        shell.rebuild_current_screen = rebuilds
        shell.restore_points_screen_state = rp_screen.RestorePointsScreenState(
            view=rp_screen.VIEW_LIST
        )

        rp_screen._on_open_confirm(shell, "rp1")

        state = shell.restore_points_screen_state
        self.assertEqual(state.view, rp_screen.VIEW_LIST)  # stayed put
        self.assertIsNone(state.selected_rp_id)
        self.assertEqual(state.status_text, i18n.t("apply.busy"))
        self.assertEqual(state.status_kind, "warn")
        rebuilds.assert_called_once()

        self._finish(shell, service)

        rp_screen._on_open_confirm(shell, "rp1")
        self.assertEqual(state.view, rp_screen.VIEW_CONFIRM)
        self.assertEqual(state.selected_rp_id, "rp1")
        self.assertEqual(state.status_text, "")

    def test_confirm_build_skips_preview_while_busy(self) -> None:
        # A CONFIRM rebuild can land while busy (e.g. the restore button's
        # own refusal rebuilds the view) — the build must then skip the
        # pre-restore preview's live HID read.
        rp = _rp()
        for busy, expect_preview_call in ((True, False), (False, True)):
            with self.subTest(busy=busy):
                fake_service = _FakeRpScreenService(valid=[rp])
                state = rp_screen.RestorePointsScreenState(
                    view=rp_screen.VIEW_CONFIRM, selected_rp_id=rp.id
                )
                shell = _rp_screen_shell_with(fake_service, screen_state=state)
                shell.hid_busy = busy
                with _PatchedScreen(shell) as ps:
                    ps.build()
                preview_called = any(
                    name == "compute_restore_preview"
                    for name, _args, _kw in fake_service.calls
                )
                self.assertEqual(preview_called, expect_preview_call)

    def test_in_progress_build_mid_job_skips_frame_callback(self) -> None:
        # Any rebuild while view == IN_PROGRESS (nav away +
        # back, locale change) re-registered the one-frame _execute_restore
        # callback; the busy guard then refused it and snapped the view back
        # to CONFIRM while the real restore was still running. While busy
        # the build must skip the registration — the in-flight job's
        # completion drives the IN_PROGRESS -> RESULT transition.
        rp = _rp()
        for busy, expect_registration in ((True, False), (False, True)):
            with self.subTest(busy=busy):
                fake_service = _FakeRpScreenService(valid=[rp])
                state = rp_screen.RestorePointsScreenState(
                    view=rp_screen.VIEW_IN_PROGRESS, selected_rp_id=rp.id
                )
                shell = _rp_screen_shell_with(fake_service, screen_state=state)
                shell.hid_busy = busy
                with _PatchedScreen(shell) as ps:
                    ps.build()
                registered = any(
                    name == "set_frame_callback"
                    for name, _args, _kw in ps.calls
                )
                self.assertEqual(registered, expect_registration)
                # Either way the view stays IN_PROGRESS after the build.
                self.assertEqual(state.view, rp_screen.VIEW_IN_PROGRESS)


# ---------------------------------------------------------------------------
# Mid-job rebuilds recreate the HID-flow buttons disabled —
# rebuild_current_screen re-applies the disable after the builder runs.
# ---------------------------------------------------------------------------


class RebuildMidJobButtonStateTests(_BusyWindowTestCase):
    def _real_rebuild(self, shell):
        """The bound real method (the _make_shell helper stubs it out)."""

        return AppShell.rebuild_current_screen.__get__(shell)

    def test_rebuild_mid_job_recreates_flow_buttons_disabled(self) -> None:
        shell, service = self._busy_shell()
        shell.refresh_current_screen = lambda: None  # not under test

        with patch.dict(
            AppShell.SCREEN_BUILDERS, {"home": lambda _shell, _parent: None}
        ), patch(
            "zd_app.ui.app_shell.dpg.get_item_children", return_value=[]
        ), patch(
            "zd_app.ui.app_shell.dpg.delete_item"
        ):
            _, config = _capture_widget_state(self._real_rebuild(shell))

        # The rebuild's trailing pass re-disabled every flow button the
        # builder would have recreated enabled.
        for tag in _HID_FLOW_BUTTON_TAGS:
            self.assertIs(config[tag]["enabled"], False, tag)

        self._finish(shell, service)

        # Not busy: the rebuild leaves button enablement alone.
        with patch.dict(
            AppShell.SCREEN_BUILDERS, {"home": lambda _shell, _parent: None}
        ), patch(
            "zd_app.ui.app_shell.dpg.get_item_children", return_value=[]
        ), patch(
            "zd_app.ui.app_shell.dpg.delete_item"
        ):
            _, config = _capture_widget_state(self._real_rebuild(shell))
        for tag in _HID_FLOW_BUTTON_TAGS:
            self.assertNotIn(tag, config)


# ---------------------------------------------------------------------------
# B8 — Safe Import Save + Apply rides the HID-job seam (Safe-Import jobbing
# work): job = RP capture + write burst + settle/
# read-back verify; on_done = result banner + footer combo + rebuild + result
# modal + failure modal. Store-only Save stays inline; sync mode keeps
# today's statement order.
# ---------------------------------------------------------------------------


class SafeImportApplyJobbingTests(unittest.TestCase):
    def _plant_import(self, shell):
        """Give the shell a scanned import the way B7's tests do."""

        audit = SimpleNamespace()
        shell._safe_import_result = SimpleNamespace(
            profile=SimpleNamespace(snapshot=empty_snapshot()),
            generated_name="Imported Profile",
            audit=audit,
            categories={},
        )
        shell._safe_import_selected = set()
        return audit

    def _gated_apply(self, failures=()):
        """An _apply_snapshot_to_controller stub gated like _RecordingService."""

        started = threading.Event()
        release = threading.Event()

        def apply_fn(snapshot):
            started.set()
            if not release.wait(timeout=_GATE_TIMEOUT_S):
                raise AssertionError("test gate never released")
            return SimpleNamespace(
                failed=list(failures),
                succeeded=0 if failures else 1,
                total_attempted=1,
                retry_recoveries=0,
            )

        return MagicMock(side_effect=apply_fn), started, release

    def test_threaded_save_apply_jobs_device_sequence_ui_after_drain(self) -> None:
        snapshot = empty_snapshot(polling_rate=PollingRate.HZ_8000)
        service = _RecordingService(snapshot)
        shell = _make_shell(service, hid_executor=threaded_hid_executor)
        shell._dpg_context_ready = True
        audit = self._plant_import(shell)
        apply_mock, started, release = self._gated_apply()
        shell._apply_snapshot_to_controller = apply_mock
        shell._refresh_footer_profile_combo = MagicMock()
        shell._show_apply_failure_modal = MagicMock()
        rebuilds = MagicMock()
        shell.rebuild_current_screen = rebuilds

        with patch(
            "zd_app.ui.app_shell.dpg.get_value",
            side_effect={safe_import_screen.NAME_INPUT: "Imported Profile"}.__getitem__,
        ), patch(
            "zd_app.ui.app_shell.safe_import.open_result"
        ) as open_result, patch(
            "zd_app.ui.app_shell.time.sleep"
        ), patch(
            "zd_app.ui.app_shell.dpg.delete_item"
        ) as delete_item:
            returned: list = []
            _capture_widget_state(
                lambda: returned.append(
                    shell.safe_import_apply(apply_to_controller=True)
                )
            )

            # The render thread came straight back: disk save done, the
            # import modals were torn down at job start, busy flag is up
            # while the worker sits mid-burst.
            self.assertIsNone(returned[0])
            shell.wrapper_profile_store.save.assert_called_once()
            self.assertTrue(started.wait(_GATE_TIMEOUT_S))
            self.assertTrue(shell._hid_job_in_flight)
            deleted = {call.args[0] for call in delete_item.call_args_list}
            self.assertIn(safe_import_screen.CONFIRM_MODAL, deleted)
            self.assertIn(safe_import_screen.PREVIEW_MODAL, deleted)
            # The RP capture already ran job-side, before the burst (no
            # restore_point_service wired -> its None result was recorded).
            self.assertIsNone(audit.restore_point_name)
            # None of the on_done UI has fired mid-job.
            open_result.assert_not_called()
            shell.device_service.record_apply_result.assert_not_called()
            shell._refresh_footer_profile_combo.assert_not_called()
            rebuilds.assert_not_called()
            self.assertEqual(service.read_calls, 0)  # verify read still pending

            # Another HID flow is refused outright mid-job (B1 contract).
            with patch("zd_app.ui.app_shell.dpg.get_value") as busy_get_value:
                busy_values, _ = _capture_widget_state(
                    lambda: shell.apply_polling_rate(_POLLING_LABEL)
                )
            busy_get_value.assert_not_called()
            self.assertEqual(sum(service.write_calls.values()), 0)
            self.assertEqual(busy_values["footer_status_text"], _busy_banner())

            release.set()
            _drain_queued_completions(shell, 1)
            # The job has fully finished (read-back included, on the worker)
            # but the completion hasn't drained: the busy window holds across
            # the WHOLE sequence, and the UI still hasn't moved.
            self.assertTrue(shell._hid_job_in_flight)
            self.assertEqual(service.read_calls, 1)
            open_result.assert_not_called()

            _capture_widget_state(shell._drain_hid_job_completions)

            self.assertFalse(shell._hid_job_in_flight)
            open_result.assert_called_once()
            shell._refresh_footer_profile_combo.assert_called_once_with(
                selected_name="Imported Profile"
            )
            rebuilds.assert_called_once()
            recorded = shell.device_service.record_apply_result.call_args
            self.assertIs(recorded.args[0], True)
            shell._show_apply_failure_modal.assert_not_called()

    def test_threaded_failed_apply_failure_modal_from_on_done_audit_as_today(
        self,
    ) -> None:
        snapshot = empty_snapshot(polling_rate=PollingRate.HZ_8000)
        service = _RecordingService(snapshot)
        shell = _make_shell(service, hid_executor=threaded_hid_executor)
        shell._dpg_context_ready = True
        audit = self._plant_import(shell)
        failure = SimpleNamespace(setting_label="deadzones", error="write failed")
        apply_mock, started, release = self._gated_apply(failures=[failure])
        shell._apply_snapshot_to_controller = apply_mock
        shell._show_apply_failure_modal = MagicMock()

        with patch(
            "zd_app.ui.app_shell.dpg.get_value",
            side_effect={safe_import_screen.NAME_INPUT: "Imported Profile"}.__getitem__,
        ), patch(
            "zd_app.ui.app_shell.safe_import.open_result"
        ) as open_result, patch(
            "zd_app.ui.app_shell.time.sleep"
        ) as sleep_mock, patch(
            "zd_app.ui.app_shell.dpg.delete_item"
        ):
            _capture_widget_state(
                lambda: shell.safe_import_apply(apply_to_controller=True)
            )
            self.assertTrue(started.wait(_GATE_TIMEOUT_S))
            shell._show_apply_failure_modal.assert_not_called()

            release.set()
            _drain_queued_completions(shell, 1)
            # Job finished; the failure modal is on_done-side and waits for
            # the render-thread drain.
            shell._show_apply_failure_modal.assert_not_called()

            _capture_widget_state(shell._drain_hid_job_completions)

        shell._show_apply_failure_modal.assert_called_once()
        self.assertIs(
            shell._show_apply_failure_modal.call_args.args[0],
            shell._last_apply_result,
        )
        # A5 fix: on a failed/partial apply ONLY the failure modal is shown
        # (matching the regular profile-apply path). The "Saved as" result modal
        # is NOT opened in the same completion pass — stacking it would hide the
        # failure modal under the benched modal law. The save outcome stays in
        # the result banner.
        open_result.assert_not_called()
        # Audit lands exactly as today's failed-apply path: downgraded to
        # "sent", not verified, the verify read-back (and its settle sleep)
        # skipped entirely.
        self.assertEqual(audit.controller_write, "sent")
        self.assertFalse(audit.verified)
        self.assertFalse(hasattr(audit, "verify_mismatched"))
        self.assertFalse(hasattr(audit, "verify_read_failed"))
        self.assertEqual(service.read_calls, 0)
        sleep_mock.assert_not_called()
        recorded = shell.device_service.record_apply_result.call_args
        self.assertIs(recorded.args[0], True)  # saved-as banner, as today

    def test_store_only_save_never_enqueues_a_job(self) -> None:
        service = _RecordingService(empty_snapshot())
        shell = _make_shell(service, hid_executor=threaded_hid_executor)
        shell._dpg_context_ready = True
        audit = self._plant_import(shell)
        shell._apply_snapshot_to_controller = MagicMock()

        with patch(
            "zd_app.ui.app_shell.dpg.get_value",
            side_effect={safe_import_screen.NAME_INPUT: "Imported Profile"}.__getitem__,
        ), patch(
            "zd_app.ui.app_shell.safe_import.open_result"
        ) as open_result, patch(
            "zd_app.ui.app_shell.dpg.delete_item"
        ) as delete_item:
            _capture_widget_state(
                lambda: shell.safe_import_apply(apply_to_controller=False)
            )
            # Inline and synchronous: the result modal is already open by
            # the time the callback returns.
            open_result.assert_called_once()

        self.assertFalse(shell._hid_job_in_flight)
        self.assertTrue(shell._hid_job_completions.empty())
        shell._apply_snapshot_to_controller.assert_not_called()
        # Store-only still swaps the import modals out for the result modal
        # — since the 2026-06-11 modal-swap seam that teardown happens in
        # the seam's first half instead of inside open_result (patched
        # here), so it IS visible to delete_item now. What store-only must
        # never do is run the device sequence, asserted above.
        deleted = {call.args[0] for call in delete_item.call_args_list}
        self.assertEqual(
            deleted,
            {
                safe_import_screen.CONFIRM_MODAL,
                safe_import_screen.PREVIEW_MODAL,
                safe_import_screen.RESULT_MODAL,
            },
        )
        shell.wrapper_profile_store.save.assert_called_once()
        shell._apply_snapshot_to_controller.assert_not_called()
        self.assertEqual(audit.controller_write, "not_performed")
        self.assertEqual(service.read_calls, 0)
        self.assertEqual(sum(service.write_calls.values()), 0)

    def test_sync_mode_call_order_is_todays_inline_order(self) -> None:
        order: list = []
        snapshot = empty_snapshot(polling_rate=PollingRate.HZ_8000)
        service = _RecordingService(snapshot)
        shell = _make_shell(service)  # hid_executor=None — sync mode
        shell._dpg_context_ready = True
        self._plant_import(shell)
        shell.rebuild_current_screen = lambda: order.append("rebuild")
        shell.wrapper_profile_store.save.side_effect = (
            lambda profile: order.append("save")
        )
        shell._apply_snapshot_to_controller = MagicMock(
            side_effect=lambda snap: order.append("apply")
            or SimpleNamespace(failed=[])
        )

        def record_capture():
            order.append("capture")
            return None

        shell._create_safe_import_restore_point = record_capture
        service.get_all_settings = lambda: order.append("read") or snapshot
        shell.device_service.record_apply_result.side_effect = (
            lambda success, message: order.append("record")
        )

        with patch(
            "zd_app.ui.app_shell.dpg.get_value",
            side_effect={safe_import_screen.NAME_INPUT: "Imported Profile"}.__getitem__,
        ), patch(
            "zd_app.ui.app_shell.time.sleep",
            side_effect=lambda s: order.append("settle"),
        ), patch(
            "zd_app.ui.app_shell.safe_import.open_result",
            side_effect=lambda s: order.append("result"),
        ), patch(
            "zd_app.ui.app_shell.dpg.delete_item"
        ):
            _capture_widget_state(
                lambda: shell.safe_import_apply(apply_to_controller=True)
            )

        # The pre-seam inline order, statement for statement.
        self.assertEqual(
            order,
            [
                "save",
                "capture",
                "apply",
                "settle",
                "read",
                "record",
                "rebuild",
                "result",
            ],
        )
        self.assertFalse(shell._hid_job_in_flight)  # sync mode never sets it


if __name__ == "__main__":
    unittest.main()
