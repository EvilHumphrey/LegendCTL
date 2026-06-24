"""Tests for SettingsApplyCoordinator.

Coordinator was extracted from zd_app/ui/app_shell.py. These tests prove the
seam: aggregation, retry-bookkeeping, callback ordering, and DPG-freedom.
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from zd_app.services.settings_apply_coordinator import (
    ApplyFailure,
    ApplyResult,
    SettingsApplyCoordinator,
    outcome_is_success,
)
from zd_app.services.settings_service import (
    AxisInversion,
    BackPaddleBinding,
    ButtonMapping,
    ButtonSlot,
    ControllerSnapshot,
    LightingMode,
    LightingSettings,
    LightingZone,
    MacroSlot,
    PollingRate,
    RgbColor,
    SensitivityAnchor,
    SetPollingRateOutcome,
    SetPollingRateResult,
    SetStepSizeOutcome,
    SetStepSizeResult,
    SettingsService,
    StickDeadzones,
    TriggerMode,
    TriggerSettings,
    TriggerVibrationMode,
    VibrationSettings,
)


def _polling_result(outcome: SetPollingRateOutcome, error_code: int | None = None) -> SetPollingRateResult:
    return SetPollingRateResult(
        outcome=outcome,
        rate=PollingRate.HZ_1000,
        error_code=error_code,
        bytes_written=65,
        payload_hex="00",
        elapsed_ms=1,
    )


def _empty_snapshot(**overrides) -> ControllerSnapshot:
    base = dict(
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
    )
    base.update(overrides)
    return ControllerSnapshot(**base)


def _polling_only_snapshot() -> ControllerSnapshot:
    return _empty_snapshot(polling_rate=PollingRate.HZ_1000)


def _back_paddle_only_snapshot() -> ControllerSnapshot:
    return _empty_snapshot(
        back_paddle_bindings={MacroSlot.M1: BackPaddleBinding(target=None)},
    )


class CoordinatorAggregationTests(unittest.TestCase):
    def test_apply_snapshot_aggregates_per_setting_outcomes(self) -> None:
        settings_service = mock.Mock()
        settings_service.set_polling_rate.return_value = _polling_result(SetPollingRateOutcome.OK_WITH_RETRY)
        settings_service.set_button_binding.return_value = _polling_result(SetPollingRateOutcome.OK)

        coordinator = SettingsApplyCoordinator(settings_service)
        snapshot = _empty_snapshot(
            polling_rate=PollingRate.HZ_1000,
            button_bindings={ButtonSlot.A: ButtonMapping(), ButtonSlot.B: ButtonMapping()},
        )

        result = coordinator.apply_snapshot(snapshot)

        self.assertEqual(result.total_attempted, 3)  # polling + 2 button bindings
        self.assertEqual(result.succeeded, 3)
        self.assertEqual(result.retry_recoveries, 1)  # the polling write used retry
        self.assertEqual(result.failed, [])

    def test_apply_failure_carries_transient_flag(self) -> None:
        settings_service = mock.Mock()
        settings_service.set_polling_rate.return_value = _polling_result(
            SetPollingRateOutcome.WRITE_FAILED, error_code=1167
        )

        coordinator = SettingsApplyCoordinator(settings_service)
        result = coordinator.apply_snapshot(_polling_only_snapshot())

        self.assertEqual(result.total_attempted, 1)
        self.assertEqual(result.succeeded, 0)
        self.assertEqual(len(result.failed), 1)
        failure = result.failed[0]
        self.assertEqual(failure.setting_label, "polling")
        self.assertTrue(failure.is_transient, "WRITE_FAILED outcome should be flagged transient")

    def test_apply_failure_open_failed_is_not_transient(self) -> None:
        """Sanity: only WRITE_FAILED is transient; other failure outcomes are not."""
        settings_service = mock.Mock()
        settings_service.set_polling_rate.return_value = _polling_result(
            SetPollingRateOutcome.OPEN_FAILED
        )

        coordinator = SettingsApplyCoordinator(settings_service)
        result = coordinator.apply_snapshot(_polling_only_snapshot())

        self.assertEqual(len(result.failed), 1)
        self.assertFalse(result.failed[0].is_transient)


class CoordinatorBackPaddleTests(unittest.TestCase):
    def test_legacy_snapshot_without_back_paddle_skips_those_writes(self) -> None:
        """Profiles missing back_paddle_bindings must NOT generate paddle writes."""
        settings_service = mock.Mock()
        settings_service.set_polling_rate.return_value = _polling_result(SetPollingRateOutcome.OK)

        coordinator = SettingsApplyCoordinator(settings_service)
        snapshot = _empty_snapshot(polling_rate=PollingRate.HZ_1000)
        # back_paddle_bindings intentionally not passed (legacy profile shape)

        result = coordinator.apply_snapshot(snapshot)

        self.assertEqual(result.total_attempted, 1)
        settings_service.set_back_paddle_binding.assert_not_called()

    def test_back_paddle_on_success_callback_fires_on_ok_outcome(self) -> None:
        settings_service = mock.Mock()
        settings_service.set_back_paddle_binding.return_value = _polling_result(
            SetPollingRateOutcome.OK
        )

        coordinator = SettingsApplyCoordinator(settings_service)
        captured: list[tuple[MacroSlot, BackPaddleBinding]] = []
        snapshot = _back_paddle_only_snapshot()

        coordinator.apply_snapshot(
            snapshot,
            on_back_paddle_apply=lambda slot, binding: captured.append((slot, binding)),
        )

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], MacroSlot.M1)


class CoordinatorCallbacksTests(unittest.TestCase):
    def test_callbacks_fire_in_order(self) -> None:
        events: list[str] = []

        settings_service = mock.Mock()

        def _record_polling_call(*args, **kwargs):
            events.append("write")
            return _polling_result(SetPollingRateOutcome.OK)

        settings_service.set_polling_rate.side_effect = _record_polling_call

        coordinator = SettingsApplyCoordinator(
            settings_service,
            on_apply_started=lambda: events.append("started"),
            on_apply_finished=lambda r: events.append("finished"),
        )

        coordinator.apply_snapshot(_polling_only_snapshot())

        self.assertEqual(events, ["started", "write", "finished"])

    def test_callbacks_fire_even_when_settings_service_is_none(self) -> None:
        events: list[str] = []
        coordinator = SettingsApplyCoordinator(
            None,
            on_apply_started=lambda: events.append("started"),
            on_apply_finished=lambda r: events.append("finished"),
        )

        result = coordinator.apply_snapshot(_polling_only_snapshot())

        self.assertEqual(events, ["started", "finished"])
        self.assertEqual(result.total_attempted, 0)
        self.assertEqual(len(result.failed), 1)


class CoordinatorRetryTests(unittest.TestCase):
    def test_retry_failures_uses_retry_fn(self) -> None:
        coordinator = SettingsApplyCoordinator(mock.Mock())
        retry_fn = mock.Mock(return_value=_polling_result(SetPollingRateOutcome.OK))
        on_success = mock.Mock()

        failures = [
            ApplyFailure(
                setting_label="polling",
                error="transient",
                is_transient=True,
                retry_fn=retry_fn,
                on_success=on_success,
            )
        ]

        retry_result = coordinator.retry_failures(failures)

        retry_fn.assert_called_once()
        on_success.assert_called_once()
        self.assertEqual(retry_result.total_attempted, 1)
        self.assertEqual(retry_result.succeeded, 1)
        self.assertEqual(retry_result.failed, [])

    def test_retry_failures_empty_returns_empty_result(self) -> None:
        coordinator = SettingsApplyCoordinator(mock.Mock())
        retry_result = coordinator.retry_failures([])
        self.assertEqual(retry_result.total_attempted, 0)
        self.assertEqual(retry_result.failed, [])


class CoordinatorExceptionPathTests(unittest.TestCase):
    """Cover the setter-exception and listener-exception branches of the apply pipeline.

    The contract: a setter exception becomes an ApplyFailure with the exception
    text and a retry_fn pointing at the original write. A listener exception
    is logged at WARNING level and swallowed so the rest of the snapshot keeps
    applying. The reasoning is in the coordinator's _safe_invoke comment.
    """

    def test_on_apply_started_callback_raise_is_swallowed(self) -> None:
        """Completes the _safe_invoke coverage at apply_snapshot's start hook."""
        settings_service = mock.Mock()
        settings_service.set_polling_rate.return_value = _polling_result(SetPollingRateOutcome.OK)
        raising_started = mock.Mock(side_effect=RuntimeError("started boom"))

        coordinator = SettingsApplyCoordinator(
            settings_service,
            on_apply_started=raising_started,
        )

        with self.assertLogs("zd_app.services.settings_apply_coordinator", level="WARNING") as captured:
            result = coordinator.apply_snapshot(_polling_only_snapshot())

        raising_started.assert_called_once()
        self.assertEqual(result.succeeded, 1)
        settings_service.set_polling_rate.assert_called_once()
        self.assertTrue(any("on_apply_started" in msg and "started boom" in msg for msg in captured.output))

    def test_on_apply_finished_raise_in_no_service_branch_is_swallowed(self) -> None:
        """Covers the settings_service=None early-return _safe_invoke site."""
        raising_finished = mock.Mock(side_effect=RuntimeError("no-service boom"))
        coordinator = SettingsApplyCoordinator(
            settings_service=None,
            on_apply_finished=raising_finished,
        )

        with self.assertLogs("zd_app.services.settings_apply_coordinator", level="WARNING") as captured:
            result = coordinator.apply_snapshot(_polling_only_snapshot())

        raising_finished.assert_called_once()
        self.assertEqual(result.total_attempted, 0)
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(result.failed[0].setting_label, "settings_service")
        self.assertEqual(result.failed[0].error, "apply.failure.error.not_available")
        self.assertTrue(any("on_apply_finished" in msg and "no-service boom" in msg for msg in captured.output))

    def test_apply_snapshot_handles_setter_exception(self) -> None:
        settings_service = mock.Mock()
        settings_service.set_polling_rate.side_effect = RuntimeError("HID write failed")

        coordinator = SettingsApplyCoordinator(settings_service)
        result = coordinator.apply_snapshot(_polling_only_snapshot())

        self.assertEqual(result.total_attempted, 1)
        self.assertEqual(result.succeeded, 0)
        self.assertEqual(len(result.failed), 1)
        failure = result.failed[0]
        self.assertEqual(failure.setting_label, "polling")
        self.assertIn("HID write failed", failure.error)
        self.assertFalse(failure.is_transient)
        self.assertIsNotNone(failure.retry_fn)

    def test_on_apply_finished_callback_raise_is_swallowed(self) -> None:
        settings_service = mock.Mock()
        settings_service.set_polling_rate.return_value = _polling_result(SetPollingRateOutcome.OK)
        raising_finished = mock.Mock(side_effect=RuntimeError("listener boom"))

        coordinator = SettingsApplyCoordinator(
            settings_service,
            on_apply_finished=raising_finished,
        )

        with self.assertLogs("zd_app.services.settings_apply_coordinator", level="WARNING") as captured:
            result = coordinator.apply_snapshot(_polling_only_snapshot())

        raising_finished.assert_called_once()
        self.assertEqual(result.succeeded, 1)
        self.assertTrue(any("on_apply_finished" in msg and "listener boom" in msg for msg in captured.output))

    def test_on_back_paddle_apply_callback_raise_does_not_skip_subsequent_writes(self) -> None:
        settings_service = mock.Mock()
        settings_service.set_back_paddle_binding.return_value = _polling_result(SetPollingRateOutcome.OK)
        applied: list[MacroSlot] = []

        def callback(slot: MacroSlot, _binding: BackPaddleBinding) -> None:
            applied.append(slot)
            if slot is MacroSlot.M1:
                raise RuntimeError("listener error on M1")

        coordinator = SettingsApplyCoordinator(settings_service)
        snapshot = _empty_snapshot(
            back_paddle_bindings={
                MacroSlot.M1: BackPaddleBinding(target=None),
                MacroSlot.M2: BackPaddleBinding(target=None),
            },
        )

        with self.assertLogs("zd_app.services.settings_apply_coordinator", level="WARNING"):
            result = coordinator.apply_snapshot(snapshot, on_back_paddle_apply=callback)

        self.assertEqual(applied, [MacroSlot.M1, MacroSlot.M2])
        self.assertEqual(result.succeeded, 2)
        self.assertEqual(settings_service.set_back_paddle_binding.call_count, 2)

    def test_retry_failures_handles_on_success_raise(self) -> None:
        coordinator = SettingsApplyCoordinator(mock.Mock())
        retry_results = [
            _polling_result(SetPollingRateOutcome.OK),
            _polling_result(SetPollingRateOutcome.OK),
        ]
        retry_fn_a = mock.Mock(return_value=retry_results[0])
        retry_fn_b = mock.Mock(return_value=retry_results[1])
        raising_on_success = mock.Mock(side_effect=RuntimeError("on_success boom"))
        ok_on_success = mock.Mock()

        failures = [
            ApplyFailure(
                setting_label="polling",
                error="transient",
                is_transient=True,
                retry_fn=retry_fn_a,
                on_success=raising_on_success,
            ),
            ApplyFailure(
                setting_label="vibration",
                error="transient",
                is_transient=True,
                retry_fn=retry_fn_b,
                on_success=ok_on_success,
            ),
        ]

        with self.assertLogs("zd_app.services.settings_apply_coordinator", level="WARNING"):
            retry_result = coordinator.retry_failures(failures)

        retry_fn_a.assert_called_once()
        retry_fn_b.assert_called_once()
        raising_on_success.assert_called_once()
        ok_on_success.assert_called_once()
        self.assertEqual(retry_result.total_attempted, 2)
        self.assertEqual(retry_result.succeeded, 2)
        self.assertEqual(retry_result.failed, [])


