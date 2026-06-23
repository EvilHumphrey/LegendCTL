"""Unit tests for the Health Report pure-function measurements.

The synthetic-sample fixtures in this file are the canonical specification of
how the §1-§4 statistics ought to behave; if a measurement function changes
semantics, the failing test name should make the intent obvious.

The measurement definitions follow the documented Health Report measurement contract.
"""

from __future__ import annotations

import math
import random
import unittest

from zd_app.services.health_report import (
    CadenceQualityLabel,
    OverallStatus,
    RangeCoverageLabel,
    RestNoiseLabel,
    Sample,
    SampleQuality,
    TriggerSmoothnessLabel,
    compute_observed_cadence,
    compute_overall_status,
    compute_stick_range,
    compute_stick_rest_noise,
    compute_trigger_range,
    filter_samples_by_time_range,
)
from zd_app.services.health_report.measurements import (
    CADENCE_CONSISTENT_GAP_RATIO,
    CADENCE_GAP_2X_FLOOR_MS,
    CADENCE_GAP_2X_RATIO,
    CADENCE_GAP_4X_FLOOR_MS,
    CADENCE_GAP_4X_RATIO,
    GOOD_REST_SAMPLE_COUNT,
    REST_NOISE_DEADZONE_MARGIN_PCT,
    REST_NOISE_GOOD_P99_PCT,
    REST_NOISE_NOTICEABLE_P99_PCT,
    TRIGGER_NOISY_VIOLATION_RATIO,
    _effective_gap_threshold_ms,
    _trigger_smoothness_label,
)


# ---------------------------------------------------------------------------
# Sample builders
# ---------------------------------------------------------------------------


def _sample(
    *,
    t_ns: int,
    packet: int,
    lx: int = 0,
    ly: int = 0,
    rx: int = 0,
    ry: int = 0,
    lt: int = 0,
    rt: int = 0,
) -> Sample:
    return Sample(
        timestamp_ns=t_ns,
        packet_number=packet,
        left_stick_x=lx,
        left_stick_y=ly,
        right_stick_x=rx,
        right_stick_y=ry,
        left_trigger=lt,
        right_trigger=rt,
    )


def _rest_samples(*, n: int, noise_amplitude: int, center: tuple[int, int] = (0, 0),
                  interval_ns: int = 1_000_000, seed: int = 0) -> list[Sample]:
    """Generate ``n`` samples with deterministic radial noise around ``center``."""

    rng = random.Random(seed)
    samples = []
    cx, cy = center
    for i in range(n):
        theta = rng.random() * 2.0 * math.pi
        r = rng.random() * noise_amplitude
        x = int(round(cx + r * math.cos(theta)))
        y = int(round(cy + r * math.sin(theta)))
        samples.append(_sample(t_ns=i * interval_ns, packet=i, lx=x, ly=y))
    return samples


def _rotation_samples(*, sectors_covered: int, samples_per_sector: int,
                      radius: int, interval_ns: int = 1_000_000,
                      total_sectors: int = 32) -> list[Sample]:
    """Sweep ``sectors_covered`` of ``total_sectors`` at the given radius."""

    samples = []
    t = 0
    packet = 0
    for sector_idx in range(sectors_covered):
        # Sample inside the sector, away from edges so the bucket is unambiguous.
        for j in range(samples_per_sector):
            angle_fraction = (sector_idx + (j + 0.5) / samples_per_sector) / total_sectors
            theta = angle_fraction * 2.0 * math.pi
            x = int(round(radius * math.cos(theta)))
            y = int(round(radius * math.sin(theta)))
            samples.append(_sample(t_ns=t, packet=packet, lx=x, ly=y))
            t += interval_ns
            packet += 1
    return samples


