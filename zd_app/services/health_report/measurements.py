"""Pure-function measurement primitives for the Controller Health Report.

Each ``compute_*`` function takes a ``Sequence[Sample]`` and (where relevant)
side-specific or config-specific inputs, and returns one of the dataclasses
in :mod:`zd_app.services.health_report.models`. No I/O, no DPG, no threads.
This file is the most-tested part of the feature — fixtures live in
``tests/test_health_report_measurements.py``.

Per the measurement contract, every metric carries:

- an *observed value* (raw numbers from the samples),
- an *interpretation* (a label band — Good / Limited / etc.),
- a *boundary* (the dataclass's name + the label set make this implicit;
  the exporter copy says it explicitly).

The numeric thresholds at the bottom of this file are starting points per
the "Thresholds: initial defaults, not product truth" rule. They are
intentionally easy to retune; do not treat them as load-bearing precision.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Iterable, Optional, Sequence

from zd_app.services.health_report.models import (
    CadenceQualityLabel,
    ObservedCadence,
    OverallStatus,
    RangeCoverageLabel,
    RestNoiseLabel,
    Sample,
    SampleQuality,
    StickRange,
    StickRestNoise,
    StickSectorCoverage,
    TriggerRange,
    TriggerSmoothnessLabel,
)


# ---------------------------------------------------------------------------
# Constants — the "Thresholds: initial defaults" rule
# ---------------------------------------------------------------------------

# Stick samples carry signed integer percent of full axis travel
# ([-100, 100]), the same unit the wrapper's Sticks-tab deadzone slider
# uses (see Sample / sample_capture._scale_xinput_axis_to_pct). p99 radial
# bands below are in that percent unit; operator-tunable after hardware
# trials. The previous raw-int16 thresholds (700 / 1800 / 200) map to
# roughly 2.1% / 5.5% / 0.6% of full travel; the percent values here keep
# the same rough shape, rounded to clean operator-readable integers.
REST_NOISE_GOOD_P99_PCT = 2.0
REST_NOISE_NOTICEABLE_P99_PCT = 6.0
# Deadzone suggestion: ceil(p99 + small margin). Margin is small so the
# suggestion stays close to the observed noise; the user can grow it if they
# prefer headroom. Unit is integer percent — same as the slider.
REST_NOISE_DEADZONE_MARGIN_PCT = 1.0

# Stick range: number of polar sectors and coverage threshold.
STICK_RANGE_SECTORS_DEFAULT = 32
STICK_RANGE_COVERAGE_THRESHOLD_PCT = 0.90  # 90% of observed max radius
STICK_RANGE_GOOD_PCT = 0.95
STICK_RANGE_LIMITED_PCT = 0.80

# Trigger range thresholds (0..255).
TRIGGER_FULL_RANGE_MIN_MAX = (2, 253)
TRIGGER_NEAR_FULL_MIN_MAX = (5, 250)
TRIGGER_NOISE_TOLERANCE_RAW = 2   # allowed jitter in monotonicity check
TRIGGER_LARGE_DELTA_THRESHOLD = 25  # raw-unit jump above which "stepped"
TRIGGER_STEPPED_DELTA_COUNT = 3     # how many large deltas trigger "stepped"
# Fraction of monotonicity violations above which a near-full trigger is "noisy"
# rather than "smooth". A ratio + small-n floor instead of ``len(values) // 5``:
# integer-division degenerates at n<5 (``n // 5 == 0`` makes a single violation
# trip NOISY). Not reachable today (MIN_SAMPLES_FOR_TRIGGER == 30), but hardened
# so it can't silently degrade if that minimum is ever lowered. Behaviour is
# unchanged at n>=30: 0.2 * 30 == 6, matching the old ``30 // 5``.
TRIGGER_NOISY_VIOLATION_RATIO = 0.2

# Cadence quality (per the measurement contract — conservative; honest at 8K).
CADENCE_GAP_2X_RATIO = 2.0
CADENCE_GAP_4X_RATIO = 4.0
CADENCE_GAP_10MS_ABS_MS = 10.0
# Absolute jitter floors for the ratio-based gap thresholds. The Windows
# app-layer HID delivery + Python scheduling produces typical 1-2 ms jitter
# even on a healthy controller; at high configured polling rates (4 kHz,
# 8 kHz) the 2× / 4× thresholds shrink below the OS jitter floor and would
# over-count. Effective threshold per tier =
# max(ratio × expected_ms, floor).
#
# Floor value (10 ms) is calibrated against local testing,
# where a healthy ZD Ultimate Legend at 8000 Hz configured produced
# p95 ≈ 5.7 ms and p99 ≈ 9.25 ms — natural Windows
# app-layer jitter. A floor at 5 ms left ~9% of intervals counting as
# "gaps" on this healthy run; 10 ms is above the natural p99 and well
# below the 16 ms 60 Hz frame budget, so a "gap" still corresponds to
# something a user could perceive. The 4× floor stays at 10 ms (same
# value as CADENCE_GAP_10MS_ABS_MS — at 8 kHz these two counters report
# the same number, which is honest, not lying).
CADENCE_GAP_2X_FLOOR_MS = 10.0
CADENCE_GAP_4X_FLOOR_MS = 10.0
# At 8 kHz configured rate (0.125 ms expected), app-layer scheduling can
# dominate; tolerate a much higher absolute jitter before calling
# "inconsistent". Threshold below is the fraction of samples that may exceed
# the floored 2× threshold before quality drops a band.
CADENCE_CONSISTENT_GAP_RATIO = 0.02     # 2% of samples can be > floored 2× threshold
CADENCE_INCONSISTENT_GAP_RATIO = 0.10   # 10% triggers "inconsistent"

# Sample-quality thresholds.
MIN_SAMPLES_FOR_REST = 30           # 5 s at ~6 Hz still gives a label
MIN_SAMPLES_FOR_RANGE = 60
MIN_SAMPLES_FOR_TRIGGER = 30
MIN_SAMPLES_FOR_CADENCE = 40        # need enough deltas (=N-1) to median
GOOD_REST_SAMPLE_COUNT = 500        # measurement contract: "if possible"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Return the value at ``fraction`` (0..1) of a sorted sequence.

    Uses the nearest-rank method (no interpolation). Empty sequence returns
    0.0 so caller can keep the dataclass populated without conditional
    branches. Caller is responsible for asserting non-empty when meaningful.
    """

    if not sorted_values:
        return 0.0
    if fraction <= 0.0:
        return float(sorted_values[0])
    if fraction >= 1.0:
        return float(sorted_values[-1])
    # Nearest-rank: index = ceil(N * f) - 1, clamped.
    n = len(sorted_values)
    idx = max(0, min(n - 1, math.ceil(n * fraction) - 1))
    return float(sorted_values[idx])


