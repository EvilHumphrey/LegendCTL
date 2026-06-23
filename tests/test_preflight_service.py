from __future__ import annotations

import unittest

from zd_app.services.preflight_service import (
    DEFAULT_PREFLIGHT_ACTION_HINT,
    PreflightService,
    UNKNOWN_PREFLIGHT_STATE_LABEL,
    _action_hint,
    _state_label,
)
from zd_app.protocol.preflight_visibility import OfficialUiProbe, summarize_preflight
from zd_app.protocol.trigger_interface import PublicProbeResult


def _result_for_state(state: str):
    if state == "ready_for_settings":
        return summarize_preflight(
            launch_official=False,
            official_ui=OfficialUiProbe(
                app_running=True,
                launched=False,
                main_window="ZDGame",
                settings_window=None,
                device_settings_button=True,
                no_device_connected=False,
                connect_via_usb=False,
                visible_names=("Device Settings",),
            ),
            public_paths=["public"],
            public_probe=PublicProbeResult(
                path="public",
                attempted=True,
                ok=True,
                last_error=0,
                access_mode="public_identify",
                out_hex="03010100000000003d410421",
                matched_expected=True,
            ),
            hidden_visible_paths=[],
        )
    if state == "public_visible_ui_mismatch":
        return summarize_preflight(
            launch_official=False,
            official_ui=OfficialUiProbe(
                app_running=True,
                launched=False,
                main_window="ZDGame",
                settings_window=None,
                device_settings_button=False,
                no_device_connected=True,
                connect_via_usb=True,
                visible_names=("No ZD device connected",),
            ),
            public_paths=["public"],
            public_probe=PublicProbeResult(
                path="public",
                attempted=True,
                ok=True,
                last_error=0,
                access_mode="public_identify",
                out_hex="03010100000000003d410421",
                matched_expected=True,
            ),
            hidden_visible_paths=[],
        )
    raise AssertionError(f"Unsupported test state: {state}")


class PreflightServiceTests(unittest.TestCase):
    def test_run_session_preflight_reports_ready_state(self) -> None:
        service = PreflightService(runner=lambda: _result_for_state("ready_for_settings"))

        snapshot = service.run_session_preflight()

        self.assertEqual(snapshot.state, "ready_for_settings")
        self.assertEqual(snapshot.state_label, "Ready For Settings")
        self.assertEqual(snapshot.official_ui_label, "Device Settings Visible")
        self.assertEqual(snapshot.public_probe_label, "Public Identify OK")
        self.assertEqual(snapshot.hidden_visibility_label, "Hidden Not Visible")
        self.assertIn("good time", snapshot.action_hint)

    def test_run_session_preflight_reports_ui_mismatch(self) -> None:
        service = PreflightService(runner=lambda: _result_for_state("public_visible_ui_mismatch"))

        snapshot = service.run_session_preflight()

        self.assertEqual(snapshot.state, "public_visible_ui_mismatch")
        self.assertEqual(snapshot.state_label, "Windows/App Mismatch")
        self.assertEqual(snapshot.official_ui_label, "No Device Connected")
        self.assertEqual(snapshot.public_probe_label, "Public Identify OK")
        self.assertIn("mismatch", snapshot.action_hint.lower())

    def test_run_session_preflight_handles_probe_failure(self) -> None:
        service = PreflightService(runner=lambda: (_ for _ in ()).throw(RuntimeError("probe broke")))

        snapshot = service.run_session_preflight()

        self.assertEqual(snapshot.state, "error")
        self.assertEqual(snapshot.state_label, "Preflight Failed")
        self.assertEqual(snapshot.official_ui_label, "Probe Failed")
        self.assertIn("could not complete", snapshot.summary)
        self.assertIn("probe broke", snapshot.hidden_visibility_label)

    def test_state_label_falls_back_to_unknown_for_unmapped_state(self) -> None:
        with self.assertLogs("zd_app.services.preflight_service", level="WARNING") as captured:
            label = _state_label("reconnecting_public_probe")

        self.assertEqual(label, UNKNOWN_PREFLIGHT_STATE_LABEL)
        self.assertEqual(label, "Unknown")
        self.assertIn(
            "Unmapped preflight state label requested: reconnecting_public_probe",
            captured.output[0],
        )

    def test_action_hint_falls_back_to_default_for_unmapped_state(self) -> None:
        with self.assertLogs("zd_app.services.preflight_service", level="WARNING") as captured:
            hint = _action_hint("reconnecting_public_probe")

        self.assertEqual(hint, DEFAULT_PREFLIGHT_ACTION_HINT)
        self.assertIn(
            "Unmapped preflight action hint requested: reconnecting_public_probe",
            captured.output[0],
        )


