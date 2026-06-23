"""Tests for the Health Report orchestrator + sample collector.

The orchestrator uses an injected clock so tests can drive the state machine
in zero wall time. The sample collector uses an injected provider so tests
can produce deterministic samples without loading the XInput DLL.

Integration test at the bottom drives a full synthetic run end-to-end and
asserts the produced :class:`HealthReport` has the expected shape.
"""

from __future__ import annotations

import math
import threading
import time
import unittest
from itertools import count
from typing import Optional
from unittest.mock import patch

from zd_app.services.health_report import (
    DeviceContext,
    HealthReport,
    HealthReportService,
    HealthReportState,
    OverallStatus,
    Sample,
    SampleCollector,
    StepProgress,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeClock:
    """Hand-driven clock so the orchestrator state machine moves in 0 wall time."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt_s: float) -> None:
        self._t += dt_s


class _ScriptedProvider:
    """Replay a list of pre-made samples. Returns None once exhausted."""

    def __init__(self, samples: list[Sample]) -> None:
        self._samples = list(samples)
        self._idx = 0

    def __call__(self) -> Optional[Sample]:
        if self._idx >= len(self._samples):
            return None
        sample = self._samples[self._idx]
        self._idx += 1
        return sample


class _SpyCollector:
    """Stand-in :class:`SampleCollector` that records pause/resume calls.

    Drop-in for ``zd_app.services.health_report.service.SampleCollector``
    (the name the service module imports). Doesn't spawn a worker thread —
    pause/resume/start/stop are pure call counters — so tests can assert
    the wiring deterministically.
    """

    def __init__(self, provider, *, sample_cap: int = 0,
                 poll_yield_ns: int = 0) -> None:
        self._provider = provider
        self._running = False
        self.start_calls = 0
        self.stop_calls = 0
        self.pause_calls = 0
        self.resume_calls = 0

    def start(self) -> None:
        self._running = True
        self.start_calls += 1

    def stop(self, *, join_timeout_s: float = 2.0) -> list[Sample]:
        self._running = False
        self.stop_calls += 1
        return []

    @property
    def running(self) -> bool:
        return self._running

    def snapshot_samples(self) -> list[Sample]:
        return []

    def pause(self) -> None:
        self.pause_calls += 1

    def resume(self) -> None:
        self.resume_calls += 1


def _device_provider(*, hz: int = 1000) -> DeviceContext:
    return DeviceContext(
        controller_name="ZD Ultimate Legend",
        configured_polling_hz=hz,
        profile_name="Apex",
    )


# ---------------------------------------------------------------------------
# SampleCollector
# ---------------------------------------------------------------------------


class SampleCollectorTests(unittest.TestCase):
    def test_collects_samples_until_stopped(self) -> None:
        # Provider that returns one new sample per call, then None.
        seq = iter(range(50))

        def provider() -> Optional[Sample]:
            try:
                i = next(seq)
            except StopIteration:
                return None
            return Sample(
                timestamp_ns=i * 1_000_000, packet_number=i,
                left_stick_x=i, left_stick_y=0,
                right_stick_x=0, right_stick_y=0,
                left_trigger=0, right_trigger=0,
            )

        collector = SampleCollector(provider, poll_yield_ns=0)
        collector.start()
        # Wait for the provider to exhaust + drain.
        deadline = time.perf_counter() + 1.0
        while time.perf_counter() < deadline:
            if len(collector.snapshot_samples()) >= 50:
                break
            time.sleep(0.01)
        samples = collector.stop()

        self.assertEqual(len(samples), 50)
        self.assertEqual([s.packet_number for s in samples], list(range(50)))

    def test_starting_twice_raises(self) -> None:
        collector = SampleCollector(lambda: None, poll_yield_ns=10_000_000)
        collector.start()
        try:
            with self.assertRaises(RuntimeError):
                collector.start()
        finally:
            collector.stop()

    def test_sample_cap_caps_collected_count(self) -> None:
        # Provider that produces samples unboundedly.
        c = count()

        def provider() -> Sample:
            i = next(c)
            return Sample(
                timestamp_ns=i * 1_000, packet_number=i,
                left_stick_x=0, left_stick_y=0,
                right_stick_x=0, right_stick_y=0,
                left_trigger=0, right_trigger=0,
            )

        collector = SampleCollector(provider, sample_cap=20, poll_yield_ns=0)
        collector.start()
        # Wait until cap is hit (worker self-exits).
        deadline = time.perf_counter() + 1.0
        while collector.running and time.perf_counter() < deadline:
            time.sleep(0.01)
        samples = collector.stop()

        self.assertLessEqual(len(samples), 20)
        self.assertGreaterEqual(len(samples), 20)

    def test_provider_exception_stops_capture_cleanly(self) -> None:
        def provider() -> Sample:
            raise RuntimeError("boom")

        collector = SampleCollector(provider, poll_yield_ns=10_000_000)
        collector.start()
        deadline = time.perf_counter() + 1.0
        while collector.running and time.perf_counter() < deadline:
            time.sleep(0.01)
        samples = collector.stop()

        self.assertEqual(samples, [])

    def test_provider_returning_none_yields_no_sample_but_keeps_running(self) -> None:
        # Returns None for first 5 calls, then 5 real samples, then None.
        counter = {"n": 0}

        def provider() -> Optional[Sample]:
            counter["n"] += 1
            if counter["n"] <= 5:
                return None
            if counter["n"] <= 10:
                i = counter["n"] - 5
                return Sample(
                    timestamp_ns=i * 1_000, packet_number=i,
                    left_stick_x=0, left_stick_y=0,
                    right_stick_x=0, right_stick_y=0,
                    left_trigger=0, right_trigger=0,
                )
            return None

        collector = SampleCollector(provider, poll_yield_ns=0)
        collector.start()
        deadline = time.perf_counter() + 1.0
        while len(collector.snapshot_samples()) < 5 and time.perf_counter() < deadline:
            time.sleep(0.01)
        samples = collector.stop()

        self.assertEqual(len(samples), 5)


# ---------------------------------------------------------------------------
# Orchestrator state machine
# ---------------------------------------------------------------------------


class HealthReportServiceStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._services: list[HealthReportService] = []

    def tearDown(self) -> None:
        # Defensive: any service whose start_test() ran but who never
        # reached COMPLETE/CANCELLED leaks a daemon worker thread that
        # tight-loops at full CPU until process exit. Forcibly cancel
        # all services after each test so the collector joins cleanly.
        for service in self._services:
            try:
                service.cancel()
            except Exception:
                pass
        self._services = []

    def _make_service(
        self,
        *,
        samples: Optional[list[Sample]] = None,
        clock: Optional[_FakeClock] = None,
        rest_s: float = 0.005,
        rotation_s: float = 0.005,
        trigger_s: float = 0.005,
    ) -> tuple[HealthReportService, _FakeClock]:
        clk = clock or _FakeClock()
        provider = _ScriptedProvider(samples or [])
        service = HealthReportService(
            sample_provider=provider,
            device_context_provider=_device_provider,
            clock=clk,
            step_durations_s={
                HealthReportState.REST: rest_s,
                HealthReportState.ROTATION: rotation_s,
                HealthReportState.TRIGGER: trigger_s,
            },
            app_version="2.0.0",
            app_build_commit="testhash",
            local_timestamp_provider=lambda: "2026-05-22T20:00:00-07:00",
        )
        self._services.append(service)
        return service, clk

    def test_initial_state_is_idle(self) -> None:
        service, _ = self._make_service()

        self.assertEqual(service.state, HealthReportState.IDLE)
        self.assertIsNone(service.report)

    def test_begin_transitions_to_preface(self) -> None:
        service, _ = self._make_service()

        service.begin()

        self.assertEqual(service.state, HealthReportState.PREFACE)

    def test_begin_twice_raises_runtime_error(self) -> None:
        service, _ = self._make_service()
        service.begin()

        with self.assertRaises(RuntimeError):
            service.begin()

    def test_start_test_moves_to_ready_rest(self) -> None:
        service, _ = self._make_service()
        service.begin()

        service.start_test()

        # Sampling does NOT begin until the user clicks Start step 1.
        self.assertEqual(service.state, HealthReportState.READY_REST)
        self.assertIsNone(service._collector)

    def test_start_test_without_preface_raises(self) -> None:
        service, _ = self._make_service()

        with self.assertRaises(RuntimeError):
            service.start_test()

    def test_proceed_to_step_from_ready_rest_starts_collector_and_enters_rest(self) -> None:
        service, _ = self._make_service()
        service.begin()
        service.start_test()

        service.proceed_to_step()

        self.assertEqual(service.state, HealthReportState.REST)
        self.assertIsNotNone(service._collector)

    def test_proceed_to_step_from_idle_raises(self) -> None:
        service, _ = self._make_service()

        with self.assertRaises(RuntimeError):
            service.proceed_to_step()

    def test_proceed_to_step_from_rest_raises(self) -> None:
        service, _ = self._make_service()
        service.begin()
        service.start_test()
        service.proceed_to_step()  # -> REST

        with self.assertRaises(RuntimeError):
            service.proceed_to_step()

    def test_tick_at_end_of_rest_lands_in_ready_rotation(self) -> None:
        service, clk = self._make_service(rest_s=1.0)
        service.begin()
        service.start_test()
        service.proceed_to_step()

        clk.advance(1.5)
        service.tick()

        self.assertEqual(service.state, HealthReportState.READY_ROTATION)

    def test_tick_in_ready_state_is_noop(self) -> None:
        service, clk = self._make_service(rest_s=1.0)
        service.begin()
        service.start_test()

        clk.advance(99.0)
        service.tick()

        self.assertEqual(service.state, HealthReportState.READY_REST)

    def test_full_user_paced_run_walks_every_ready_pause(self) -> None:
        service, clk = self._make_service(rest_s=1.0, rotation_s=1.0, trigger_s=1.0)
        service.begin()
        service.start_test()
        self.assertEqual(service.state, HealthReportState.READY_REST)

        service.proceed_to_step()
        self.assertEqual(service.state, HealthReportState.REST)

        clk.advance(0.5)
        service.tick()
        self.assertEqual(service.state, HealthReportState.REST)

        clk.advance(0.6)
        service.tick()
        self.assertEqual(service.state, HealthReportState.READY_ROTATION)

        service.proceed_to_step()
        self.assertEqual(service.state, HealthReportState.ROTATION)

        clk.advance(1.5)
        service.tick()
        self.assertEqual(service.state, HealthReportState.READY_TRIGGER)

        service.proceed_to_step()
        self.assertEqual(service.state, HealthReportState.TRIGGER)

        clk.advance(1.5)
        service.tick()
        self.assertEqual(service.state, HealthReportState.COMPLETE)
        self.assertIsNotNone(service.report)

    def test_tick_in_idle_state_is_noop(self) -> None:
        service, _ = self._make_service()

        service.tick()
        self.assertEqual(service.state, HealthReportState.IDLE)

    # ------------------------------------------------------------------
    # Pause/resume wiring across READY pauses
    # ------------------------------------------------------------------

    def test_enter_ready_pause_pauses_collector(self) -> None:
        from zd_app.services.health_report import service as svc_module

        with patch.object(svc_module, "SampleCollector", _SpyCollector):
            service, clk = self._make_service(rest_s=1.0)
            service.begin()
            service.start_test()
            service.proceed_to_step()  # creates spy, transitions to REST

            collector = service._collector
            self.assertIsInstance(collector, _SpyCollector)
            self.assertEqual(collector.pause_calls, 0)

            # Advance past REST duration; tick lands in READY_ROTATION and
            # pauses the collector.
            clk.advance(1.5)
            service.tick()

            self.assertEqual(service.state, HealthReportState.READY_ROTATION)
            self.assertEqual(collector.pause_calls, 1)

    def test_proceed_to_step_resumes_collector_for_rotation(self) -> None:
        from zd_app.services.health_report import service as svc_module

        with patch.object(svc_module, "SampleCollector", _SpyCollector):
            service, clk = self._make_service(rest_s=1.0, rotation_s=1.0)
            service.begin()
            service.start_test()
            service.proceed_to_step()  # READY_REST -> REST (creates spy)
            collector = service._collector
            # First proceed_to_step calls resume() once on the freshly-
            # created collector — a documented no-op since _paused is False
            # at creation, but we keep the call symmetric across all three
            # transitions to avoid a special-case branch.
            self.assertEqual(collector.resume_calls, 1)

            clk.advance(1.5)
            service.tick()  # REST -> READY_ROTATION (collector.pause())
            self.assertEqual(service.state, HealthReportState.READY_ROTATION)
            self.assertEqual(collector.pause_calls, 1)

            service.proceed_to_step()  # READY_ROTATION -> ROTATION (resume())
            self.assertEqual(service.state, HealthReportState.ROTATION)
            self.assertEqual(collector.resume_calls, 2)

    def test_pause_resume_resilient_to_collector_exceptions(self) -> None:
        from zd_app.services.health_report import service as svc_module

        class _RaisingCollector(_SpyCollector):
            def pause(self) -> None:
                super().pause()
                raise RuntimeError("simulated pause failure")

            def resume(self) -> None:
                super().resume()
                raise RuntimeError("simulated resume failure")

        with patch.object(svc_module, "SampleCollector", _RaisingCollector):
            service, clk = self._make_service(rest_s=1.0, rotation_s=1.0)
            service.begin()
            service.start_test()
            # First proceed_to_step calls resume() — must not crash.
            service.proceed_to_step()
            self.assertEqual(service.state, HealthReportState.REST)

            # Tick into READY_ROTATION calls pause() — must not crash.
            clk.advance(1.5)
            service.tick()
            self.assertEqual(service.state, HealthReportState.READY_ROTATION)

            # And the next proceed_to_step calls resume() — must not crash.
            service.proceed_to_step()
            self.assertEqual(service.state, HealthReportState.ROTATION)

    def _run_to_complete(self, service, clk) -> None:
        service.begin()
        service.start_test()
        for _ in range(3):
            service.proceed_to_step()
            clk.advance(1.0)
            service.tick()

    def test_tick_in_complete_state_is_noop(self) -> None:
        service, clk = self._make_service()
        self._run_to_complete(service, clk)
        self.assertEqual(service.state, HealthReportState.COMPLETE)

        clk.advance(10.0)
        service.tick()
        self.assertEqual(service.state, HealthReportState.COMPLETE)

    def test_cancel_mid_rest_stops_collector_and_transitions(self) -> None:
        service, _ = self._make_service(rest_s=1.0)
        service.begin()
        service.start_test()
        service.proceed_to_step()

        service.cancel()

        self.assertEqual(service.state, HealthReportState.CANCELLED)
        self.assertIsNone(service.report)

    def test_cancel_from_ready_state_lands_in_cancelled(self) -> None:
        service, _ = self._make_service()
        service.begin()
        service.start_test()
        self.assertEqual(service.state, HealthReportState.READY_REST)

        service.cancel()

        self.assertEqual(service.state, HealthReportState.CANCELLED)

    def test_cancel_from_idle_is_safe(self) -> None:
        service, _ = self._make_service()

        service.cancel()

        self.assertEqual(service.state, HealthReportState.CANCELLED)

    def test_reset_returns_to_idle_and_clears_report(self) -> None:
        service, clk = self._make_service()
        self._run_to_complete(service, clk)
        self.assertEqual(service.state, HealthReportState.COMPLETE)

        service.reset()

        self.assertEqual(service.state, HealthReportState.IDLE)
        self.assertIsNone(service.report)

    def test_step_progress_reports_elapsed_and_remaining(self) -> None:
        service, clk = self._make_service(rest_s=2.0)
        service.begin()
        service.start_test()
        service.proceed_to_step()

        clk.advance(0.5)
        progress = service.step_progress()

        self.assertEqual(progress.state, HealthReportState.REST)
        self.assertAlmostEqual(progress.elapsed_s, 0.5)
        self.assertAlmostEqual(progress.duration_s, 2.0)
        self.assertAlmostEqual(progress.fraction, 0.25)
        self.assertAlmostEqual(progress.remaining_s, 1.5)

    def test_step_progress_in_ready_returns_zero_duration(self) -> None:
        service, _ = self._make_service()
        service.begin()
        service.start_test()

        progress = service.step_progress()

        # READY states have no countdown — the user controls when sampling
        # starts, so a 0-duration progress means "no progress bar yet".
        self.assertEqual(progress.state, HealthReportState.READY_REST)
        self.assertEqual(progress.duration_s, 0.0)
        self.assertEqual(progress.fraction, 0.0)
        self.assertEqual(progress.remaining_s, 0.0)

    def test_step_progress_in_idle_returns_zeros(self) -> None:
        service, _ = self._make_service()

        progress = service.step_progress()

        self.assertEqual(progress.state, HealthReportState.IDLE)
        self.assertEqual(progress.duration_s, 0.0)
        self.assertEqual(progress.fraction, 0.0)

    def test_caveats_make_it_into_the_final_report(self) -> None:
        service, clk = self._make_service()
        service.begin()
        service.start_test()
        service.add_caveat("Movement during rest, retest suggested")
        service.add_caveat("Trigger not pulled fully")
        # Empty / whitespace caveats are dropped.
        service.add_caveat("   ")

        for _ in range(3):
            service.proceed_to_step()
            clk.advance(1.0)
            service.tick()
        self.assertEqual(service.state, HealthReportState.COMPLETE)
        report = service.report
        assert report is not None

        self.assertEqual(
            report.caveats,
            ("Movement during rest, retest suggested", "Trigger not pulled fully"),
        )


# ---------------------------------------------------------------------------
# End-to-end integration: synthetic samples → final report
# ---------------------------------------------------------------------------


def _build_synthetic_run_samples(
    *,
    rest_duration_ns: int,
    rotation_duration_ns: int,
    trigger_duration_ns: int,
    rest_interval_ns: int = 1_000_000,
    rotation_interval_ns: int = 1_000_000,
    trigger_interval_ns: int = 1_000_000,
) -> tuple[list[Sample], int, int, int]:
    """Build a stitched (rest, rotation, trigger) sample sequence.

    Returns (samples, rest_end_ns, rotation_end_ns, trigger_end_ns) so the
    test can confirm the orchestrator's step boundaries align well enough
    that the measurements receive non-empty samples.
    """

    samples: list[Sample] = []
    packet = 0

    # Rest segment: quiet noise around center.
    t = 0
    rest_end = rest_duration_ns
    while t < rest_end:
        samples.append(Sample(
            timestamp_ns=t, packet_number=packet,
            left_stick_x=(packet % 7) - 3, left_stick_y=(packet % 5) - 2,
            right_stick_x=(packet % 11) - 5, right_stick_y=(packet % 13) - 6,
            left_trigger=0, right_trigger=0,
        ))
        packet += 1
        t += rest_interval_ns

    # Rotation segment: sweep 32 sectors at 85% of full axis travel (stick
    # storage is signed integer percent; see Sample docstring).
    rotation_start = t
    rotation_end = rotation_start + rotation_duration_ns
    while t < rotation_end:
        progress = (t - rotation_start) / (rotation_end - rotation_start)
        theta = progress * 4.0 * math.pi  # 2 revolutions
        lx = int(85 * math.cos(theta))
        ly = int(85 * math.sin(theta))
        samples.append(Sample(
            timestamp_ns=t, packet_number=packet,
            left_stick_x=lx, left_stick_y=ly,
            right_stick_x=lx, right_stick_y=ly,
            left_trigger=0, right_trigger=0,
        ))
        packet += 1
        t += rotation_interval_ns

    # Trigger segment: press-hold-release on both triggers.
    trigger_start = t
    trigger_end = trigger_start + trigger_duration_ns
    half = (trigger_end - trigger_start) // 2
    while t < trigger_end:
        offset = t - trigger_start
        if offset < half:
            v = int(255 * (offset / half))
        else:
            v = int(255 * (1.0 - (offset - half) / half))
        samples.append(Sample(
            timestamp_ns=t, packet_number=packet,
            left_stick_x=0, left_stick_y=0,
            right_stick_x=0, right_stick_y=0,
            left_trigger=v, right_trigger=v,
        ))
        packet += 1
        t += trigger_interval_ns

    return samples, rest_end, rotation_end, trigger_end


class HealthReportEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self._services: list[HealthReportService] = []

    def tearDown(self) -> None:
        for service in self._services:
            try:
                service.cancel()
            except Exception:
                pass
        self._services = []

    def test_synthetic_full_run_produces_populated_report(self) -> None:
        rest_dur_s = 0.5
        rotation_dur_s = 0.5
        trigger_dur_s = 0.5
        rest_ns = int(rest_dur_s * 1_000_000_000)
        rotation_ns = int(rotation_dur_s * 1_000_000_000)
        trigger_ns = int(trigger_dur_s * 1_000_000_000)
        # Use a fairly tight interval so each step gets ~500 samples (> all
        # min thresholds for GOOD quality).
        interval_ns = 1_000_000   # 1 ms = 1 kHz

        samples_data, _, _, _ = _build_synthetic_run_samples(
            rest_duration_ns=rest_ns,
            rotation_duration_ns=rotation_ns,
            trigger_duration_ns=trigger_ns,
            rest_interval_ns=interval_ns,
            rotation_interval_ns=interval_ns,
            trigger_interval_ns=interval_ns,
        )

        # Provider delivers one sample per call, then None. We don't simulate
        # perf_counter_ns; we use the samples' own ns timestamps which align
        # with the orchestrator's perf_counter_ns boundaries within ±tick.
        provider = _ScriptedProvider(samples_data)
        clock_start = 1000.0
        clk = _FakeClock(start=clock_start)

        # Build a service whose perf_counter_ns(0) maps onto sample t=0 by
        # patching perf_counter_ns through the service constructor — but the
        # service uses time.perf_counter_ns directly. Instead, the sample
        # timestamps don't need to align exactly: filter_samples_by_time_range
        # is forgiving when the orchestrator's perf_counter_ns starts well
        # after time 0. We use a much-greater-than-sample-timestamp start so
        # the orchestrator's slicing falls fully inside the provider stream.
        #
        # Concretely: orchestrator's t_zero_ns = perf_counter_ns(); step
        # boundaries are computed in perf_counter_ns space. The sample
        # provider returns samples whose timestamps are 0..(rest+rot+trig)ns.
        # If perf_counter_ns starts at >> total_ns, NO samples fall in the
        # step windows — they're all "before" the run.
        #
        # Fix: monkey-patch a time source so the orchestrator anchors at 0.
        from zd_app.services.health_report import service as svc_module

            # Capture-and-restore so the test is self-contained.
        original_perf_counter_ns = time.perf_counter_ns
        ns_clock_state = {"n": 0}

        def fake_perf_counter_ns() -> int:
            return ns_clock_state["n"]

        time.perf_counter_ns = fake_perf_counter_ns

        try:
            service = HealthReportService(
                sample_provider=provider,
                device_context_provider=_device_provider,
                clock=clk,
                step_durations_s={
                    HealthReportState.REST: rest_dur_s,
                    HealthReportState.ROTATION: rotation_dur_s,
                    HealthReportState.TRIGGER: trigger_dur_s,
                },
                app_version="2.0.0",
                app_build_commit="testhash",
                local_timestamp_provider=lambda: "2026-05-22T20:00:00-07:00",
            )
            self._services.append(service)

            service.begin()
            ns_clock_state["n"] = 0
            service.start_test()
            # PREFACE -> READY_REST: collector hasn't started yet. The
            # first proceed_to_step() spins it up (and anchors t_zero at
            # our patched perf_counter_ns == 0).
            service.proceed_to_step()

            # Drain the provider into the collector before clock-advancing,
            # then move the clock past each step boundary.
            deadline = time.perf_counter() + 1.0
            assert service._collector is not None
            while time.perf_counter() < deadline:
                if len(service._collector.snapshot_samples()) >= len(samples_data):
                    break
                time.sleep(0.01)

            # Advance clock + ns clock in lockstep, tick to land in the
            # READY_* gate, then proceed_to_step() into the next sampling
            # state. The trigger step ends in COMPLETE so no proceed call
            # follows it.
            for advance_s, ns_target, is_last in (
                (rest_dur_s, rest_ns, False),
                (rotation_dur_s, rest_ns + rotation_ns, False),
                (trigger_dur_s, rest_ns + rotation_ns + trigger_ns, True),
            ):
                clk.advance(advance_s)
                ns_clock_state["n"] = ns_target
                service.tick()
                if not is_last:
                    service.proceed_to_step()

            self.assertEqual(service.state, HealthReportState.COMPLETE)
            report: HealthReport = service.report
            assert report is not None
        finally:
            time.perf_counter_ns = original_perf_counter_ns

        # Report shape assertions
        self.assertEqual(report.app_version, "2.0.0")
        self.assertEqual(report.app_build_commit, "testhash")
        self.assertEqual(report.device.controller_name, "ZD Ultimate Legend")
        self.assertEqual(report.device.configured_polling_hz, 1000)

        # Step measurements should have non-zero sample counts.
        self.assertGreater(report.stick_rest_noise.left.sample_count, 0)
        self.assertGreater(report.stick_range.left.sample_count, 0)
        self.assertGreater(report.trigger_range.left.sample_count, 0)
        # Overall status is one of the qualitative labels (no 0-100 score).
        self.assertIn(
            report.overall_status,
            {
                OverallStatus.NORMAL,
                OverallStatus.TUNING_SUGGESTED,
                OverallStatus.RETEST_RECOMMENDED,
                OverallStatus.POSSIBLE_ISSUE,
            },
        )

class WearLedgerIntegrationTests(unittest.TestCase):
    """HealthReportService emits a wear-ledger event on finalize."""

    def test_finalize_emits_health_report_event(self) -> None:
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from zd_app.services.wear_ledger import WearLedgerService

        with tempfile.TemporaryDirectory() as tmp:
            ledger = WearLedgerService(base_dir=Path(tmp) / "wear_ledger")
            service = HealthReportService(
                sample_provider=lambda: None,
                device_context_provider=_device_provider,
                wear_ledger=ledger,
            )
            service._device_at_start = _device_provider()
            service._step_starts_at_ns = {
                HealthReportState.REST: 0,
                HealthReportState.ROTATION: 100,
                HealthReportState.TRIGGER: 200,
            }
            service._step_ends_at_ns = {
                HealthReportState.REST: 99,
                HealthReportState.ROTATION: 199,
                HealthReportState.TRIGGER: 299,
            }
            # Drive _finalize() via a minimal fake collector that returns no
            # samples. This exercises the post-collector hook path in
            # production code (not a re-call of the hook block in the test).
            service._collector = SimpleNamespace(
                stop=lambda *, join_timeout_s=2.0: [],
                running=False,
            )
            service._finalize()

            self.assertEqual(service.state, HealthReportState.COMPLETE)
            self.assertIsNotNone(service.report)

            events = ledger.read_events()
            health_events = [e for e in events if e.event_type == "health_report"]
            self.assertEqual(len(health_events), 1)
            self.assertIn("overall_status", health_events[0].details)
            assert service.report is not None
            self.assertEqual(
                health_events[0].details["overall_status"],
                service.report.overall_status.value,
            )

    def test_finalize_scrubs_controller_name_in_ledger_detail(self) -> None:
        """A home-path-shaped controller_name is scrubbed before it lands in
        the wear-ledger HEALTH_REPORT event detail (defensive consistency with
        the exporters, which scrub controller_name)."""
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from zd_app.services.wear_ledger import WearLedgerService

        leaky_name = r"ZD @ C:\Users\humphrey\AppData\controller"

        def _leaky_device_provider(*, hz: int = 1000) -> DeviceContext:
            return DeviceContext(
                controller_name=leaky_name,
                configured_polling_hz=hz,
                profile_name="Apex",
            )

        with tempfile.TemporaryDirectory() as tmp:
            ledger = WearLedgerService(base_dir=Path(tmp) / "wear_ledger")
            service = HealthReportService(
                sample_provider=lambda: None,
                device_context_provider=_leaky_device_provider,
                wear_ledger=ledger,
            )
            service._device_at_start = _leaky_device_provider()
            service._step_starts_at_ns = {
                HealthReportState.REST: 0,
                HealthReportState.ROTATION: 100,
                HealthReportState.TRIGGER: 200,
            }
            service._step_ends_at_ns = {
                HealthReportState.REST: 99,
                HealthReportState.ROTATION: 199,
                HealthReportState.TRIGGER: 299,
            }
            service._collector = SimpleNamespace(
                stop=lambda *, join_timeout_s=2.0: [],
                running=False,
            )
            service._finalize()

            events = ledger.read_events()
            health_events = [e for e in events if e.event_type == "health_report"]
            self.assertEqual(len(health_events), 1)
            detail = health_events[0].details["device_controller_name"]
            self.assertNotIn(r"C:\Users", detail)
            self.assertNotIn("humphrey", detail)

    def test_finalize_without_ledger_does_not_crash(self) -> None:
        from types import SimpleNamespace

        service = HealthReportService(
            sample_provider=lambda: None,
            device_context_provider=_device_provider,
        )
        service._device_at_start = _device_provider()
        service._step_starts_at_ns = {
            HealthReportState.REST: 0,
            HealthReportState.ROTATION: 100,
            HealthReportState.TRIGGER: 200,
        }
        service._step_ends_at_ns = {
            HealthReportState.REST: 99,
            HealthReportState.ROTATION: 199,
            HealthReportState.TRIGGER: 299,
        }
        service._collector = SimpleNamespace(
            stop=lambda *, join_timeout_s=2.0: [],
            running=False,
        )
        service._finalize()
        self.assertEqual(service.state, HealthReportState.COMPLETE)


if __name__ == "__main__":
    unittest.main()