def _median_absolute_deviation(values: Sequence[float], reference: float) -> float:
    if not values:
        return 0.0
    return median(abs(v - reference) for v in values)


def _effective_gap_threshold_ms(
    ratio: float, expected_interval_ms: float, floor_ms: float
) -> float:
    """Gap threshold = max(ratio × expected, floor).

    At high configured polling rates expected_ms shrinks below practical
    Windows app-layer scheduling jitter (~1-2 ms typical on healthy
    hardware), so the floor takes over to avoid false-positive gap counts
    that would otherwise drop the cadence label to SOME_GAPS / INCONSISTENT
    on a perfectly fine controller. See CADENCE_GAP_*_FLOOR_MS comments for
    the rationale on floor values.
    """

    return max(ratio * expected_interval_ms, floor_ms)


def _duration_ms(
    samples: Sequence[Sample],
    *,
    step_boundaries_ns: Optional[Sequence[tuple[int, int]]] = None,
) -> float:
    if len(samples) < 2:
        return 0.0
    if not step_boundaries_ns:
        return (samples[-1].timestamp_ns - samples[0].timestamp_ns) / 1_000_000.0
    # Active-sampling duration: sum (last - first) per step window so
    # READY-pause dead time between steps doesn't inflate the figure. Each
    # boundary uses the same inclusive `start_ns <= ts <= end_ns` rule as
    # filter_samples_by_time_range.
    total_ns = 0
    for start_ns, end_ns in step_boundaries_ns:
        first: Optional[int] = None
        last: Optional[int] = None
        for s in samples:
            if start_ns <= s.timestamp_ns <= end_ns:
                if first is None:
                    first = s.timestamp_ns
                last = s.timestamp_ns
        if first is not None and last is not None and last > first:
            total_ns += last - first
    return total_ns / 1_000_000.0


