"""Readiness Check — quick-mode pre-match ritual for the Health Report pipeline.

A 20-second three-phase pass that samples rest noise, stick range, and trigger
travel back-to-back without READY-gate user-pacing. Returns a single
green / yellow / red verdict suitable for "verify before tonight's queue"
rather than a full diagnostic report.

State machine (no operator-paced gates; sampling runs continuously)::

    IDLE --start--> REST --(REST_DURATION elapses)--> RANGE
                                                       |
                                                       v
                                                       (RANGE_DURATION elapses)
                                                       |
                                                       v
                                                     TRIGGER
                                                       |
                                                       v
                                                       (TRIGGER_DURATION elapses)
                                                       |
                                                       v
                                                    COMPLETE

    cancel() / reset() return to IDLE.

The total sampling budget is ``QUICK_TOTAL_BUDGET_S`` (20.0 s). The service
reuses :class:`SampleCollector` from the full Health Report (same XInput
provider, same Sample model) and the same threshold constants from
:mod:`zd_app.services.health_report.measurements` so verdicts stay
consistent with the full-mode wizard.

Output is a transient :class:`ReadinessVerdict` — no persistence to
``%APPDATA%\\health_reports\\``, no Markdown / JSON exporters.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from zd_app.services.health_report.measurements import (
    CADENCE_GAP_2X_FLOOR_MS,
    REST_NOISE_GOOD_P99_PCT,
    REST_NOISE_NOTICEABLE_P99_PCT,
    STICK_RANGE_GOOD_PCT,
    STICK_RANGE_LIMITED_PCT,
    TRIGGER_LARGE_DELTA_THRESHOLD,
    TRIGGER_NEAR_FULL_MIN_MAX,
    TRIGGER_STEPPED_DELTA_COUNT,
    compute_observed_cadence,
    compute_stick_range,
    compute_stick_rest_noise,
    compute_trigger_range,
    filter_samples_by_time_range,
)
from zd_app.services.health_report.models import (
    CadenceQualityLabel,
    DeviceContext,
    RangeCoverageLabel,
    RestNoiseLabel,
    Sample,
    TriggerSmoothnessLabel,
)
from zd_app.services.health_report.sample_capture import (
    SampleCollector,
    SampleProvider,
)
from zd_app.services.wear_ledger import WearLedgerService
from zd_app.services.wear_ledger.models import READINESS_CHECK


logger = logging.getLogger(__name__)


# Phase durations compress the full-mode 5+10+6 = 21 s wizard into a single
# 5+8+7 = 20 s contiguous pass. Each phase reuses the *same* measurement
# primitives as the full report; the shorter rotation window trades sample
# count for time-to-verdict, and the SampleCollector caps don't change.
REST_DURATION_S = 5.0
RANGE_DURATION_S = 8.0
TRIGGER_DURATION_S = 7.0
QUICK_TOTAL_BUDGET_S = REST_DURATION_S + RANGE_DURATION_S + TRIGGER_DURATION_S

# Verdict budget: the headline is "20s total runtime"; the spec calls a
# regression if the implementation pushes past 30s including overhead.
QUICK_TOTAL_BUDGET_REGRESSION_S = 30.0


class QuickCheckPhase(str, Enum):
    """Sampling phase. Values are stable identifiers (used by i18n)."""

    REST = "rest"
    RANGE = "range"
    TRIGGER = "trigger"


# Phase ordering — the spec calls this out explicitly so tests can lock it.
PHASE_ORDER: tuple[QuickCheckPhase, ...] = (
    QuickCheckPhase.REST,
    QuickCheckPhase.RANGE,
    QuickCheckPhase.TRIGGER,
)

PHASE_DURATIONS_S: dict[QuickCheckPhase, float] = {
    QuickCheckPhase.REST: REST_DURATION_S,
    QuickCheckPhase.RANGE: RANGE_DURATION_S,
    QuickCheckPhase.TRIGGER: TRIGGER_DURATION_S,
}


class QuickCheckState(str, Enum):
    """Lifecycle state for the quick-mode check."""

    IDLE = "idle"
    REST = "rest"
    RANGE = "range"
    TRIGGER = "trigger"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


# Map sampling-phase states (REST / RANGE / TRIGGER) to their phase enum.
_STATE_TO_PHASE: dict[QuickCheckState, QuickCheckPhase] = {
    QuickCheckState.REST: QuickCheckPhase.REST,
    QuickCheckState.RANGE: QuickCheckPhase.RANGE,
    QuickCheckState.TRIGGER: QuickCheckPhase.TRIGGER,
}


class ReadinessStatus(str, Enum):
    """Top-line verdict for the readiness check.

    ``GREEN``  — all three phases clean. "Today this controller is trustworthy."
    ``YELLOW`` — one minor issue (noticeable rest noise, some range gaps,
                 mild trigger limitation, etc.).
    ``RED``    — clear problem (high rest noise, range retest, dead/noisy
                 trigger, inconsistent cadence) OR insufficient samples
                 (treated as RED so the user re-runs rather than trusts a
                 thin sample).
    """

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclass(frozen=True)
class ReadinessVerdict:
    """Output of a single readiness-check run.

    ``status`` is the green/yellow/red headline. ``observations`` is a list of
    short i18n key references (each rendered separately by the screen) — the
    screen turns each key into a one-line bullet. 3-4 bullets is the spec
    target; the verdict logic may emit fewer when no issues are observed.
    """

    status: ReadinessStatus
    observations: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuickCheckProgress:
    """Snapshot of progress, suitable for UI rendering during RUNNING state."""

    state: QuickCheckState
    phase: Optional[QuickCheckPhase]
    elapsed_s: float
    total_budget_s: float
    samples_collected: int

    @property
    def fraction(self) -> float:
        if self.total_budget_s <= 0:
            return 0.0
        return max(0.0, min(1.0, self.elapsed_s / self.total_budget_s))

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.total_budget_s - self.elapsed_s)


# Observation i18n keys. The screen renders each key as a bullet line. The
# verdict logic appends keys conditionally — see ``_classify_*`` helpers
# below.
OBS_REST_CLEAN = "readiness_check.obs.rest_clean"
OBS_REST_NOTICEABLE = "readiness_check.obs.rest_noticeable"
OBS_REST_HIGH = "readiness_check.obs.rest_high"
OBS_RANGE_GOOD = "readiness_check.obs.range_good"
OBS_RANGE_LIMITED = "readiness_check.obs.range_limited"
OBS_RANGE_RETEST = "readiness_check.obs.range_retest"
OBS_TRIGGER_SMOOTH = "readiness_check.obs.trigger_smooth"
OBS_TRIGGER_LIMITED = "readiness_check.obs.trigger_limited"
OBS_TRIGGER_NOISY = "readiness_check.obs.trigger_noisy"
OBS_TRIGGER_STEPPED = "readiness_check.obs.trigger_stepped"
OBS_CADENCE_CONSISTENT = "readiness_check.obs.cadence_consistent"
OBS_CADENCE_SOME_GAPS = "readiness_check.obs.cadence_some_gaps"
OBS_CADENCE_INCONSISTENT = "readiness_check.obs.cadence_inconsistent"
OBS_INSUFFICIENT_SAMPLES = "readiness_check.obs.insufficient_samples"


class QuickCheckService:
    """Owns the readiness-check state machine + the sample collector lifecycle.

    Production wiring (from ``app_shell.py``):

    - ``sample_provider`` = :func:`make_xinput_sample_provider`.
    - ``device_context_provider`` reads device + polling-rate context from
      the live services (only ``configured_polling_hz`` matters for
      cadence-band scoring; controller_name / profile_name are unused here).
    - ``clock`` = ``time.monotonic`` (default).

    Phase durations are *injectable* so tests can shrink the wall-clock
    requirement. Don't lower them in production — the 20 s budget is the
    headline of the feature.
    """

    def __init__(
        self,
        *,
        sample_provider: SampleProvider,
        device_context_provider: Callable[[], DeviceContext],
        clock: Callable[[], float] = time.monotonic,
        phase_durations_s: Optional[dict[QuickCheckPhase, float]] = None,
        wear_ledger: Optional[WearLedgerService] = None,
    ) -> None:
        self._sample_provider = sample_provider
        self._device_context_provider = device_context_provider
        self._clock = clock
        self._phase_durations_s = dict(PHASE_DURATIONS_S)
        if phase_durations_s:
            self._phase_durations_s.update(phase_durations_s)
        self._wear_ledger = wear_ledger

        self._state = QuickCheckState.IDLE
        self._collector: Optional[SampleCollector] = None
        self._run_started_at: Optional[float] = None
        self._phase_started_at: Optional[float] = None
        self._t_zero_ns: Optional[int] = None
        self._t_zero_clock: Optional[float] = None
        self._phase_starts_at_ns: dict[QuickCheckPhase, int] = {}
        self._phase_ends_at_ns: dict[QuickCheckPhase, int] = {}
        self._device_at_start: Optional[DeviceContext] = None
        self._verdict: Optional[ReadinessVerdict] = None

    # ------------------------------------------------------------------
    # Read-only properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> QuickCheckState:
        return self._state

    @property
    def verdict(self) -> Optional[ReadinessVerdict]:
        return self._verdict

    @property
    def total_budget_s(self) -> float:
        return sum(self._phase_durations_s.values())

    def current_phase(self) -> Optional[QuickCheckPhase]:
        return _STATE_TO_PHASE.get(self._state)

    def progress(self) -> QuickCheckProgress:
        budget = self.total_budget_s
        if self._run_started_at is None:
            return QuickCheckProgress(
                state=self._state,
                phase=self.current_phase(),
                elapsed_s=0.0,
                total_budget_s=budget,
                samples_collected=self._samples_collected(),
            )
        elapsed = max(0.0, self._clock() - self._run_started_at)
        return QuickCheckProgress(
            state=self._state,
            phase=self.current_phase(),
            elapsed_s=elapsed,
            total_budget_s=budget,
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
        """IDLE -> REST. Spins up the collector and begins phase 1.

        Unlike the full-mode wizard there's no preface or READY gate — the
        user clicks "Run readiness check" and sampling starts immediately.
        """

        if self._state != QuickCheckState.IDLE:
            raise RuntimeError(f"start() requires IDLE state; was {self._state.value}")
        self._device_at_start = self._device_context_provider()
        self._collector = SampleCollector(self._sample_provider)
        self._collector.start()
        self._run_started_at = self._clock()
        self._t_zero_clock = self._run_started_at
        self._t_zero_ns = time.perf_counter_ns()
        self._verdict = None
        self._phase_starts_at_ns = {}
        self._phase_ends_at_ns = {}
        self._start_phase(QuickCheckState.REST)

    def tick(self) -> None:
        """Advance the state machine if the current phase's duration elapsed.

        Safe to call from a UI frame callback at any cadence; states without
        a duration (IDLE / COMPLETE / CANCELLED) are no-ops.
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
        if self._state == QuickCheckState.REST:
            self._start_phase(QuickCheckState.RANGE)
        elif self._state == QuickCheckState.RANGE:
            self._start_phase(QuickCheckState.TRIGGER)
        elif self._state == QuickCheckState.TRIGGER:
            self._finalize()

    def cancel(self) -> None:
        """Stop sampling and put the service in CANCELLED."""

        if self._collector is not None and self._collector.running:
            try:
                self._collector.stop()
            except Exception:  # noqa: BLE001 — never propagate from cancel
                logger.exception("SampleCollector.stop() failed during cancel")
        self._collector = None
        self._phase_started_at = None
        self._run_started_at = None
        self._state = QuickCheckState.CANCELLED

    def reset(self) -> None:
        """Return to IDLE so the user can run the check again."""

        if self._collector is not None and self._collector.running:
            try:
                self._collector.stop()
            except Exception:  # noqa: BLE001
                logger.exception("SampleCollector.stop() failed during reset")
        self._collector = None
        self._state = QuickCheckState.IDLE
        self._run_started_at = None
        self._phase_started_at = None
        self._phase_starts_at_ns = {}
        self._phase_ends_at_ns = {}
        self._t_zero_ns = None
        self._t_zero_clock = None
        self._device_at_start = None
        self._verdict = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _start_phase(self, state: QuickCheckState) -> None:
        self._state = state
        self._phase_started_at = self._clock()
        phase = _STATE_TO_PHASE[state]
        if self._t_zero_clock is not None and self._t_zero_ns is not None:
            offset_s = self._phase_started_at - self._t_zero_clock
            offset_ns = int(offset_s * 1_000_000_000)
            self._phase_starts_at_ns[phase] = self._t_zero_ns + offset_ns
        else:
            self._phase_starts_at_ns[phase] = time.perf_counter_ns()

    def _end_current_phase(self) -> None:
        if self._state not in _STATE_TO_PHASE:
            return
        if self._t_zero_clock is None or self._t_zero_ns is None:
            return
        phase = _STATE_TO_PHASE[self._state]
        offset_s = self._clock() - self._t_zero_clock
        offset_ns = int(offset_s * 1_000_000_000)
        self._phase_ends_at_ns[phase] = self._t_zero_ns + offset_ns

    def _finalize(self) -> None:
        if self._collector is None:
            self._state = QuickCheckState.COMPLETE
            return
        try:
            samples = self._collector.stop()
        except Exception:  # noqa: BLE001
            logger.exception("SampleCollector.stop() failed during finalize")
            samples = []
        self._collector = None
        self._state = QuickCheckState.COMPLETE
        finalized_at = self._clock()
        self._phase_started_at = None
        self._verdict = self._build_verdict(samples)
        if self._wear_ledger is not None and self._verdict is not None:
            status_value = self._verdict.status.value
            duration_ms = (
                int(max(0.0, finalized_at - self._run_started_at) * 1000)
                if self._run_started_at is not None
                else 0
            )
            self._wear_ledger.append(
                READINESS_CHECK,
                summary=f"Readiness check: {status_value}",
                details={
                    "status": status_value,
                    "observations": list(self._verdict.observations),
                    "duration_ms": duration_ms,
                },
            )

    def _build_verdict(self, samples: list[Sample]) -> ReadinessVerdict:
        rest_samples = self._slice_samples(samples, QuickCheckPhase.REST)
        range_samples = self._slice_samples(samples, QuickCheckPhase.RANGE)
        trigger_samples = self._slice_samples(samples, QuickCheckPhase.TRIGGER)

        return classify_verdict(
            rest_samples=rest_samples,
            range_samples=range_samples,
            trigger_samples=trigger_samples,
            all_samples=samples,
            configured_polling_hz=(
                self._device_at_start.configured_polling_hz
                if self._device_at_start is not None
                else None
            ),
            phase_boundaries_ns=self._phase_window_pairs(),
        )

    def _slice_samples(
        self,
        samples: list[Sample],
        phase: QuickCheckPhase,
    ) -> list[Sample]:
        start_ns = self._phase_starts_at_ns.get(phase)
        end_ns = self._phase_ends_at_ns.get(phase)
        if start_ns is None or end_ns is None:
            return []
        return filter_samples_by_time_range(samples, start_ns=start_ns, end_ns=end_ns)

    def _phase_window_pairs(self) -> list[tuple[int, int]]:
        pairs: list[tuple[int, int]] = []
        for phase in PHASE_ORDER:
            start_ns = self._phase_starts_at_ns.get(phase)
            end_ns = self._phase_ends_at_ns.get(phase)
            if start_ns is not None and end_ns is not None:
                pairs.append((start_ns, end_ns))
        return pairs


