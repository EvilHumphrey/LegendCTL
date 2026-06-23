"""Tests for :mod:`zd_app.services.module_passport.characterize`.

Two halves:

1. **Metric pure functions** — synthetic samples drive each of the four
   new metrics (asymmetry, bitness, tremor, linearity) plus the outer-
   deadzone band helper, asserting they produce sensible values for
   known-good / known-worn / empty inputs.
2. **classify_fingerprint** — assembles per-phase fixture samples for
   each of the three overall-status bands and asserts the verdict
   reduction.
3. **CharacterizationOrchestrator** — fake-clock walk through the state
   machine (start -> READY_REST -> REST -> READY_ROTATION -> ROTATION ->
   READY_SETTLE -> SETTLE -> COMPLETE), asserting transitions, the cancel
   path, and the COMPLETE-time fingerprint construction.
"""

from __future__ import annotations

import math
import time
import unittest
from itertools import count
from typing import Optional

from zd_app.services.health_report.models import Sample
from zd_app.services.module_passport.characterize import (
    ASYMMETRY_GOOD,
    ASYMMETRY_WATCH,
    BITNESS_GOOD,
    BITNESS_WATCH,
    CENTERING_OFFSET_GOOD_PCT,
    CHARACTERIZATION_TOTAL_S,
    CIRCULARITY_COVERAGE_GOOD,
    CharacterizationOrchestrator,
    CharacterizationPhase,
    CharacterizationState,
    LINEARITY_GOOD,
    LINEARITY_WATCH,
    NOISE_FLOOR_GOOD_PCT,
    NOISE_FLOOR_WATCH_PCT,
    OUTER_DEADZONE_GOOD_SPAN,
    OUTER_DEADZONE_WATCH_SPAN,
    PHASE_DURATIONS_S,
    PHASE_ORDER,
    REST_DURATION_S,
    ROTATION_DURATION_S,
    SETTLE_DURATION_S,
    TREMOR_GOOD,
    TREMOR_WATCH,
    classify_fingerprint,
    compute_asymmetry_score,
    compute_bitness_observed,
    compute_linearity_score,
    compute_outer_deadzone_band,
    compute_tremor_metric,
)
from zd_app.storage.module_passport_models import (
    SIDE_LEFT,
    STATUS_GOOD,
    STATUS_WATCH,
    STATUS_WEAR_OBSERVED,
)


# ---------------------------------------------------------------------------
# Sample builders
# ---------------------------------------------------------------------------


def _rest_samples(
    n: int,
    *,
    side: str = SIDE_LEFT,
    noise: float = 0.0,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
) -> list[Sample]:
    """Build n rest samples sitting near (offset_x, offset_y) +/- noise."""

    samples: list[Sample] = []
    for i in range(n):
        # Use a deterministic "noise" pattern so the test is repeatable;
        # alternating signs keep mean centred but inject ±noise.
        jitter = noise * ((i % 5) - 2) / 2.0
        x = int(round(offset_x + jitter))
        y = int(round(offset_y - jitter))
        kwargs = {
            "timestamp_ns": i * 1_000_000,
            "packet_number": i,
            "left_stick_x": 0,
            "left_stick_y": 0,
            "right_stick_x": 0,
            "right_stick_y": 0,
            "left_trigger": 0,
            "right_trigger": 0,
        }
        if side == SIDE_LEFT:
            kwargs["left_stick_x"] = x
            kwargs["left_stick_y"] = y
        else:
            kwargs["right_stick_x"] = x
            kwargs["right_stick_y"] = y
        samples.append(Sample(**kwargs))
    return samples