# ---------------------------------------------------------------------------
# §1 — Stick rest noise
# ---------------------------------------------------------------------------


def compute_stick_rest_noise(samples: Sequence[Sample], *, side: str) -> StickRestNoise:
    """Compute rest-noise summary for one stick.

    ``side`` must be ``"left"`` or ``"right"``. Other values raise
    ``ValueError`` (programmer error, not user input).
    """

    xs, ys = _stick_xy_arrays(samples, side)
    n = len(xs)
    duration_ms = _duration_ms(samples)

    if n == 0:
        return _empty_stick_rest_noise(duration_ms)

    med_x = float(median(xs))
    med_y = float(median(ys))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    dxs = [x - med_x for x in xs]
    dys = [y - med_y for y in ys]
    rs = [math.hypot(dx, dy) for dx, dy in zip(dxs, dys)]
    sorted_rs = sorted(rs)
    max_r = sorted_rs[-1]
    p95_r = _percentile(sorted_rs, 0.95)
    p99_r = _percentile(sorted_rs, 0.99)
    max_abs_x = max(abs(dx) for dx in dxs)
    max_abs_y = max(abs(dy) for dy in dys)
    suggested_deadzone = int(math.ceil(p99_r + REST_NOISE_DEADZONE_MARGIN_PCT))

    if n < MIN_SAMPLES_FOR_REST:
        quality = SampleQuality.INSUFFICIENT
        label = RestNoiseLabel.INSUFFICIENT
    else:
        quality = SampleQuality.GOOD if n >= GOOD_REST_SAMPLE_COUNT else SampleQuality.LIMITED
        label = _rest_noise_label(p99_r)

    return StickRestNoise(
        sample_count=n,
        duration_ms=duration_ms,
        median_center_x=med_x,
        median_center_y=med_y,
        mean_center_x=mean_x,
        mean_center_y=mean_y,
        max_abs_x=max_abs_x,
        max_abs_y=max_abs_y,
        max_r=max_r,
        p95_r=p95_r,
        p99_r=p99_r,
        suggested_deadzone_from_sample=suggested_deadzone,
        quality=quality,
        label=label,
    )


def _empty_stick_rest_noise(duration_ms: float) -> StickRestNoise:
    return StickRestNoise(
        sample_count=0,
        duration_ms=duration_ms,
        median_center_x=0.0,
        median_center_y=0.0,
        mean_center_x=0.0,
        mean_center_y=0.0,
        max_abs_x=0.0,
        max_abs_y=0.0,
        max_r=0.0,
        p95_r=0.0,
        p99_r=0.0,
        suggested_deadzone_from_sample=0,
        quality=SampleQuality.INSUFFICIENT,
        label=RestNoiseLabel.INSUFFICIENT,
    )


def _rest_noise_label(p99_r: float) -> RestNoiseLabel:
    if p99_r <= REST_NOISE_GOOD_P99_PCT:
        return RestNoiseLabel.GOOD
    if p99_r <= REST_NOISE_NOTICEABLE_P99_PCT:
        return RestNoiseLabel.NOTICEABLE
    return RestNoiseLabel.HIGH


# ---------------------------------------------------------------------------
# §2 — Stick range / circularity
# ---------------------------------------------------------------------------