class NoOutcomeWiringTests(unittest.TestCase):
    """A write_fn returning no outcome is a failure.

    ``outcome_is_success(None)`` used to return True, so a miswired write_fn
    (returning None, or an object without an ``outcome`` attribute) was
    silently counted as a SUCCESS in apply results. Production setters always
    return a Set*Result with a real outcome enum; the None tolerance only
    masked wiring bugs.
    """

    def test_none_outcome_is_not_success(self) -> None:
        self.assertIs(outcome_is_success(None), False)

    def test_write_fn_returning_object_without_outcome_lands_in_failed(self) -> None:
        settings_service = mock.Mock()
        settings_service.set_polling_rate.return_value = object()  # no .outcome

        coordinator = SettingsApplyCoordinator(settings_service)
        result = coordinator.apply_snapshot(_polling_only_snapshot())

        self.assertEqual(result.total_attempted, 1)
        self.assertEqual(result.succeeded, 0)
        self.assertEqual(len(result.failed), 1)
        failure = result.failed[0]
        self.assertEqual(failure.setting_label, "polling")
        self.assertEqual(failure.error, "write returned no outcome")
        self.assertFalse(failure.is_transient)
        self.assertIsNotNone(failure.retry_fn)

    def test_retry_fn_returning_none_lands_in_failed(self) -> None:
        coordinator = SettingsApplyCoordinator(mock.Mock())
        failures = [
            ApplyFailure(
                setting_label="polling",
                error="transient",
                is_transient=True,
                retry_fn=mock.Mock(return_value=None),
            )
        ]

        retry_result = coordinator.retry_failures(failures)

        self.assertEqual(retry_result.total_attempted, 1)
        self.assertEqual(retry_result.succeeded, 0)
        self.assertEqual(len(retry_result.failed), 1)
        self.assertEqual(retry_result.failed[0].error, "write returned no outcome")


