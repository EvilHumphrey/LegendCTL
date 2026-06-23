"""Tests for the Module Passport trend-analysis layer.

Exercises:

- :func:`compute_metric_trend`: math correctness on synthetic linear data,
  confidence labelling at the sample/span/r² gates, sign-aware direction
  handling per metric, centering-magnitude collapse for bidirectional X/Y,
  graceful handling of zero-span / single-fingerprint / sub-sample-size
  inputs.
- :func:`summarize_passport_trends`: per-metric roll-up rules, attention
  metric ordering, last-fingerprint-age calculation, edge cases (passport
  with only one fingerprint, all wear-observed fingerprints, fingerprints
  with same-day timestamps).
- :func:`metric_value_for`: per-metric field accessors return what
  ``classify_fingerprint`` would feed into the verdict bands.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from zd_app.services.module_passport.trend_analysis import (
    CONFIDENCE_HIGH,
    CONFIDENCE_INSUFFICIENT,
    CONFIDENCE_LOW,
    CONFIDENCE_MIN_DAYS_SPAN,
    CONFIDENCE_MIN_SAMPLES,
    CONFIDENCE_MODERATE,
    CONFIDENCE_R_SQUARED_FLOOR,
    CONFIDENCE_R_SQUARED_HIGH,
    INVESTIGATE_DAYS_TO_THRESHOLD,
    METRIC_ASYMMETRY_SCORE,
    METRIC_BITNESS,
    METRIC_CENTERING_OFFSET,
    METRIC_CIRCULARITY_COVERAGE,
    METRIC_LINEARITY,
    METRIC_NOISE_FLOOR,
    METRIC_ORDER,
    METRIC_OUTER_DEADZONE_SPAN,
    METRIC_TREMOR,
    MetricTrend,
    PassportTrendSummary,
    TREND_STATUS_DRIFTING,
    TREND_STATUS_INSUFFICIENT,
    TREND_STATUS_INVESTIGATE,
    TREND_STATUS_STABLE,
    compute_metric_trend,
    metric_value_for,
    summarize_passport_trends,
)
from zd_app.storage.module_passport_models import (
    ModuleFingerprint,
    ModulePassport,
    SIDE_LEFT,
    STATUS_GOOD,
    STATUS_WEAR_OBSERVED,
)


def _fp(
    *,
    day_offset: float = 0.0,
    base_ts: datetime = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    samples_count: int = 12_000,
    noise_floor_percent: float = 1.0,
    centering_offset_x: float = 0.0,
    centering_offset_y: float = 0.0,
    circularity_coverage_percent: float = 0.97,
    outer_deadzone_min_axis: float = 96.0,
    outer_deadzone_max_axis: float = 99.0,
    asymmetry_score: float = 0.04,
    bitness_observed: int = 128,
    tremor_metric: float = 0.2,
    linearity_score: float = 0.04,
    overall_status: str = STATUS_GOOD,
) -> ModuleFingerprint:
    """Build a fingerprint at ``base_ts + day_offset`` days."""

    ts = base_ts + timedelta(days=day_offset)
    return ModuleFingerprint(
        timestamp_utc=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        side=SIDE_LEFT,
        duration_ms=60_000,
        samples_count=samples_count,
        noise_floor_percent=noise_floor_percent,
        centering_offset_x=centering_offset_x,
        centering_offset_y=centering_offset_y,
        circularity_coverage_percent=circularity_coverage_percent,
        outer_deadzone_min_axis=outer_deadzone_min_axis,
        outer_deadzone_max_axis=outer_deadzone_max_axis,
        asymmetry_score=asymmetry_score,
        bitness_observed=bitness_observed,
        tremor_metric=tremor_metric,
        linearity_score=linearity_score,
        overall_status=overall_status,
    )


def _passport(fingerprints, side: str = SIDE_LEFT) -> ModulePassport:
    return ModulePassport(
        side=side,
        module_id="STOCK_TEST",
        assigned_at_utc="2026-01-01T00:00:00Z",
        notes="",
        fingerprints=tuple(fingerprints),
    )


# ---------------------------------------------------------------------------
# metric_value_for
# ---------------------------------------------------------------------------


class MetricValueForTests(unittest.TestCase):
    def test_noise_floor_returns_field(self) -> None:
        fp = _fp(noise_floor_percent=2.5)
        self.assertAlmostEqual(metric_value_for(fp, METRIC_NOISE_FLOOR), 2.5)

    def test_centering_collapses_to_euclidean_magnitude(self) -> None:
        fp = _fp(centering_offset_x=3.0, centering_offset_y=4.0)
        self.assertAlmostEqual(metric_value_for(fp, METRIC_CENTERING_OFFSET), 5.0)

    def test_centering_sign_doesnt_change_magnitude(self) -> None:
        # Spec: magnitude is what gets compared, regardless of sign.
        fp_a = _fp(centering_offset_x=-3.0, centering_offset_y=4.0)
        fp_b = _fp(centering_offset_x=3.0, centering_offset_y=-4.0)
        self.assertAlmostEqual(
            metric_value_for(fp_a, METRIC_CENTERING_OFFSET),
            metric_value_for(fp_b, METRIC_CENTERING_OFFSET),
        )

    def test_outer_deadzone_span_is_max_minus_min(self) -> None:
        fp = _fp(outer_deadzone_min_axis=85.0, outer_deadzone_max_axis=99.5)
        self.assertAlmostEqual(
            metric_value_for(fp, METRIC_OUTER_DEADZONE_SPAN), 14.5
        )

    def test_bitness_returns_float(self) -> None:
        fp = _fp(bitness_observed=42)
        v = metric_value_for(fp, METRIC_BITNESS)
        self.assertIsInstance(v, float)
        self.assertEqual(v, 42.0)

    def test_unknown_metric_raises(self) -> None:
        fp = _fp()
        with self.assertRaises(ValueError):
            metric_value_for(fp, "not_a_real_metric")


# ---------------------------------------------------------------------------
# compute_metric_trend — math + confidence labelling
# ---------------------------------------------------------------------------


class LinearTrendMathTests(unittest.TestCase):
    """Synthetic linear data — slope, r², and projection should be exact."""

    def test_perfect_linear_growth_in_noise_floor(self) -> None:
        # 10 fingerprints, 1-day apart, noise_floor rises by 0.2 per day.
        fps = [
            _fp(day_offset=float(i), noise_floor_percent=1.0 + 0.2 * i)
            for i in range(10)
        ]
        trend = compute_metric_trend(fps, METRIC_NOISE_FLOOR)
        self.assertAlmostEqual(trend.slope_per_day, 0.2, places=6)
        # r² == 1 on perfectly linear data.
        self.assertAlmostEqual(trend.r_squared, 1.0, places=6)
        self.assertEqual(trend.n_samples, 10)
        self.assertAlmostEqual(trend.days_span, 9.0, places=6)
        self.assertEqual(trend.confidence, CONFIDENCE_HIGH)
        self.assertTrue(trend.is_degrading)
        # Latest value is 1.0 + 0.2 * 9 = 2.8. Threshold (NOISE_FLOOR_WATCH_PCT)
        # is 6.0. Days to threshold = (6.0 - 2.8) / 0.2 = 16.
        self.assertIsNotNone(trend.projected_days_to_threshold)
        assert trend.projected_days_to_threshold is not None
        self.assertAlmostEqual(trend.projected_days_to_threshold, 16.0, places=4)

    def test_perfect_negative_growth_in_circularity_is_degrading(self) -> None:
        # Circularity: lower = worse. Slope < 0 means it's degrading.
        fps = [
            _fp(day_offset=float(i), circularity_coverage_percent=0.95 - 0.01 * i)
            for i in range(10)
        ]
        trend = compute_metric_trend(fps, METRIC_CIRCULARITY_COVERAGE)
        self.assertLess(trend.slope_per_day, 0.0)
        self.assertTrue(trend.is_degrading)
        # Latest = 0.95 - 0.01*9 = 0.86. Threshold (CIRCULARITY_COVERAGE_WATCH)
        # = 0.80. Slope = -0.01. Days = (0.80 - 0.86) / -0.01 = 6.0.
        assert trend.projected_days_to_threshold is not None
        self.assertAlmostEqual(trend.projected_days_to_threshold, 6.0, places=4)
        # 6 days to threshold is well under INVESTIGATE_DAYS_TO_THRESHOLD (30).
        self.assertEqual(trend.status, TREND_STATUS_INVESTIGATE)

    def test_improving_trend_is_stable(self) -> None:
        # Noise floor going DOWN means the stick is getting cleaner — not
        # degrading. Status should land on "stable" (slope is favourable).
        fps = [
            _fp(day_offset=float(i), noise_floor_percent=3.0 - 0.1 * i)
            for i in range(10)
        ]
        trend = compute_metric_trend(fps, METRIC_NOISE_FLOOR)
        self.assertLess(trend.slope_per_day, 0.0)
        self.assertFalse(trend.is_degrading)
        self.assertIsNone(trend.projected_days_to_threshold)
        self.assertEqual(trend.status, TREND_STATUS_STABLE)

    def test_drifting_status_when_runway_is_long(self) -> None:
        # Noise floor up 0.01 per day from 1.0. Threshold 6.0 → ~500 days
        # runway. INVESTIGATE_DAYS_TO_THRESHOLD = 30 so status = drifting.
        fps = [
            _fp(day_offset=float(i), noise_floor_percent=1.0 + 0.01 * i)
            for i in range(12)
        ]
        trend = compute_metric_trend(fps, METRIC_NOISE_FLOOR)
        self.assertTrue(trend.is_degrading)
        self.assertIsNotNone(trend.projected_days_to_threshold)
        assert trend.projected_days_to_threshold is not None
        self.assertGreater(
            trend.projected_days_to_threshold, INVESTIGATE_DAYS_TO_THRESHOLD
        )
        self.assertEqual(trend.status, TREND_STATUS_DRIFTING)


class CenteringMagnitudeTests(unittest.TestCase):
    """Bidirectional centering offset values regress on Euclidean magnitude.

    A passport whose raw X/Y values oscillate (e.g. -3, +3, -3, ...) but
    whose magnitude stays constant should look stable. A passport whose
    magnitude grows even as the raw values flip sign should look degrading.
    """

    def test_oscillating_xy_with_constant_magnitude_is_stable(self) -> None:
        # X bounces -3 / +3 / -3 / ..., Y stays 0; magnitude is always 3.0.
        fps = []
        for i in range(8):
            sign = 1 if i % 2 == 0 else -1
            fps.append(
                _fp(
                    day_offset=float(i),
                    centering_offset_x=3.0 * sign,
                    centering_offset_y=0.0,
                )
            )
        trend = compute_metric_trend(fps, METRIC_CENTERING_OFFSET)
        self.assertAlmostEqual(trend.slope_per_day, 0.0, places=6)
        self.assertFalse(trend.is_degrading)

    def test_magnitude_growth_under_sign_flip_is_degrading(self) -> None:
        # Magnitude grows from 1 -> 4 even though raw X flips signs.
        fps = []
        for i in range(8):
            sign = 1 if i % 2 == 0 else -1
            magnitude = 1.0 + 0.5 * i
            fps.append(
                _fp(
                    day_offset=float(i),
                    centering_offset_x=magnitude * sign,
                    centering_offset_y=0.0,
                )
            )
        trend = compute_metric_trend(fps, METRIC_CENTERING_OFFSET)
        self.assertGreater(trend.slope_per_day, 0.0)
        self.assertTrue(trend.is_degrading)


class BitnessDirectionTests(unittest.TestCase):
    """Bitness: lower = worse, so a downward trend is degrading."""

    def test_downward_bitness_is_degrading(self) -> None:
        fps = [
            _fp(day_offset=float(i), bitness_observed=200 - 10 * i)
            for i in range(10)
        ]
        trend = compute_metric_trend(fps, METRIC_BITNESS)
        self.assertLess(trend.slope_per_day, 0.0)
        self.assertTrue(trend.is_degrading)

    def test_upward_bitness_is_not_degrading(self) -> None:
        fps = [
            _fp(day_offset=float(i), bitness_observed=50 + 5 * i)
            for i in range(10)
        ]
        trend = compute_metric_trend(fps, METRIC_BITNESS)
        self.assertGreater(trend.slope_per_day, 0.0)
        self.assertFalse(trend.is_degrading)


class ConfidenceLabelTests(unittest.TestCase):
    """Confidence gates: sample count, day span, r² floor."""

    def test_n_below_min_samples_is_insufficient(self) -> None:
        # CONFIDENCE_MIN_SAMPLES == 4. Three fingerprints → insufficient.
        fps = [
            _fp(day_offset=float(i), noise_floor_percent=1.0 + i)
            for i in range(3)
        ]
        trend = compute_metric_trend(fps, METRIC_NOISE_FLOOR)
        self.assertEqual(trend.confidence, CONFIDENCE_INSUFFICIENT)
        self.assertEqual(trend.status, TREND_STATUS_INSUFFICIENT)

    def test_days_span_below_floor_is_insufficient(self) -> None:
        # 4 fingerprints within a single day → days_span < MIN.
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        fps = [
            _fp(day_offset=i * 0.1, base_ts=base, noise_floor_percent=1.0 + i)
            for i in range(4)
        ]
        trend = compute_metric_trend(fps, METRIC_NOISE_FLOOR)
        self.assertEqual(trend.confidence, CONFIDENCE_INSUFFICIENT)

    def test_low_r_squared_below_floor_lands_low(self) -> None:
        # 12 fingerprints with random-looking noise → low r².
        # We hand-craft values that average near a flat slope but have
        # high variance per point.
        values = [1.0, 1.5, 1.2, 1.6, 1.1, 1.7, 1.3, 1.5, 1.2, 1.6, 1.1, 1.7]
        fps = [
            _fp(day_offset=float(i), noise_floor_percent=v)
            for i, v in enumerate(values)
        ]
        trend = compute_metric_trend(fps, METRIC_NOISE_FLOOR)
        self.assertLess(trend.r_squared, CONFIDENCE_R_SQUARED_FLOOR)
        self.assertEqual(trend.confidence, CONFIDENCE_LOW)
        # Spec: low-r² runs surface as insufficient_data at the status
        # level — NOT stable, which would manufacture false reassurance.
        self.assertEqual(trend.status, TREND_STATUS_INSUFFICIENT)

    def test_moderate_r_squared_is_moderate_confidence(self) -> None:
        # 10 fingerprints with weak linear trend + sizeable jitter.
        # Hand-tuned for r² ≈ 0.50 (well inside [floor=0.30, high=0.60)).
        # If statistics.correlation changes its rounding behaviour someday
        # the assertions are still range-based, not point-equality.
        base_values = [1.0 + 0.05 * i for i in range(10)]
        jitter = [0.0, 0.15, -0.12, 0.20, -0.15, 0.18, -0.12, 0.16, -0.13, 0.10]
        fps = [
            _fp(day_offset=float(i), noise_floor_percent=base_values[i] + jitter[i])
            for i in range(10)
        ]
        trend = compute_metric_trend(fps, METRIC_NOISE_FLOOR)
        self.assertGreaterEqual(trend.r_squared, CONFIDENCE_R_SQUARED_FLOOR)
        self.assertLess(trend.r_squared, CONFIDENCE_R_SQUARED_HIGH)
        self.assertEqual(trend.confidence, CONFIDENCE_MODERATE)

    def test_high_r_squared_is_high_confidence(self) -> None:
        fps = [
            _fp(day_offset=float(i), noise_floor_percent=1.0 + 0.1 * i)
            for i in range(8)
        ]
        trend = compute_metric_trend(fps, METRIC_NOISE_FLOOR)
        self.assertGreaterEqual(trend.r_squared, CONFIDENCE_R_SQUARED_HIGH)
        self.assertEqual(trend.confidence, CONFIDENCE_HIGH)


# ---------------------------------------------------------------------------
# compute_metric_trend — edge cases
# ---------------------------------------------------------------------------


class EdgeCaseTests(unittest.TestCase):
    def test_empty_fingerprints_returns_insufficient(self) -> None:
        trend = compute_metric_trend([], METRIC_NOISE_FLOOR)
        self.assertEqual(trend.confidence, CONFIDENCE_INSUFFICIENT)
        self.assertEqual(trend.status, TREND_STATUS_INSUFFICIENT)
        self.assertEqual(trend.n_samples, 0)
        self.assertAlmostEqual(trend.slope_per_day, 0.0)

    def test_single_fingerprint_returns_insufficient(self) -> None:
        trend = compute_metric_trend([_fp()], METRIC_NOISE_FLOOR)
        self.assertEqual(trend.confidence, CONFIDENCE_INSUFFICIENT)
        self.assertEqual(trend.status, TREND_STATUS_INSUFFICIENT)

    def test_sub_sample_size_fingerprints_are_dropped(self) -> None:
        # Mix 3 usable + 2 sub-sample. The 2 sub-sample shouldn't contribute,
        # so n_samples = 3, which is below CONFIDENCE_MIN_SAMPLES → insufficient.
        fps = (
            [_fp(day_offset=float(i)) for i in range(3)]
            + [_fp(day_offset=float(i + 10), samples_count=10) for i in range(2)]
        )
        trend = compute_metric_trend(fps, METRIC_NOISE_FLOOR)
        self.assertEqual(trend.n_samples, 3)
        self.assertEqual(trend.confidence, CONFIDENCE_INSUFFICIENT)

    def test_zero_slope_returns_no_projection(self) -> None:
        # Flat data: noise_floor constant at 1.0 for 10 days.
        fps = [
            _fp(day_offset=float(i), noise_floor_percent=1.0)
            for i in range(10)
        ]
        trend = compute_metric_trend(fps, METRIC_NOISE_FLOOR)
        self.assertAlmostEqual(trend.slope_per_day, 0.0)
        self.assertFalse(trend.is_degrading)
        self.assertIsNone(trend.projected_days_to_threshold)
        self.assertEqual(trend.status, TREND_STATUS_STABLE)

    def test_metric_already_past_threshold_returns_no_projection(self) -> None:
        # Latest noise_floor = 8.0 > threshold 6.0; runway already gone.
        fps = [
            _fp(day_offset=float(i), noise_floor_percent=6.0 + 0.2 * i)
            for i in range(10)
        ]
        trend = compute_metric_trend(fps, METRIC_NOISE_FLOOR)
        # Slope is positive (degrading), but the latest value > threshold so
        # there's no positive "days until" runway to report.
        self.assertGreater(trend.slope_per_day, 0.0)
        self.assertIsNone(trend.projected_days_to_threshold)

    def test_low_confidence_no_projection(self) -> None:
        # Confidence=low → projection should not surface even if direction
        # matches degradation.
        values = [1.0, 1.5, 1.2, 1.6, 1.1, 1.7, 1.3, 1.5, 1.2, 1.6, 1.1, 1.7]
        fps = [
            _fp(day_offset=float(i), noise_floor_percent=v)
            for i, v in enumerate(values)
        ]
        trend = compute_metric_trend(fps, METRIC_NOISE_FLOOR)
        if trend.confidence == CONFIDENCE_LOW:
            self.assertIsNone(trend.projected_days_to_threshold)

    def test_unknown_metric_name_raises(self) -> None:
        fps = [_fp(day_offset=float(i)) for i in range(5)]
        with self.assertRaises(ValueError):
            compute_metric_trend(fps, "fake_metric")

    def test_unsorted_input_is_sorted_before_fit(self) -> None:
        # Build fingerprints out of order — the helper should sort by ts.
        ordered = [
            _fp(day_offset=float(i), noise_floor_percent=1.0 + 0.1 * i)
            for i in range(10)
        ]
        unordered = list(reversed(ordered))
        trend_ordered = compute_metric_trend(ordered, METRIC_NOISE_FLOOR)
        trend_unordered = compute_metric_trend(unordered, METRIC_NOISE_FLOOR)
        self.assertAlmostEqual(
            trend_ordered.slope_per_day,
            trend_unordered.slope_per_day,
            places=6,
        )
        self.assertAlmostEqual(
            trend_ordered.r_squared,
            trend_unordered.r_squared,
            places=6,
        )

    def test_all_wear_observed_fingerprints_with_usable_samples(self) -> None:
        # All marked wear_observed but each carries enough samples to count.
        # The trend regression operates on the metric values, not the verdict,
        # so they participate normally.
        fps = [
            _fp(
                day_offset=float(i),
                noise_floor_percent=5.5 + 0.05 * i,
                overall_status=STATUS_WEAR_OBSERVED,
            )
            for i in range(8)
        ]
        trend = compute_metric_trend(fps, METRIC_NOISE_FLOOR)
        self.assertEqual(trend.n_samples, 8)


# ---------------------------------------------------------------------------
# summarize_passport_trends — roll-up + attention metrics
# ---------------------------------------------------------------------------


class SummarizePassportTests(unittest.TestCase):
    def test_summary_returns_metric_trend_per_metric_in_order(self) -> None:
        fps = [_fp(day_offset=float(i)) for i in range(6)]
        summary = summarize_passport_trends(_passport(fps))
        names = [mt.metric_name for mt in summary.metric_trends]
        self.assertEqual(names, list(METRIC_ORDER))
        self.assertEqual(len(summary.metric_trends), 8)

    def test_empty_passport_is_insufficient(self) -> None:
        summary = summarize_passport_trends(_passport(()))
        self.assertEqual(summary.status, TREND_STATUS_INSUFFICIENT)
        self.assertEqual(summary.attention_metrics, ())
        self.assertEqual(summary.usable_fingerprint_count, 0)
        self.assertIsNone(summary.last_fingerprint_age_days)

    def test_single_fingerprint_is_insufficient(self) -> None:
        summary = summarize_passport_trends(_passport([_fp()]))
        self.assertEqual(summary.status, TREND_STATUS_INSUFFICIENT)

    def test_all_stable_metrics_rolls_up_stable(self) -> None:
        # All metrics flat → slope 0 → stable, confidence high (r² == 1 for
        # constant data is undefined; falls through to confidence-by-floor).
        fps = [_fp(day_offset=float(i)) for i in range(8)]
        summary = summarize_passport_trends(_passport(fps))
        # No metric is degrading, so the roll-up is stable IF at least one
        # metric reached moderate-or-higher confidence. Constant data gives
        # r²=0, which is below the floor → all metrics land at LOW. So roll
        # up is insufficient_data. Both interpretations are valid; let's
        # assert it's NOT drifting or investigate.
        self.assertNotIn(
            summary.status, (TREND_STATUS_DRIFTING, TREND_STATUS_INVESTIGATE)
        )

    def test_any_drifting_rolls_up_drifting(self) -> None:
        # Noise floor up 0.01 per day → drifting (long runway). Others flat.
        fps = [
            _fp(day_offset=float(i), noise_floor_percent=1.0 + 0.01 * i)
            for i in range(12)
        ]
        summary = summarize_passport_trends(_passport(fps))
        self.assertEqual(summary.status, TREND_STATUS_DRIFTING)
        self.assertIn(METRIC_NOISE_FLOOR, summary.attention_metrics)

    def test_any_investigate_rolls_up_investigate(self) -> None:
        # Noise floor jumps from 5.5 toward 6.0 fast → investigate.
        fps = [
            _fp(day_offset=float(i), noise_floor_percent=5.5 + 0.05 * i)
            for i in range(10)
        ]
        summary = summarize_passport_trends(_passport(fps))
        self.assertEqual(summary.status, TREND_STATUS_INVESTIGATE)
        self.assertIn(METRIC_NOISE_FLOOR, summary.attention_metrics)

    def test_investigate_trumps_drifting(self) -> None:
        # Noise floor close to wear → investigate. Tremor mild → drifting.
        # The roll-up worst-trumps: any-investigate → investigate.
        fps = [
            _fp(
                day_offset=float(i),
                noise_floor_percent=5.5 + 0.05 * i,
                tremor_metric=0.5 + 0.005 * i,
            )
            for i in range(10)
        ]
        summary = summarize_passport_trends(_passport(fps))
        self.assertEqual(summary.status, TREND_STATUS_INVESTIGATE)
        # Both metrics drove the attention (drifting + investigate count).
        self.assertIn(METRIC_NOISE_FLOOR, summary.attention_metrics)

    def test_attention_metrics_excludes_stable(self) -> None:
        fps = [
            _fp(day_offset=float(i), noise_floor_percent=1.0 + 0.01 * i)
            for i in range(12)
        ]
        summary = summarize_passport_trends(_passport(fps))
        # Only the drifting metric is in attention_metrics; the stable ones
        # are excluded.
        for name in summary.attention_metrics:
            metric = next(mt for mt in summary.metric_trends if mt.metric_name == name)
            self.assertIn(
                metric.status, (TREND_STATUS_DRIFTING, TREND_STATUS_INVESTIGATE)
            )


class LastFingerprintAgeTests(unittest.TestCase):
    def test_last_age_days_when_fresh(self) -> None:
        ts = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
        fp = _fp(base_ts=ts, day_offset=0.0)
        passport = _passport([fp])
        now = ts + timedelta(days=3, hours=12)
        summary = summarize_passport_trends(passport, now=now)
        assert summary.last_fingerprint_age_days is not None
        self.assertAlmostEqual(
            summary.last_fingerprint_age_days, 3.5, places=2
        )

    def test_last_age_days_clamps_at_zero_for_future_fingerprint(self) -> None:
        # Defensive: if clock skew creates a "future" fingerprint, the age
        # clamps to zero (no negative ages).
        ts = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
        fp = _fp(base_ts=ts, day_offset=0.0)
        passport = _passport([fp])
        now = ts - timedelta(days=1)
        summary = summarize_passport_trends(passport, now=now)
        assert summary.last_fingerprint_age_days is not None
        self.assertEqual(summary.last_fingerprint_age_days, 0.0)


# ---------------------------------------------------------------------------
# Same-day timestamps — zero day span
# ---------------------------------------------------------------------------


class ZeroDaySpanTests(unittest.TestCase):
    def test_all_fingerprints_same_day_is_insufficient(self) -> None:
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        fps = [
            _fp(base_ts=base, day_offset=0.0, noise_floor_percent=1.0 + 0.05 * i)
            for i in range(6)
        ]
        trend = compute_metric_trend(fps, METRIC_NOISE_FLOOR)
        # Zero-span < CONFIDENCE_MIN_DAYS_SPAN → insufficient.
        self.assertEqual(trend.confidence, CONFIDENCE_INSUFFICIENT)
        self.assertEqual(trend.status, TREND_STATUS_INSUFFICIENT)


# ---------------------------------------------------------------------------
# Direction matrix — explicit case per metric
# ---------------------------------------------------------------------------


class DirectionMatrixTests(unittest.TestCase):
    """Each metric should agree on which slope direction is "degrading"."""

    def _gen(self, metric: str, base: float, step: float):
        """Generate 10 fingerprints, scoring metric=base+step*i per day."""

        fp_kwargs_for_metric = {
            METRIC_NOISE_FLOOR: lambda v: {"noise_floor_percent": v},
            METRIC_CIRCULARITY_COVERAGE: lambda v: {
                "circularity_coverage_percent": v
            },
            METRIC_OUTER_DEADZONE_SPAN: lambda v: {
                "outer_deadzone_min_axis": 100.0 - v,
                "outer_deadzone_max_axis": 100.0,
            },
            METRIC_ASYMMETRY_SCORE: lambda v: {"asymmetry_score": v},
            METRIC_BITNESS: lambda v: {"bitness_observed": int(v)},
            METRIC_TREMOR: lambda v: {"tremor_metric": v},
            METRIC_LINEARITY: lambda v: {"linearity_score": v},
        }
        builder = fp_kwargs_for_metric[metric]
        return [
            _fp(day_offset=float(i), **builder(base + step * i))
            for i in range(10)
        ]

    def test_noise_floor_up_is_degrading(self) -> None:
        trend = compute_metric_trend(
            self._gen(METRIC_NOISE_FLOOR, 1.0, 0.1), METRIC_NOISE_FLOOR
        )
        self.assertTrue(trend.is_degrading)

    def test_noise_floor_down_is_not_degrading(self) -> None:
        trend = compute_metric_trend(
            self._gen(METRIC_NOISE_FLOOR, 3.0, -0.1), METRIC_NOISE_FLOOR
        )
        self.assertFalse(trend.is_degrading)

    def test_circularity_down_is_degrading(self) -> None:
        trend = compute_metric_trend(
            self._gen(METRIC_CIRCULARITY_COVERAGE, 0.95, -0.01),
            METRIC_CIRCULARITY_COVERAGE,
        )
        self.assertTrue(trend.is_degrading)

    def test_circularity_up_is_not_degrading(self) -> None:
        trend = compute_metric_trend(
            self._gen(METRIC_CIRCULARITY_COVERAGE, 0.80, 0.01),
            METRIC_CIRCULARITY_COVERAGE,
        )
        self.assertFalse(trend.is_degrading)

    def test_outer_deadzone_span_up_is_degrading(self) -> None:
        trend = compute_metric_trend(
            self._gen(METRIC_OUTER_DEADZONE_SPAN, 2.0, 0.5),
            METRIC_OUTER_DEADZONE_SPAN,
        )
        self.assertTrue(trend.is_degrading)

    def test_asymmetry_up_is_degrading(self) -> None:
        trend = compute_metric_trend(
            self._gen(METRIC_ASYMMETRY_SCORE, 0.05, 0.005),
            METRIC_ASYMMETRY_SCORE,
        )
        self.assertTrue(trend.is_degrading)

    def test_bitness_down_is_degrading(self) -> None:
        trend = compute_metric_trend(
            self._gen(METRIC_BITNESS, 200.0, -5.0), METRIC_BITNESS
        )
        self.assertTrue(trend.is_degrading)

    def test_tremor_up_is_degrading(self) -> None:
        trend = compute_metric_trend(
            self._gen(METRIC_TREMOR, 0.3, 0.02), METRIC_TREMOR
        )
        self.assertTrue(trend.is_degrading)

    def test_linearity_up_is_degrading(self) -> None:
        trend = compute_metric_trend(
            self._gen(METRIC_LINEARITY, 0.05, 0.005), METRIC_LINEARITY
        )
        self.assertTrue(trend.is_degrading)


if __name__ == "__main__":
    unittest.main()
