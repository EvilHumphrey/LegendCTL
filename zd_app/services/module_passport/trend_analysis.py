"""Trend analysis for :class:`ModulePassport` fingerprint histories.

Pure functions only — no I/O, no DPG, no service handles. The screen layer
reads a passport from :class:`ModulePassportService` and passes the
fingerprint tuple through :func:`summarize_passport_trends` to get a
per-metric breakdown plus a roll-up status. The math is straight
least-squares regression of (timestamp_days, metric_value) pairs.

The eight metric trends mirror what the MVP detail view already renders:
``noise_floor``, ``centering_offset`` (Euclidean magnitude across X+Y),
``circularity_coverage_percent``, ``outer_deadzone_span`` (max - min),
``asymmetry_score``, ``bitness_observed``, ``tremor_metric``,
``linearity_score``. Centering is collapsed into one magnitude trend
because the MVP verdict logic itself uses Euclidean magnitude — keeping
the two surfaces aligned avoids a "trend says X but verdict says Y" mode
mismatch on the same passport.

Confidence labelling is load-bearing. Sub-sample-size runs are dropped
before regression (the MVP marks them ``wear_observed`` regardless so
their values aren't trustworthy as trend points). Below the configured
sample floor, day-span floor, or r² floor, the metric trend surfaces as
``insufficient_data`` — never as ``stable``, which would manufacture
false reassurance.

Wear-observed thresholds are re-imported from
:mod:`zd_app.services.module_passport.characterize` so the trend module
follows any tuning of the MVP bands automatically (single source of
truth).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import correlation, linear_regression, StatisticsError
from typing import Optional, Sequence

from zd_app.services.health_report.measurements import (
    MIN_SAMPLES_FOR_RANGE,
    MIN_SAMPLES_FOR_REST,
)
from zd_app.services.module_passport.characterize import (
    ASYMMETRY_WATCH,
    BITNESS_WATCH,
    CENTERING_OFFSET_WATCH_PCT,
    CIRCULARITY_COVERAGE_WATCH,
    LINEARITY_WATCH,
    NOISE_FLOOR_WATCH_PCT,
    OUTER_DEADZONE_WATCH_SPAN,
    TREMOR_WATCH,
)
from zd_app.storage.module_passport_models import ModuleFingerprint, ModulePassport


# ---------------------------------------------------------------------------
# Tunables — operator-adjustable confidence gates
# ---------------------------------------------------------------------------


CONFIDENCE_MIN_SAMPLES = 4
CONFIDENCE_MIN_DAYS_SPAN = 7.0
CONFIDENCE_R_SQUARED_FLOOR = 0.30
CONFIDENCE_R_SQUARED_HIGH = 0.60

# Projected-days-to-threshold cutoff: a metric trending toward wear with
# this little runway gets bumped from "drifting" up to "investigate".
INVESTIGATE_DAYS_TO_THRESHOLD = 30.0

# When the most recent fingerprint is older than this, the screen renders a
# "consider re-characterizing" hint — the trend confidence will only stay
# fresh if the operator runs new characterizations.
ATTENTION_AGE_DAYS = 90.0


# ---------------------------------------------------------------------------
# Confidence / status labels
# ---------------------------------------------------------------------------


CONFIDENCE_INSUFFICIENT = "insufficient_data"
CONFIDENCE_LOW = "low"
CONFIDENCE_MODERATE = "moderate"
CONFIDENCE_HIGH = "high"

CONFIDENCE_LABELS: tuple[str, ...] = (
    CONFIDENCE_INSUFFICIENT,
    CONFIDENCE_LOW,
    CONFIDENCE_MODERATE,
    CONFIDENCE_HIGH,
)

TREND_STATUS_INSUFFICIENT = "insufficient_data"
TREND_STATUS_STABLE = "stable"
TREND_STATUS_DRIFTING = "drifting"
TREND_STATUS_INVESTIGATE = "investigate"

TREND_STATUSES: tuple[str, ...] = (
    TREND_STATUS_INSUFFICIENT,
    TREND_STATUS_STABLE,
    TREND_STATUS_DRIFTING,
    TREND_STATUS_INVESTIGATE,
)


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------


METRIC_NOISE_FLOOR = "noise_floor"
METRIC_CENTERING_OFFSET = "centering_offset"
METRIC_CIRCULARITY_COVERAGE = "circularity_coverage"
METRIC_OUTER_DEADZONE_SPAN = "outer_deadzone_span"
METRIC_ASYMMETRY_SCORE = "asymmetry_score"
METRIC_BITNESS = "bitness"
METRIC_TREMOR = "tremor"
METRIC_LINEARITY = "linearity"

# Stable order — matches the MVP detail row order so the operator's eye
# tracks across views consistently.
METRIC_ORDER: tuple[str, ...] = (
    METRIC_NOISE_FLOOR,
    METRIC_CENTERING_OFFSET,
    METRIC_CIRCULARITY_COVERAGE,
    METRIC_OUTER_DEADZONE_SPAN,
    METRIC_ASYMMETRY_SCORE,
    METRIC_BITNESS,
    METRIC_TREMOR,
    METRIC_LINEARITY,
)

# Direction toward the wear_observed band. +1 means "higher = worse" (the
# value is moving toward the threshold from below). -1 means "lower =
# worse". Centering is collapsed into the Euclidean magnitude before
# regression — the magnitude itself can only go up to indicate wear, so
# +1 is correct here (the bidirectional X/Y axes are absorbed by the
# magnitude computation, mirroring the MVP's classify_fingerprint logic).
_METRIC_WEAR_DIRECTION: dict[str, int] = {
    METRIC_NOISE_FLOOR: +1,
    METRIC_CENTERING_OFFSET: +1,
    METRIC_CIRCULARITY_COVERAGE: -1,
    METRIC_OUTER_DEADZONE_SPAN: +1,
    METRIC_ASYMMETRY_SCORE: +1,
    METRIC_BITNESS: -1,
    METRIC_TREMOR: +1,
    METRIC_LINEARITY: +1,
}


# Wear-observed thresholds per metric — single source of truth lives in
# characterize.py. Centering uses the per-axis watch threshold applied to
# the Euclidean magnitude (matches classify_fingerprint).
_METRIC_WEAR_THRESHOLD: dict[str, float] = {
    METRIC_NOISE_FLOOR: NOISE_FLOOR_WATCH_PCT,
    METRIC_CENTERING_OFFSET: CENTERING_OFFSET_WATCH_PCT,
    METRIC_CIRCULARITY_COVERAGE: CIRCULARITY_COVERAGE_WATCH,
    METRIC_OUTER_DEADZONE_SPAN: OUTER_DEADZONE_WATCH_SPAN,
    METRIC_ASYMMETRY_SCORE: ASYMMETRY_WATCH,
    METRIC_BITNESS: float(BITNESS_WATCH),
    METRIC_TREMOR: TREMOR_WATCH,
    METRIC_LINEARITY: LINEARITY_WATCH,
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricTrend:
    """Regression result for one metric across a passport's fingerprints.

    Fields:
        metric_name: One of the ``METRIC_*`` constants — matches an entry
            in :data:`METRIC_ORDER`.
        slope_per_day: Signed slope in metric-units-per-calendar-day.
        r_squared: Coefficient of determination of the linear fit. 0.0
            when the regression failed or there's no signal.
        n_samples: Count of fingerprints that contributed to the fit (excludes
            sub-sample-size runs that the MVP marks ``wear_observed``).
        days_span: Calendar days between the oldest and newest contributing
            fingerprints.
        latest_value: Most-recent contributing fingerprint's metric value
            (post-collapse, e.g. Euclidean magnitude for centering).
        projected_days_to_threshold: Days from the latest fingerprint until
            the metric would cross its wear_observed band, assuming the
            current slope holds. ``None`` when not actionable: slope is
            zero, confidence is below moderate, direction is away from the
            wear band, or the metric is already past the threshold.
        confidence: Categorical label — :data:`CONFIDENCE_LABELS`.
        status: Per-metric bucket — :data:`TREND_STATUSES`. Roll-up to the
            passport-level status happens in
            :func:`summarize_passport_trends`.
        is_degrading: True iff the slope direction is toward the wear band
            with sufficient confidence. Surfaced for tests / debug.
    """

    metric_name: str
    slope_per_day: float
    r_squared: float
    n_samples: int
    days_span: float
    latest_value: float
    projected_days_to_threshold: Optional[float]
    confidence: str
    status: str
    is_degrading: bool


@dataclass(frozen=True)
class PassportTrendSummary:
    """Roll-up of all metric trends for one side's passport.

    Fields:
        side: ``"left"`` or ``"right"``.
        status: Roll-up status — :data:`TREND_STATUSES`. Worst-trumps:
            any ``investigate`` metric trend → ``investigate``; any
            ``drifting`` → ``drifting``; otherwise ``stable`` if at least
            one metric reached moderate-or-higher confidence; else
            ``insufficient_data``.
        metric_trends: One :class:`MetricTrend` per entry in
            :data:`METRIC_ORDER`. Order is stable so the UI can render
            consistently across rebuilds.
        attention_metrics: Names of the metrics whose status is
            ``drifting`` or ``investigate`` — i.e. the ones driving the
            roll-up. Empty when status is ``stable`` /
            ``insufficient_data``.
        last_fingerprint_age_days: Days between ``now`` and the most-
            recent fingerprint. ``None`` when the passport has no
            fingerprints.
        usable_fingerprint_count: How many fingerprints passed the
            sub-sample-size filter and contributed to the regressions.
            ``< CONFIDENCE_MIN_SAMPLES`` means every metric trend will
            land at ``insufficient_data``.
    """

    side: str
    status: str
    metric_trends: tuple[MetricTrend, ...]
    attention_metrics: tuple[str, ...]
    last_fingerprint_age_days: Optional[float]
    usable_fingerprint_count: int


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def metric_value_for(fingerprint: ModuleFingerprint, metric_name: str) -> float:
    """Extract the per-metric value from a fingerprint.

    Centering is collapsed to its Euclidean magnitude here so callers
    don't have to know about the X/Y split. The outer-deadzone span is
    computed as ``max - min`` so the regression operates on the same
    quantity the MVP verdict logic uses.
    """

    if metric_name == METRIC_NOISE_FLOOR:
        return float(fingerprint.noise_floor_percent)
    if metric_name == METRIC_CENTERING_OFFSET:
        return math.hypot(
            float(fingerprint.centering_offset_x),
            float(fingerprint.centering_offset_y),
        )
    if metric_name == METRIC_CIRCULARITY_COVERAGE:
        return float(fingerprint.circularity_coverage_percent)
    if metric_name == METRIC_OUTER_DEADZONE_SPAN:
        return float(
            fingerprint.outer_deadzone_max_axis - fingerprint.outer_deadzone_min_axis
        )
    if metric_name == METRIC_ASYMMETRY_SCORE:
        return float(fingerprint.asymmetry_score)
    if metric_name == METRIC_BITNESS:
        return float(fingerprint.bitness_observed)
    if metric_name == METRIC_TREMOR:
        return float(fingerprint.tremor_metric)
    if metric_name == METRIC_LINEARITY:
        return float(fingerprint.linearity_score)
    raise ValueError(f"unknown metric_name: {metric_name!r}")


def compute_metric_trend(
    fingerprints: Sequence[ModuleFingerprint],
    metric_name: str,
) -> MetricTrend:
    """Regress one metric across the (filtered) fingerprint history.

    The caller is responsible for passing the FULL fingerprint tuple;
    this function filters out sub-sample-size runs internally so the
    regression doesn't pick up untrustworthy data points. Returns a
    ``MetricTrend`` with ``confidence=insufficient_data`` and slope/r²
    of zero when the math can't run (too few points, zero day span,
    flat data → undefined correlation).
    """

    if metric_name not in _METRIC_WEAR_DIRECTION:
        raise ValueError(f"unknown metric_name: {metric_name!r}")

    usable = _filter_usable_fingerprints(fingerprints)

    if not usable:
        return _insufficient_trend(metric_name, latest_value=0.0)

    values = [metric_value_for(fp, metric_name) for fp in usable]
    latest_value = values[-1]
    timestamps = [_parse_utc(fp.timestamp_utc) for fp in usable]

    n = len(usable)
    days_span = (timestamps[-1] - timestamps[0]).total_seconds() / 86_400.0

    if n < CONFIDENCE_MIN_SAMPLES or days_span < CONFIDENCE_MIN_DAYS_SPAN:
        return _insufficient_trend(
            metric_name,
            latest_value=latest_value,
            n_samples=n,
            days_span=days_span,
        )

    days_from_first = [
        (ts - timestamps[0]).total_seconds() / 86_400.0 for ts in timestamps
    ]

    try:
        regression = linear_regression(days_from_first, values)
        slope = float(regression.slope)
    except (StatisticsError, ValueError):
        return _insufficient_trend(
            metric_name,
            latest_value=latest_value,
            n_samples=n,
            days_span=days_span,
        )

    try:
        r = correlation(days_from_first, values)
        r_squared = float(r * r)
    except (StatisticsError, ValueError):
        # Constant data is a degenerate input for the Pearson correlation
        # (denominator stdev is zero). Practically the linear fit is
        # perfect — every "residual" is zero — so we report r² = 1.0,
        # high confidence, slope zero. The status falls out to "stable"
        # naturally. This avoids the false-alarm where a perfectly flat
        # passport reads as "insufficient_data".
        if all(v == values[0] for v in values):
            r_squared = 1.0
        else:
            r_squared = 0.0

    confidence = _confidence_label(
        n_samples=n, days_span=days_span, r_squared=r_squared
    )

    direction = _METRIC_WEAR_DIRECTION[metric_name]
    is_degrading = _is_degrading(slope=slope, direction=direction)

    projected = _projected_days_to_threshold(
        metric_name=metric_name,
        latest_value=latest_value,
        slope=slope,
        confidence=confidence,
        is_degrading=is_degrading,
    )

    status = _per_metric_status(
        confidence=confidence,
        is_degrading=is_degrading,
        projected_days_to_threshold=projected,
    )

    return MetricTrend(
        metric_name=metric_name,
        slope_per_day=slope,
        r_squared=r_squared,
        n_samples=n,
        days_span=days_span,
        latest_value=latest_value,
        projected_days_to_threshold=projected,
        confidence=confidence,
        status=status,
        is_degrading=is_degrading,
    )


def summarize_passport_trends(
    passport: ModulePassport,
    *,
    now: Optional[datetime] = None,
) -> PassportTrendSummary:
    """Run :func:`compute_metric_trend` for every metric and roll up.

    ``now`` defaults to ``datetime.now(timezone.utc)``; the parameter is
    injectable for deterministic tests.
    """

    if now is None:
        now = datetime.now(timezone.utc)

    metric_trends = tuple(
        compute_metric_trend(passport.fingerprints, name) for name in METRIC_ORDER
    )

    usable = _filter_usable_fingerprints(passport.fingerprints)
    last_age_days = _last_fingerprint_age_days(usable, now=now)

    attention_metrics = tuple(
        mt.metric_name
        for mt in metric_trends
        if mt.status in (TREND_STATUS_DRIFTING, TREND_STATUS_INVESTIGATE)
    )

    status = _roll_up_status(metric_trends)

    return PassportTrendSummary(
        side=passport.side,
        status=status,
        metric_trends=metric_trends,
        attention_metrics=attention_metrics,
        last_fingerprint_age_days=last_age_days,
        usable_fingerprint_count=len(usable),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _insufficient_trend(
    metric_name: str,
    *,
    latest_value: float = 0.0,
    n_samples: int = 0,
    days_span: float = 0.0,
) -> MetricTrend:
    return MetricTrend(
        metric_name=metric_name,
        slope_per_day=0.0,
        r_squared=0.0,
        n_samples=n_samples,
        days_span=days_span,
        latest_value=latest_value,
        projected_days_to_threshold=None,
        confidence=CONFIDENCE_INSUFFICIENT,
        status=TREND_STATUS_INSUFFICIENT,
        is_degrading=False,
    )


def _parse_utc(timestamp_utc: str) -> datetime:
    """Parse the wrapper's ``YYYY-MM-DDTHH:MM:SSZ`` shape into a UTC datetime."""

    # ``fromisoformat`` accepts ``+00:00`` but not ``Z`` until 3.11; py312 is
    # fine but be defensive across the older format.
    text = timestamp_utc.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _filter_usable_fingerprints(
    fingerprints: Sequence[ModuleFingerprint],
) -> list[ModuleFingerprint]:
    """Drop sub-sample-size runs and runs whose timestamps fail to parse.

    The MVP marks insufficient-sample runs ``wear_observed`` regardless,
    so their metric values aren't reliable trend points. We require the
    same minimum-sample bar that ``classify_fingerprint`` uses.
    """

    threshold = MIN_SAMPLES_FOR_REST + MIN_SAMPLES_FOR_RANGE
    out: list[ModuleFingerprint] = []
    for fp in fingerprints:
        if fp.samples_count < threshold:
            continue
        try:
            _parse_utc(fp.timestamp_utc)
        except ValueError:
            continue
        out.append(fp)
    # Sort by timestamp ascending so regression x-axis is monotonic — the
    # service writes fingerprints in append order, but a corrupted file
    # could end up out of order on disk.
    out.sort(key=lambda fp: _parse_utc(fp.timestamp_utc))
    return out