class PublicIdentifyProbeRetryLadderTests(unittest.TestCase):
    """Retry ladder for the public identify probe.

    All tests use injected probe_fn + sleep_fn so they never touch real
    HID / USB and the sleep schedule does not add wall-clock time.
    """

    TEST_PATH = r"\\?\usb#vid_413d&pid_2104&mi_00#7&1234#{guid}"

    def _result(
        self,
        *,
        ok: bool,
        last_error: int,
        matched: bool = False,
    ) -> PublicProbeResult:
        return PublicProbeResult(
            path=self.TEST_PATH,
            attempted=True,
            ok=ok,
            last_error=last_error,
            access_mode="read_write",
            out_hex="03010100000000003d410421" if matched else "",
            matched_expected=matched,
        )

    def _invoke_with_sequence(
        self, outcomes: list[PublicProbeResult]
    ) -> tuple[PublicProbeResult, list[float], int]:
        """Run the retry wrapper with scripted per-attempt outcomes.

        Returns (final_result, sleep_durations, probe_call_count).
        """
        from zd_app.protocol.trigger_interface import (
            _run_public_identify_probe_with_retry,
        )

        sleeps: list[float] = []
        call_count = 0
        outcomes_iter = iter(outcomes)

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def fake_probe(*_args, **_kwargs) -> PublicProbeResult:
            nonlocal call_count
            call_count += 1
            return next(outcomes_iter)

        result = _run_public_identify_probe_with_retry(
            path=self.TEST_PATH,
            mode_name="read_write",
            share_mode=3,
            creation_disposition=3,
            flags_and_attributes=128,
            sleep_fn=fake_sleep,
            probe_fn=fake_probe,
        )
        return result, sleeps, call_count

    def test_happy_path_first_attempt_success_no_retry(self) -> None:
        """Attempt 1 returns ok=True, matched=True. No sleeps, no retries."""
        result, sleeps, calls = self._invoke_with_sequence(
            [self._result(ok=True, last_error=0, matched=True)]
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.matched_expected)
        self.assertEqual(result.retries_fired, 0)
        self.assertEqual(result.retries_converted, 0)
        self.assertEqual(result.retry_errors_observed, ())
        self.assertEqual(sleeps, [])
        self.assertEqual(calls, 1)

    def test_single_shot_flake_recovers_on_second_attempt(self) -> None:
        """First attempt ERROR_FILE_NOT_FOUND, second attempt succeeds.

        This is the exact failure mode observed during live integration
        verification on real hardware.
        """
        result, sleeps, calls = self._invoke_with_sequence(
            [
                self._result(ok=False, last_error=2),  # ERROR_FILE_NOT_FOUND
                self._result(ok=True, last_error=0, matched=True),
            ]
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.matched_expected)
        self.assertEqual(result.retries_fired, 1)
        self.assertEqual(result.retries_converted, 1)
        self.assertEqual(result.retry_errors_observed, (2,))
        # Backoff schedule: 100 ms before the single retry.
        self.assertEqual(sleeps, [0.1])
        self.assertEqual(calls, 2)

    def test_two_failures_then_success_uses_second_backoff_slot(self) -> None:
        """Attempts 1 and 2 fail with retryable codes; attempt 3 succeeds."""
        result, sleeps, calls = self._invoke_with_sequence(
            [
                self._result(ok=False, last_error=2),
                self._result(ok=False, last_error=3),  # ERROR_PATH_NOT_FOUND
                self._result(ok=True, last_error=0, matched=True),
            ]
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.matched_expected)
        self.assertEqual(result.retries_fired, 2)
        self.assertEqual(result.retries_converted, 1)
        self.assertEqual(result.retry_errors_observed, (2, 3))
        # Backoff schedule: 100 ms before retry 1, 250 ms before retry 2.
        self.assertEqual(sleeps, [0.1, 0.25])
        self.assertEqual(calls, 3)

    def test_all_three_attempts_fail_with_retryable_errors(self) -> None:
        """Exhausted ladder returns the last failing result with retry evidence."""
        result, sleeps, calls = self._invoke_with_sequence(
            [
                self._result(ok=False, last_error=2),
                self._result(ok=False, last_error=2),
                self._result(ok=False, last_error=2),
            ]
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.last_error, 2)
        self.assertEqual(result.retries_fired, 2)
        self.assertEqual(result.retries_converted, 0)
        self.assertEqual(result.retry_errors_observed, (2, 2))
        self.assertEqual(sleeps, [0.1, 0.25])
        self.assertEqual(calls, 3)

    def test_non_retryable_error_returns_immediately_without_retry(self) -> None:
        """ERROR_ACCESS_DENIED (5) is outside RETRYABLE_OPEN_ERRORS. No retry."""
        result, sleeps, calls = self._invoke_with_sequence(
            [self._result(ok=False, last_error=5)]
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.last_error, 5)
        self.assertEqual(result.retries_fired, 0)
        self.assertEqual(result.retries_converted, 0)
        self.assertEqual(result.retry_errors_observed, ())
        self.assertEqual(sleeps, [])
        self.assertEqual(calls, 1)

    def test_mixed_retryable_then_non_retryable_stops_on_non_retryable(
        self,
    ) -> None:
        """First retryable, second non-retryable: return on the non-retryable."""
        result, sleeps, calls = self._invoke_with_sequence(
            [
                self._result(ok=False, last_error=2),  # retryable
                self._result(ok=False, last_error=5),  # ERROR_ACCESS_DENIED
            ]
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.last_error, 5)
        self.assertEqual(result.retries_fired, 1)
        self.assertEqual(result.retries_converted, 0)
        self.assertEqual(result.retry_errors_observed, (2,))
        self.assertEqual(sleeps, [0.1])
        self.assertEqual(calls, 2)

    def test_public_probe_result_defaults_are_no_retry_shape(self) -> None:
        """Backward compat: existing callers that omit retry fields are unchanged."""
        result = PublicProbeResult(
            path="p",
            attempted=True,
            ok=True,
            last_error=0,
            access_mode="x",
            out_hex="",
            matched_expected=True,
        )

        self.assertEqual(result.retries_fired, 0)
        self.assertEqual(result.retries_converted, 0)
        self.assertEqual(result.retry_errors_observed, ())

    def test_preflight_integration_flake_recovers_to_ready_for_settings(
        self,
    ) -> None:
        """End-to-end: patch run_public_identify_probe to fail-then-succeed.

        This is the exact integration path seen in local verification — a
        single-shot ERROR_FILE_NOT_FOUND would
        have driven state=unclear pre-ladder. With the ladder in place,
        the retry converts and state=ready_for_settings.
        """
        from unittest import mock

        from zd_app.services.preflight_service import PreflightService

        call_count = 0

        def flaky_probe(
            path, mode_name, share_mode, creation_disposition, flags_and_attributes
        ):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return PublicProbeResult(
                    path=path,
                    attempted=True,
                    ok=False,
                    last_error=2,  # ERROR_FILE_NOT_FOUND
                    access_mode="read_write",
                    out_hex="",
                    matched_expected=False,
                )
            return PublicProbeResult(
                path=path,
                attempted=True,
                ok=True,
                last_error=0,
                access_mode="read_write",
                out_hex="03010100000000003d410421",
                matched_expected=True,
            )

        with mock.patch(
            "zd_app.protocol.trigger_interface.run_public_identify_probe",
            side_effect=flaky_probe,
        ), mock.patch(
            "zd_app.protocol.preflight_visibility.run_official_ui_probe",
            return_value=_result_for_state("ready_for_settings").official_ui,
        ), mock.patch(
            "zd_app.protocol.preflight_visibility.public_hid_paths",
            return_value=["fake_public_path"],
        ), mock.patch(
            "zd_app.protocol.preflight_visibility.filter_hid_paths",
            return_value=[],
        ), mock.patch(
            "zd_app.protocol.trigger_interface.time.sleep",
            return_value=None,
        ):
            snapshot = PreflightService().run_session_preflight()

        self.assertEqual(snapshot.state, "ready_for_settings")
        self.assertEqual(snapshot.public_probe_label, "Public Identify OK")
        # Exactly two probe calls — first flakes, second succeeds.
        self.assertEqual(call_count, 2)


if __name__ == "__main__":
    unittest.main()