class CoordinatorDpgFreeTests(unittest.TestCase):
    def test_coordinator_module_does_not_import_dpg(self) -> None:
        """Coordinator must stay DPG-free so it can be unit-tested without a DPG context."""
        # Re-import in a clean subprocess-equivalent: drop dearpygui from sys.modules first,
        # then verify importing the coordinator does not pull it back in.
        for name in [n for n in list(sys.modules) if "dearpygui" in n]:
            sys.modules.pop(name, None)
        for name in [n for n in list(sys.modules) if n == "zd_app.services.settings_apply_coordinator"]:
            sys.modules.pop(name, None)

        import zd_app.services.settings_apply_coordinator  # noqa: F401

        for name in sys.modules:
            self.assertFalse(
                name.startswith("dearpygui"),
                f"importing the coordinator pulled in {name}; should be DPG-free",
            )


def _step_size_result(outcome: SetStepSizeOutcome = SetStepSizeOutcome.OK, value: int = 131) -> SetStepSizeResult:
    return SetStepSizeResult(
        outcome=outcome,
        value=value,
        error_code=None,
        bytes_written=65,
        payload_hex="00",
        elapsed_ms=1,
    )


def _real_service_with_raising_readback() -> SettingsService:
    """A real SettingsService whose every HID read-back RAISES a TimeoutError.

    Drives the REAL set_step_size_verified through the coordinator so the test
    exercises the exception-safe read-back path (the crash fix) end-to-end, not
    a mock. Writes succeed; only reads raise -- the on-hardware failure mode.
    """
    handle = 0x1234

    def _read_file(_handle: int, _length: int, timeout_ms: int) -> bytes:
        raise TimeoutError(f"HID read timed out after {timeout_ms}ms")

    clock = {"t": 0.0}

    def _clock() -> float:
        clock["t"] += 0.001
        return clock["t"]

    return SettingsService(
        enumerate_paths=lambda: ["fake-hid-path"],
        open_write_handle=lambda _p: (handle, 0),
        open_read_write_handle=lambda _p: (handle, 0),
        write_file=lambda _h, payload: (True, 0, len(payload)),
        read_file=_read_file,
        close_handle=lambda _h: True,
        clock=_clock,
        sleep=lambda _s: None,
    )