def compute_stick_range(
    samples: Sequence[Sample],
    *,
    side: str,
    center_xy: tuple[float, float] = (0.0, 0.0),
    sector_count: int = STICK_RANGE_SECTORS_DEFAULT,
    coverage_threshold_pct: float = STICK_RANGE_COVERAGE_THRESHOLD_PCT,
) -> StickRange:
    """Compute range/coverage summary for one stick."""

    xs, ys = _stick_xy_arrays(samples, side)
    n = len(xs)
    duration_ms = _duration_ms(samples)

    if n == 0:
        return _empty_stick_range(duration_ms, center_xy, sector_count, coverage_threshold_pct)

    center_x, center_y = center_xy
    dxs = [x - center_x for x in xs]
    dys = [y - center_y for y in ys]
    rs = [math.hypot(dx, dy) for dx, dy in zip(dxs, dys)]

    sector_max = [0.0] * sector_count
    for dx, dy, r in zip(dxs, dys, rs):
        theta = math.atan2(dy, dx)
        if theta < 0:
            theta += 2.0 * math.pi
        idx = min(sector_count - 1, int(theta / (2.0 * math.pi) * sector_count))
        if r > sector_max[idx]:
            sector_max[idx] = r

    observed_max = max(rs)
    threshold_radius = coverage_threshold_pct * observed_max
    sectors_meeting = sum(1 for m in sector_max if m >= threshold_radius and m > 0)
    coverage_pct = sectors_meeting / sector_count if sector_count else 0.0

    weakest_idx: Optional[int] = None
    weakest_pct: Optional[float] = None
    if observed_max > 0:
        weakest_idx = min(range(sector_count), key=lambda i: sector_max[i])
        weakest_pct = sector_max[weakest_idx] / observed_max

    # Cardinal reach: max raw value along each axis from the center.
    cardinal_reach_up = int(round(max((y - center_y) for y in ys)))
    cardinal_reach_down = int(round(-min((y - center_y) for y in ys)))
    cardinal_reach_left = int(round(-min((x - center_x) for x in xs)))
    cardinal_reach_right = int(round(max((x - center_x) for x in xs)))

    sectors = tuple(
        StickSectorCoverage(
            sector_index=i,
            angle_start_rad=(i / sector_count) * 2.0 * math.pi,
            angle_end_rad=((i + 1) / sector_count) * 2.0 * math.pi,
            max_observed_radius=sector_max[i],
        )
        for i in range(sector_count)
    )

    if n < MIN_SAMPLES_FOR_RANGE:
        quality = SampleQuality.INSUFFICIENT
        label = RangeCoverageLabel.INSUFFICIENT
    else:
        quality = SampleQuality.GOOD if n >= MIN_SAMPLES_FOR_RANGE * 4 else SampleQuality.LIMITED
        label = _range_coverage_label(coverage_pct)

    return StickRange(
        sample_count=n,
        duration_ms=duration_ms,
        center_used_x=center_x,
        center_used_y=center_y,
        min_x=int(round(min(xs))),
        max_x=int(round(max(xs))),
        min_y=int(round(min(ys))),
        max_y=int(round(max(ys))),
        cardinal_reach_up=cardinal_reach_up,
        cardinal_reach_down=cardinal_reach_down,
        cardinal_reach_left=cardinal_reach_left,
        cardinal_reach_right=cardinal_reach_right,
        sectors=sectors,
        sector_coverage_threshold_pct=coverage_threshold_pct,
        sector_coverage_pct=coverage_pct,
        weakest_sector_index=weakest_idx,
        weakest_sector_pct_of_max=weakest_pct,
        quality=quality,
        label=label,
    )


def _empty_stick_range(
    duration_ms: float,
    center_xy: tuple[float, float],
    sector_count: int,
    coverage_threshold_pct: float,
) -> StickRange:
    sectors = tuple(
        StickSectorCoverage(
            sector_index=i,
            angle_start_rad=(i / sector_count) * 2.0 * math.pi,
            angle_end_rad=((i + 1) / sector_count) * 2.0 * math.pi,
            max_observed_radius=0.0,
        )
        for i in range(sector_count)
    )
    return StickRange(
        sample_count=0,
        duration_ms=duration_ms,
        center_used_x=center_xy[0],
        center_used_y=center_xy[1],
        min_x=0,
        max_x=0,
        min_y=0,
        max_y=0,
        cardinal_reach_up=0,
        cardinal_reach_down=0,
        cardinal_reach_left=0,
        cardinal_reach_right=0,
        sectors=sectors,
        sector_coverage_threshold_pct=coverage_threshold_pct,
        sector_coverage_pct=0.0,
        weakest_sector_index=None,
        weakest_sector_pct_of_max=None,
        quality=SampleQuality.INSUFFICIENT,
        label=RangeCoverageLabel.INSUFFICIENT,
    )