def _rotation_samples(
    n: int,
    *,
    side: str = SIDE_LEFT,
    radius: float = 90.0,
    radius_jitter: float = 0.0,
    quadrant_radii: Optional[dict[int, float]] = None,
    start_offset_ns: int = 200_000_000,
) -> list[Sample]:
    """Trace a circle of n samples at radius (optionally per-quadrant variance)."""

    samples: list[Sample] = []
    for i in range(n):
        theta = (i / n) * 2.0 * math.pi
        q = int((theta / (2.0 * math.pi)) * 4) % 4
        r = quadrant_radii.get(q, radius) if quadrant_radii else radius
        r += radius_jitter * math.sin(theta * 4)
        x = int(round(r * math.cos(theta)))
        y = int(round(r * math.sin(theta)))
        kwargs = {
            "timestamp_ns": start_offset_ns + i * 1_000_000,
            "packet_number": i + 10_000,
            "left_stick_x": 0,
            "left_stick_y": 0,
            "right_stick_x": 0,
            "right_stick_y": 0,
            "left_trigger": 0,
            "right_trigger": 0,
        }
        if side == SIDE_LEFT:
            kwargs["left_stick_x"] = x
            kwargs["left_stick_y"] = y
        else:
            kwargs["right_stick_x"] = x
            kwargs["right_stick_y"] = y
        samples.append(Sample(**kwargs))
    return samples


def _settle_samples(
    n: int,
    *,
    side: str = SIDE_LEFT,
    distinct: int = 50,
    start_offset_ns: int = 600_000_000,
) -> list[Sample]:
    """Return-to-rest trace with ``distinct`` unique (x, y) tuples."""

    samples: list[Sample] = []
    for i in range(n):
        # Cycle through `distinct` unique values then loop.
        step = i % distinct
        x = int(round((distinct - 1 - step) * 100 / max(1, distinct - 1)))
        y = int(round((distinct - 1 - step) * 50 / max(1, distinct - 1)))
        kwargs = {
            "timestamp_ns": start_offset_ns + i * 1_000_000,
            "packet_number": i + 20_000,
            "left_stick_x": 0,
            "left_stick_y": 0,
            "right_stick_x": 0,
            "right_stick_y": 0,
            "left_trigger": 0,
            "right_trigger": 0,
        }
        if side == SIDE_LEFT:
            kwargs["left_stick_x"] = x
            kwargs["left_stick_y"] = y
        else:
            kwargs["right_stick_x"] = x
            kwargs["right_stick_y"] = y
        samples.append(Sample(**kwargs))
    return samples


# ---------------------------------------------------------------------------
# Asymmetry
# ---------------------------------------------------------------------------


class AsymmetryTests(unittest.TestCase):
    def test_uniform_circle_is_symmetric(self) -> None:
        samples = _rotation_samples(360, radius=80.0)
        self.assertLess(compute_asymmetry_score(samples, side=SIDE_LEFT), 0.05)

    def test_one_quadrant_short_increases_score(self) -> None:
        # Quadrant 0 reaches only 40 percent; others reach 80 percent.
        samples = _rotation_samples(
            360, radius=80.0, quadrant_radii={0: 40.0}
        )
        score = compute_asymmetry_score(samples, side=SIDE_LEFT)
        self.assertGreater(score, ASYMMETRY_GOOD)

    def test_empty_samples_returns_zero(self) -> None:
        self.assertEqual(compute_asymmetry_score([], side=SIDE_LEFT), 0.0)


# ---------------------------------------------------------------------------
# Bitness
# ---------------------------------------------------------------------------


class BitnessTests(unittest.TestCase):
    def test_many_distinct_values_high_bitness(self) -> None:
        samples = _settle_samples(200, distinct=80)
        self.assertGreaterEqual(
            compute_bitness_observed(samples, side=SIDE_LEFT),
            BITNESS_GOOD,
        )

    def test_few_distinct_values_low_bitness(self) -> None:
        samples = _settle_samples(200, distinct=5)
        self.assertLess(
            compute_bitness_observed(samples, side=SIDE_LEFT),
            BITNESS_WATCH,
        )

    def test_empty_returns_zero(self) -> None:
        self.assertEqual(compute_bitness_observed([], side=SIDE_LEFT), 0)


# ---------------------------------------------------------------------------
# Tremor
# ---------------------------------------------------------------------------