class CoordinatorStepSizeTrailerTests(unittest.TestCase):
    """The step_size write is deferred to AFTER the rest of the burst.

    Background: hardware testing on 2026-05-25 showed the firmware silently
    rejects step_size (cat 0x0d) writes inside multi-field bursts at ~67% rate.
    Deferring to a trailer after a short settle restores 100% commit.
    """

    def test_step_size_writes_after_all_other_settings(self) -> None:
        """When the snapshot has multiple fields, step_size must be the LAST setter call."""
        settings_service = mock.Mock()
        settings_service.set_polling_rate.return_value = _polling_result(SetPollingRateOutcome.OK)
        settings_service.set_all_deadzones.return_value = _polling_result(SetPollingRateOutcome.OK)
        settings_service.set_button_binding.return_value = _polling_result(SetPollingRateOutcome.OK)
        settings_service.set_step_size_verified.return_value = _step_size_result()

        coordinator = SettingsApplyCoordinator(
            settings_service,
            step_size_trailer_delay_s=0.0,  # no real sleep in tests
        )
        snapshot = _empty_snapshot(
            polling_rate=PollingRate.HZ_1000,
            deadzones=StickDeadzones(left_center=0, right_center=0, left_outer=0, right_outer=0),
            button_bindings={ButtonSlot.A: ButtonMapping()},
            step_size=131,
        )

        result = coordinator.apply_snapshot(snapshot)

        # All four writes happened.
        self.assertEqual(result.total_attempted, 4)
        self.assertEqual(result.succeeded, 4)
        # Call-order assertion: step_size is the LAST setter invoked. It now goes
        # through the verified setter (write + read-back) per the revert-to-1 fix.
        method_calls = [c[0] for c in settings_service.method_calls]
        self.assertEqual(method_calls[-1], "set_step_size_verified")
        # And the earlier writes precede it.
        self.assertIn("set_polling_rate", method_calls[:-1])
        self.assertIn("set_all_deadzones", method_calls[:-1])
        self.assertIn("set_button_binding", method_calls[:-1])

    def test_step_size_trailer_sleeps_before_write_when_delay_positive(self) -> None:
        """A positive trailer delay must invoke time.sleep before the step_size write."""
        settings_service = mock.Mock()
        settings_service.set_step_size_verified.return_value = _step_size_result()

        coordinator = SettingsApplyCoordinator(
            settings_service,
            step_size_trailer_delay_s=0.123,
        )
        snapshot = _empty_snapshot(step_size=131)

        with mock.patch("zd_app.services.settings_apply_coordinator.time.sleep") as sleep:
            result = coordinator.apply_snapshot(snapshot)

        self.assertEqual(result.total_attempted, 1)
        self.assertEqual(result.succeeded, 1)
        sleep.assert_called_once_with(0.123)
        settings_service.set_step_size_verified.assert_called_once_with(131)

    def test_step_size_trailer_skips_sleep_when_delay_zero(self) -> None:
        """A zero (or negative) trailer delay must NOT invoke time.sleep."""
        settings_service = mock.Mock()
        settings_service.set_step_size_verified.return_value = _step_size_result()

        coordinator = SettingsApplyCoordinator(
            settings_service,
            step_size_trailer_delay_s=0.0,
        )
        snapshot = _empty_snapshot(step_size=131)

        with mock.patch("zd_app.services.settings_apply_coordinator.time.sleep") as sleep:
            coordinator.apply_snapshot(snapshot)

        sleep.assert_not_called()
        settings_service.set_step_size_verified.assert_called_once_with(131)

    def test_step_size_trailer_skipped_when_snapshot_step_size_is_none(self) -> None:
        """No trailer write (and no sleep) when snapshot.step_size is None."""
        settings_service = mock.Mock()
        settings_service.set_polling_rate.return_value = _polling_result(SetPollingRateOutcome.OK)

        coordinator = SettingsApplyCoordinator(
            settings_service,
            step_size_trailer_delay_s=0.1,
        )
        snapshot = _empty_snapshot(polling_rate=PollingRate.HZ_1000)
        # step_size left as None.

        with mock.patch("zd_app.services.settings_apply_coordinator.time.sleep") as sleep:
            result = coordinator.apply_snapshot(snapshot)

        self.assertEqual(result.total_attempted, 1)
        sleep.assert_not_called()
        settings_service.set_step_size_verified.assert_not_called()

    def test_step_size_trailer_records_failure_into_apply_result(self) -> None:
        """A failing step_size trailer write must surface as an ApplyFailure."""
        settings_service = mock.Mock()
        settings_service.set_step_size_verified.return_value = _step_size_result(
            outcome=SetStepSizeOutcome.WRITE_FAILED,
            value=131,
        )

        coordinator = SettingsApplyCoordinator(
            settings_service,
            step_size_trailer_delay_s=0.0,
        )
        snapshot = _empty_snapshot(step_size=131)

        result = coordinator.apply_snapshot(snapshot)

        self.assertEqual(result.total_attempted, 1)
        self.assertEqual(result.succeeded, 0)
        self.assertEqual(len(result.failed), 1)
        failure = result.failed[0]
        self.assertEqual(failure.setting_label, "step_size")
        # WRITE_FAILED is transient per the existing convention.
        self.assertTrue(failure.is_transient)

    def test_step_size_trailer_default_delay_is_100ms(self) -> None:
        """Default trailer delay matches the 100ms hardware-validated commit threshold."""
        coordinator = SettingsApplyCoordinator(mock.Mock())
        self.assertAlmostEqual(coordinator._step_size_trailer_delay_s, 0.1)

    def test_step_size_trailer_uses_verified_setter(self) -> None:
        """The apply trailer must drive step_size through set_step_size_verified.

        Revert-to-1 fix (2026-06-23): a plain set_step_size could be silently
        rejected by the firmware with nothing detecting it. The apply path must
        call the verify-and-retry setter and never the unverified one.
        """
        settings_service = mock.Mock()
        settings_service.set_step_size_verified.return_value = _step_size_result()

        coordinator = SettingsApplyCoordinator(
            settings_service,
            step_size_trailer_delay_s=0.0,
        )
        coordinator.apply_snapshot(_empty_snapshot(step_size=131))

        settings_service.set_step_size_verified.assert_called_once_with(131)
        settings_service.set_step_size.assert_not_called()

    def test_step_size_trailer_survives_readback_timeout_no_crash(self) -> None:
        """The verified trailer must survive a read-back that RAISES (no crash).

        Crash regression: the verify read-back can raise a
        TimeoutError on real hardware. Driven through the REAL verified setter
        (not a mock), the apply must complete without the exception escaping, and
        the step_size row must DEGRADE to success -- the write itself committed at
        the seam, only the read-back failed -- rather than a false failure. Proves
        fix 1 (exception-safe set_step_size_verified) composes with the trailer
        that fix 2 deliberately left on the verified setter.
        """
        service = _real_service_with_raising_readback()
        # Spy that still runs the REAL method, so we prove the trailer used it.
        service.set_step_size_verified = mock.Mock(wraps=service.set_step_size_verified)

        coordinator = SettingsApplyCoordinator(
            service,
            step_size_trailer_delay_s=0.0,  # no real sleep in tests
        )

        # Must NOT raise (the crash). On base, the read-back TimeoutError escapes
        # the verified setter; the coordinator's write-wrapper catches it and
        # records a FAILURE, so succeeded==0 there instead of the degraded 1.
        result = coordinator.apply_snapshot(_empty_snapshot(step_size=146))

        service.set_step_size_verified.assert_called_once_with(146)
        self.assertEqual(result.succeeded, 1)
        self.assertEqual(result.failed, [])

    def test_step_size_verify_failure_surfaces_as_apply_failure(self) -> None:
        """A VERIFY_FAILED step_size outcome must land as an ApplyFailure row.

        This is the "device silently reverted to 1" case: the write seam said OK
        but the read-back never matched, so the verified setter returns
        VERIFY_FAILED. The apply result must show it failed (not a false success)
        and it must NOT be flagged transient (it already exhausted its retries).
        """
        settings_service = mock.Mock()
        settings_service.set_step_size_verified.return_value = _step_size_result(
            outcome=SetStepSizeOutcome.VERIFY_FAILED,
            value=131,
        )

        coordinator = SettingsApplyCoordinator(
            settings_service,
            step_size_trailer_delay_s=0.0,
        )
        result = coordinator.apply_snapshot(_empty_snapshot(step_size=131))

        self.assertEqual(result.succeeded, 0)
        self.assertEqual(len(result.failed), 1)
        failure = result.failed[0]
        self.assertEqual(failure.setting_label, "step_size")
        self.assertFalse(
            failure.is_transient,
            "VERIFY_FAILED already retried internally -- not transient",
        )


