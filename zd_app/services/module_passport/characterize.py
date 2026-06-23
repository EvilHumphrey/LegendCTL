"""Characterization runner for Module Passport.

A 60-second per-side pass with three back-to-back phases:

- ``REST`` (15s) — operator leaves the stick centered. Source for
  noise_floor_percent, centering_offset_x/y, tremor_metric.
- ``ROTATION`` (30s) — operator traces the outer edge. Source for
  circularity_coverage_percent, outer_deadzone_min/max_axis,
  asymmetry_score, linearity_score.
- ``SETTLE`` (15s) — operator releases the stick. Source for
  bitness_observed (discrete-step count during return-to-rest).

Reuses :class:`zd_app.services.health_report.sample_capture.SampleCollector`
and the per-sample percent unit ([-100, 100]) so the metrics ride the same
calibration as the Health Report family. Existing measurement primitives
(``compute_stick_rest_noise``, ``compute_stick_range``) are reused where
applicable; the four "new" metrics (asymmetry, bitness, tremor, linearity)
are local pure functions exposed for direct test usage.

Threshold tuning lives at the bottom of this file. The verdict reduction
follows the spec's heuristic: any metric in the "wear_observed" band ->
``wear_observed`` overall; otherwise one or more "watch" bands ->
``watch``; otherwise ``good``.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Sequence

from zd_app.services.health_report.measurements import (
    MIN_SAMPLES_FOR_RANGE,
    MIN_SAMPLES_FOR_REST,
    STICK_RANGE_COVERAGE_THRESHOLD_PCT,
    STICK_RANGE_SECTORS_DEFAULT,
    compute_stick_range,
    compute_stick_rest_noise,
    filter_samples_by_time_range,
)
from zd_app.services.health_report.models import Sample
from zd_app.services.health_report.sample_capture import (
    SampleCollector,
    SampleProvider,
)
from zd_app.storage.module_passport_models import (
    ModuleFingerprint,
    SIDES,
    STATUS_GOOD,
    STATUS_WATCH,
    STATUS_WEAR_OBSERVED,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase definitions
# ---------------------------------------------------------------------------


REST_DURATION_S = 15.0
ROTATION_DURATION_S = 30.0
SETTLE_DURATION_S = 15.0
CHARACTERIZATION_TOTAL_S = REST_DURATION_S + ROTATION_DURATION_S + SETTLE_DURATION_S


class CharacterizationPhase(str, Enum):
    """Sampling phase. Values are stable identifiers (used by i18n)."""

    REST = "rest"
    ROTATION = "rotation"
    SETTLE = "settle"


PHASE_ORDER: tuple[CharacterizationPhase, ...] = (
    CharacterizationPhase.REST,
    CharacterizationPhase.ROTATION,
    CharacterizationPhase.SETTLE,
)


PHASE_DURATIONS_S: dict[CharacterizationPhase, float] = {
    CharacterizationPhase.REST: REST_DURATION_S,
    CharacterizationPhase.ROTATION: ROTATION_DURATION_S,
    CharacterizationPhase.SETTLE: SETTLE_DURATION_S,
}


# ---------------------------------------------------------------------------
# Thresholds (operator-tunable; values match the spec's "good/watch/wear" bands)
# ---------------------------------------------------------------------------


# Noise floor (percent of full travel). Same units as the wrapper deadzone
# slider; values pulled from the Health Report's rest-noise band starting
# points so a controller flagged "noticeable" rest noise in the Health
# Report also lands in "watch" here.
NOISE_FLOOR_GOOD_PCT = 2.0
NOISE_FLOOR_WATCH_PCT = 6.0

# Centering offset. A centered stick should sit near (0, 0); 3% off is
# noticeable when shooting in games, 6%+ is wear-territory.
CENTERING_OFFSET_GOOD_PCT = 3.0
CENTERING_OFFSET_WATCH_PCT = 6.0

# Circularity coverage (fraction of sectors above threshold radius).
# Mirrors the Health Report's STICK_RANGE_GOOD_PCT / LIMITED_PCT split.
CIRCULARITY_COVERAGE_GOOD = 0.95
CIRCULARITY_COVERAGE_WATCH = 0.80

# Outer-deadzone window. The expectation is that at full travel the
# observed |axis| stays near the cap (100 in percent units). A min that
# drops well below the max means the stick can't reach full travel on
# parts of the rotation (worn corner).
OUTER_DEADZONE_GOOD_SPAN = 8.0   # max - min <= 8 percent points
OUTER_DEADZONE_WATCH_SPAN = 16.0

# Asymmetry — variance across 4 quadrants of the rotation samples,
# normalised to the overall mean radius. Anything past a few percent is
# meaningful.
ASYMMETRY_GOOD = 0.10
ASYMMETRY_WATCH = 0.25

# Bitness — distinct integer (x, y) tuples observed in the SETTLE trace.
# A smooth analog stick produces many distinct samples in the return-to-
# rest curve; a quantized / worn stick steps through a small set.
BITNESS_GOOD = 30
BITNESS_WATCH = 12

# Tremor — high-frequency jitter proxy at rest. Computed as the mean
# absolute first difference of the radial position; expressed in percent
# points per sample. Healthy modules are well under 0.5 / sample.
TREMOR_GOOD = 0.5
TREMOR_WATCH = 1.5

# Linearity — std-dev of rotation-trace radius divided by mean radius. A
# clean outer-rim rotation traces near-constant radius; deviations show as
# higher relative variability.
LINEARITY_GOOD = 0.10
LINEARITY_WATCH = 0.20


# ---------------------------------------------------------------------------
# Progress dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CharacterizationProgress:
    """UI-rendering snapshot. Mirrors :class:`QuickCheckProgress`."""

    phase: Optional[CharacterizationPhase]
    phase_elapsed_s: float
    phase_duration_s: float
    overall_elapsed_s: float
    overall_total_s: float
    samples_collected: int

    @property
    def phase_fraction(self) -> float:
        if self.phase_duration_s <= 0:
            return 0.0
        return max(0.0, min(1.0, self.phase_elapsed_s / self.phase_duration_s))

    @property
    def overall_fraction(self) -> float:
        if self.overall_total_s <= 0:
            return 0.0
        return max(0.0, min(1.0, self.overall_elapsed_s / self.overall_total_s))

    @property
    def overall_remaining_s(self) -> float:
        return max(0.0, self.overall_total_s - self.overall_elapsed_s)


# ---------------------------------------------------------------------------
# Metric computation — pure functions exposed for direct test usage
# ---------------------------------------------------------------------------


def compute_asymmetry_score(samples: Sequence[Sample], *, side: str) -> float:
    """Variance of per-quadrant max-radius around the overall mean.

    Returns the standard deviation of the 4 quadrants' max radii divided
    by the overall mean radius. 0 means perfectly symmetric reach across
    quadrants; larger values mean one or more quadrants under-reach.

    Returns 0.0 when there's no usable signal (no samples / zero mean).
    """

    xs, ys = _stick_xy(samples, side)
    if not xs:
        return 0.0
    radii = [math.hypot(x, y) for x, y in zip(xs, ys)]
    overall_mean = sum(radii) / len(radii) if radii else 0.0
    if overall_mean <= 0.0:
        return 0.0
    quadrant_max: list[float] = [0.0, 0.0, 0.0, 0.0]
    for x, y, r in zip(xs, ys, radii):
        theta = math.atan2(y, x)
        if theta < 0:
            theta += 2.0 * math.pi
        idx = min(3, int(theta / (math.pi / 2.0)))
        if r > quadrant_max[idx]:
            quadrant_max[idx] = r
    if not any(quadrant_max):
        return 0.0
    mean_quadrant = sum(quadrant_max) / 4.0
    variance = sum((m - mean_quadrant) ** 2 for m in quadrant_max) / 4.0
    std_dev = math.sqrt(variance)
    return std_dev / overall_mean


def compute_bitness_observed(samples: Sequence[Sample], *, side: str) -> int:
    """Number of distinct integer (x, y) tuples observed for ``side``.

    The Sample fields are already integer percent of full travel, so we
    count distinct tuples directly. A smooth stick returning to rest
    sweeps through many values; a quantized / worn stick will step
    through only a few.
    """

    xs, ys = _stick_xy(samples, side)
    if not xs:
        return 0
    return len({(int(x), int(y)) for x, y in zip(xs, ys)})


def compute_tremor_metric(samples: Sequence[Sample], *, side: str) -> float:
    """Mean absolute first-difference of radial position at rest.

    Higher = more high-frequency jitter (i.e. the stick is twitching
    around the centre faster). Expressed in percent points per sample so
    it's roughly comparable across different sample cadences.

    Returns 0.0 when there are fewer than two samples — no diffs to take.
    """

    xs, ys = _stick_xy(samples, side)
    if len(xs) < 2:
        return 0.0
    radii = [math.hypot(x, y) for x, y in zip(xs, ys)]
    diffs = [abs(radii[i] - radii[i - 1]) for i in range(1, len(radii))]
    if not diffs:
        return 0.0
    return sum(diffs) / len(diffs)


def compute_linearity_score(samples: Sequence[Sample], *, side: str) -> float:
    """Std-dev of rotation-trace radius divided by mean radius.

    Operator was supposed to trace the outer rim, so the ideal trace is a
    constant-radius circle. A low value means the trace held a near-
    constant radius; a high value means the stick wobbled in radius
    during the rotation (or didn't actually reach the outer rim
    consistently). Returns 0.0 when there's no signal (no samples /
    near-zero mean radius).
    """

    xs, ys = _stick_xy(samples, side)
    if not xs:
        return 0.0
    radii = [math.hypot(x, y) for x, y in zip(xs, ys)]
    if not radii:
        return 0.0
    mean_r = sum(radii) / len(radii)
    if mean_r <= 0.0:
        return 0.0
    variance = sum((r - mean_r) ** 2 for r in radii) / len(radii)
    return math.sqrt(variance) / mean_r


def compute_outer_deadzone_band(
    samples: Sequence[Sample], *, side: str
) -> tuple[float, float]:
    """Return ``(min_reach, max_reach)`` of per-sector max-radius.

    Bins the rotation samples into 16 angular sectors and records the
    maximum observed radius in each non-empty sector. The pair returned
    is ``(min, max)`` of those sector maxima, so a wide span means at
    least one sector of the outer edge under-reaches relative to the
    rest — the classic worn-corner symptom.

    Sectors with zero samples are ignored (the operator may not have
    swept the whole circle in some runs); the verdict considers
    insufficient-sample runs separately.

    Returns ``(0.0, 0.0)`` when there are no rotation samples.
    """

    xs, ys = _stick_xy(samples, side)
    if not xs:
        return 0.0, 0.0
    sector_count = 16
    sector_max: list[float] = [0.0] * sector_count
    sector_seen: list[bool] = [False] * sector_count
    for x, y in zip(xs, ys):
        r = math.hypot(x, y)
        theta = math.atan2(y, x)
        if theta < 0:
            theta += 2.0 * math.pi
        idx = min(sector_count - 1, int(theta / (2.0 * math.pi) * sector_count))
        sector_seen[idx] = True
        if r > sector_max[idx]:
            sector_max[idx] = r
    reachable = [sector_max[i] for i in range(sector_count) if sector_seen[i]]
    if not reachable:
        return 0.0, 0.0
    return float(min(reachable)), float(max(reachable))


# ---------------------------------------------------------------------------
# Verdict reduction
# ---------------------------------------------------------------------------


def _band_for(value: float, *, good: float, watch: float) -> str:
    """3-band classifier where lower is better."""

    if value <= good:
        return STATUS_GOOD
    if value <= watch:
        return STATUS_WATCH
    return STATUS_WEAR_OBSERVED


def _band_for_higher_is_better(value: float, *, good: float, watch: float) -> str:
    """3-band classifier where higher is better (e.g. circularity, bitness)."""

    if value >= good:
        return STATUS_GOOD
    if value >= watch:
        return STATUS_WATCH
    return STATUS_WEAR_OBSERVED


def _reduce_overall(bands: Sequence[str]) -> str:
    """Reduce per-metric bands to a single overall_status.

    Any "wear_observed" wins. Otherwise any "watch" -> "watch". Otherwise
    everything was "good" -> "good".
    """

    if any(b == STATUS_WEAR_OBSERVED for b in bands):
        return STATUS_WEAR_OBSERVED
    if any(b == STATUS_WATCH for b in bands):
        return STATUS_WATCH
    return STATUS_GOOD


def classify_fingerprint(
    *,
    timestamp_utc: str,
    side: str,
    duration_ms: int,
    rest_samples: Sequence[Sample],
    rotation_samples: Sequence[Sample],
    settle_samples: Sequence[Sample],
) -> ModuleFingerprint:
    """Build a :class:`ModuleFingerprint` from per-phase samples.

    Pure function: takes already-sliced samples for each phase, computes
    the 8 metrics, reduces them to a 3-band ``overall_status``, and
    returns the frozen fingerprint. Tests use this directly with
    synthetic sample arrays.
    """

    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}; got {side!r}")

    rest_total = len(rest_samples)
    rotation_total = len(rotation_samples)
    settle_total = len(settle_samples)
    samples_count = rest_total + rotation_total + settle_total

    # Below the minimum sample bar -> conservative "wear_observed" verdict so
    # the operator re-runs rather than trusts a thin trace. Returns numerics
    # filled out to whatever the metrics produce on the truncated sample.
    insufficient = (
        rest_total < MIN_SAMPLES_FOR_REST
        or rotation_total < MIN_SAMPLES_FOR_RANGE
    )

    rest = compute_stick_rest_noise(rest_samples, side=side)
    rotation_center = (rest.median_center_x, rest.median_center_y)
    rotation = compute_stick_range(
        rotation_samples,
        side=side,
        center_xy=rotation_center,
        sector_count=STICK_RANGE_SECTORS_DEFAULT,
        coverage_threshold_pct=STICK_RANGE_COVERAGE_THRESHOLD_PCT,
    )

    outer_min, outer_max = compute_outer_deadzone_band(rotation_samples, side=side)
    asymmetry = compute_asymmetry_score(rotation_samples, side=side)
    bitness = compute_bitness_observed(settle_samples, side=side)
    tremor = compute_tremor_metric(rest_samples, side=side)
    linearity = compute_linearity_score(rotation_samples, side=side)

    centering_offset = math.hypot(rest.median_center_x, rest.median_center_y)
    outer_span = outer_max - outer_min

    if insufficient:
        overall_status = STATUS_WEAR_OBSERVED
    else:
        bands = [
            _band_for(
                rest.p99_r,
                good=NOISE_FLOOR_GOOD_PCT,
                watch=NOISE_FLOOR_WATCH_PCT,
            ),
            _band_for(
                centering_offset,
                good=CENTERING_OFFSET_GOOD_PCT,
                watch=CENTERING_OFFSET_WATCH_PCT,
            ),
            _band_for_higher_is_better(
                rotation.sector_coverage_pct,
                good=CIRCULARITY_COVERAGE_GOOD,
                watch=CIRCULARITY_COVERAGE_WATCH,
            ),
            _band_for(
                outer_span,
                good=OUTER_DEADZONE_GOOD_SPAN,
                watch=OUTER_DEADZONE_WATCH_SPAN,
            ),
            _band_for(
                asymmetry,
                good=ASYMMETRY_GOOD,
                watch=ASYMMETRY_WATCH,
            ),
            _band_for_higher_is_better(
                float(bitness),
                good=float(BITNESS_GOOD),
                watch=float(BITNESS_WATCH),
            ),
            _band_for(
                tremor,
                good=TREMOR_GOOD,
                watch=TREMOR_WATCH,
            ),
            _band_for(
                linearity,
                good=LINEARITY_GOOD,
                watch=LINEARITY_WATCH,
            ),
        ]
        overall_status = _reduce_overall(bands)

    return ModuleFingerprint(
        timestamp_utc=timestamp_utc,
        side=side,
        duration_ms=int(duration_ms),
        samples_count=int(samples_count),
        noise_floor_percent=float(rest.p99_r),
        centering_offset_x=float(rest.median_center_x),
        centering_offset_y=float(rest.median_center_y),
        circularity_coverage_percent=float(rotation.sector_coverage_pct),
        outer_deadzone_min_axis=float(outer_min),
        outer_deadzone_max_axis=float(outer_max),
        asymmetry_score=float(asymmetry),
        bitness_observed=int(bitness),
        tremor_metric=float(tremor),
        linearity_score=float(linearity),
        overall_status=overall_status,
    )


# ---------------------------------------------------------------------------
# State-machine orchestrator
# ---------------------------------------------------------------------------


class CharacterizationState(str, Enum):
    """Lifecycle state for the characterization run."""

    IDLE = "idle"
    READY_REST = "ready_rest"
    REST = "rest"
    READY_ROTATION = "ready_rotation"
    ROTATION = "rotation"
    READY_SETTLE = "ready_settle"
    SETTLE = "settle"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


_STATE_TO_PHASE: dict[CharacterizationState, CharacterizationPhase] = {
    CharacterizationState.REST: CharacterizationPhase.REST,
    CharacterizationState.ROTATION: CharacterizationPhase.ROTATION,
    CharacterizationState.SETTLE: CharacterizationPhase.SETTLE,
}


_READY_TO_NEXT_STATE: dict[CharacterizationState, CharacterizationState] = {
    CharacterizationState.READY_REST: CharacterizationState.REST,
    CharacterizationState.READY_ROTATION: CharacterizationState.ROTATION,
    CharacterizationState.READY_SETTLE: CharacterizationState.SETTLE,
}


class CharacterizationOrchestrator:
    """State machine that drives the SampleCollector across the 3 phases.

    Mirrors :class:`zd_app.services.health_report.HealthReportService` and
    its READY-gate pattern: the UI calls :meth:`start` to enter
    ``READY_REST``, then :meth:`proceed_to_step` to begin each sampling
    phase. :meth:`tick` from a frame callback advances time within an
    active sampling phase; READY gates wait for explicit user action.

    On COMPLETE, :attr:`fingerprint` carries the classified result. The
    UI then calls :meth:`commit_to_service` to persist it through the
    :class:`ModulePassportService`.
    """

    def __init__(
        self,
        *,
        side: str,
        sample_provider: SampleProvider,
        clock: Callable[[], float] = time.monotonic,
        phase_durations_s: Optional[dict[CharacterizationPhase, float]] = None,
        utc_now_iso: Callable[[], str] = lambda: time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        ),
    ) -> None:
        if side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}; got {side!r}")
        self._side = side
        self._sample_provider = sample_provider
        self._clock = clock
        self._phase_durations_s = dict(PHASE_DURATIONS_S)
        if phase_durations_s:
            self._phase_durations_s.update(phase_durations_s)
        self._utc_now_iso = utc_now_iso

        self._state = CharacterizationState.IDLE
        self._collector: Optional[SampleCollector] = None
        self._phase_started_at: Optional[float] = None
        self._t_zero_ns: Optional[int] = None
        self._t_zero_clock: Optional[float] = None
        self._run_started_at: Optional[float] = None
        self._phase_starts_at_ns: dict[CharacterizationPhase, int] = {}
        self._phase_ends_at_ns: dict[CharacterizationPhase, int] = {}
        self._fingerprint: Optional[ModuleFingerprint] = None
        self._final_duration_ms: int = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def side(self) -> str:
        return self._side

    @property
    def state(self) -> CharacterizationState:
        return self._state

    @property
    def fingerprint(self) -> Optional[ModuleFingerprint]:
        return self._fingerprint

    @property
    def total_budget_s(self) -> float:
        return sum(self._phase_durations_s.values())

    def current_phase(self) -> Optional[CharacterizationPhase]:
        return _STATE_TO_PHASE.get(self._state)

    def progress(self) -> CharacterizationProgress:
        budget = self.total_budget_s
        if self._state == CharacterizationState.COMPLETE:
            return CharacterizationProgress(
                phase=None,
                phase_elapsed_s=0.0,
                phase_duration_s=0.0,
                overall_elapsed_s=budget,
                overall_total_s=budget,
                samples_collected=self._samples_collected(),
            )
        phase = self.current_phase()
        if phase is None or self._phase_started_at is None:
            overall_elapsed = (
                max(0.0, self._clock() - self._run_started_at)
                if self._run_started_at is not None
                else 0.0
            )
            return CharacterizationProgress(
                phase=phase,
                phase_elapsed_s=0.0,
                phase_duration_s=self._phase_durations_s.get(phase, 0.0) if phase else 0.0,
                overall_elapsed_s=overall_elapsed,
                overall_total_s=budget,
                samples_collected=self._samples_collected(),
            )
        phase_elapsed = max(0.0, self._clock() - self._phase_started_at)
        phase_duration = self._phase_durations_s[phase]
        overall_elapsed = (
            max(0.0, self._clock() - self._run_started_at)
            if self._run_started_at is not None
            else 0.0
        )
        return CharacterizationProgress(
            phase=phase,
            phase_elapsed_s=phase_elapsed,
            phase_duration_s=phase_duration,
            overall_elapsed_s=overall_elapsed,
            overall_total_s=budget,
            samples_collected=self._samples_collected(),
        )

    def _samples_collected(self) -> int:
        if self._collector is None:
            return 0
        return len(self._collector.snapshot_samples())

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def start(self) -> None:
        """IDLE -> READY_REST. Spins up the collector; sampling begins on
        :meth:`proceed_to_step`.
        """

        if self._state != CharacterizationState.IDLE:
            raise RuntimeError(
                f"start() requires IDLE state; was {self._state.value}"
            )
        self._collector = SampleCollector(self._sample_provider)
        self._collector.start()
        # Pause appends immediately — only sampling phases collect samples.
        self._collector.pause()
        self._t_zero_clock = self._clock()
        self._t_zero_ns = time.perf_counter_ns()
        self._run_started_at = self._t_zero_clock
        self._phase_starts_at_ns = {}
        self._phase_ends_at_ns = {}
        self._fingerprint = None
        self._state = CharacterizationState.READY_REST

    def proceed_to_step(self) -> None:
        """Advance from a READY_* gate into its sampling phase."""

        if self._state not in _READY_TO_NEXT_STATE:
            raise RuntimeError(
                f"proceed_to_step() requires a READY_* state; was {self._state.value}"
            )
        next_state = _READY_TO_NEXT_STATE[self._state]
        if self._collector is not None and self._collector.running:
            try:
                self._collector.resume()
            except Exception:  # noqa: BLE001 — never crash the wizard
                logger.exception(
                    "ModulePassport: collector.resume() failed during proceed_to_step"
                )
        self._start_phase(next_state)

    def tick(self) -> None:
        """Advance the state machine if the current phase's duration elapsed.

        No-op when the state isn't an active sampling phase, mirroring the
        Health Report pattern.
        """

        if self._state not in _STATE_TO_PHASE:
            return
        if self._phase_started_at is None:
            return
        phase = _STATE_TO_PHASE[self._state]
        elapsed = self._clock() - self._phase_started_at
        if elapsed < self._phase_durations_s[phase]:
            return
        self._end_current_phase()
        if self._state == CharacterizationState.REST:
            self._enter_ready_pause(CharacterizationState.READY_ROTATION)
        elif self._state == CharacterizationState.ROTATION:
            self._enter_ready_pause(CharacterizationState.READY_SETTLE)
        elif self._state == CharacterizationState.SETTLE:
            self._finalize()

    def cancel(self) -> None:
        """Stop sampling and transition to CANCELLED."""

        if self._collector is not None and self._collector.running:
            try:
                self._collector.stop()
            except Exception:  # noqa: BLE001
                logger.exception("ModulePassport: collector.stop() failed in cancel")
        self._collector = None
        self._phase_started_at = None
        self._state = CharacterizationState.CANCELLED

    def reset(self) -> None:
        """Return to IDLE so a new characterization can be run."""

        if self._collector is not None and self._collector.running:
            try:
                self._collector.stop()
            except Exception:  # noqa: BLE001
                logger.exception("ModulePassport: collector.stop() failed in reset")
        self._collector = None
        self._state = CharacterizationState.IDLE
        self._phase_started_at = None
        self._run_started_at = None
        self._phase_starts_at_ns = {}
        self._phase_ends_at_ns = {}
        self._t_zero_ns = None
        self._t_zero_clock = None
        self._fingerprint = None
        self._final_duration_ms = 0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _start_phase(self, state: CharacterizationState) -> None:
        self._state = state
        self._phase_started_at = self._clock()
        phase = _STATE_TO_PHASE[state]
        if self._t_zero_clock is not None and self._t_zero_ns is not None:
            offset_s = self._phase_started_at - self._t_zero_clock
            offset_ns = int(offset_s * 1_000_000_000)
            self._phase_starts_at_ns[phase] = self._t_zero_ns + offset_ns
        else:
            self._phase_starts_at_ns[phase] = time.perf_counter_ns()

    def _enter_ready_pause(self, ready_state: CharacterizationState) -> None:
        self._state = ready_state
        self._phase_started_at = None
        if self._collector is not None and self._collector.running:
            try:
                self._collector.pause()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "ModulePassport: collector.pause() failed in ready pause"
                )

    def _end_current_phase(self) -> None:
        if self._t_zero_clock is None or self._t_zero_ns is None:
            return
        if self._state not in _STATE_TO_PHASE:
            return
        phase = _STATE_TO_PHASE[self._state]
        offset_s = self._clock() - self._t_zero_clock
        offset_ns = int(offset_s * 1_000_000_000)
        self._phase_ends_at_ns[phase] = self._t_zero_ns + offset_ns

    def _finalize(self) -> None:
        if self._collector is None:
            self._state = CharacterizationState.COMPLETE
            return
        try:
            samples = self._collector.stop()
        except Exception:  # noqa: BLE001
            logger.exception("ModulePassport: collector.stop() failed in finalize")
            samples = []
        finalize_clock = self._clock()
        self._collector = None
        self._state = CharacterizationState.COMPLETE
        self._phase_started_at = None
        rest_samples = self._slice_samples(samples, CharacterizationPhase.REST)
        rotation_samples = self._slice_samples(samples, CharacterizationPhase.ROTATION)
        settle_samples = self._slice_samples(samples, CharacterizationPhase.SETTLE)
        duration_ms = 0
        if self._run_started_at is not None:
            duration_ms = int(max(0.0, finalize_clock - self._run_started_at) * 1000)
        self._final_duration_ms = duration_ms
        self._fingerprint = classify_fingerprint(
            timestamp_utc=self._utc_now_iso(),
            side=self._side,
            duration_ms=duration_ms,
            rest_samples=rest_samples,
            rotation_samples=rotation_samples,
            settle_samples=settle_samples,
        )

    def _slice_samples(
        self,
        samples: list[Sample],
        phase: CharacterizationPhase,
    ) -> list[Sample]:
        start_ns = self._phase_starts_at_ns.get(phase)
        end_ns = self._phase_ends_at_ns.get(phase)
        if start_ns is None or end_ns is None:
            return []
        return filter_samples_by_time_range(samples, start_ns=start_ns, end_ns=end_ns)


# ---------------------------------------------------------------------------
# Sample accessors
# ---------------------------------------------------------------------------


def _stick_xy(samples: Sequence[Sample], side: str) -> tuple[list[int], list[int]]:
    if side == "left":
        return [s.left_stick_x for s in samples], [s.left_stick_y for s in samples]
    if side == "right":
        return [s.right_stick_x for s in samples], [s.right_stick_y for s in samples]
    raise ValueError(f"side must be 'left' or 'right'; got {side!r}")


__all__ = [
    "ASYMMETRY_GOOD",
    "ASYMMETRY_WATCH",
    "BITNESS_GOOD",
    "BITNESS_WATCH",
    "CHARACTERIZATION_TOTAL_S",
    "CENTERING_OFFSET_GOOD_PCT",
    "CENTERING_OFFSET_WATCH_PCT",
    "CIRCULARITY_COVERAGE_GOOD",
    "CIRCULARITY_COVERAGE_WATCH",
    "CharacterizationOrchestrator",
    "CharacterizationPhase",
    "CharacterizationProgress",
    "CharacterizationState",
    "LINEARITY_GOOD",
    "LINEARITY_WATCH",
    "NOISE_FLOOR_GOOD_PCT",
    "NOISE_FLOOR_WATCH_PCT",
    "OUTER_DEADZONE_GOOD_SPAN",
    "OUTER_DEADZONE_WATCH_SPAN",
    "PHASE_DURATIONS_S",
    "PHASE_ORDER",
    "REST_DURATION_S",
    "ROTATION_DURATION_S",
    "SETTLE_DURATION_S",
    "TREMOR_GOOD",
    "TREMOR_WATCH",
    "classify_fingerprint",
    "compute_asymmetry_score",
    "compute_bitness_observed",
    "compute_linearity_score",
    "compute_outer_deadzone_band",
    "compute_tremor_metric",
]
