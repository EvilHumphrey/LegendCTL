"""Apply-pipeline hook tests for Restore Points (RPU4 + RPU5).

Each test stubs ``shell.restore_point_service`` with a :class:`_RecordingService`
and exercises the shell entry point that should fire ``service.capture(...)``
with the expected trigger. The fakes are intentionally permissive so the
tests can assert call counts + trigger types without depending on the real
SettingsService / SettingsApplyCoordinator implementations.

Coverage:

- ``_apply_wrapper_profile_resolved`` fires
  ``before_profile_apply_with_device_settings`` when ``include_device=True``
  and NOT when ``include_device=False``.
- ``_create_safe_import_restore_point`` fires ``before_safe_import_apply``
  via the service and migrates off the old WrapperProfileStore bridge.
- ``apply_step_size`` + ``apply_polling_rate`` fire
  ``before_manual_device_setting_write`` and are debounced — a second call
  within ``MANUAL_DEVICE_WRITE_RP_WINDOW_S`` is skipped.
- ``refresh_from_controller`` fires ``first_readable_connect`` exactly once
  per identity per session.
- ``manual_save_restore_point`` fires ``manual`` and propagates the title.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app.services._log_entry import ComposedLogEntry, render_log_message
from zd_app.services.settings_apply_coordinator import outcome_label
from zd_app.services.settings_service import (
    ControllerSnapshot,
    PollingRate,
)
from zd_app.storage.restore_point_models import (
    RestorePointTrigger,
)
from zd_app.ui import app_shell as app_shell_module


class _RecordingService:
    """Records every :meth:`capture` call without doing any IO."""

    def __init__(self) -> None:
        self.captures: list[tuple[str, str | None, str | None]] = []
        self._counter = 0
        # Screen path may also call list_with_skipped via the shell at some
        # point — keep it accessible even though hook tests don't use it.
        self.list_with_skipped = MagicMock(return_value=([], []))

    def capture(
        self,
        trigger: RestorePointTrigger,
        *,
        title: str | None = None,
        device_identity=None,
        fresh_read_max_age_s: float = 30.0,
        cached_snapshot=None,
        cached_snapshot_ts=None,
    ):
        self._counter += 1
        rp_id = f"rp_recorded_{self._counter}"
        self.captures.append((trigger.type, title, rp_id))
        return SimpleNamespace(
            id=rp_id,
            title=title or f"{trigger.source_label} — recorded",
            trigger=trigger,
        )

    def restore(self, *args, **kwargs):
        raise AssertionError("restore() unexpected in hook tests")


def _make_shell_with_recording_rp_service():
    """Build a shell whose ``restore_point_service`` is a recording stub."""

    settings_service = MagicMock()
    # Polling-rate combo callbacks check hydration state; mark it hydrated
    # so the apply_polling_rate / apply_step_size live-write callbacks
    # actually proceed past the read-miss guard.
    settings_service.set_polling_rate.return_value = SimpleNamespace(
        outcome="success",
    )
    settings_service.set_step_size.return_value = SimpleNamespace(
        outcome="success",
    )
    # The live slider path writes via the plain set_step_size; the apply path
    # uses the verified setter. Delegate verified -> set_step_size so either path
    # records through the same return value the RP-hook tests expect.
    settings_service.set_step_size_verified.side_effect = (
        lambda value, *a, **k: settings_service.set_step_size(value)
    )
    rp_service = _RecordingService()
    shell = make_shell(settings_service=settings_service, restore_point_service=rp_service)
    shell._polling_rate_hydrated = True
    shell._step_size_hydrated = True
    return shell, rp_service


def _trigger_types(service: _RecordingService) -> list[str]:
    return [trigger for trigger, _, _ in service.captures]


# ---------------------------------------------------------------------------
# before_profile_apply_with_device_settings
# ---------------------------------------------------------------------------


class ProfileApplyHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.shell, self.service = _make_shell_with_recording_rp_service()
        # Stub the snapshot apply so the resolved-apply call doesn't touch
        # the real coordinator; we only care that the hook fires.
        self.shell._apply_wrapper_profile_snapshot = MagicMock()

    def _profile(self, *, with_device: bool):
        snapshot = ControllerSnapshot(
            polling_rate=PollingRate.HZ_1000 if with_device else None,
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
            motion_settings=None,
            back_paddle_bindings={},
            step_size=128 if with_device else None,
        )
        return SimpleNamespace(name="Apex", snapshot=snapshot)

    def test_include_device_true_fires_before_profile_apply_with_device_settings(self) -> None:
        profile = self._profile(with_device=True)
        self.shell._apply_wrapper_profile_resolved(
            "Apex", profile, include_device=True
        )
        self.assertIn(
            "before_profile_apply_with_device_settings",
            _trigger_types(self.service),
        )

    def test_include_device_false_does_not_fire_capture(self) -> None:
        profile = self._profile(with_device=False)
        self.shell._apply_wrapper_profile_resolved(
            "Apex", profile, include_device=False
        )
        self.assertEqual(self.service.captures, [])

    def test_capture_failure_stops_before_write_then_continue_redispatches(self) -> None:
        dpg.create_context()
        self.addCleanup(dpg.destroy_context)
        self.shell._dpg_context_ready = True
        self.service.capture = MagicMock(return_value=None)
        self.shell._apply_wrapper_profile_snapshot = MagicMock()
        profile = self._profile(with_device=True)

        self.shell._apply_wrapper_profile_resolved("Apex", profile, include_device=True)

        self.shell._apply_wrapper_profile_snapshot.assert_not_called()
        self.assertTrue(dpg.does_item_exist(app_shell_module.APPLY_NO_RESTORE_POINT_MODAL))
        self.assertEqual(
            dpg.get_item_label(app_shell_module.APPLY_NO_RESTORE_POINT_MODAL),
            "No restore point",
        )
        self.assertTrue(
            dpg.does_item_exist(app_shell_module.APPLY_NO_RESTORE_POINT_CONTINUE_BUTTON)
        )

        dpg.get_item_callback(
            app_shell_module.APPLY_NO_RESTORE_POINT_CONTINUE_BUTTON
        )()

        self.shell._apply_wrapper_profile_snapshot.assert_called_once()
        self.assertTrue(
            self.shell._apply_wrapper_profile_snapshot.call_args.kwargs[
                "no_restore_point"
            ]
        )
        self.assertEqual(self.service.capture.call_count, 1)

    def test_capture_failure_cancel_writes_nothing_and_logs_cancel(self) -> None:
        dpg.create_context()
        self.addCleanup(dpg.destroy_context)
        self.shell._dpg_context_ready = True
        self.service.capture = MagicMock(return_value=None)
        self.shell._apply_wrapper_profile_snapshot = MagicMock()
        profile = self._profile(with_device=True)

        self.shell._apply_wrapper_profile_resolved("Apex", profile, include_device=True)
        dpg.get_item_callback(app_shell_module.APPLY_NO_RESTORE_POINT_CANCEL_BUTTON)()

        self.shell._apply_wrapper_profile_snapshot.assert_not_called()
        self.shell.device_service.log_i18n_event.assert_called_with("actions.cancel")


# ---------------------------------------------------------------------------
# before_safe_import_apply
# ---------------------------------------------------------------------------


class SafeImportHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.shell, self.service = _make_shell_with_recording_rp_service()

    def test_safe_import_path_fires_before_safe_import_apply(self) -> None:
        result = self.shell._create_safe_import_restore_point()
        self.assertEqual(_trigger_types(self.service), ["before_safe_import_apply"])
        self.assertIsNotNone(result)

    def test_safe_import_path_returns_none_when_service_missing(self) -> None:
        self.shell.restore_point_service = None
        result = self.shell._create_safe_import_restore_point()
        self.assertIsNone(result)

    def test_safe_import_path_no_longer_writes_to_wrapper_profile_store(self) -> None:
        # The old bridge persisted via wrapper_profile_store.save(); the new
        # path goes through RestorePointService.capture(). Verify the bridge
        # is gone — the wrapper-profile store must not receive a save call
        # from this path even when a restore point is created.
        self.shell.wrapper_profile_store.save.reset_mock()
        self.shell._create_safe_import_restore_point()
        self.shell.wrapper_profile_store.save.assert_not_called()


# ---------------------------------------------------------------------------
# before_manual_device_setting_write (debounce)
# ---------------------------------------------------------------------------


class ManualDeviceWriteHookTests(unittest.TestCase):
    """Drive the hook helper directly.

    Calling :meth:`AppShell.apply_step_size` / :meth:`apply_polling_rate`
    chains into :meth:`_record_settings_apply_result` +
    :meth:`refresh_shell`, both of which touch DPG / the title manager /
    ctypes layers that segfault at process exit when invoked without a
    real DPG context. The hook itself is what we want to test — the
    apply-method wrappers are thin orchestration around it.
    """

    def setUp(self) -> None:
        self.shell, self.service = _make_shell_with_recording_rp_service()

    def test_step_size_hook_fires_once_then_debounces(self) -> None:
        self.shell._maybe_capture_before_manual_device_write(field_key="step_size")
        self.shell._maybe_capture_before_manual_device_write(field_key="step_size")
        self.shell._maybe_capture_before_manual_device_write(field_key="step_size")
        captures = [trigger for trigger, _, _ in self.service.captures]
        self.assertEqual(captures, ["before_manual_device_setting_write"])

    def test_polling_rate_hook_fires_once_then_debounces(self) -> None:
        self.shell._maybe_capture_before_manual_device_write(field_key="polling_rate")
        self.shell._maybe_capture_before_manual_device_write(field_key="polling_rate")
        captures = [trigger for trigger, _, _ in self.service.captures]
        self.assertEqual(captures, ["before_manual_device_setting_write"])

    def test_separate_fields_dont_share_debounce_state(self) -> None:
        self.shell._maybe_capture_before_manual_device_write(field_key="step_size")
        self.shell._maybe_capture_before_manual_device_write(field_key="polling_rate")
        # Both should fire because the debounce is per-field-key.
        captures = [trigger for trigger, _, _ in self.service.captures]
        self.assertEqual(captures, [
            "before_manual_device_setting_write",
            "before_manual_device_setting_write",
        ])

    def test_same_device_window_still_debounces(self) -> None:
        self.shell.device_service.state.stable_identifier = "zd-unit-a"

        first_allowed, first_disclosed = self.shell._manual_device_write_gate(
            field_key="step_size", on_continue=lambda: None
        )
        second_allowed, second_disclosed = self.shell._manual_device_write_gate(
            field_key="step_size", on_continue=lambda: None
        )

        self.assertTrue(first_allowed)
        self.assertFalse(first_disclosed)
        self.assertTrue(second_allowed)
        self.assertFalse(second_disclosed)
        self.assertEqual(len(self.service.captures), 1)

    def test_a_to_b_swap_re_gates_restore_point_and_consent_windows(self) -> None:
        # Audit26 ST-F1: A's successful restore point and no-checkpoint
        # consent must not cover the same field on B during the seven-second
        # window. B's capture failure therefore reopens the explicit gate.
        self.shell.device_service.state.stable_identifier = "zd-unit-a"
        self.service.capture = MagicMock(
            side_effect=[SimpleNamespace(id="rp-a"), None]
        )
        self.shell._open_no_restore_point_confirm = MagicMock()

        allowed_a, disclosed_a = self.shell._manual_device_write_gate(
            field_key="step_size", on_continue=lambda: None
        )
        self.assertTrue(allowed_a)
        self.assertFalse(disclosed_a)
        self.shell._manual_no_restore_point_confirmed_for_field[
            ("zd-unit-a", "step_size")
        ] = app_shell_module.time.monotonic()

        self.shell.device_service.state.stable_identifier = "zd-unit-b"
        allowed_b, disclosed_b = self.shell._manual_device_write_gate(
            field_key="step_size", on_continue=lambda: None
        )

        self.assertFalse(allowed_b)
        self.assertFalse(disclosed_b)
        self.assertEqual(self.service.capture.call_count, 2)
        self.shell._open_no_restore_point_confirm.assert_called_once()
        self.assertEqual(self.shell._last_manual_rp_for_field, {})
        self.assertEqual(self.shell._manual_no_restore_point_confirmed_for_field, {})

    def test_disconnect_observation_clears_manual_write_windows(self) -> None:
        self.shell.device_service.state.connection_state = "connected"
        self.shell.device_service.state.stable_identifier = "zd-unit-a"
        self.shell._sync_manual_write_windows_to_device_state()
        scope_key = ("zd-unit-a", "step_size")
        self.shell._last_manual_rp_for_field[scope_key] = 1.0
        self.shell._manual_no_restore_point_confirmed_for_field[scope_key] = 1.0

        self.shell.device_service.state.connection_state = "no_device"
        self.shell.device_service.state.stable_identifier = "unknown"
        self.shell._sync_manual_write_windows_to_device_state()

        self.assertEqual(self.shell._last_manual_rp_for_field, {})
        self.assertEqual(self.shell._manual_no_restore_point_confirmed_for_field, {})

    def test_pending_consent_restarts_the_gate_after_an_identity_change(self) -> None:
        # The confirmation modal is asynchronous. If it was opened for A and B
        # arrives before Continue, only the normal B gate may resume the action.
        self.shell.device_service.state.stable_identifier = "zd-unit-a"
        self.service.capture = MagicMock(return_value=None)
        self.shell._open_no_restore_point_confirm = MagicMock()
        resumed_without_regate = MagicMock()
        re_gate = MagicMock()

        allowed, disclosed = self.shell._manual_device_write_gate(
            field_key="step_size",
            on_continue=resumed_without_regate,
            on_identity_changed=re_gate,
        )
        self.assertFalse(allowed)
        self.assertFalse(disclosed)
        continue_callback = self.shell._open_no_restore_point_confirm.call_args.kwargs[
            "on_continue"
        ]

        self.shell.device_service.state.stable_identifier = "zd-unit-b"
        continue_callback()

        resumed_without_regate.assert_not_called()
        re_gate.assert_called_once_with()

    def test_capture_fires_again_after_debounce_window_elapses(self) -> None:
        self.shell._maybe_capture_before_manual_device_write(field_key="step_size")
        # Rewind the debounce timestamp so the next call is past the window.
        scope_key = (
            self.shell.device_service.state.stable_identifier,
            "step_size",
        )
        self.shell._last_manual_rp_for_field[scope_key] -= (
            app_shell_module.MANUAL_DEVICE_WRITE_RP_WINDOW_S + 1.0
        )
        self.shell._maybe_capture_before_manual_device_write(field_key="step_size")
        captures = [trigger for trigger, _, _ in self.service.captures]
        self.assertEqual(len(captures), 2)

    def test_apply_step_size_source_contains_disclosed_gate_before_write(self) -> None:
        # Source-level smoke: the public ``apply_step_size`` entry point's
        # body must invoke the disclosed gate with ``field_key='step_size'``
        # BEFORE the write path dispatch.
        # Inspecting source avoids the segfault that DPG-less invocation of
        # the full apply chain triggers on Windows at process exit while
        # still proving the wiring. After the drag-storm-debounce work the actual
        # ``settings_service.set_step_size`` call lives in the extracted
        # ``_do_write_step_size`` helper; the dispatch from ``apply_step_size``
        # to that helper is the load-bearing post-hook anchor.
        import inspect
        src = inspect.getsource(self.shell.__class__.apply_step_size)
        hook_idx = src.find("_manual_device_write_gate")
        write_idx = src.find("_do_write_step_size(")
        self.assertGreater(hook_idx, 0, "step_size hook missing")
        self.assertGreater(write_idx, 0, "step_size write dispatch missing")
        self.assertLess(hook_idx, write_idx, "hook must fire BEFORE write")
        self.assertIn("field_key=\"step_size\"", src)

    def test_apply_polling_rate_source_contains_disclosed_gate_before_write(self) -> None:
        # See ``test_apply_step_size_source_contains_hook_call`` — same
        # rationale for inspecting the dispatch to ``_do_write_polling_rate``
        # instead of the inner ``settings_service.set_polling_rate`` call.
        import inspect
        src = inspect.getsource(self.shell.__class__.apply_polling_rate)
        hook_idx = src.find("_manual_device_write_gate")
        write_idx = src.find("_do_write_polling_rate(")
        self.assertGreater(hook_idx, 0, "polling_rate hook missing")
        self.assertGreater(write_idx, 0, "polling_rate write dispatch missing")
        self.assertLess(hook_idx, write_idx, "hook must fire BEFORE write")
        self.assertIn("field_key=\"polling_rate\"", src)

    def test_step_size_capture_failure_blocks_write_until_continue_and_marks_result(self) -> None:
        dpg.create_context()
        self.addCleanup(dpg.destroy_context)
        self.shell._dpg_context_ready = True
        self.shell._step_size_hydrated = True
        self.service.capture = MagicMock(return_value=None)
        self.shell.refresh_shell = MagicMock()
        self.shell._maybe_offer_save_step_size_to_profile = MagicMock()
        self.shell._record_settings_apply_result = MagicMock()

        self.shell.apply_step_size(42)

        self.shell.settings_service.set_step_size.assert_not_called()
        self.assertTrue(dpg.does_item_exist(app_shell_module.APPLY_NO_RESTORE_POINT_MODAL))

        dpg.get_item_callback(
            app_shell_module.APPLY_NO_RESTORE_POINT_CONTINUE_BUTTON
        )()

        self.shell.settings_service.set_step_size.assert_called_once_with(42)
        self.assertTrue(
            self.shell._record_settings_apply_result.call_args.kwargs[
                "no_restore_point"
            ]
        )

    def test_step_size_capture_failure_cancel_writes_nothing(self) -> None:
        dpg.create_context()
        self.addCleanup(dpg.destroy_context)
        self.shell._dpg_context_ready = True
        self.shell._step_size_hydrated = True
        self.service.capture = MagicMock(return_value=None)

        self.shell.apply_step_size(42)
        dpg.get_item_callback(app_shell_module.APPLY_NO_RESTORE_POINT_CANCEL_BUTTON)()

        self.shell.settings_service.set_step_size.assert_not_called()
        self.shell.device_service.log_i18n_event.assert_called_with("actions.cancel")

    def test_disclosed_result_appends_note_and_emits_activity_event(self) -> None:
        self.shell._update_apply_status = MagicMock()

        self.shell._record_settings_apply_result(
            True,
            "Applied setting.",
            no_restore_point=True,
        )

        recorded = self.shell.device_service.record_apply_result.call_args.args[1]
        self.assertIsInstance(recorded, ComposedLogEntry)
        self.assertIn(
            "Note: no restore point was created before this apply.",
            render_log_message(recorded),
        )
        self.shell.device_service.log_i18n_event.assert_not_called()


# ---------------------------------------------------------------------------
# first_readable_connect
# ---------------------------------------------------------------------------


class FirstReadableConnectHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.shell, self.service = _make_shell_with_recording_rp_service()

    def test_first_refresh_fires_first_readable_connect_once(self) -> None:
        # Drive the helper directly — refresh_from_controller's full path
        # requires a real settings_service. The hook is the public surface.
        self.shell._maybe_capture_first_readable_connect()
        self.shell._maybe_capture_first_readable_connect()
        self.shell._maybe_capture_first_readable_connect()
        captures = [trigger for trigger, _, _ in self.service.captures]
        self.assertEqual(captures, ["first_readable_connect"])

    def test_different_identity_fires_again(self) -> None:
        self.shell._maybe_capture_first_readable_connect()
        # Simulate identity change (different product_string).
        self.shell.device_service.state.product_name = "Other Controller"
        self.shell._maybe_capture_first_readable_connect()
        captures = [trigger for trigger, _, _ in self.service.captures]
        self.assertEqual(captures, ["first_readable_connect", "first_readable_connect"])


# ---------------------------------------------------------------------------
# Manual button (RPU5)
# ---------------------------------------------------------------------------


class ManualButtonHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.shell, self.service = _make_shell_with_recording_rp_service()

    def test_manual_save_restore_point_fires_manual_trigger(self) -> None:
        rp = self.shell.manual_save_restore_point()
        self.assertIsNotNone(rp)
        captures = [trigger for trigger, _, _ in self.service.captures]
        self.assertEqual(captures, ["manual"])

    def test_manual_save_restore_point_forwards_title(self) -> None:
        self.shell.manual_save_restore_point(title="My checkpoint")
        triggers_titles = [(trigger, title) for trigger, title, _ in self.service.captures]
        self.assertEqual(triggers_titles, [("manual", "My checkpoint")])

    def test_manual_save_returns_none_without_service(self) -> None:
        self.shell.restore_point_service = None
        result = self.shell.manual_save_restore_point()
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Defensive: capture exceptions never escape the apply path
# ---------------------------------------------------------------------------


class CaptureExceptionContainmentTests(unittest.TestCase):
    def test_capture_exception_is_swallowed_by_helper(self) -> None:
        shell, _ = _make_shell_with_recording_rp_service()
        shell.restore_point_service.capture = MagicMock(side_effect=RuntimeError("hid timeout"))
        # The hook helper must not raise (the broad except / log path is
        # the load-bearing contract that prevents a transient HID issue
        # from breaking apply / refresh / manual-button flows).
        shell._maybe_capture_before_manual_device_write(field_key="step_size")
        # The debounce timestamp should NOT have been set (capture returned
        # None), so a follow-up call would still attempt capture — that's
        # the intentional retry-on-transient-failure behavior.
        self.assertNotIn(
            (shell.device_service.state.stable_identifier, "step_size"),
            shell._last_manual_rp_for_field,
        )

    def test_first_connect_helper_swallows_capture_exception(self) -> None:
        shell, _ = _make_shell_with_recording_rp_service()
        shell.restore_point_service.capture = MagicMock(side_effect=RuntimeError("hid timeout"))
        shell._maybe_capture_first_readable_connect()
        # The session-set should NOT have grown because capture returned
        # None — same retry-on-transient logic as the manual-write debounce.
        self.assertEqual(shell._first_connect_captured, set())

    def test_manual_save_swallows_capture_exception(self) -> None:
        shell, _ = _make_shell_with_recording_rp_service()
        shell.restore_point_service.capture = MagicMock(side_effect=RuntimeError("hid timeout"))
        result = shell.manual_save_restore_point()
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