# Helpers for building vulnerable-field deltas used by CoordinatorFieldTrailerTests.
def _vibration_delta() -> VibrationSettings:
    return VibrationSettings(
        left_grip_strength=1,
        right_grip_strength=1,
        left_trigger_motor_strength=1,
        right_trigger_motor_strength=1,
        mode=TriggerVibrationMode.NATIVE,
    )


def _deadzones_delta() -> StickDeadzones:
    return StickDeadzones(left_center=1, right_center=1, left_outer=1, right_outer=1)


def _sensitivity_delta() -> tuple[SensitivityAnchor, SensitivityAnchor, SensitivityAnchor]:
    return (
        SensitivityAnchor(x=25, y=25),
        SensitivityAnchor(x=50, y=51),
        SensitivityAnchor(x=75, y=75),
    )


def _trigger_delta() -> TriggerSettings:
    return TriggerSettings(range_min=1, range_max=80, mode=TriggerMode.LONG)


def _lighting_delta() -> LightingSettings:
    return LightingSettings(
        light_on=True,
        mode=LightingMode.ALWAYS_ON,
        brightness_byte=128,
        color=RgbColor(r=1, g=0, b=0),
    )


def _full_vulnerable_snapshot(**overrides) -> ControllerSnapshot:
    """Snapshot with all 10 vulnerable fields set to deltas (no polling/step_size).

    axis_inversion was reclassified into the vulnerable bucket on 2026-05-25
    after the axis_inversion investigation found the prior 0% reading
    was a read-offset bug, not a write failure. Writes share the same in-
    burst-cascade band as the other 8 fields.
    """
    base = dict(
        vibration=_vibration_delta(),
        deadzones=_deadzones_delta(),
        axis_inversion_left=AxisInversion(x_inverted=True, y_inverted=False),
        axis_inversion_right=AxisInversion(x_inverted=False, y_inverted=True),
        sensitivity_left=_sensitivity_delta(),
        sensitivity_right=_sensitivity_delta(),
        trigger_left=_trigger_delta(),
        trigger_right=_trigger_delta(),
        button_bindings={ButtonSlot.A: ButtonMapping(), ButtonSlot.B: ButtonMapping()},
        lighting_zones={LightingZone.HOME: _lighting_delta()},
    )
    base.update(overrides)
    return _empty_snapshot(**base)


# Names of the setter methods invoked for each vulnerable field (used for
# call-order assertions). Ordered to mirror apply_snapshot's trailer sequence.
_VULNERABLE_SETTER_NAMES = (
    "set_vibration",
    "set_all_deadzones",
    "set_left_stick_inversion",
    "set_right_stick_inversion",
    "set_left_stick_sensitivity_curve",
    "set_right_stick_sensitivity_curve",
    "set_left_trigger_settings",
    "set_right_trigger_settings",
    "set_button_binding",
    "set_zone_lighting",
)


