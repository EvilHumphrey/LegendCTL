"""Orchestrator service for the Controller Health Report wizard.

State machine::

    IDLE --begin--> PREFACE --start_test--> READY_REST
                                                |
                                                | proceed_to_step()
                                                v
                                              REST --(time elapses)--> READY_ROTATION
                                                                          |
                                                                          | proceed_to_step()
                                                                          v
                                                                       ROTATION --(time)--> READY_TRIGGER
                                                                                              |
                                                                                              | proceed_to_step()
                                                                                              v
                                                                                            TRIGGER --(time)--> COMPLETE

    cancel() from any state -> CANCELLED
    reset() from any state  -> IDLE

The READY_* states gate each sampling step until the user confirms they're
ready (by design: don't start sampling while the user is still reading
the instructions). The sample collector starts at the first
``proceed_to_step()`` call and runs for the rest of REST→TRIGGER; the
collector keeps running across READY_ROTATION / READY_TRIGGER pauses, but
those samples fall outside any officially-recorded step window so the per-
step measurements only see the user-paced sampling intervals.

``tick()`` is the external advancement mechanism — the UI calls it from a
frame callback and a fake clock drives it in tests. ``tick()`` is a no-op in
READY_* states (they advance only on explicit ``proceed_to_step()``).

No DPG, no I/O. Exporters are pure functions invoked separately by the UI.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from zd_app.services.health_report.measurements import (
    compute_overall_status,
    compute_stick_range,
    compute_stick_rest_noise,
    compute_trigger_range,
    filter_samples_by_time_range,
)
from zd_app.services.health_report.models import (
    DeviceContext,
    HealthReport,
    Sample,
    StickRangeReport,
    StickRestNoiseReport,
    TriggerRangeReport,
)
from zd_app.services.health_report.sample_capture import (
    SampleCollector,
    SampleProvider,
)
from zd_app.services.path_scrub import scrub_value
from zd_app.services.wear_ledger import WearLedgerService
from zd_app.services.wear_ledger.models import HEALTH_REPORT


logger = logging.getLogger(__name__)


# Step durations from the recommended 3-step wizard.
REST_DURATION_S = 5.0
ROTATION_DURATION_S = 10.0
TRIGGER_DURATION_S = 6.0


class HealthReportState(str, Enum):
    """State machine vertex. Values are stable identifiers (used by tests and
    UI i18n keys like ``health_report.state.rest``).
    """

    IDLE = "idle"
    PREFACE = "preface"
    READY_REST = "ready_rest"
    REST = "rest"
    READY_ROTATION = "ready_rotation"
    ROTATION = "rotation"
    READY_TRIGGER = "ready_trigger"
    TRIGGER = "trigger"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


# Map each READY_* gate to the sampling step it precedes.
NEXT_STEP_FOR_READY: dict["HealthReportState", "HealthReportState"] = {}
# Populated below (after the class body) once the enum members exist.


@dataclass(frozen=True)
class StepProgress:
    """Snapshot of the active step's progress, suitable for UI rendering."""

    state: HealthReportState
    elapsed_s: float
    duration_s: float
    samples_collected: int

    @property
    def fraction(self) -> float:
        if self.duration_s <= 0:
            return 0.0
        return max(0.0, min(1.0, self.elapsed_s / self.duration_s))

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.duration_s - self.elapsed_s)


NEXT_STEP_FOR_READY = {
    HealthReportState.READY_REST: HealthReportState.REST,
    HealthReportState.READY_ROTATION: HealthReportState.ROTATION,
    HealthReportState.READY_TRIGGER: HealthReportState.TRIGGER,
}


# Per-step duration table; tests can override the dict to short-circuit
# real-time waits. READY_* states are deliberately absent — they advance
# only on explicit user action.
_DEFAULT_STEP_DURATIONS: dict[HealthReportState, float] = {
    HealthReportState.REST: REST_DURATION_S,
    HealthReportState.ROTATION: ROTATION_DURATION_S,
    HealthReportState.TRIGGER: TRIGGER_DURATION_S,
}