# ---------------------------------------------------------------------------
# Pure-function verdict logic — exposed for direct test usage
# ---------------------------------------------------------------------------


def classify_verdict(
    *,
    rest_samples: list[Sample],
    range_samples: list[Sample],
    trigger_samples: list[Sample],
    all_samples: Optional[list[Sample]] = None,
    configured_polling_hz: Optional[int] = None,
    phase_boundaries_ns: Optional[list[tuple[int, int]]] = None,
) -> ReadinessVerdict:
    """Score the three phases and reduce to a single readiness verdict.

    Reuses :func:`compute_stick_rest_noise`, :func:`compute_stick_range`,
    :func:`compute_trigger_range`, :func:`compute_observed_cadence` from the
    full Health Report so verdict labels stay consistent.

    The reduction rule:

    - Any phase short on samples -> ``RED`` + an ``insufficient_samples``
      observation (we prefer the user re-runs over trusting a thin sample).
    - Any *hard* issue (HIGH rest noise, RETEST range, NOISY trigger,
      INCONSISTENT cadence) -> ``RED``.
    - Otherwise, any *minor* issue (NOTICEABLE rest, LIMITED range/trigger,
      STEPPED trigger, SOME_GAPS cadence) -> ``YELLOW``.
    - All three phases clean -> ``GREEN``.
    """

    rest_left = compute_stick_rest_noise(rest_samples, side="left")
    rest_right = compute_stick_rest_noise(rest_samples, side="right")
    range_left = compute_stick_range(
        range_samples,
        side="left",
        center_xy=(rest_left.median_center_x, rest_left.median_center_y),
    )
    range_right = compute_stick_range(
        range_samples,
        side="right",
        center_xy=(rest_right.median_center_x, rest_right.median_center_y),
    )
    trig_left = compute_trigger_range(trigger_samples, side="left")
    trig_right = compute_trigger_range(trigger_samples, side="right")

    cadence_input = all_samples if all_samples is not None else (
        rest_samples + range_samples + trigger_samples
    )
    cadence = compute_observed_cadence(
        cadence_input,
        configured_polling_hz=configured_polling_hz,
        step_boundaries_ns=phase_boundaries_ns if phase_boundaries_ns else None,
    )

    observations: list[str] = []
    has_red = False
    has_yellow = False

    # Rest noise — worst of L/R drives the band.
    rest_band = _worst_rest_label(rest_left.label, rest_right.label)
    if rest_band == RestNoiseLabel.INSUFFICIENT:
        observations.append(OBS_INSUFFICIENT_SAMPLES)
        has_red = True
    elif rest_band == RestNoiseLabel.HIGH:
        observations.append(OBS_REST_HIGH)
        has_red = True
    elif rest_band == RestNoiseLabel.NOTICEABLE:
        observations.append(OBS_REST_NOTICEABLE)
        has_yellow = True
    else:
        observations.append(OBS_REST_CLEAN)

    # Range coverage — worst of L/R drives the band.
    range_band = _worst_range_label(range_left.label, range_right.label)
    if range_band == RangeCoverageLabel.INSUFFICIENT:
        # Already emitted insufficient observation under rest? Avoid dupes.
        if OBS_INSUFFICIENT_SAMPLES not in observations:
            observations.append(OBS_INSUFFICIENT_SAMPLES)
        has_red = True
    elif range_band == RangeCoverageLabel.RETEST or range_band == RangeCoverageLabel.UNEVEN:
        observations.append(OBS_RANGE_RETEST)
        has_red = True
    elif range_band == RangeCoverageLabel.LIMITED:
        observations.append(OBS_RANGE_LIMITED)
        has_yellow = True
    else:
        observations.append(OBS_RANGE_GOOD)

    # Trigger smoothness — worst of L/R drives the band.
    trigger_band = _worst_trigger_label(trig_left.label, trig_right.label)
    if trigger_band == TriggerSmoothnessLabel.INSUFFICIENT:
        if OBS_INSUFFICIENT_SAMPLES not in observations:
            observations.append(OBS_INSUFFICIENT_SAMPLES)
        has_red = True
    elif trigger_band == TriggerSmoothnessLabel.NOISY:
        observations.append(OBS_TRIGGER_NOISY)
        has_red = True
    elif trigger_band == TriggerSmoothnessLabel.STEPPED:
        observations.append(OBS_TRIGGER_STEPPED)
        has_yellow = True
    elif trigger_band == TriggerSmoothnessLabel.LIMITED:
        observations.append(OBS_TRIGGER_LIMITED)
        has_yellow = True
    else:
        observations.append(OBS_TRIGGER_SMOOTH)

    # Cadence is optional context (omit observation if INSUFFICIENT — we
    # don't want a separate "insufficient" line for cadence when the other
    # phases were also thin; the screen has limited room for bullets).
    if cadence.label == CadenceQualityLabel.INCONSISTENT:
        observations.append(OBS_CADENCE_INCONSISTENT)
        has_red = True
    elif cadence.label == CadenceQualityLabel.SOME_GAPS:
        observations.append(OBS_CADENCE_SOME_GAPS)
        has_yellow = True
    elif cadence.label == CadenceQualityLabel.CONSISTENT:
        # Only surface "consistent" if there's room for ~4 bullets and we
        # haven't already emitted 4 — keeps the verdict card tight.
        if len(observations) < 4:
            observations.append(OBS_CADENCE_CONSISTENT)
    # INSUFFICIENT cadence with no observation suppression: skipped above.

    if has_red:
        status = ReadinessStatus.RED
    elif has_yellow:
        status = ReadinessStatus.YELLOW
    else:
        status = ReadinessStatus.GREEN

    return ReadinessVerdict(status=status, observations=tuple(observations))