class CoordinatorFieldTrailerTests(unittest.TestCase):
    """The 8 vulnerable fields are deferred to per-field trailers.

    Background: hardware testing found 8 of 9 testable in-burst-
    eligible fields share the step_size firmware-rejection quirk (commit rates
    14-90% in burst). A bulk-batch trailer alternative was tested + rejected —
    the cascade re-triggers in the back-to-back batch and
    sensitivity_right drops to 14%. The chosen fix: each
    vulnerable field is preceded by a ``field_trailer_delay_s`` sleep so the
    firmware has a quiet window before the next write. polling_rate stays in
    the main burst (clean in burst). axis_inversion and back_paddle_bindings
    keep their original main-burst positions as out of scope here.
    """

    def _coordinator_with_all_setters_ok(self, **kwargs) -> tuple[mock.Mock, SettingsApplyCoordinator]:
        settings_service = mock.Mock()
        # Mirror the setter return values the apply pipeline checks. We use
        # the polling-result shape for everything — _make_writer only reads
        # ``outcome`` from the result, so any setter outcome works as long as
        # ``outcome_is_success`` returns True.
        ok = _polling_result(SetPollingRateOutcome.OK)
        for name in (
            "set_polling_rate",
            "set_vibration",
            "set_all_deadzones",
            "set_left_stick_inversion",
            "set_right_stick_inversion",
            "set_left_stick_sensitivity_curve",
            "set_right_stick_sensitivity_curve",
            "set_left_trigger_settings",
            "set_right_trigger_settings",
            "set_button_binding",
            "set_zone_lighting",
            "set_back_paddle_binding",
        ):
            getattr(settings_service, name).return_value = ok
        settings_service.set_step_size_verified.return_value = _step_size_result()
        coordinator = SettingsApplyCoordinator(
            settings_service,
            step_size_trailer_delay_s=kwargs.pop("step_size_trailer_delay_s", 0.0),
            field_trailer_delay_s=kwargs.pop("field_trailer_delay_s", 0.0),
            **kwargs,
        )
        return settings_service, coordinator

    def test_vulnerable_fields_write_after_polling_and_before_step_size(self) -> None:
        """polling_rate first; the 8 vulnerable setters between; step_size last."""
        settings_service, coordinator = self._coordinator_with_all_setters_ok()
        snapshot = _full_vulnerable_snapshot(
            polling_rate=PollingRate.HZ_1000,
            step_size=131,
        )

        coordinator.apply_snapshot(snapshot)

        method_calls = [c[0] for c in settings_service.method_calls]
        # polling first, step_size last (now via the verified setter)
        self.assertEqual(method_calls[0], "set_polling_rate")
        self.assertEqual(method_calls[-1], "set_step_size_verified")
        # Every vulnerable setter appears strictly between polling (index 0)
        # and step_size (index -1).
        for setter_name in _VULNERABLE_SETTER_NAMES:
            self.assertIn(setter_name, method_calls[1:-1])

    def test_per_field_trailer_sleeps_before_each_vulnerable_write(self) -> None:
        """A positive field_trailer_delay must drive one sleep per vulnerable write."""
        settings_service, coordinator = self._coordinator_with_all_setters_ok(
            field_trailer_delay_s=0.123,
            step_size_trailer_delay_s=0.0,
        )
        snapshot = _full_vulnerable_snapshot()

        with mock.patch("zd_app.services.settings_apply_coordinator.time.sleep") as sleep:
            coordinator.apply_snapshot(snapshot)

        # 8 scalar vulnerable fields (vibration, deadzones, axis_inv_l/r,
        # sens_l/r, trig_l/r) + 2 button slots + 1 lighting zone = 11 writes.
        self.assertEqual(sleep.call_count, 11)
        for call_args in sleep.call_args_list:
            self.assertAlmostEqual(call_args.args[0], 0.123)

    def test_trailer_skips_sleep_when_field_delay_zero(self) -> None:
        """A zero field_trailer_delay must NOT invoke time.sleep for vulnerable writes."""
        settings_service, coordinator = self._coordinator_with_all_setters_ok(
            field_trailer_delay_s=0.0,
            step_size_trailer_delay_s=0.0,
        )
        snapshot = _full_vulnerable_snapshot()

        with mock.patch("zd_app.services.settings_apply_coordinator.time.sleep") as sleep:
            coordinator.apply_snapshot(snapshot)

        sleep.assert_not_called()

    def test_no_trailer_sleep_when_no_vulnerable_fields_present(self) -> None:
        """polling-only snapshot triggers no field-trailer sleep (and no vulnerable writes)."""
        settings_service, coordinator = self._coordinator_with_all_setters_ok(
            field_trailer_delay_s=0.1,
            step_size_trailer_delay_s=0.1,
        )

        with mock.patch("zd_app.services.settings_apply_coordinator.time.sleep") as sleep:
            coordinator.apply_snapshot(_polling_only_snapshot())

        sleep.assert_not_called()
        for setter_name in _VULNERABLE_SETTER_NAMES:
            getattr(settings_service, setter_name).assert_not_called()

    def test_each_button_binding_slot_gets_its_own_trailer_sleep(self) -> None:
        """16-slot button-binding payloads must produce 16 sleeps, not 1."""
        settings_service, coordinator = self._coordinator_with_all_setters_ok(
            field_trailer_delay_s=0.1,
            step_size_trailer_delay_s=0.0,
        )
        button_bindings = {slot: ButtonMapping() for slot in ButtonSlot}
        snapshot = _empty_snapshot(button_bindings=button_bindings)

        with mock.patch("zd_app.services.settings_apply_coordinator.time.sleep") as sleep:
            coordinator.apply_snapshot(snapshot)

        self.assertEqual(sleep.call_count, len(button_bindings))
        self.assertEqual(settings_service.set_button_binding.call_count, len(button_bindings))

    def test_each_lighting_zone_gets_its_own_trailer_sleep(self) -> None:
        """Each lighting-zone entry must drive its own settle, mirroring button bindings."""
        settings_service, coordinator = self._coordinator_with_all_setters_ok(
            field_trailer_delay_s=0.1,
            step_size_trailer_delay_s=0.0,
        )
        lighting_zones = {zone: _lighting_delta() for zone in LightingZone}
        snapshot = _empty_snapshot(lighting_zones=lighting_zones)

        with mock.patch("zd_app.services.settings_apply_coordinator.time.sleep") as sleep:
            coordinator.apply_snapshot(snapshot)

        self.assertEqual(sleep.call_count, len(lighting_zones))
        self.assertEqual(settings_service.set_zone_lighting.call_count, len(lighting_zones))

    def test_field_trailer_default_delay_is_100ms(self) -> None:
        """Default field-trailer delay matches the 100ms hardware-validated threshold."""
        coordinator = SettingsApplyCoordinator(mock.Mock())
        self.assertAlmostEqual(coordinator._field_trailer_delay_s, 0.1)

    def test_axis_inversion_writes_in_per_field_trailer(self) -> None:
        """axis_inversion was absorbed into the per-field trailer after the
        axis_inversion investigation fixed the get_axis_inversion read
        bug and showed axis_inv writes share the in-burst-cascade band.
        """
        settings_service, coordinator = self._coordinator_with_all_setters_ok(
            field_trailer_delay_s=0.1,
            step_size_trailer_delay_s=0.0,
        )
        snapshot = _empty_snapshot(
            polling_rate=PollingRate.HZ_1000,
            axis_inversion_left=AxisInversion(x_inverted=True, y_inverted=False),
            axis_inversion_right=AxisInversion(x_inverted=False, y_inverted=True),
        )

        with mock.patch("zd_app.services.settings_apply_coordinator.time.sleep") as sleep:
            coordinator.apply_snapshot(snapshot)

        # Two sleeps — one before each axis_inv trailer write.
        self.assertEqual(sleep.call_count, 2)
        for call_args in sleep.call_args_list:
            self.assertAlmostEqual(call_args.args[0], 0.1)
        # Order: polling first (main burst), then axis_inv trailers.
        method_calls = [c[0] for c in settings_service.method_calls]
        self.assertEqual(
            method_calls,
            [
                "set_polling_rate",
                "set_left_stick_inversion",
                "set_right_stick_inversion",
            ],
        )

    def test_back_paddle_bindings_stay_in_main_burst_not_trailer(self) -> None:
        """back_paddle writes must fire BEFORE the first trailer sleep (write-only field; spec keeps in burst)."""
        settings_service, coordinator = self._coordinator_with_all_setters_ok(
            field_trailer_delay_s=0.1,
            step_size_trailer_delay_s=0.0,
        )
        snapshot = _empty_snapshot(
            polling_rate=PollingRate.HZ_1000,
            back_paddle_bindings={
                MacroSlot.M1: BackPaddleBinding(target=None),
                MacroSlot.M2: BackPaddleBinding(target=None),
            },
            vibration=_vibration_delta(),
        )

        with mock.patch("zd_app.services.settings_apply_coordinator.time.sleep") as sleep:
            coordinator.apply_snapshot(snapshot)

        # Exactly one sleep (for vibration) — back_paddle is NOT trailered.
        sleep.assert_called_once_with(0.1)
        method_calls = [c[0] for c in settings_service.method_calls]
        # Both back_paddle writes occur before the vulnerable trailer.
        vibration_idx = method_calls.index("set_vibration")
        back_paddle_indices = [i for i, n in enumerate(method_calls) if n == "set_back_paddle_binding"]
        self.assertEqual(len(back_paddle_indices), 2)
        for idx in back_paddle_indices:
            self.assertLess(idx, vibration_idx)

    def test_include_device_false_path_main_burst_empty_trailer_fires(self) -> None:
        """The "Apply profile only" path: polling/step_size None, but trailer still fires."""
        # ``include_device=False`` clears polling_rate + step_size in the snapshot the
        # apply path receives (see zd_app/ui/safe_import_model.without_device_settings).
        # The vulnerable trailer must still fire for the remaining feel/cosmetic fields.
        settings_service, coordinator = self._coordinator_with_all_setters_ok(
            field_trailer_delay_s=0.1,
            step_size_trailer_delay_s=0.1,
        )
        snapshot = _full_vulnerable_snapshot(
            polling_rate=None,
            step_size=None,
        )

        with mock.patch("zd_app.services.settings_apply_coordinator.time.sleep") as sleep:
            coordinator.apply_snapshot(snapshot)

        settings_service.set_polling_rate.assert_not_called()
        settings_service.set_step_size.assert_not_called()
        # 8 scalars + 2 button slots + 1 lighting zone = 11 trailer sleeps, no step_size sleep.
        self.assertEqual(sleep.call_count, 11)
        for setter_name in _VULNERABLE_SETTER_NAMES:
            self.assertTrue(
                getattr(settings_service, setter_name).called,
                f"{setter_name} should fire in the trailer even with include_device=False",
            )

    def test_apply_only_vibration_writes_one_trailer_with_one_sleep(self) -> None:
        """Single-vulnerable-field snapshot → exactly one trailer sleep + one write."""
        settings_service, coordinator = self._coordinator_with_all_setters_ok(
            field_trailer_delay_s=0.05,
            step_size_trailer_delay_s=0.0,
        )
        snapshot = _empty_snapshot(vibration=_vibration_delta())

        with mock.patch("zd_app.services.settings_apply_coordinator.time.sleep") as sleep:
            result = coordinator.apply_snapshot(snapshot)

        sleep.assert_called_once_with(0.05)
        settings_service.set_vibration.assert_called_once()
        self.assertEqual(result.total_attempted, 1)
        self.assertEqual(result.succeeded, 1)

    def test_vulnerable_setter_exception_surfaces_as_apply_failure(self) -> None:
        """A failing vulnerable trailer write must surface through ApplyFailure like step_size does."""
        settings_service, coordinator = self._coordinator_with_all_setters_ok(
            field_trailer_delay_s=0.0,
        )
        settings_service.set_vibration.return_value = _polling_result(
            SetPollingRateOutcome.WRITE_FAILED, error_code=1167
        )
        snapshot = _empty_snapshot(
            polling_rate=PollingRate.HZ_1000,
            vibration=_vibration_delta(),
        )

        result = coordinator.apply_snapshot(snapshot)

        # polling succeeded, vibration failed.
        self.assertEqual(result.total_attempted, 2)
        self.assertEqual(result.succeeded, 1)
        self.assertEqual(len(result.failed), 1)
        failure = result.failed[0]
        self.assertEqual(failure.setting_label, "vibration")
        self.assertTrue(failure.is_transient)
        self.assertIsNotNone(failure.retry_fn)