def _range_coverage_label(coverage_pct: float) -> RangeCoverageLabel:
    if coverage_pct >= STICK_RANGE_GOOD_PCT:
        return RangeCoverageLabel.GOOD
    if coverage_pct >= STICK_RANGE_LIMITED_PCT:
        return RangeCoverageLabel.LIMITED
    return RangeCoverageLabel.RETEST


# ---------------------------------------------------------------------------
# §3 — Trigger range / smoothness
# ---------------------------------------------------------------------------


def compute_trigger_range(samples: Sequence[Sample], *, side: str) -> TriggerRange:
    """Compute one trigger's range + smoothness summary.

    ``side`` is ``"left"`` or ``"right"``.
    """

    values = _trigger_values(samples, side)
    n = len(values)
    duration_ms = _duration_ms(samples)

    if n == 0:
        return _empty_trigger_range(duration_ms)

    observed_min = min(values)
    observed_max = max(values)
    observed_travel = observed_max - observed_min

    # Monotonicity violations: count direction reversals beyond noise
    # tolerance. We split press/release on the peak index.
    peak_idx = max(range(n), key=lambda i: values[i])
    monotonicity_violations = 0
    largest_adjacent_delta = 0
    for i in range(1, n):
        delta = values[i] - values[i - 1]
        if abs(delta) > largest_adjacent_delta:
            largest_adjacent_delta = abs(delta)
        if i <= peak_idx:
            # press phase: expect non-decreasing within tolerance
            if delta < -TRIGGER_NOISE_TOLERANCE_RAW:
                monotonicity_violations += 1
        else:
            # release phase: expect non-increasing within tolerance
            if delta > TRIGGER_NOISE_TOLERANCE_RAW:
                monotonicity_violations += 1

    if n < MIN_SAMPLES_FOR_TRIGGER:
        quality = SampleQuality.INSUFFICIENT
        label = TriggerSmoothnessLabel.INSUFFICIENT
    else:
        quality = SampleQuality.GOOD if n >= MIN_SAMPLES_FOR_TRIGGER * 4 else SampleQuality.LIMITED
        label = _trigger_smoothness_label(
            observed_min=observed_min,
            observed_max=observed_max,
            monotonicity_violations=monotonicity_violations,
            largest_adjacent_delta=largest_adjacent_delta,
            values=values,
        )

    return TriggerRange(
        sample_count=n,
        duration_ms=duration_ms,
        observed_min=observed_min,
        observed_max=observed_max,
        observed_travel=observed_travel,
        monotonicity_violations=monotonicity_violations,
        largest_adjacent_delta=largest_adjacent_delta,
        quality=quality,
        label=label,
    )


def _empty_trigger_range(duration_ms: float) -> TriggerRange:
    return TriggerRange(
        sample_count=0,
        duration_ms=duration_ms,
        observed_min=0,
        observed_max=0,
        observed_travel=0,
        monotonicity_violations=0,
        largest_adjacent_delta=0,
        quality=SampleQuality.INSUFFICIENT,
        label=TriggerSmoothnessLabel.INSUFFICIENT,
    )


def _trigger_smoothness_label(
    *,
    observed_min: int,
    observed_max: int,
    monotonicity_violations: int,
    largest_adjacent_delta: int,
    values: Sequence[int],
) -> TriggerSmoothnessLabel:
    full_min, full_max = TRIGGER_FULL_RANGE_MIN_MAX
    near_min, near_max = TRIGGER_NEAR_FULL_MIN_MAX
    full_range = observed_min <= full_min and observed_max >= full_max
    near_full = observed_min <= near_min and observed_max >= near_max
    if not near_full:
        return TriggerSmoothnessLabel.LIMITED
    # Count large deltas to decide "stepped" vs "noisy" vs "smooth".
    large_delta_count = sum(
        1
        for i in range(1, len(values))
        if abs(values[i] - values[i - 1]) >= TRIGGER_LARGE_DELTA_THRESHOLD
    )
    if large_delta_count >= TRIGGER_STEPPED_DELTA_COUNT:
        return TriggerSmoothnessLabel.STEPPED
    if (
        len(values) >= 10
        and monotonicity_violations / len(values) > TRIGGER_NOISY_VIOLATION_RATIO
    ):
        return TriggerSmoothnessLabel.NOISY
    if full_range:
        return TriggerSmoothnessLabel.SMOOTH
    return TriggerSmoothnessLabel.SMOOTH