def _confidence_label(
    *, n_samples: int, days_span: float, r_squared: float
) -> str:
    if n_samples < CONFIDENCE_MIN_SAMPLES or days_span < CONFIDENCE_MIN_DAYS_SPAN:
        return CONFIDENCE_INSUFFICIENT
    if r_squared >= CONFIDENCE_R_SQUARED_HIGH:
        return CONFIDENCE_HIGH
    if r_squared >= CONFIDENCE_R_SQUARED_FLOOR:
        return CONFIDENCE_MODERATE
    return CONFIDENCE_LOW


def _is_degrading(*, slope: float, direction: int) -> bool:
    """Sign-aware degradation check.

    ``direction = +1`` → metric going up is bad (slope > 0 is degrading).
    ``direction = -1`` → metric going down is bad (slope < 0 is degrading).
    Slopes of exactly zero are treated as not degrading — the regression
    didn't find a movement direction so we don't claim one.
    """

    if direction > 0:
        return slope > 0.0
    if direction < 0:
        return slope < 0.0
    return False


def _projected_days_to_threshold(
    *,
    metric_name: str,
    latest_value: float,
    slope: float,
    confidence: str,
    is_degrading: bool,
) -> Optional[float]:
    """Linear extrapolation from latest value to the wear band threshold.

    Returns ``None`` when:

    - confidence is below moderate (we don't extrapolate from low-r² fits)
    - the metric isn't degrading toward wear (no actionable runway)
    - the metric is already past the wear threshold (no "days until" meaning)
    - the slope is zero (would divide by zero)
    """

    if confidence not in (CONFIDENCE_MODERATE, CONFIDENCE_HIGH):
        return None
    if not is_degrading or slope == 0.0:
        return None
    threshold = _METRIC_WEAR_THRESHOLD[metric_name]
    direction = _METRIC_WEAR_DIRECTION[metric_name]
    if direction > 0:
        # Higher = worse: threshold sits above latest_value. Already past?
        if latest_value >= threshold:
            return None
        runway = (threshold - latest_value) / slope
    else:
        # Lower = worse: threshold sits below latest_value. Already past?
        if latest_value <= threshold:
            return None
        runway = (threshold - latest_value) / slope
    if runway < 0:
        return None
    return float(runway)