class TremorTests(unittest.TestCase):
    def test_no_jitter_zero_tremor(self) -> None:
        samples = _rest_samples(120, noise=0.0)
        self.assertEqual(compute_tremor_metric(samples, side=SIDE_LEFT), 0.0)

    def test_higher_noise_higher_tremor(self) -> None:
        quiet = _rest_samples(120, noise=0.4)
        noisy = _rest_samples(120, noise=4.0)
        self.assertLess(
            compute_tremor_metric(quiet, side=SIDE_LEFT),
            compute_tremor_metric(noisy, side=SIDE_LEFT),
        )

    def test_fewer_than_two_samples_returns_zero(self) -> None:
        self.assertEqual(compute_tremor_metric([], side=SIDE_LEFT), 0.0)
        single = _rest_samples(1, noise=0.0)
        self.assertEqual(compute_tremor_metric(single, side=SIDE_LEFT), 0.0)


# ---------------------------------------------------------------------------
# Linearity
# ---------------------------------------------------------------------------


class LinearityTests(unittest.TestCase):
    def test_constant_radius_low_score(self) -> None:
        samples = _rotation_samples(360, radius=85.0, radius_jitter=0.0)
        self.assertLess(compute_linearity_score(samples, side=SIDE_LEFT), LINEARITY_GOOD)

    def test_radius_wobble_higher_score(self) -> None:
        samples = _rotation_samples(360, radius=85.0, radius_jitter=30.0)
        self.assertGreater(
            compute_linearity_score(samples, side=SIDE_LEFT),
            LINEARITY_GOOD,
        )

    def test_empty_returns_zero(self) -> None:
        self.assertEqual(compute_linearity_score([], side=SIDE_LEFT), 0.0)


# ---------------------------------------------------------------------------
# Outer deadzone band
# ---------------------------------------------------------------------------


class OuterDeadzoneBandTests(unittest.TestCase):
    def test_uniform_circle_narrow_span(self) -> None:
        samples = _rotation_samples(360, radius=95.0)
        lo, hi = compute_outer_deadzone_band(samples, side=SIDE_LEFT)
        self.assertLess(hi - lo, OUTER_DEADZONE_GOOD_SPAN)

    def test_quadrant_under_reach_wide_span(self) -> None:
        samples = _rotation_samples(
            360, radius=95.0, quadrant_radii={0: 60.0}
        )
        lo, hi = compute_outer_deadzone_band(samples, side=SIDE_LEFT)
        self.assertGreater(hi - lo, OUTER_DEADZONE_WATCH_SPAN)

    def test_empty_returns_zero_zero(self) -> None:
        self.assertEqual(
            compute_outer_deadzone_band([], side=SIDE_LEFT),
            (0.0, 0.0),
        )


# ---------------------------------------------------------------------------
# classify_fingerprint — verdict reduction
# ---------------------------------------------------------------------------