# ---------------------------------------------------------------------------
# §4 — Observed report cadence
# ---------------------------------------------------------------------------


def compute_observed_cadence(
    samples: Sequence[Sample],
    *,
    configured_polling_hz: Optional[int],
    step_boundaries_ns: Optional[Sequence[tuple[int, int]]] = None,
) -> ObservedCadence:
    """Compute inter-arrival cadence stats from packet-number transitions.

    Per the measurement contract: only count timestamps where ``packet_number`` advanced
    (a duplicate packet means no new HID report arrived since last poll).
    The resulting deltas are app-layer inter-report intervals.

    ``step_boundaries_ns`` (optional) lists ``(start_ns, end_ns)`` windows
    defining the sampling-step intervals; when provided, deltas that bridge
    two different step windows (i.e. cross a READY pause) are excluded from
    the stats and ``duration_ms`` becomes the sum of active per-step spans.
    Empty or ``None`` preserves the original flat-list behaviour.
    """

    # Defensive: empty list is equivalent to None (flat-list behaviour).
    boundaries = (
        list(step_boundaries_ns) if step_boundaries_ns else None
    )

    duration_ms = _duration_ms(samples, step_boundaries_ns=boundaries)
    transition_timestamps_ns = _packet_transition_timestamps(samples)
    if boundaries is None:
        deltas_ms = [
            (transition_timestamps_ns[i] - transition_timestamps_ns[i - 1]) / 1_000_000.0
            for i in range(1, len(transition_timestamps_ns))
        ]
    else:
        deltas_ms = []
        for i in range(1, len(transition_timestamps_ns)):
            prev_ts = transition_timestamps_ns[i - 1]
            curr_ts = transition_timestamps_ns[i]
            for start_ns, end_ns in boundaries:
                if start_ns <= prev_ts <= end_ns and start_ns <= curr_ts <= end_ns:
                    deltas_ms.append((curr_ts - prev_ts) / 1_000_000.0)
                    break
    n_intervals = len(deltas_ms)

    if configured_polling_hz and configured_polling_hz > 0:
        expected_interval_ms: Optional[float] = 1000.0 / configured_polling_hz
    else:
        expected_interval_ms = None

    if n_intervals == 0:
        return _empty_observed_cadence(duration_ms, configured_polling_hz, expected_interval_ms)

    sorted_deltas = sorted(deltas_ms)
    median_dt = float(median(deltas_ms))
    p95_dt = _percentile(sorted_deltas, 0.95)
    p99_dt = _percentile(sorted_deltas, 0.99)
    min_dt = sorted_deltas[0]
    max_dt = sorted_deltas[-1]
    reference = expected_interval_ms if expected_interval_ms is not None else median_dt
    mad = _median_absolute_deviation(deltas_ms, reference)

    if expected_interval_ms is not None:
        gap_2x_threshold = _effective_gap_threshold_ms(
            CADENCE_GAP_2X_RATIO, expected_interval_ms, CADENCE_GAP_2X_FLOOR_MS
        )
        gap_4x_threshold = _effective_gap_threshold_ms(
            CADENCE_GAP_4X_RATIO, expected_interval_ms, CADENCE_GAP_4X_FLOOR_MS
        )
        gaps_2x = sum(1 for d in deltas_ms if d > gap_2x_threshold)
        gaps_4x = sum(1 for d in deltas_ms if d > gap_4x_threshold)
    else:
        gaps_2x = 0
        gaps_4x = 0
    gaps_10ms = sum(1 for d in deltas_ms if d > CADENCE_GAP_10MS_ABS_MS)

    if n_intervals < MIN_SAMPLES_FOR_CADENCE:
        quality = SampleQuality.INSUFFICIENT
        label = CadenceQualityLabel.INSUFFICIENT
    else:
        quality = SampleQuality.GOOD
        label = _cadence_label(n_intervals, gaps_2x, expected_interval_ms)

    return ObservedCadence(
        configured_polling_hz=configured_polling_hz,
        expected_interval_ms=expected_interval_ms,
        # Total observed packet transitions; equals n_intervals + 1 on the
        # flat-list path, and stays the true transition count even when
        # step_boundaries_ns filters some deltas out of the stats.
        sample_count=len(transition_timestamps_ns),
        duration_ms=duration_ms,
        observed_median_interval_ms=median_dt,
        observed_p95_interval_ms=p95_dt,
        observed_p99_interval_ms=p99_dt,
        observed_min_interval_ms=min_dt,
        observed_max_interval_ms=max_dt,
        observed_median_absolute_deviation_ms=mad,
        gaps_over_2x_expected=gaps_2x,
        gaps_over_4x_expected=gaps_4x,
        gaps_over_10ms=gaps_10ms,
        quality=quality,
        label=label,
    )


