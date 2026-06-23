"""Sample-capture-level tests for the Controller Health Report.

Stick X/Y values are rescaled from XInput int16 ([-32768, 32767]) to signed
integer percent of full axis travel ([-100, 100]) at capture time so the
downstream measurement code — and the suggested-deadzone value the user sees
in the report — share units with the wrapper's Sticks-tab deadzone slider
(0..100 percent). Per the measurement contract: "If the app already has configurable
deadzone units, report in the same units the user sees in the UI."
"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from typing import Optional

from zd_app.services.health_report.models import Sample
from zd_app.services.health_report.sample_capture import (
    SampleCollector,
    _scale_xinput_axis_to_pct,
    make_xinput_sample_provider,
)


class ScaleXInputAxisToPctTests(unittest.TestCase):
    def test_zero_maps_to_zero(self) -> None:
        self.assertEqual(_scale_xinput_axis_to_pct(0), 0)

    def test_full_positive_maps_to_100(self) -> None:
        self.assertEqual(_scale_xinput_axis_to_pct(32767), 100)

    def test_full_negative_maps_to_minus_100(self) -> None:
        # int16 min is -32768; the scaler must clamp to -100 (not -101).
        self.assertEqual(_scale_xinput_axis_to_pct(-32768), -100)

    def test_half_positive_maps_to_about_50(self) -> None:
        self.assertEqual(_scale_xinput_axis_to_pct(16384), 50)

    def test_half_negative_maps_to_about_minus_50(self) -> None:
        self.assertEqual(_scale_xinput_axis_to_pct(-16384), -50)

    def test_small_noise_maps_to_zero_for_clean_rest(self) -> None:
        # Local testing saw p99 ≈ 16-29 in int16 (< 0.1% of full
        # travel) — those samples should mostly land at 0 in the percent
        # scale, which is the right answer for an essentially-still stick.
        for raw in (-30, -10, -1, 1, 10, 30):
            with self.subTest(raw=raw):
                self.assertEqual(_scale_xinput_axis_to_pct(raw), 0)

    def test_quarter_positive_is_about_25(self) -> None:
        self.assertEqual(_scale_xinput_axis_to_pct(8192), 25)

    def test_output_is_int(self) -> None:
        self.assertIsInstance(_scale_xinput_axis_to_pct(12345), int)

    def test_output_clamped_into_bounds(self) -> None:
        # Defensive: synthetic out-of-spec inputs must not escape the slider
        # range, no matter what XInput hands us.
        self.assertEqual(_scale_xinput_axis_to_pct(40000), 100)
        self.assertEqual(_scale_xinput_axis_to_pct(-40000), -100)


class XInputSampleProviderScalingTests(unittest.TestCase):
    """End-to-end check that ``make_xinput_sample_provider`` returns samples
    in the percent scale rather than raw int16. The DLL is stubbed.
    """

    @staticmethod
    def _fake_dll(thumb_lx: int, thumb_ly: int,
                  thumb_rx: int, thumb_ry: int,
                  trigger_l: int = 0, trigger_r: int = 0,
                  packet_number: int = 7) -> object:
        class _FakeDLL:
            def XInputGetState(self, _user_index, state_ref):
                state = state_ref._obj
                state.dwPacketNumber = packet_number
                state.Gamepad.sThumbLX = thumb_lx
                state.Gamepad.sThumbLY = thumb_ly
                state.Gamepad.sThumbRX = thumb_rx
                state.Gamepad.sThumbRY = thumb_ry
                state.Gamepad.bLeftTrigger = trigger_l
                state.Gamepad.bRightTrigger = trigger_r
                state.Gamepad.wButtons = 0
                return 0  # _ERROR_SUCCESS

        return _FakeDLL()

    def test_provider_returns_sticks_in_percent_unit(self) -> None:
        provider = make_xinput_sample_provider(
            dll_loader=lambda: self._fake_dll(
                thumb_lx=32767, thumb_ly=-32768,
                thumb_rx=16384, thumb_ry=0,
                trigger_l=200, trigger_r=10,
            ),
            time_source_ns=lambda: 12345,
        )

        sample = provider()

        self.assertIsNotNone(sample)
        self.assertEqual(sample.left_stick_x, 100)
        self.assertEqual(sample.left_stick_y, -100)
        self.assertEqual(sample.right_stick_x, 50)
        self.assertEqual(sample.right_stick_y, 0)
        # Triggers remain byte-scale per Sample contract.
        self.assertEqual(sample.left_trigger, 200)
        self.assertEqual(sample.right_trigger, 10)
        self.assertTrue(sample.connected)
        self.assertEqual(sample.timestamp_ns, 12345)
        self.assertEqual(sample.packet_number, 7)


class SampleCollectorPauseResumeTests(unittest.TestCase):
    """pause()/resume() — the sample-collector pause/resume fix.

    Operator-paced READY_* pauses can be many seconds long; the previous
    behavior was that the collector kept appending throughout, which
    consumed the cap before TRIGGER could record any samples.
    pause() suspends appends without stopping the worker so XInput stays
    warm; resume() re-enables appends.
    """

    @staticmethod
    def _counting_provider() -> tuple[callable, dict]:
        """Provider that returns a fresh Sample on every call and tracks
        the call count + a monotonically-increasing packet_number."""

        state = {"calls": 0}

        def _provider() -> Optional[Sample]:
            state["calls"] += 1
            return Sample(
                timestamp_ns=state["calls"] * 1_000,
                packet_number=state["calls"],
                left_stick_x=0, left_stick_y=0,
                right_stick_x=0, right_stick_y=0,
                left_trigger=0, right_trigger=0,
            )

        return _provider, state

    def _wait_for_calls(self, state: dict, target: int, *, timeout_s: float = 1.0) -> None:
        deadline = time.perf_counter() + timeout_s
        while state["calls"] < target and time.perf_counter() < deadline:
            time.sleep(0.005)

    def test_pause_blocks_appends_but_worker_keeps_polling(self) -> None:
        provider, state = self._counting_provider()
        collector = SampleCollector(provider, poll_yield_ns=0)
        collector.start()
        try:
            # Let the worker accumulate some real samples.
            self._wait_for_calls(state, target=200)
            collector.pause()
            samples_at_pause = len(collector.snapshot_samples())
            calls_at_pause = state["calls"]

            # Let the worker churn past the pause boundary; call count must
            # keep climbing (worker still polling), sample count must not.
            self._wait_for_calls(state, target=calls_at_pause + 500)
            samples_after_pause = len(collector.snapshot_samples())
            calls_after_pause = state["calls"]
        finally:
            collector.stop()

        self.assertGreater(calls_after_pause, calls_at_pause + 200)
        # ``samples_at_pause`` may include one in-flight sample that landed
        # between the lock release in the worker and pause() taking the
        # lock; both are correct under the spec ("in-flight sample is
        # dropped"). Allow ±1 jitter, no more.
        self.assertLessEqual(samples_after_pause - samples_at_pause, 1)

    def test_resume_re_enables_appends(self) -> None:
        provider, state = self._counting_provider()
        collector = SampleCollector(provider, poll_yield_ns=0)
        collector.start()
        try:
            self._wait_for_calls(state, target=100)
            collector.pause()
            samples_at_pause = len(collector.snapshot_samples())
            calls_at_pause = state["calls"]
            self._wait_for_calls(state, target=calls_at_pause + 200)
            collector.resume()

            # Wait for the worker to append enough new samples that the
            # delta is unambiguously > 0.
            deadline = time.perf_counter() + 1.0
            while time.perf_counter() < deadline:
                if len(collector.snapshot_samples()) >= samples_at_pause + 50:
                    break
                time.sleep(0.005)
            samples_after_resume = len(collector.snapshot_samples())
        finally:
            collector.stop()

        self.assertGreater(samples_after_resume, samples_at_pause)

    def test_pause_resume_idempotent(self) -> None:
        collector = SampleCollector(lambda: None, poll_yield_ns=10_000_000)
        # Before start.
        collector.pause()
        collector.pause()
        collector.resume()
        collector.resume()
        # And after start, mixed sequences.
        collector.start()
        try:
            collector.pause()
            collector.pause()
            collector.resume()
            collector.resume()
            collector.pause()
            collector.resume()
        finally:
            collector.stop()
        # No exceptions == pass.

    def test_pause_before_start_is_safe(self) -> None:
        provider, state = self._counting_provider()
        collector = SampleCollector(provider, poll_yield_ns=0)
        collector.pause()
        collector.start()
        try:
            # The collector starts paused; no appends should accumulate
            # until resume(). Give the worker plenty of wall time to call
            # the provider.
            self._wait_for_calls(state, target=200)
            self.assertEqual(len(collector.snapshot_samples()), 0)

            collector.resume()
            # Now appends should resume; wait for some to land.
            deadline = time.perf_counter() + 1.0
            while time.perf_counter() < deadline:
                if len(collector.snapshot_samples()) >= 20:
                    break
                time.sleep(0.005)
            self.assertGreater(len(collector.snapshot_samples()), 0)
        finally:
            collector.stop()

    def test_cap_still_honoured_after_unpause(self) -> None:
        provider, state = self._counting_provider()
        collector = SampleCollector(provider, sample_cap=30, poll_yield_ns=0)
        # Pause, let the provider churn past cap-equivalent worth of calls
        # (none should land), then resume and watch the cap kick in.
        collector.pause()
        collector.start()
        try:
            self._wait_for_calls(state, target=500)
            self.assertEqual(len(collector.snapshot_samples()), 0)

            collector.resume()
            deadline = time.perf_counter() + 1.0
            while collector.running and time.perf_counter() < deadline:
                time.sleep(0.01)
            samples = collector.snapshot_samples()
        finally:
            collector.stop()

        # The cap is 30 — worker self-exits at exactly the cap.
        self.assertEqual(len(samples), 30)


if __name__ == "__main__":
    unittest.main()