# ---------------------------------------------------------------------------
# Label-reduction helpers
# ---------------------------------------------------------------------------


def _worst_rest_label(left: RestNoiseLabel, right: RestNoiseLabel) -> RestNoiseLabel:
    rank = {
        RestNoiseLabel.GOOD: 0,
        RestNoiseLabel.NOTICEABLE: 1,
        RestNoiseLabel.HIGH: 2,
        RestNoiseLabel.INSUFFICIENT: 3,
    }
    return left if rank[left] >= rank[right] else right


def _worst_range_label(
    left: RangeCoverageLabel, right: RangeCoverageLabel
) -> RangeCoverageLabel:
    rank = {
        RangeCoverageLabel.GOOD: 0,
        RangeCoverageLabel.LIMITED: 1,
        RangeCoverageLabel.UNEVEN: 2,
        RangeCoverageLabel.RETEST: 3,
        RangeCoverageLabel.INSUFFICIENT: 4,
    }
    return left if rank[left] >= rank[right] else right


def _worst_trigger_label(
    left: TriggerSmoothnessLabel, right: TriggerSmoothnessLabel
) -> TriggerSmoothnessLabel:
    rank = {
        TriggerSmoothnessLabel.SMOOTH: 0,
        TriggerSmoothnessLabel.LIMITED: 1,
        TriggerSmoothnessLabel.STEPPED: 2,
        TriggerSmoothnessLabel.NOISY: 3,
        TriggerSmoothnessLabel.INSUFFICIENT: 4,
    }
    return left if rank[left] >= rank[right] else right


__all__ = [
    "OBS_CADENCE_CONSISTENT",
    "OBS_CADENCE_INCONSISTENT",
    "OBS_CADENCE_SOME_GAPS",
    "OBS_INSUFFICIENT_SAMPLES",
    "OBS_RANGE_GOOD",
    "OBS_RANGE_LIMITED",
    "OBS_RANGE_RETEST",
    "OBS_REST_CLEAN",
    "OBS_REST_HIGH",
    "OBS_REST_NOTICEABLE",
    "OBS_TRIGGER_LIMITED",
    "OBS_TRIGGER_NOISY",
    "OBS_TRIGGER_SMOOTH",
    "OBS_TRIGGER_STEPPED",
    "PHASE_DURATIONS_S",
    "PHASE_ORDER",
    "QUICK_TOTAL_BUDGET_REGRESSION_S",
    "QUICK_TOTAL_BUDGET_S",
    "RANGE_DURATION_S",
    "REST_DURATION_S",
    "TRIGGER_DURATION_S",
    "QuickCheckPhase",
    "QuickCheckProgress",
    "QuickCheckService",
    "QuickCheckState",
    "ReadinessStatus",
    "ReadinessVerdict",
    "classify_verdict",
]