def _empty_observed_cadence(
    duration_ms: float,
    configured_polling_hz: Optional[int],
    expected_interval_ms: Optional[float],
) -> ObservedCadence:
    return ObservedCadence(
        configured_polling_hz=configured_polling_hz,
        expected_interval_ms=expected_interval_ms,
        sample_count=0,
        duration_ms=duration_ms,
        observed_median_interval_ms=0.0,
        observed_p95_interval_ms=0.0,
        observed_p99_interval_ms=0.0,
        observed_min_interval_ms=0.0,
        observed_max_interval_ms=0.0,
        observed_median_absolute_deviation_ms=0.0,
        gaps_over_2x_expected=0,
        gaps_over_4x_expected=0,
        gaps_over_10ms=0,
        quality=SampleQuality.INSUFFICIENT,
        label=CadenceQualityLabel.INSUFFICIENT,
    )


def _cadence_label(
    n_intervals: int,
    gaps_2x: int,
    expected_interval_ms: Optional[float],
) -> CadenceQualityLabel:
    if expected_interval_ms is None:
        return CadenceQualityLabel.CONSISTENT  # can't compare; just call it consistent if we have samples
    gap_ratio = gaps_2x / n_intervals
    if gap_ratio <= CADENCE_CONSISTENT_GAP_RATIO:
        return CadenceQualityLabel.CONSISTENT
    if gap_ratio <= CADENCE_INCONSISTENT_GAP_RATIO:
        return CadenceQualityLabel.SOME_GAPS
    return CadenceQualityLabel.INCONSISTENT


def _packet_transition_timestamps(samples: Sequence[Sample]) -> list[int]:
    """Return timestamps where the packet number advanced (new HID report).

    Includes the first sample as the anchor; subsequent identical packet
    numbers are skipped. The number of returned timestamps is the number of
    distinct packets observed.
    """

    if not samples:
        return []
    result: list[int] = [samples[0].timestamp_ns]
    last_packet = samples[0].packet_number
    for s in samples[1:]:
        if s.packet_number != last_packet:
            result.append(s.timestamp_ns)
            last_packet = s.packet_number
    return result


# ---------------------------------------------------------------------------
# Overall status (the "Overall report labels" rule — qualitative bands)
# ---------------------------------------------------------------------------