class HealthReportService:
    """Owns the wizard state machine + the sample collector lifecycle.

    Production wiring (from ``app_shell.py``):

    - ``sample_provider`` = :func:`make_xinput_sample_provider`.
    - ``device_context_provider`` reads device + profile name + configured
      polling rate from the existing services.
    - ``clock`` = ``time.monotonic`` (default).
    """

    def __init__(
        self,
        *,
        sample_provider: SampleProvider,
        device_context_provider: Callable[[], DeviceContext],
        clock: Callable[[], float] = time.monotonic,
        step_durations_s: Optional[dict[HealthReportState, float]] = None,
        app_version: str = "",
        app_build_commit: Optional[str] = None,
        local_timestamp_provider: Callable[[], str] = lambda: time.strftime(
            "%Y-%m-%dT%H:%M:%S%z"
        ),
        wear_ledger: Optional[WearLedgerService] = None,
    ) -> None:
        self._sample_provider = sample_provider
        self._device_context_provider = device_context_provider
        self._clock = clock
        self._step_durations_s = dict(_DEFAULT_STEP_DURATIONS)
        if step_durations_s:
            self._step_durations_s.update(step_durations_s)
        self._app_version = app_version
        self._app_build_commit = app_build_commit
        self._local_timestamp_provider = local_timestamp_provider
        self._wear_ledger = wear_ledger

        self._state = HealthReportState.IDLE
        self._collector: Optional[SampleCollector] = None
        # Wall-clock (monotonic) markers around step boundaries.
        self._step_started_at: Optional[float] = None
        self._step_starts_at_ns: dict[HealthReportState, int] = {}
        self._step_ends_at_ns: dict[HealthReportState, int] = {}
        self._t_zero_ns: Optional[int] = None    # perf_counter_ns at run start
        self._t_zero_clock: Optional[float] = None
        self._report: Optional[HealthReport] = None
        self._device_at_start: Optional[DeviceContext] = None
        self._caveats: list[str] = []

    # ------------------------------------------------------------------
    # Read-only properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> HealthReportState:
        return self._state

    @property
    def report(self) -> Optional[HealthReport]:
        return self._report

    @property
    def step_duration_s(self) -> float:
        return self._step_durations_s.get(self._state, 0.0)

    def step_progress(self) -> StepProgress:
        duration = self._step_durations_s.get(self._state, 0.0)
        if self._step_started_at is None or duration <= 0:
            return StepProgress(self._state, 0.0, 0.0, self._samples_collected())
        elapsed = max(0.0, self._clock() - self._step_started_at)
        return StepProgress(self._state, elapsed, duration, self._samples_collected())

    def _samples_collected(self) -> int:
        if self._collector is None:
            return 0
        return len(self._collector.snapshot_samples())

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def begin(self) -> None:
        """IDLE -> PREFACE. Cheap; just shows the intro state."""

        if self._state != HealthReportState.IDLE:
            raise RuntimeError(f"begin() requires IDLE state; was {self._state.value}")
        self._device_at_start = self._device_context_provider()
        self._caveats = []
        self._state = HealthReportState.PREFACE

    def start_test(self) -> None:
        """PREFACE -> READY_REST. The user must click "Start step 1" before
        sampling begins; that's what :meth:`proceed_to_step` then handles.

        Sample collection does NOT begin here — the wizard pauses on a
        READY_REST screen so the user has time to read the rest-step
        instructions before sticks are sampled (local testing showed
        users still settling the controller into position while the rest
        window was already counting down).
        """

        if self._state != HealthReportState.PREFACE:
            raise RuntimeError(
                f"start_test() requires PREFACE state; was {self._state.value}"
            )
        self._state = HealthReportState.READY_REST

    def proceed_to_step(self) -> None:
        """READY_REST -> REST (and starts the collector); READY_ROTATION ->
        ROTATION; READY_TRIGGER -> TRIGGER. Raises from any non-READY state.

        The collector starts on the READY_REST -> REST transition and runs
        for the rest of the wizard; subsequent READY pauses do NOT stop it,
        they just gate when each step boundary is recorded.
        """

        if self._state not in NEXT_STEP_FOR_READY:
            raise RuntimeError(
                f"proceed_to_step() requires a READY_* state; was {self._state.value}"
            )
        next_step = NEXT_STEP_FOR_READY[self._state]
        if self._state == HealthReportState.READY_REST:
            self._collector = SampleCollector(self._sample_provider)
            self._collector.start()
            self._t_zero_ns = time.perf_counter_ns()
            self._t_zero_clock = self._clock()
        # Re-enable sample appends after the READY pause. For the first
        # proceed_to_step (READY_REST -> REST) this is a no-op because the
        # collector was just created with _paused=False; keeping the call
        # symmetric across all three transitions avoids special-case logic.
        if self._collector is not None and self._collector.running:
            try:
                self._collector.resume()
            except Exception:  # noqa: BLE001 — pause/resume must never crash the wizard
                logger.exception("SampleCollector.resume() failed during proceed_to_step")
        self._start_step(next_step)

    def tick(self) -> None:
        """Advance the state machine if the current step's duration elapsed.

        Safe to call from a UI frame callback at any cadence; states without
        a duration (IDLE / PREFACE / READY_* / COMPLETE / CANCELLED) are
        no-ops — READY_* gates wait for an explicit
        :meth:`proceed_to_step` call.
        """

        if self._state not in self._step_durations_s:
            return
        if self._step_started_at is None:
            return
        elapsed = self._clock() - self._step_started_at
        if elapsed < self._step_durations_s[self._state]:
            return
        # Step's allotted time elapsed; advance.
        self._end_current_step()
        if self._state == HealthReportState.REST:
            self._enter_ready_pause(HealthReportState.READY_ROTATION)
        elif self._state == HealthReportState.ROTATION:
            self._enter_ready_pause(HealthReportState.READY_TRIGGER)
        elif self._state == HealthReportState.TRIGGER:
            self._finalize()

    def cancel(self) -> None:
        """Stop sampling and put the service in CANCELLED.

        Idempotent from any state. After cancel, the only valid transitions
        are :meth:`reset` (back to IDLE) or staying CANCELLED.
        """

        if self._collector is not None and self._collector.running:
            try:
                self._collector.stop()
            except Exception:  # noqa: BLE001 — never propagate from cancel
                logger.exception("SampleCollector.stop() failed during cancel")
        self._collector = None
        self._step_started_at = None
        self._state = HealthReportState.CANCELLED

    def reset(self) -> None:
        """Return to IDLE so the wizard can run again (Retest button)."""

        if self._collector is not None and self._collector.running:
            try:
                self._collector.stop()
            except Exception:  # noqa: BLE001
                logger.exception("SampleCollector.stop() failed during reset")
        self._collector = None
        self._state = HealthReportState.IDLE
        self._step_started_at = None
        self._step_starts_at_ns = {}
        self._step_ends_at_ns = {}
        self._t_zero_ns = None
        self._t_zero_clock = None
        self._report = None
        self._device_at_start = None
        self._caveats = []

    def add_caveat(self, message: str) -> None:
        """Append a caveat string to the final report (e.g. retry reason)."""

        message = (message or "").strip()
        if message:
            self._caveats.append(message)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _start_step(self, step: HealthReportState) -> None:
        self._state = step
        self._step_started_at = self._clock()
        # Map clock time to perf_counter_ns so we can slice samples by ns.
        if self._t_zero_clock is not None and self._t_zero_ns is not None:
            offset_s = self._step_started_at - self._t_zero_clock
            offset_ns = int(offset_s * 1_000_000_000)
            self._step_starts_at_ns[step] = self._t_zero_ns + offset_ns
        else:
            self._step_starts_at_ns[step] = time.perf_counter_ns()

    def _enter_ready_pause(self, ready_state: HealthReportState) -> None:
        """Transition from a sampling step into the next READY_* gate.

        The sample collector keeps running across READY_* pauses but stops
        appending — operator-paced READY waits were burning the cap and
        starving downstream sampling steps (observed in local testing).
        We also stop the per-step elapsed clock so
        :meth:`step_progress` doesn't report bogus remaining-time numbers
        while the user reads the next step's instructions.
        """

        self._state = ready_state
        self._step_started_at = None
        if self._collector is not None and self._collector.running:
            try:
                self._collector.pause()
            except Exception:  # noqa: BLE001 — pause/resume must never crash the wizard
                logger.exception("SampleCollector.pause() failed during ready pause")

    def _end_current_step(self) -> None:
        if self._t_zero_clock is None or self._t_zero_ns is None or self._step_started_at is None:
            return
        # End ns = now (clock) translated to perf_counter_ns.
        offset_s = self._clock() - self._t_zero_clock
        offset_ns = int(offset_s * 1_000_000_000)
        self._step_ends_at_ns[self._state] = self._t_zero_ns + offset_ns

    def _finalize(self) -> None:
        if self._collector is None:
            self._state = HealthReportState.COMPLETE
            return
        try:
            samples = self._collector.stop()
        except Exception:  # noqa: BLE001
            logger.exception("SampleCollector.stop() failed during finalize")
            samples = []
        self._collector = None
        self._state = HealthReportState.COMPLETE
        self._step_started_at = None
        self._report = self._build_report(samples)
        if self._wear_ledger is not None and self._report is not None:
            overall_status = getattr(self._report.overall_status, "value", str(self._report.overall_status))
            self._wear_ledger.append(
                HEALTH_REPORT,
                summary=f"Health report: {overall_status}",
                details={
                    "overall_status": overall_status,
                    "caveats_count": len(self._report.caveats),
                    "device_controller_name": scrub_value(
                        self._report.device.controller_name
                    ),
                },
            )

    def _build_report(self, samples: list[Sample]) -> HealthReport:
        rest_samples = self._slice_samples(samples, HealthReportState.REST)
        rotation_samples = self._slice_samples(samples, HealthReportState.ROTATION)
        trigger_samples = self._slice_samples(samples, HealthReportState.TRIGGER)

        rest_left = compute_stick_rest_noise(rest_samples, side="left")
        rest_right = compute_stick_rest_noise(rest_samples, side="right")
        rest_report = StickRestNoiseReport(left=rest_left, right=rest_right)

        rotation_center_left = (rest_left.median_center_x, rest_left.median_center_y)
        rotation_center_right = (rest_right.median_center_x, rest_right.median_center_y)
        range_left = compute_stick_range(
            rotation_samples, side="left", center_xy=rotation_center_left
        )
        range_right = compute_stick_range(
            rotation_samples, side="right", center_xy=rotation_center_right
        )
        range_report = StickRangeReport(left=range_left, right=range_right)

        trigger_report = TriggerRangeReport(
            left=compute_trigger_range(trigger_samples, side="left"),
            right=compute_trigger_range(trigger_samples, side="right"),
        )

        overall = compute_overall_status(
            left_rest=rest_left,
            right_rest=rest_right,
            left_range=range_left,
            right_range=range_right,
            left_trigger=trigger_report.left,
            right_trigger=trigger_report.right,
        )

        device = self._device_at_start or DeviceContext(
            controller_name=None, configured_polling_hz=None, profile_name=None
        )

        return HealthReport(
            app_version=self._app_version,
            app_build_commit=self._app_build_commit,
            generated_at_local=self._local_timestamp_provider(),
            device=device,
            stick_rest_noise=rest_report,
            stick_range=range_report,
            trigger_range=trigger_report,
            overall_status=overall,
            caveats=tuple(self._caveats),
        )

    def _slice_samples(
        self,
        samples: list[Sample],
        step: HealthReportState,
    ) -> list[Sample]:
        start_ns = self._step_starts_at_ns.get(step)
        end_ns = self._step_ends_at_ns.get(step)
        if start_ns is None or end_ns is None:
            return []
        return filter_samples_by_time_range(samples, start_ns=start_ns, end_ns=end_ns)


__all__ = [
    "REST_DURATION_S",
    "ROTATION_DURATION_S",
    "TRIGGER_DURATION_S",
    "HealthReportService",
    "HealthReportState",
    "NEXT_STEP_FOR_READY",
    "StepProgress",
]