def _per_metric_status(
    *,
    confidence: str,
    is_degrading: bool,
    projected_days_to_threshold: Optional[float],
) -> str:
    """Bucket a metric's trend into stable / drifting / investigate / insufficient.

    Low-confidence runs (low r² or below the sample/span floor) surface
    as ``insufficient_data`` — never as ``stable``, which would
    manufacture false reassurance about a noisy fit.
    """

    if confidence in (CONFIDENCE_INSUFFICIENT, CONFIDENCE_LOW):
        return TREND_STATUS_INSUFFICIENT
    if not is_degrading:
        return TREND_STATUS_STABLE
    if (
        projected_days_to_threshold is not None
        and projected_days_to_threshold <= INVESTIGATE_DAYS_TO_THRESHOLD
    ):
        return TREND_STATUS_INVESTIGATE
    return TREND_STATUS_DRIFTING


def _roll_up_status(metric_trends: Sequence[MetricTrend]) -> str:
    """Worst-trumps roll-up across per-metric statuses."""

    has_investigate = any(
        mt.status == TREND_STATUS_INVESTIGATE for mt in metric_trends
    )
    if has_investigate:
        return TREND_STATUS_INVESTIGATE
    has_drifting = any(mt.status == TREND_STATUS_DRIFTING for mt in metric_trends)
    if has_drifting:
        return TREND_STATUS_DRIFTING
    has_stable = any(mt.status == TREND_STATUS_STABLE for mt in metric_trends)
    if has_stable:
        return TREND_STATUS_STABLE
    return TREND_STATUS_INSUFFICIENT