def _trigger_pull_release_samples(
    *,
    min_value: int = 0,
    max_value: int = 255,
    samples_per_phase: int = 20,
    interval_ns: int = 1_000_000,
    side: str = "left",
) -> list[Sample]:
    """Generate a press-hold-release trigger curve."""

    samples = []
    t = 0
    packet = 0

    def emit(value: int) -> None:
        nonlocal t, packet
        if side == "left":
            samples.append(_sample(t_ns=t, packet=packet, lt=value))
        else:
            samples.append(_sample(t_ns=t, packet=packet, rt=value))
        t += interval_ns
        packet += 1

    # Press
    for i in range(samples_per_phase):
        v = min_value + int((max_value - min_value) * (i + 1) / samples_per_phase)
        emit(v)
    # Hold (small noise tolerated)
    for _ in range(samples_per_phase // 2):
        emit(max_value)
    # Release
    for i in range(samples_per_phase):
        v = max_value - int((max_value - min_value) * (i + 1) / samples_per_phase)
        emit(v)
    return samples


def _cadence_samples_at_hz(
    *,
    hz: int,
    duration_s: float,
    jitter_ms: float = 0.0,
    extra_gap_count: int = 0,
    extra_gap_ms: float = 0.0,
    seed: int = 0,
) -> list[Sample]:
    """Generate cadence samples with optional uniform jitter + planned gaps.

    Each sample has its own packet_number so cadence picks up every one.
    """

    rng = random.Random(seed)
    interval_ms = 1000.0 / hz
    n = int(duration_s * hz)
    t_ns = 0
    samples = []
    for packet in range(n):
        samples.append(_sample(t_ns=t_ns, packet=packet))
        wait_ms = interval_ms
        if jitter_ms:
            wait_ms += (rng.random() - 0.5) * 2 * jitter_ms
        if packet < extra_gap_count:
            wait_ms = max(wait_ms, extra_gap_ms)
        t_ns += int(wait_ms * 1_000_000)
    return samples


def _cadence_samples_with_realistic_8khz_jitter(
    *,
    n: int,
    seed: int = 0,
    long_gap_count: int = 0,
    long_gap_ms: float = 15.0,
) -> list[Sample]:
    """Generate an 8 kHz sample stream with Windows-realistic jitter.

    About 94% of inter-poll intervals are tight (≈0.125 ms with sub-jitter
    noise) and about 6% are bumped into the 0.3-3.5 ms range, mirroring
    the Windows app-layer scheduling slips that show up at 8 kHz on healthy
    hardware. ``long_gap_count`` (when > 0) forces that many spread-out
    intervals to ``long_gap_ms`` to simulate the rare driver burp that
    should still trip the gap counters even with the threshold floor in
    place. Seeded deterministically so test runs are repeatable.
    """

    rng = random.Random(seed)
    long_gap_indices: set[int] = set()
    if long_gap_count > 0:
        long_gap_indices = {int(i * n / long_gap_count) for i in range(long_gap_count)}
    samples: list[Sample] = []
    t_ns = 0
    for packet in range(n):
        samples.append(_sample(t_ns=t_ns, packet=packet))
        if packet in long_gap_indices:
            wait_ms = long_gap_ms
        else:
            r = rng.random()
            if r < 0.06:
                wait_ms = rng.uniform(0.3, 3.5)
            else:
                wait_ms = max(0.0, rng.gauss(0.125, 0.02))
        t_ns += int(wait_ms * 1_000_000)
    return samples


def _segmented_cadence_samples(
    *,
    n_segments: int = 3,
    segment_samples: int = 200,
    interval_ns: int = 1_000_000,
    pause_ns: int = 15_000_000_000,
) -> tuple[list[Sample], list[tuple[int, int]]]:
    """Build a cadence stream with ``n_segments`` contiguous segments
    separated by ``pause_ns`` gaps; returns ``(samples, boundaries)`` where
    each boundary is ``(first_ts_in_segment, last_ts_in_segment)`` and uses
    inclusive endpoints (matching ``filter_samples_by_time_range``).
    Mirrors the post-health-report-fixes sample shape on real hardware: clean
    in-step intervals plus one multi-second gap between adjacent steps.
    """

    samples: list[Sample] = []
    boundaries: list[tuple[int, int]] = []
    packet = 0
    t = 0
    for _ in range(n_segments):
        seg_first = t
        seg_last = t
        for _ in range(segment_samples):
            samples.append(_sample(t_ns=t, packet=packet))
            seg_last = t
            packet += 1
            t += interval_ns
        boundaries.append((seg_first, seg_last))
        t = seg_last + pause_ns
    return samples, boundaries


# ---------------------------------------------------------------------------
# §1 — Stick rest noise
# ---------------------------------------------------------------------------


class StickRestNoiseTests(unittest.TestCase):
    def test_quiet_rest_labels_good_with_useful_deadzone(self) -> None:
        # Sample storage is signed integer percent of full axis travel; an
        # amplitude of 1 percent yields radial p99 well under the 2% GOOD
        # threshold and exercises the suggested-deadzone math without
        # collapsing to all zeros.
        samples = _rest_samples(n=600, noise_amplitude=1, seed=1)

        result = compute_stick_rest_noise(samples, side="left")

        self.assertEqual(result.sample_count, 600)
        self.assertEqual(result.label, RestNoiseLabel.GOOD)
        self.assertEqual(result.quality, SampleQuality.GOOD)
        self.assertLess(result.p99_r, REST_NOISE_GOOD_P99_PCT)
        self.assertEqual(
            result.suggested_deadzone_from_sample,
            math.ceil(result.p99_r + REST_NOISE_DEADZONE_MARGIN_PCT),
        )

    def test_noticeable_band_for_mid_range_p99(self) -> None:
        # Amplitude 5% lands p99 between the 2% GOOD and 6% NOTICEABLE
        # thresholds.
        samples = _rest_samples(n=GOOD_REST_SAMPLE_COUNT, noise_amplitude=5, seed=2)

        result = compute_stick_rest_noise(samples, side="left")

        self.assertEqual(result.label, RestNoiseLabel.NOTICEABLE)
        self.assertGreater(result.p99_r, REST_NOISE_GOOD_P99_PCT)
        self.assertLessEqual(result.p99_r, REST_NOISE_NOTICEABLE_P99_PCT)

    def test_high_band_when_p99_exceeds_noticeable_threshold(self) -> None:
        # Amplitude 12% lands p99 well above the 6% NOTICEABLE threshold.
        samples = _rest_samples(n=GOOD_REST_SAMPLE_COUNT, noise_amplitude=12, seed=3)

        result = compute_stick_rest_noise(samples, side="left")

        self.assertEqual(result.label, RestNoiseLabel.HIGH)
        self.assertGreater(result.p99_r, REST_NOISE_NOTICEABLE_P99_PCT)

    def test_empty_samples_returns_insufficient_no_crash(self) -> None:
        result = compute_stick_rest_noise([], side="left")

        self.assertEqual(result.sample_count, 0)
        self.assertEqual(result.label, RestNoiseLabel.INSUFFICIENT)
        self.assertEqual(result.quality, SampleQuality.INSUFFICIENT)
        self.assertEqual(result.suggested_deadzone_from_sample, 0)

    def test_too_few_samples_labels_insufficient(self) -> None:
        samples = _rest_samples(n=5, noise_amplitude=1)

        result = compute_stick_rest_noise(samples, side="left")

        self.assertEqual(result.quality, SampleQuality.INSUFFICIENT)
        self.assertEqual(result.label, RestNoiseLabel.INSUFFICIENT)

    def test_right_side_argument_uses_right_stick_fields(self) -> None:
        # Left stick parked off-center; right stick basically at rest. The
        # right-side computation must not pull in left-side fields.
        samples = [_sample(t_ns=i, packet=i, lx=30, ly=30, rx=0, ry=0)
                   for i in range(GOOD_REST_SAMPLE_COUNT)]

        right_result = compute_stick_rest_noise(samples, side="right")

        self.assertLess(right_result.p99_r, 1)

    def test_unknown_side_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            compute_stick_rest_noise([_sample(t_ns=0, packet=0)], side="middle")


# ---------------------------------------------------------------------------
# §2 — Stick range / circularity
# ---------------------------------------------------------------------------


class StickRangeTests(unittest.TestCase):
    # Sample storage is signed integer percent (see Sample docstring); a
    # radius of 85 represents 85% of full axis travel — close to what a
    # well-rotated stick actually reaches.

    def test_full_rotation_reaches_good_coverage(self) -> None:
        samples = _rotation_samples(
            sectors_covered=32, samples_per_sector=10, radius=85
        )

        result = compute_stick_range(samples, side="left", sector_count=32)

        self.assertEqual(result.sample_count, 320)
        self.assertEqual(result.label, RangeCoverageLabel.GOOD)
        self.assertGreaterEqual(result.sector_coverage_pct, 0.95)
        self.assertIsNotNone(result.weakest_sector_index)

    def test_partial_rotation_flags_retest(self) -> None:
        samples = _rotation_samples(
            sectors_covered=20, samples_per_sector=10, radius=85
        )

        result = compute_stick_range(samples, side="left", sector_count=32)

        # 20/32 = 0.625 < STICK_RANGE_LIMITED_PCT (0.80)
        self.assertEqual(result.label, RangeCoverageLabel.RETEST)
        self.assertLess(result.sector_coverage_pct, 0.80)

    def test_limited_rotation_flags_limited(self) -> None:
        samples = _rotation_samples(
            sectors_covered=27, samples_per_sector=10, radius=85
        )

        result = compute_stick_range(samples, side="left", sector_count=32)

        # 27/32 = 0.84 — between LIMITED (0.80) and GOOD (0.95)
        self.assertEqual(result.label, RangeCoverageLabel.LIMITED)

    def test_min_max_x_y_match_rotation_extents(self) -> None:
        samples = _rotation_samples(
            sectors_covered=32, samples_per_sector=4, radius=60
        )

        result = compute_stick_range(samples, side="left", sector_count=32)

        # Rotation extents should be close to ±radius on each axis. Allow
        # one-percent slack for rounding in the sample builder.
        self.assertGreater(result.max_x, 55)
        self.assertLess(result.min_x, -55)
        self.assertGreater(result.max_y, 55)
        self.assertLess(result.min_y, -55)
        for reach in (result.cardinal_reach_up, result.cardinal_reach_down,
                      result.cardinal_reach_left, result.cardinal_reach_right):
            self.assertGreater(reach, 55)

    def test_empty_samples_returns_insufficient_with_zero_sectors(self) -> None:
        result = compute_stick_range([], side="left", sector_count=32)

        self.assertEqual(result.sample_count, 0)
        self.assertEqual(result.label, RangeCoverageLabel.INSUFFICIENT)
        self.assertEqual(result.sector_coverage_pct, 0.0)
        self.assertIsNone(result.weakest_sector_index)
        self.assertEqual(len(result.sectors), 32)

    def test_too_few_samples_below_min_labels_insufficient(self) -> None:
        samples = _rotation_samples(sectors_covered=2, samples_per_sector=2, radius=60)

        result = compute_stick_range(samples, side="left", sector_count=32)

        self.assertEqual(result.label, RangeCoverageLabel.INSUFFICIENT)


# ---------------------------------------------------------------------------
# §3 — Trigger range / smoothness
# ---------------------------------------------------------------------------


class TriggerRangeTests(unittest.TestCase):
    def test_full_clean_pull_release_labels_smooth(self) -> None:
        samples = _trigger_pull_release_samples(min_value=0, max_value=255)

        result = compute_trigger_range(samples, side="left")

        self.assertEqual(result.observed_min, 0)
        self.assertEqual(result.observed_max, 255)
        self.assertEqual(result.observed_travel, 255)
        self.assertEqual(result.label, TriggerSmoothnessLabel.SMOOTH)
        self.assertEqual(result.monotonicity_violations, 0)

    def test_limited_range_when_pull_does_not_reach_top(self) -> None:
        samples = _trigger_pull_release_samples(min_value=0, max_value=180)

        result = compute_trigger_range(samples, side="left")

        self.assertEqual(result.label, TriggerSmoothnessLabel.LIMITED)

    def test_stepped_label_when_many_large_deltas(self) -> None:
        # Build a press-release that reaches full 0-255 range in clean ramps
        # with steps large enough (>= TRIGGER_LARGE_DELTA_THRESHOLD = 25) to
        # trigger the STEPPED label. The TRIGGER_NOISE_TOLERANCE_RAW (2)
        # noise band is also exceeded but every step is monotonic.
        press = list(range(0, 256, 51))         # 0, 51, 102, 153, 204, 255
        hold = [255] * 5
        release = list(range(255, -1, -51))     # 255, 204, 153, 102, 51, 0
        values = (press + hold + release) * 5   # repeat for sample-count threshold
        samples = [
            _sample(t_ns=i * 1_000_000, packet=i, lt=value)
            for i, value in enumerate(values)
        ]

        result = compute_trigger_range(samples, side="left")

        self.assertEqual(result.observed_min, 0)
        self.assertEqual(result.observed_max, 255)
        self.assertEqual(result.label, TriggerSmoothnessLabel.STEPPED)
        self.assertGreaterEqual(result.largest_adjacent_delta, 51)

    def test_noisy_label_when_monotonicity_violations_exceed_threshold(self) -> None:
        # A press-release that reaches full range but zig-zags often enough
        # that monotonicity_violations > len(values)//5, with each step
        # individually below TRIGGER_LARGE_DELTA_THRESHOLD so the STEPPED
        # branch doesn't win first.
        values: list[int] = [0]
        # Press phase: +20 then -10 repeated, net +10 per pair, until near top.
        while values[-1] < 230:
            values.append(values[-1] + 20)
            values.append(values[-1] - 10)
        values.append(255)
        values += [255] * 4
        # Release phase: -20 then +10 repeated, net -10, mirror.
        while values[-1] > 20:
            values.append(values[-1] - 20)
            values.append(values[-1] + 10)
        values.append(0)
        samples = [
            _sample(t_ns=i * 1_000_000, packet=i, lt=max(0, min(255, v)))
            for i, v in enumerate(values)
        ]

        result = compute_trigger_range(samples, side="left")

        self.assertEqual(result.observed_min, 0)
        self.assertEqual(result.observed_max, 255)
        self.assertEqual(result.label, TriggerSmoothnessLabel.NOISY)
        self.assertGreater(result.monotonicity_violations, len(values) // 5)

    def test_insufficient_when_too_few_samples(self) -> None:
        samples = [_sample(t_ns=i, packet=i, lt=10 * i) for i in range(5)]

        result = compute_trigger_range(samples, side="left")

        self.assertEqual(result.label, TriggerSmoothnessLabel.INSUFFICIENT)
        self.assertEqual(result.quality, SampleQuality.INSUFFICIENT)

    def test_empty_returns_insufficient_no_crash(self) -> None:
        result = compute_trigger_range([], side="left")

        self.assertEqual(result.sample_count, 0)
        self.assertEqual(result.label, TriggerSmoothnessLabel.INSUFFICIENT)


class TriggerNoisyThresholdTests(unittest.TestCase):
    """P6 [D5]: the NOISY threshold is a ratio + small-n floor, not the
    integer-division ``len(values) // 5`` that degenerates at n<5. Behaviour is
    unchanged at the live minimum (n>=30); the floor just stops a single
    violation tripping NOISY if MIN_SAMPLES_FOR_TRIGGER is ever lowered."""

    def _label(self, *, violations: int, n: int) -> TriggerSmoothnessLabel:
        # Full-range, no large deltas -> only the monotonicity ratio decides
        # SMOOTH vs NOISY.
        return _trigger_smoothness_label(
            observed_min=0,
            observed_max=255,
            monotonicity_violations=violations,
            largest_adjacent_delta=0,
            values=[100] * n,
        )

    def test_boundary_at_n30_unchanged(self) -> None:
        # 0.2 * 30 == 6, matching the old 30 // 5: 6 -> SMOOTH, 7 -> NOISY.
        self.assertEqual(self._label(violations=6, n=30), TriggerSmoothnessLabel.SMOOTH)
        self.assertEqual(self._label(violations=7, n=30), TriggerSmoothnessLabel.NOISY)

    def test_small_n_not_degenerate(self) -> None:
        # n=4 with one violation: base's ``4 // 5 == 0`` made any single
        # violation trip NOISY; the ratio + n>=10 floor no longer degenerates.
        label = self._label(violations=1, n=4)
        self.assertNotEqual(label, TriggerSmoothnessLabel.NOISY)
        self.assertEqual(label, TriggerSmoothnessLabel.SMOOTH)

    def test_ratio_constant_value(self) -> None:
        self.assertEqual(TRIGGER_NOISY_VIOLATION_RATIO, 0.2)


# ---------------------------------------------------------------------------
# §4 — Observed report cadence
# ---------------------------------------------------------------------------


class ObservedCadenceTests(unittest.TestCase):
    def test_consistent_1k_hz_cadence_labels_consistent(self) -> None:
        samples = _cadence_samples_at_hz(hz=1000, duration_s=2.0, jitter_ms=0.05)

        result = compute_observed_cadence(samples, configured_polling_hz=1000)

        self.assertEqual(result.configured_polling_hz, 1000)
        self.assertAlmostEqual(result.expected_interval_ms, 1.0, places=4)
        self.assertEqual(result.label, CadenceQualityLabel.CONSISTENT)
        self.assertAlmostEqual(result.observed_median_interval_ms, 1.0, delta=0.1)

    def test_gaps_lead_to_inconsistent_label(self) -> None:
        # 200 base samples, 30 of them with 50ms gaps — far beyond the
        # CADENCE_INCONSISTENT_GAP_RATIO threshold.
        samples = _cadence_samples_at_hz(
            hz=1000, duration_s=0.2, jitter_ms=0.05,
            extra_gap_count=30, extra_gap_ms=50.0,
        )

        result = compute_observed_cadence(samples, configured_polling_hz=1000)

        self.assertEqual(result.label, CadenceQualityLabel.INCONSISTENT)
        self.assertGreater(result.gaps_over_2x_expected, 0)
        self.assertGreater(result.gaps_over_10ms, 0)

    def test_8k_configured_with_mostly_clean_intervals_is_consistent(self) -> None:
        samples = _cadence_samples_at_hz(hz=8000, duration_s=0.05, jitter_ms=0.01)

        result = compute_observed_cadence(samples, configured_polling_hz=8000)

        self.assertEqual(result.configured_polling_hz, 8000)
        self.assertAlmostEqual(result.expected_interval_ms, 0.125, places=4)
        self.assertEqual(result.label, CadenceQualityLabel.CONSISTENT)

    def test_unknown_configured_rate_still_returns_stats(self) -> None:
        samples = _cadence_samples_at_hz(hz=1000, duration_s=0.2)

        result = compute_observed_cadence(samples, configured_polling_hz=None)

        self.assertIsNone(result.configured_polling_hz)
        self.assertIsNone(result.expected_interval_ms)
        self.assertGreater(result.sample_count, 0)
        # Without a reference, label falls through to CONSISTENT (we have
        # samples; can't compare so we don't accuse).
        self.assertEqual(result.label, CadenceQualityLabel.CONSISTENT)

    def test_packet_duplicates_are_ignored(self) -> None:
        # Two samples with the same packet number = one transition.
        samples = [
            _sample(t_ns=0, packet=10),
            _sample(t_ns=1_000_000, packet=10),
            _sample(t_ns=2_000_000, packet=11),
            _sample(t_ns=3_000_000, packet=11),
            _sample(t_ns=4_000_000, packet=12),
        ]

        result = compute_observed_cadence(samples, configured_polling_hz=None)

        # 3 distinct packets = 2 intervals.
        # sample_count in the report is transition count, which equals 3.
        self.assertEqual(result.sample_count, 3)

    def test_empty_returns_insufficient(self) -> None:
        result = compute_observed_cadence([], configured_polling_hz=1000)

        self.assertEqual(result.label, CadenceQualityLabel.INSUFFICIENT)
        self.assertEqual(result.sample_count, 0)

    def test_consistent_ratio_threshold_drives_label(self) -> None:
        # Build cadence with exactly 1% gaps (below the 2% CONSISTENT
        # threshold). The 12 ms gap is above the 10 ms 2× threshold floor at
        # 1 kHz (where ratio × expected = 2 ms loses to the floor) so each
        # one actually counts as a gap; the test then verifies the ratio
        # band still drives the label decision.
        samples = _cadence_samples_at_hz(
            hz=1000, duration_s=1.0, jitter_ms=0.05,
            extra_gap_count=10, extra_gap_ms=12.0,
        )

        result = compute_observed_cadence(samples, configured_polling_hz=1000)

        # 10/1000 = 1.0% gap ratio ≤ CADENCE_CONSISTENT_GAP_RATIO (2%)
        ratio = result.gaps_over_2x_expected / max(1, result.sample_count - 1)
        self.assertGreater(result.gaps_over_2x_expected, 0)
        self.assertLessEqual(ratio, CADENCE_CONSISTENT_GAP_RATIO)
        self.assertEqual(result.label, CadenceQualityLabel.CONSISTENT)

    def test_cadence_excludes_pause_bridging_intervals(self) -> None:
        # 3 segments × 200 samples at 1 kHz, separated by 15 s gaps. In-step
        # deltas are exactly 1 ms; the two cross-segment deltas are ~15 s
        # each. Filtering should leave only the 1 ms intervals.
        samples, boundaries = _segmented_cadence_samples()

        result = compute_observed_cadence(
            samples,
            configured_polling_hz=1000,
            step_boundaries_ns=boundaries,
        )

        self.assertLess(result.observed_max_interval_ms, 5.0)
        self.assertLess(result.observed_p99_interval_ms, 5.0)
        self.assertLess(result.observed_p95_interval_ms, 5.0)
        self.assertEqual(result.gaps_over_10ms, 0)
        self.assertEqual(result.gaps_over_4x_expected, 0)
        self.assertEqual(result.gaps_over_2x_expected, 0)
        # Total transitions observed is unchanged by filtering.
        self.assertEqual(result.sample_count, 600)

    def test_cadence_with_no_boundaries_unchanged(self) -> None:
        # Same fixture, no boundaries → original flat-list behaviour; the
        # 15 s cross-segment deltas dominate the stats.
        samples, _ = _segmented_cadence_samples()

        no_arg = compute_observed_cadence(samples, configured_polling_hz=1000)
        none_arg = compute_observed_cadence(
            samples, configured_polling_hz=1000, step_boundaries_ns=None,
        )
        empty_arg = compute_observed_cadence(
            samples, configured_polling_hz=1000, step_boundaries_ns=[],
        )

        self.assertGreater(no_arg.observed_max_interval_ms, 10_000.0)
        self.assertGreaterEqual(no_arg.gaps_over_10ms, 2)
        # Defensive: None and [] are equivalent to omitting the kwarg.
        self.assertEqual(none_arg.observed_max_interval_ms, no_arg.observed_max_interval_ms)
        self.assertEqual(empty_arg.observed_max_interval_ms, no_arg.observed_max_interval_ms)
        self.assertEqual(none_arg.duration_ms, no_arg.duration_ms)
        self.assertEqual(empty_arg.duration_ms, no_arg.duration_ms)

    def test_cadence_duration_ms_sums_step_durations_when_boundaries_provided(self) -> None:
        # Each segment spans ~199 ms (200 samples at 1 ms intervals; first to
        # last is 199 intervals). Active duration ≈ 3 × 199 = 597 ms, NOT the
        # ~30.6 s wall-clock span that includes the two 15 s pauses.
        samples, boundaries = _segmented_cadence_samples()

        result = compute_observed_cadence(
            samples,
            configured_polling_hz=1000,
            step_boundaries_ns=boundaries,
        )

        self.assertAlmostEqual(result.duration_ms, 597.0, delta=1.0)

    def test_cadence_duration_ms_full_span_when_no_boundaries(self) -> None:
        # Same fixture, no boundaries → first-to-last sample span ≈ 30.6 s.
        samples, _ = _segmented_cadence_samples()

        result = compute_observed_cadence(samples, configured_polling_hz=1000)

        self.assertGreater(result.duration_ms, 30_000.0)
        self.assertLess(result.duration_ms, 31_000.0)

    def test_cadence_boundaries_inclusive_at_endpoints(self) -> None:
        # Samples land exactly on start_ns and end_ns of each window so the
        # inclusive `start_ns <= ts <= end_ns` semantics are exercised. The
        # delta from end_ns[0] (300) to start_ns[1] (15_000_000_000) is the
        # pause-bridge case and must be excluded.
        samples = [
            _sample(t_ns=100, packet=0),                  # at start_ns[0]
            _sample(t_ns=200, packet=1),                  # in step 0
            _sample(t_ns=300, packet=2),                  # at end_ns[0]
            _sample(t_ns=15_000_000_000, packet=3),       # at start_ns[1]
            _sample(t_ns=15_000_000_100, packet=4),       # in step 1
            _sample(t_ns=15_000_000_200, packet=5),       # at end_ns[1]
        ]
        boundaries = [(100, 300), (15_000_000_000, 15_000_000_200)]

        result = compute_observed_cadence(
            samples,
            configured_polling_hz=1000,
            step_boundaries_ns=boundaries,
        )

        # 4 in-step deltas of 100 ns each; the 14.999...-s cross-step delta
        # is filtered out. Max in-step interval is 0.0001 ms.
        self.assertLess(result.observed_max_interval_ms, 1.0)
        # All 6 transitions are still counted in sample_count.
        self.assertEqual(result.sample_count, 6)

    def test_gap_thresholds_floor_at_high_polling_rates(self) -> None:
        # At 8 kHz, expected interval = 0.125 ms; raw 2× = 0.25 ms / 4× =
        # 0.5 ms — both below the Windows app-layer scheduling jitter floor.
        # The floor takes over to keep gap counts from blowing up on healthy
        # hardware.
        self.assertEqual(
            _effective_gap_threshold_ms(
                CADENCE_GAP_2X_RATIO, 0.125, CADENCE_GAP_2X_FLOOR_MS
            ),
            CADENCE_GAP_2X_FLOOR_MS,
        )
        self.assertEqual(
            _effective_gap_threshold_ms(
                CADENCE_GAP_4X_RATIO, 0.125, CADENCE_GAP_4X_FLOOR_MS
            ),
            CADENCE_GAP_4X_FLOOR_MS,
        )

    def test_gap_thresholds_use_ratio_at_low_polling_rates(self) -> None:
        # At 100 Hz, expected interval = 10.0 ms; 2× = 20.0 ms and 4× = 40.0
        # ms both exceed their respective floors (10 ms / 10 ms), so the
        # ratio-based threshold wins. (250 Hz no longer cleanly tests this
        # path post-2026-05-23 retune — at 250 Hz the 2× = 8 ms falls below
        # the new 10 ms floor; the floor dominates.)
        self.assertEqual(
            _effective_gap_threshold_ms(
                CADENCE_GAP_2X_RATIO, 10.0, CADENCE_GAP_2X_FLOOR_MS
            ),
            20.0,
        )
        self.assertEqual(
            _effective_gap_threshold_ms(
                CADENCE_GAP_4X_RATIO, 10.0, CADENCE_GAP_4X_FLOOR_MS
            ),
            40.0,
        )

    def test_8khz_healthy_cadence_labels_consistent(self) -> None:
        # Realistic 8 kHz stream with Windows-typical sub-5 ms scheduling
        # jitter. Without the floor, ~6% of intervals exceed the raw 2×
        # threshold (0.25 ms) and the label would land in SOME_GAPS /
        # INCONSISTENT — a false positive on perfectly healthy hardware.
        # With the floor, only the (almost-never) ≥ 5 ms slips count, the
        # ratio collapses well under 2%, and the label reads CONSISTENT.
        samples = _cadence_samples_with_realistic_8khz_jitter(n=2000, seed=11)

        result = compute_observed_cadence(samples, configured_polling_hz=8000)

        self.assertEqual(result.configured_polling_hz, 8000)
        self.assertAlmostEqual(result.expected_interval_ms, 0.125, places=4)
        self.assertEqual(result.label, CadenceQualityLabel.CONSISTENT)
        # Confirm the floor actually engaged — gap count is much lower than
        # the raw-2× count would have been on this synthetic.
        self.assertLess(
            result.gaps_over_2x_expected,
            int(CADENCE_CONSISTENT_GAP_RATIO * max(1, result.sample_count - 1)),
        )

    def test_8khz_with_actual_long_gaps_still_signals(self) -> None:
        # Same realistic stream but with 60 deltas forced to 15 ms — well
        # above both the 5 ms / 10 ms floors and the absolute 10 ms gap.
        # The label must drop out of CONSISTENT; the floor should not eat
        # genuinely user-perceptible gaps.
        samples = _cadence_samples_with_realistic_8khz_jitter(
            n=2000, seed=12, long_gap_count=60, long_gap_ms=15.0,
        )

        result = compute_observed_cadence(samples, configured_polling_hz=8000)

        self.assertIn(
            result.label,
            (CadenceQualityLabel.SOME_GAPS, CadenceQualityLabel.INCONSISTENT),
        )
        self.assertGreaterEqual(result.gaps_over_10ms, 60)
        self.assertGreaterEqual(result.gaps_over_2x_expected, 60)

    def test_existing_low_rate_label_behavior_with_intervals_under_5ms(self) -> None:
        # 1 kHz fixture with 30 deltas at 3 ms. Pre-floor, those 30 deltas
        # exceeded the 2× = 2 ms threshold and counted as gaps; post-floor,
        # 3 ms < 5 ms so they no longer count. A clean 1 kHz stream with
        # sub-5 ms intervals is well inside Windows scheduling tolerance and
        # should still land CONSISTENT — this test pins the deliberate
        # behavior change for the public-facing 1 kHz case.
        samples = _cadence_samples_at_hz(
            hz=1000, duration_s=1.0, jitter_ms=0.05,
            extra_gap_count=30, extra_gap_ms=3.0,
        )

        result = compute_observed_cadence(samples, configured_polling_hz=1000)

        self.assertEqual(result.label, CadenceQualityLabel.CONSISTENT)
        self.assertEqual(result.gaps_over_2x_expected, 0)


# ---------------------------------------------------------------------------
# Overall status
# ---------------------------------------------------------------------------


class OverallStatusTests(unittest.TestCase):
    def _build_clean_report_components(self):
        rest = _rest_samples(n=GOOD_REST_SAMPLE_COUNT, noise_amplitude=1)
        rotation = _rotation_samples(
            sectors_covered=32, samples_per_sector=10, radius=85
        )
        trigger = _trigger_pull_release_samples()
        return rest, rotation, trigger

    def test_clean_run_status_is_normal(self) -> None:
        rest, rotation, trigger = self._build_clean_report_components()

        status = compute_overall_status(
            left_rest=compute_stick_rest_noise(rest, side="left"),
            right_rest=compute_stick_rest_noise(rest, side="right"),
            left_range=compute_stick_range(rotation, side="left"),
            right_range=compute_stick_range(rotation, side="left"),
            left_trigger=compute_trigger_range(trigger, side="left"),
            right_trigger=compute_trigger_range(
                _trigger_pull_release_samples(side="right"), side="right"
            ),
        )

        self.assertEqual(status, OverallStatus.NORMAL)

    def test_high_rest_noise_escalates_to_possible_issue(self) -> None:
        noisy_rest = _rest_samples(n=GOOD_REST_SAMPLE_COUNT, noise_amplitude=15)
        clean_rest = _rest_samples(n=GOOD_REST_SAMPLE_COUNT, noise_amplitude=1)
        rotation = _rotation_samples(
            sectors_covered=32, samples_per_sector=10, radius=85
        )
        trigger = _trigger_pull_release_samples()

        status = compute_overall_status(
            left_rest=compute_stick_rest_noise(noisy_rest, side="left"),
            right_rest=compute_stick_rest_noise(clean_rest, side="right"),
            left_range=compute_stick_range(rotation, side="left"),
            right_range=compute_stick_range(rotation, side="left"),
            left_trigger=compute_trigger_range(trigger, side="left"),
            right_trigger=compute_trigger_range(
                _trigger_pull_release_samples(side="right"), side="right"
            ),
        )

        self.assertEqual(status, OverallStatus.POSSIBLE_ISSUE)

    def test_missing_test_data_escalates_to_retest(self) -> None:
        rest = _rest_samples(n=GOOD_REST_SAMPLE_COUNT, noise_amplitude=1)
        rotation = _rotation_samples(
            sectors_covered=32, samples_per_sector=10, radius=85
        )
        trigger = _trigger_pull_release_samples()

        status = compute_overall_status(
            left_rest=compute_stick_rest_noise(rest, side="left"),
            right_rest=compute_stick_rest_noise(rest, side="right"),
            left_range=compute_stick_range(rotation, side="left"),
            right_range=compute_stick_range(rotation, side="left"),
            left_trigger=compute_trigger_range(trigger, side="left"),
            right_trigger=compute_trigger_range([], side="right"),  # missing
        )

        self.assertEqual(status, OverallStatus.RETEST_RECOMMENDED)


# ---------------------------------------------------------------------------
# Sample filter helper
# ---------------------------------------------------------------------------


class FilterSamplesTests(unittest.TestCase):
    def test_inclusive_range_returns_samples_within_window(self) -> None:
        samples = [_sample(t_ns=i * 100, packet=i) for i in range(20)]

        sliced = filter_samples_by_time_range(samples, start_ns=500, end_ns=1500)

        self.assertEqual([s.packet_number for s in sliced], list(range(5, 16)))

    def test_empty_window_returns_empty_list(self) -> None:
        samples = [_sample(t_ns=i * 100, packet=i) for i in range(5)]

        self.assertEqual(
            filter_samples_by_time_range(samples, start_ns=10_000, end_ns=20_000),
            [],
        )


if __name__ == "__main__":
    unittest.main()
