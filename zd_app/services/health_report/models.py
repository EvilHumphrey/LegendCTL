"""Dataclasses for Controller Health Report.

All field names follow the Health Report schema's suggested-field-names honest-naming
discipline: prefer ``observed_*`` / ``configured_*`` / ``suggested_*``. Never
``true_*``, ``latency_*``, ``polling_verified_*``, ``defect_*``,
``calibrated_*``, ``hardware_health_*``. The forbidden-field-name test in
``tests/test_health_report_forbidden_phrases.py`` enforces this against the
dataclass attribute names AND the JSON exporter output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Raw sample (one HID-report observation)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sample:
    """One observation of XInput state during a Health Report run.

    ``timestamp_ns`` is ``time.perf_counter_ns()`` at the moment the app
    received the XInput state. ``packet_number`` is XInput's
    ``dwPacketNumber`` (a state-change counter; duplicates indicate no new
    HID report arrived since the last poll). Sticks are signed integer
    percent of full axis travel ([-100, 100]) — the same unit the wrapper's
    Sticks-tab deadzone slider uses — so the suggested-deadzone value the
    user sees in the report lands in the same unit as the UI control.
    Triggers are 0..255 bytes (matches XInput's bLeftTrigger / bRightTrigger
    and the operator's own trigger-travel intuition).
    """

    timestamp_ns: int
    packet_number: int
    left_stick_x: int
    left_stick_y: int
    right_stick_x: int
    right_stick_y: int
    left_trigger: int
    right_trigger: int
    connected: bool = True


# ---------------------------------------------------------------------------
# Quality / confidence labels
# ---------------------------------------------------------------------------


class SampleQuality(str, Enum):
    """How confident we are that a measurement has enough samples.

    Per the measurement contract: "Good sample: duration >= 4.5s and sample count plausibly
    matches expected report cadence band. Limited sample: too few reports or
    inconsistent cadence." Insufficient = effectively no samples.
    """

    GOOD = "good"
    LIMITED = "limited"
    INSUFFICIENT = "insufficient"


class RestNoiseLabel(str, Enum):
    """Stick rest noise band. Per the "Thresholds: initial defaults" rule."""

    GOOD = "good"
    NOTICEABLE = "noticeable"
    HIGH = "high"
    INSUFFICIENT = "insufficient"


class RangeCoverageLabel(str, Enum):
    """Stick range coverage band. Per the "Thresholds: initial defaults" rule."""

    GOOD = "good"
    LIMITED = "limited"
    UNEVEN = "uneven"
    RETEST = "retest"
    INSUFFICIENT = "insufficient"


class TriggerSmoothnessLabel(str, Enum):
    """Trigger smoothness label. Per the honest-naming policy."""

    SMOOTH = "smooth"
    STEPPED = "stepped"
    NOISY = "noisy"
    LIMITED = "limited"
    INSUFFICIENT = "insufficient"


class CadenceQualityLabel(str, Enum):
    """Observed cadence quality band. Per the honest-naming policy."""

    CONSISTENT = "consistent"
    SOME_GAPS = "some_gaps"
    INCONSISTENT = "inconsistent"
    INSUFFICIENT = "insufficient"


class OverallStatus(str, Enum):
    """Overall report status. Avoids 0-100 score per the "Overall report labels" rule."""

    NORMAL = "normal"
    TUNING_SUGGESTED = "tuning_suggested"
    RETEST_RECOMMENDED = "retest_recommended"
    POSSIBLE_ISSUE = "possible_issue"


# ---------------------------------------------------------------------------
# Per-test result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StickRestNoise:
    """One stick's rest-noise summary. Per the measurement contract.

    Coordinates: signed integer percent of full axis travel ([-100, 100]),
    matching the wrapper's deadzone slider unit (see :class:`Sample`).
    ``suggested_deadzone_from_sample`` therefore lands directly in the same
    integer-percent unit the user types into the slider. Computed against
    the median center (robust to brief touches at the start of the rest
    sample).
    """

    sample_count: int
    duration_ms: float
    median_center_x: float
    median_center_y: float
    mean_center_x: float
    mean_center_y: float
    max_abs_x: float
    max_abs_y: float
    max_r: float
    p95_r: float
    p99_r: float
    suggested_deadzone_from_sample: int
    quality: SampleQuality
    label: RestNoiseLabel


@dataclass(frozen=True)
class StickRestNoiseReport:
    """Both sticks' rest-noise summaries."""

    left: StickRestNoise
    right: StickRestNoise


@dataclass(frozen=True)
class StickSectorCoverage:
    """One sector's max-observed radius. 32 sectors per stick by default."""

    sector_index: int      # 0..N-1, 0 == 0 rad (positive X)
    angle_start_rad: float
    angle_end_rad: float
    max_observed_radius: float


@dataclass(frozen=True)
class StickRange:
    """One stick's range/circularity summary. Per the measurement contract.

    No "true circularity" score in v1 (per the measurement contract). Sector coverage and
    weakest sector only.
    """

    sample_count: int
    duration_ms: float
    center_used_x: float
    center_used_y: float
    min_x: int
    max_x: int
    min_y: int
    max_y: int
    cardinal_reach_up: int
    cardinal_reach_down: int
    cardinal_reach_left: int
    cardinal_reach_right: int
    sectors: tuple[StickSectorCoverage, ...]
    sector_coverage_threshold_pct: float
    sector_coverage_pct: float          # share of sectors meeting threshold
    weakest_sector_index: Optional[int]
    weakest_sector_pct_of_max: Optional[float]
    quality: SampleQuality
    label: RangeCoverageLabel


@dataclass(frozen=True)
class StickRangeReport:
    """Both sticks' range/circularity summaries."""

    left: StickRange
    right: StickRange


@dataclass(frozen=True)
class TriggerRange:
    """One trigger's range/smoothness summary. Per the measurement contract."""

    sample_count: int
    duration_ms: float
    observed_min: int
    observed_max: int
    observed_travel: int
    monotonicity_violations: int
    largest_adjacent_delta: int
    quality: SampleQuality
    label: TriggerSmoothnessLabel


@dataclass(frozen=True)
class TriggerRangeReport:
    """Both triggers' range/smoothness summaries."""

    left: TriggerRange
    right: TriggerRange


@dataclass(frozen=True)
class ObservedCadence:
    """Observed input-report cadence summary. Per the measurement contract.

    Consumed by the Readiness Check (:mod:`quick_check`) for its
    green/yellow/red verdict. The full Health Report no longer surfaces a
    cadence section — app-layer Hz can't see the device's true polling rate,
    so that measurement was removed from the report (2026-05-30); the
    computation primitives stay here because the Readiness Check still uses
    them.

    CRITICAL: field names use ``observed_*`` / ``configured_*`` exclusively.
    Never ``polling_*`` (except ``configured_polling_hz`` which is honestly a
    configured setting, not a measured rate) or ``latency_*``. The
    forbidden-field-name test enforces this.
    """

    configured_polling_hz: Optional[int]
    expected_interval_ms: Optional[float]
    sample_count: int
    duration_ms: float
    observed_median_interval_ms: float
    observed_p95_interval_ms: float
    observed_p99_interval_ms: float
    observed_min_interval_ms: float
    observed_max_interval_ms: float
    observed_median_absolute_deviation_ms: float
    # Gap counts. Field names are historical; the actual thresholds are
    # max(ratio × expected_ms, CADENCE_GAP_*_FLOOR_MS) so that healthy 8 kHz
    # streams with sub-ms Windows-scheduling jitter don't over-report gaps.
    # See measurements.CADENCE_GAP_*_FLOOR_MS for the floor rationale.
    gaps_over_2x_expected: int
    gaps_over_4x_expected: int
    gaps_over_10ms: int
    quality: SampleQuality
    label: CadenceQualityLabel


# ---------------------------------------------------------------------------
# Top-level report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceContext:
    """Snapshot of device + config context at report generation time."""

    controller_name: Optional[str]
    configured_polling_hz: Optional[int]
    profile_name: Optional[str]


@dataclass(frozen=True)
class HealthReport:
    """Complete Controller Health Report, ready for export."""

    app_version: str
    app_build_commit: Optional[str]
    generated_at_local: str          # ISO-ish local timestamp string
    device: DeviceContext
    stick_rest_noise: StickRestNoiseReport
    stick_range: StickRangeReport
    trigger_range: TriggerRangeReport
    overall_status: OverallStatus
    caveats: tuple[str, ...] = field(default_factory=tuple)


__all__ = [
    "Sample",
    "SampleQuality",
    "RestNoiseLabel",
    "RangeCoverageLabel",
    "TriggerSmoothnessLabel",
    "CadenceQualityLabel",
    "OverallStatus",
    "StickRestNoise",
    "StickRestNoiseReport",
    "StickSectorCoverage",
    "StickRange",
    "StickRangeReport",
    "TriggerRange",
    "TriggerRangeReport",
    "ObservedCadence",
    "DeviceContext",
    "HealthReport",
]