def _last_fingerprint_age_days(
    usable: Sequence[ModuleFingerprint],
    *,
    now: datetime,
) -> Optional[float]:
    if not usable:
        return None
    latest = _parse_utc(usable[-1].timestamp_utc)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    delta = now - latest
    return max(0.0, delta.total_seconds() / 86_400.0)


__all__ = [
    "ATTENTION_AGE_DAYS",
    "CONFIDENCE_HIGH",
    "CONFIDENCE_INSUFFICIENT",
    "CONFIDENCE_LABELS",
    "CONFIDENCE_LOW",
    "CONFIDENCE_MIN_DAYS_SPAN",
    "CONFIDENCE_MIN_SAMPLES",
    "CONFIDENCE_MODERATE",
    "CONFIDENCE_R_SQUARED_FLOOR",
    "CONFIDENCE_R_SQUARED_HIGH",
    "INVESTIGATE_DAYS_TO_THRESHOLD",
    "METRIC_ASYMMETRY_SCORE",
    "METRIC_BITNESS",
    "METRIC_CENTERING_OFFSET",
    "METRIC_CIRCULARITY_COVERAGE",
    "METRIC_LINEARITY",
    "METRIC_NOISE_FLOOR",
    "METRIC_ORDER",
    "METRIC_OUTER_DEADZONE_SPAN",
    "METRIC_TREMOR",
    "MetricTrend",
    "PassportTrendSummary",
    "TREND_STATUSES",
    "TREND_STATUS_DRIFTING",
    "TREND_STATUS_INSUFFICIENT",
    "TREND_STATUS_INVESTIGATE",
    "TREND_STATUS_STABLE",
    "compute_metric_trend",
    "metric_value_for",
    "summarize_passport_trends",
]