class CoordinatorSensitivity8PointDispatchTests(unittest.TestCase):
    """The apply path writes 8-point (0x86) on a capable device and 3-point
    (0x06) otherwise, gated on the snapshot carrying an 8-point curve."""

    _LEFT_3 = (
        SensitivityAnchor(0, 0),
        SensitivityAnchor(50, 50),
        SensitivityAnchor(100, 100),
    )
    _RIGHT_3 = (
        SensitivityAnchor(0, 0),
        SensitivityAnchor(40, 45),
        SensitivityAnchor(100, 100),
    )
    _LEFT_8 = tuple(SensitivityAnchor(i * 10, i * 10) for i in range(8))
    _RIGHT_8 = tuple(SensitivityAnchor(i * 10, min(100, i * 14)) for i in range(8))

    def _service(self) -> mock.Mock:
        svc = mock.Mock()
        ok = _polling_result(SetPollingRateOutcome.OK)
        svc.set_left_stick_sensitivity_curve.return_value = ok
        svc.set_right_stick_sensitivity_curve.return_value = ok
        svc.set_left_stick_sensitivity_curve_8point.return_value = ok
        svc.set_right_stick_sensitivity_curve_8point.return_value = ok
        return svc

    def test_capable_device_writes_8point(self) -> None:
        svc = self._service()
        svc.supports_8point_sensitivity.return_value = True
        coordinator = SettingsApplyCoordinator(svc, field_trailer_delay_s=0.0)
        snapshot = _empty_snapshot(
            sensitivity_left=self._LEFT_3,
            sensitivity_right=self._RIGHT_3,
            sensitivity_left_8point=self._LEFT_8,
            sensitivity_right_8point=self._RIGHT_8,
        )

        coordinator.apply_snapshot(snapshot)

        svc.set_left_stick_sensitivity_curve_8point.assert_called_once_with(self._LEFT_8)
        svc.set_right_stick_sensitivity_curve_8point.assert_called_once_with(self._RIGHT_8)
        svc.set_left_stick_sensitivity_curve.assert_not_called()
        svc.set_right_stick_sensitivity_curve.assert_not_called()

    def test_capability_verdict_fixed_at_start_not_reprobed_mid_apply(self) -> None:
        # A11: an earlier trailer write that disconnects invalidates the cached
        # 8-point capability; a lazy mid-pipeline re-probe on a flaky just-
        # reconnected device could flip the verdict and pick the wrong curve
        # branch. Model the flip: the probe reports capable BEFORE the deadzones
        # write and not-capable AFTER it. The apply must honor the verdict fixed
        # at the start of apply_snapshot (capable -> 8-point), not re-probe late.
        svc = self._service()
        svc.set_all_deadzones.return_value = _polling_result(SetPollingRateOutcome.OK)
        svc.supports_8point_sensitivity.side_effect = (
            lambda: not svc.set_all_deadzones.called
        )
        coordinator = SettingsApplyCoordinator(svc, field_trailer_delay_s=0.0)
        snapshot = _empty_snapshot(
            deadzones=StickDeadzones(
                left_center=0, right_center=0, left_outer=0, right_outer=0
            ),
            sensitivity_left=self._LEFT_3,
            sensitivity_right=self._RIGHT_3,
            sensitivity_left_8point=self._LEFT_8,
            sensitivity_right_8point=self._RIGHT_8,
        )

        coordinator.apply_snapshot(snapshot)

        svc.set_left_stick_sensitivity_curve_8point.assert_called_once_with(self._LEFT_8)
        svc.set_right_stick_sensitivity_curve_8point.assert_called_once_with(self._RIGHT_8)
        svc.set_left_stick_sensitivity_curve.assert_not_called()
        svc.set_right_stick_sensitivity_curve.assert_not_called()

    def test_non_capable_device_writes_3point(self) -> None:
        svc = self._service()
        svc.supports_8point_sensitivity.return_value = False
        coordinator = SettingsApplyCoordinator(svc, field_trailer_delay_s=0.0)
        snapshot = _empty_snapshot(
            sensitivity_left=self._LEFT_3,
            sensitivity_right=self._RIGHT_3,
            sensitivity_left_8point=self._LEFT_8,
            sensitivity_right_8point=self._RIGHT_8,
        )

        coordinator.apply_snapshot(snapshot)

        svc.set_left_stick_sensitivity_curve.assert_called_once_with(self._LEFT_3)
        svc.set_right_stick_sensitivity_curve.assert_called_once_with(self._RIGHT_3)
        svc.set_left_stick_sensitivity_curve_8point.assert_not_called()
        svc.set_right_stick_sensitivity_curve_8point.assert_not_called()

    def test_legacy_snapshot_without_8point_never_probes(self) -> None:
        # The guard: a snapshot with no 8-point curve must NOT consult the
        # capability probe (short-circuit), so legacy/profile applies keep the
        # exact prior 3-point path on any service.
        svc = self._service()
        coordinator = SettingsApplyCoordinator(svc, field_trailer_delay_s=0.0)
        snapshot = _empty_snapshot(
            sensitivity_left=self._LEFT_3,
            sensitivity_right=self._RIGHT_3,
        )

        coordinator.apply_snapshot(snapshot)

        svc.supports_8point_sensitivity.assert_not_called()
        svc.set_left_stick_sensitivity_curve.assert_called_once_with(self._LEFT_3)
        svc.set_right_stick_sensitivity_curve.assert_called_once_with(self._RIGHT_3)

    def test_8point_write_preserves_trailer_settle(self) -> None:
        # The per-field burst-rejection trailer must wrap the 8-point write
        # exactly as it wraps the 3-point one.
        svc = self._service()
        svc.supports_8point_sensitivity.return_value = True
        coordinator = SettingsApplyCoordinator(svc, field_trailer_delay_s=0.05)
        snapshot = _empty_snapshot(
            sensitivity_left_8point=self._LEFT_8,
            sensitivity_right_8point=self._RIGHT_8,
        )

        with mock.patch(
            "zd_app.services.settings_apply_coordinator.time.sleep"
        ) as sleep:
            coordinator.apply_snapshot(snapshot)

        svc.set_left_stick_sensitivity_curve_8point.assert_called_once_with(self._LEFT_8)
        svc.set_right_stick_sensitivity_curve_8point.assert_called_once_with(self._RIGHT_8)
        # One settle sleep per 8-point write (left + right).
        self.assertEqual(sleep.call_count, 2)
        for call_args in sleep.call_args_list:
            self.assertAlmostEqual(call_args.args[0], 0.05)

    def test_mixed_one_stick_8point_other_falls_back_to_3point(self) -> None:
        # Only LEFT carries an 8-point curve; a capable device writes LEFT via
        # 0x86 and RIGHT via the 3-point fallback.
        svc = self._service()
        svc.supports_8point_sensitivity.return_value = True
        coordinator = SettingsApplyCoordinator(svc, field_trailer_delay_s=0.0)
        snapshot = _empty_snapshot(
            sensitivity_left_8point=self._LEFT_8,
            sensitivity_right=self._RIGHT_3,
        )

        coordinator.apply_snapshot(snapshot)

        svc.set_left_stick_sensitivity_curve_8point.assert_called_once_with(self._LEFT_8)
        svc.set_right_stick_sensitivity_curve.assert_called_once_with(self._RIGHT_3)
        svc.set_left_stick_sensitivity_curve.assert_not_called()
        svc.set_right_stick_sensitivity_curve_8point.assert_not_called()


if __name__ == "__main__":
    unittest.main()