def compute_overall_status(
    *,
    left_rest: StickRestNoise,
    right_rest: StickRestNoise,
    left_range: StickRange,
    right_range: StickRange,
    left_trigger: TriggerRange,
    right_trigger: TriggerRange,
) -> OverallStatus:
    """Reduce per-test labels to an overall status. Conservative: a single
    INSUFFICIENT or a hard band (HIGH / RETEST / NOISY) escalates.

    Cadence is intentionally not a factor: the full Health Report dropped its
    observed-cadence section (2026-05-30), so the overall status reflects only
    rest noise, range coverage, and trigger smoothness.
    """

    parts = (
        left_rest.label, right_rest.label,
        left_range.label, right_range.label,
        left_trigger.label, right_trigger.label,
    )
    if any(p in {RestNoiseLabel.INSUFFICIENT, RangeCoverageLabel.INSUFFICIENT,
                 TriggerSmoothnessLabel.INSUFFICIENT}
           for p in parts):
        return OverallStatus.RETEST_RECOMMENDED
    if (left_rest.label == RestNoiseLabel.HIGH
            or right_rest.label == RestNoiseLabel.HIGH
            or left_range.label == RangeCoverageLabel.RETEST
            or right_range.label == RangeCoverageLabel.RETEST
            or left_trigger.label == TriggerSmoothnessLabel.NOISY
            or right_trigger.label == TriggerSmoothnessLabel.NOISY):
        return OverallStatus.POSSIBLE_ISSUE
    if (left_rest.label == RestNoiseLabel.NOTICEABLE
            or right_rest.label == RestNoiseLabel.NOTICEABLE
            or left_range.label == RangeCoverageLabel.LIMITED
            or right_range.label == RangeCoverageLabel.LIMITED
            or left_trigger.label == TriggerSmoothnessLabel.LIMITED
            or right_trigger.label == TriggerSmoothnessLabel.LIMITED
            or left_trigger.label == TriggerSmoothnessLabel.STEPPED
            or right_trigger.label == TriggerSmoothnessLabel.STEPPED):
        return OverallStatus.TUNING_SUGGESTED
    return OverallStatus.NORMAL


# ---------------------------------------------------------------------------
# Sample accessors
# ---------------------------------------------------------------------------


def _stick_xy_arrays(samples: Sequence[Sample], side: str) -> tuple[list[int], list[int]]:
    if side == "left":
        return [s.left_stick_x for s in samples], [s.left_stick_y for s in samples]
    if side == "right":
        return [s.right_stick_x for s in samples], [s.right_stick_y for s in samples]
    raise ValueError(f"side must be 'left' or 'right'; got {side!r}")


def _trigger_values(samples: Sequence[Sample], side: str) -> list[int]:
    if side == "left":
        return [s.left_trigger for s in samples]
    if side == "right":
        return [s.right_trigger for s in samples]
    raise ValueError(f"side must be 'left' or 'right'; got {side!r}")


def filter_samples_by_time_range(
    samples: Iterable[Sample],
    *,
    start_ns: int,
    end_ns: int,
) -> list[Sample]:
    """Return samples whose timestamp falls within ``[start_ns, end_ns]``.

    Inclusive on both ends. Returned as a list so the orchestrator can call
    measurement functions directly on the slice.
    """

    return [s for s in samples if start_ns <= s.timestamp_ns <= end_ns]


__all__ = [
    "REST_NOISE_GOOD_P99_PCT",
    "REST_NOISE_NOTICEABLE_P99_PCT",
    "REST_NOISE_DEADZONE_MARGIN_PCT",
    "STICK_RANGE_SECTORS_DEFAULT",
    "STICK_RANGE_COVERAGE_THRESHOLD_PCT",
    "STICK_RANGE_GOOD_PCT",
    "STICK_RANGE_LIMITED_PCT",
    "TRIGGER_FULL_RANGE_MIN_MAX",
    "TRIGGER_NEAR_FULL_MIN_MAX",
    "TRIGGER_NOISE_TOLERANCE_RAW",
    "TRIGGER_LARGE_DELTA_THRESHOLD",
    "TRIGGER_STEPPED_DELTA_COUNT",
    "TRIGGER_NOISY_VIOLATION_RATIO",
    "CADENCE_GAP_2X_RATIO",
    "CADENCE_GAP_4X_RATIO",
    "CADENCE_GAP_10MS_ABS_MS",
    "CADENCE_GAP_2X_FLOOR_MS",
    "CADENCE_GAP_4X_FLOOR_MS",
    "MIN_SAMPLES_FOR_REST",
    "MIN_SAMPLES_FOR_RANGE",
    "MIN_SAMPLES_FOR_TRIGGER",
    "MIN_SAMPLES_FOR_CADENCE",
    "compute_stick_rest_noise",
    "compute_stick_range",
    "compute_trigger_range",
    "compute_observed_cadence",
    "compute_overall_status",
    "filter_samples_by_time_range",
]