class ClassifyFingerprintTests(unittest.TestCase):
    def _classify(
        self,
        *,
        rest: list[Sample],
        rotation: list[Sample],
        settle: list[Sample],
    ):
        return classify_fingerprint(
            timestamp_utc="2026-05-26T18:42:11Z",
            side=SIDE_LEFT,
            duration_ms=60_000,
            rest_samples=rest,
            rotation_samples=rotation,
            settle_samples=settle,
        )

    def test_clean_module_gets_good_verdict(self) -> None:
        rest = _rest_samples(600, noise=0.0)
        rotation = _rotation_samples(720, radius=95.0)
        settle = _settle_samples(400, distinct=80)
        fp = self._classify(rest=rest, rotation=rotation, settle=settle)
        self.assertEqual(fp.overall_status, STATUS_GOOD)
        self.assertLess(fp.noise_floor_percent, NOISE_FLOOR_GOOD_PCT)
        self.assertGreaterEqual(fp.circularity_coverage_percent, CIRCULARITY_COVERAGE_GOOD)

    def test_worn_module_gets_wear_observed(self) -> None:
        rest = _rest_samples(600, noise=10.0)  # very noisy rest
        rotation = _rotation_samples(
            720, radius=80.0, quadrant_radii={0: 30.0}  # corner can't reach
        )
        settle = _settle_samples(400, distinct=4)  # quantised
        fp = self._classify(rest=rest, rotation=rotation, settle=settle)
        self.assertEqual(fp.overall_status, STATUS_WEAR_OBSERVED)

    def test_borderline_module_gets_watch(self) -> None:
        # Noise just above good (around 3.5%) -> rest_noise -> "watch".
        rest = _rest_samples(600, noise=3.5)
        rotation = _rotation_samples(720, radius=95.0)
        settle = _settle_samples(400, distinct=80)
        fp = self._classify(rest=rest, rotation=rotation, settle=settle)
        self.assertIn(fp.overall_status, (STATUS_WATCH, STATUS_WEAR_OBSERVED))
        self.assertNotEqual(fp.overall_status, STATUS_GOOD)

    def test_insufficient_samples_forces_wear_observed(self) -> None:
        rest = _rest_samples(5, noise=0.0)
        rotation = _rotation_samples(8, radius=90.0)
        settle = _settle_samples(10, distinct=4)
        fp = self._classify(rest=rest, rotation=rotation, settle=settle)
        self.assertEqual(fp.overall_status, STATUS_WEAR_OBSERVED)

    def test_returns_full_metric_payload(self) -> None:
        rest = _rest_samples(600, noise=0.0)
        rotation = _rotation_samples(720, radius=95.0)
        settle = _settle_samples(400, distinct=80)
        fp = self._classify(rest=rest, rotation=rotation, settle=settle)
        self.assertEqual(fp.timestamp_utc, "2026-05-26T18:42:11Z")
        self.assertEqual(fp.side, SIDE_LEFT)
        self.assertEqual(fp.duration_ms, 60_000)
        self.assertEqual(fp.samples_count, len(rest) + len(rotation) + len(settle))
        self.assertGreater(fp.bitness_observed, 0)
        self.assertGreaterEqual(fp.outer_deadzone_max_axis, fp.outer_deadzone_min_axis)

    def test_rejects_invalid_side(self) -> None:
        with self.assertRaises(ValueError):
            classify_fingerprint(
                timestamp_utc="2026-05-26T18:42:11Z",
                side="middle",
                duration_ms=60_000,
                rest_samples=[],
                rotation_samples=[],
                settle_samples=[],
            )


# ---------------------------------------------------------------------------
# Phase / budget constants
# ---------------------------------------------------------------------------


class PhaseBudgetTests(unittest.TestCase):
    def test_total_budget_is_60s(self) -> None:
        self.assertEqual(CHARACTERIZATION_TOTAL_S, 60.0)
        self.assertEqual(
            REST_DURATION_S + ROTATION_DURATION_S + SETTLE_DURATION_S,
            60.0,
        )

    def test_phase_order_known(self) -> None:
        self.assertEqual(
            PHASE_ORDER,
            (
                CharacterizationPhase.REST,
                CharacterizationPhase.ROTATION,
                CharacterizationPhase.SETTLE,
            ),
        )

    def test_phase_durations_dict_complete(self) -> None:
        self.assertEqual(set(PHASE_DURATIONS_S.keys()), set(PHASE_ORDER))


# ---------------------------------------------------------------------------
# Orchestrator state machine
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


class _ScriptedProvider:
    def __init__(self, samples: list[Sample]) -> None:
        self._samples = list(samples)
        self._idx = 0

    def __call__(self) -> Optional[Sample]:
        if self._idx >= len(self._samples):
            return None
        s = self._samples[self._idx]
        self._idx += 1
        return s


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _FakeClock()
        # Generate enough samples for the worst-case phase length; provider
        # returns None after the list is consumed (collector tolerates None).
        rest = _rest_samples(2000, noise=0.0)
        rotation = _rotation_samples(2000, radius=95.0)
        settle = _settle_samples(2000, distinct=80)
        self.provider = _ScriptedProvider(rest + rotation + settle)
        self.orchestrator = CharacterizationOrchestrator(
            side=SIDE_LEFT,
            sample_provider=self.provider,
            clock=self.clock,
            utc_now_iso=lambda: "2026-05-26T18:42:11Z",
        )

    def tearDown(self) -> None:
        # Force teardown of the daemon collector thread, if any.
        if self.orchestrator.state != CharacterizationState.IDLE:
            self.orchestrator.reset()

    def test_initial_state_is_idle(self) -> None:
        self.assertEqual(self.orchestrator.state, CharacterizationState.IDLE)

    def test_start_transitions_to_ready_rest(self) -> None:
        self.orchestrator.start()
        self.assertEqual(self.orchestrator.state, CharacterizationState.READY_REST)

    def test_start_from_non_idle_raises(self) -> None:
        self.orchestrator.start()
        with self.assertRaises(RuntimeError):
            self.orchestrator.start()

    def test_full_walkthrough_reaches_complete(self) -> None:
        self.orchestrator.start()
        # READY_REST -> REST
        self.orchestrator.proceed_to_step()
        self.assertEqual(self.orchestrator.state, CharacterizationState.REST)
        # Let the rest phase elapse.
        self.clock.advance(REST_DURATION_S + 0.1)
        # Give the daemon collector a moment to enqueue samples.
        time.sleep(0.05)
        self.orchestrator.tick()
        self.assertEqual(self.orchestrator.state, CharacterizationState.READY_ROTATION)
        # READY_ROTATION -> ROTATION
        self.orchestrator.proceed_to_step()
        self.assertEqual(self.orchestrator.state, CharacterizationState.ROTATION)
        self.clock.advance(ROTATION_DURATION_S + 0.1)
        time.sleep(0.05)
        self.orchestrator.tick()
        self.assertEqual(self.orchestrator.state, CharacterizationState.READY_SETTLE)
        # READY_SETTLE -> SETTLE -> COMPLETE
        self.orchestrator.proceed_to_step()
        self.assertEqual(self.orchestrator.state, CharacterizationState.SETTLE)
        self.clock.advance(SETTLE_DURATION_S + 0.1)
        time.sleep(0.05)
        self.orchestrator.tick()
        self.assertEqual(self.orchestrator.state, CharacterizationState.COMPLETE)
        self.assertIsNotNone(self.orchestrator.fingerprint)

    def test_proceed_to_step_from_non_ready_raises(self) -> None:
        self.orchestrator.start()
        self.orchestrator.proceed_to_step()  # READY_REST -> REST
        with self.assertRaises(RuntimeError):
            self.orchestrator.proceed_to_step()

    def test_tick_no_op_in_ready_state(self) -> None:
        self.orchestrator.start()
        before = self.orchestrator.state
        self.clock.advance(100.0)
        self.orchestrator.tick()
        self.assertEqual(self.orchestrator.state, before)

    def test_cancel_transitions_to_cancelled_from_any_state(self) -> None:
        self.orchestrator.start()
        self.orchestrator.proceed_to_step()
        self.orchestrator.cancel()
        self.assertEqual(self.orchestrator.state, CharacterizationState.CANCELLED)

    def test_reset_returns_to_idle(self) -> None:
        self.orchestrator.start()
        self.orchestrator.proceed_to_step()
        self.orchestrator.reset()
        self.assertEqual(self.orchestrator.state, CharacterizationState.IDLE)
        self.assertIsNone(self.orchestrator.fingerprint)

    def test_progress_during_rest_phase(self) -> None:
        self.orchestrator.start()
        self.orchestrator.proceed_to_step()
        self.clock.advance(REST_DURATION_S / 2)
        progress = self.orchestrator.progress()
        self.assertEqual(progress.phase, CharacterizationPhase.REST)
        self.assertAlmostEqual(progress.phase_fraction, 0.5, places=2)

    def test_total_budget_property(self) -> None:
        self.assertEqual(self.orchestrator.total_budget_s, CHARACTERIZATION_TOTAL_S)

    def test_rejects_invalid_side_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            CharacterizationOrchestrator(
                side="middle",
                sample_provider=self.provider,
            )


if __name__ == "__main__":
    unittest.main()
