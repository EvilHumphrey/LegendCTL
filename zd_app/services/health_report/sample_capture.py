"""High-rate XInput sample collector for the Controller Health Report.

The existing :class:`zd_app.services.xinput_poll_service.XInputPollService` is
optimized for the Diagnostics live panel: a worker thread polls at most ~120
times per second, and ``get_snapshot()`` returns a *cached* most-recent
state. That's plenty for a per-frame UI, but it can't characterize 1-8 kHz
report cadences because the cached snapshot returns the same value across
many consecutive ``get_snapshot()`` calls.

Health Report therefore runs a *dedicated* poll thread for the duration of
the test. It loops as fast as the Python+WinAPI stack allows (typically
several kHz on a desktop) and records every ``XInputGetState`` result with a
``time.perf_counter_ns()`` timestamp. The cadence measurement
(``compute_observed_cadence``) then filters to packet-number transitions —
duplicate packets mean no new HID report arrived since the last poll.

Sample cap is 500,000 — at ~3.5 kHz observed rate that's ~143 s of
continuous sampling, well past any reasonable user pacing of the three
sampling windows (5 + 10 + 6 s = 21 s of mandatory sampling, plus
arbitrary operator-paced READY pauses where appends are suspended).
This is a defensive upper bound, not an expected operating point.

The collector intentionally reuses :func:`zd_app.services.xinput_poll_service._load_dll`
and ``_XINPUT_STATE`` rather than re-implementing the DLL fallback chain.
Tests inject a fake ``snapshot_provider`` to avoid loading the real DLL.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from typing import Callable, Optional

from zd_app.services.health_report.models import Sample
from zd_app.services.xinput_poll_service import (
    _ERROR_DEVICE_NOT_CONNECTED,
    _ERROR_SUCCESS,
    _XINPUT_STATE,
    _load_dll,
)


logger = logging.getLogger(__name__)


SampleProvider = Callable[[], Optional[Sample]]
"""A callable that returns one fresh sample (or None on disconnect).

``timestamp_ns`` is filled by the provider so tests can control timing
deterministically. The orchestrator uses :func:`make_xinput_sample_provider`
in production; tests pass a list-driven stub.
"""


_SAMPLE_CAP_DEFAULT = 500_000

# Stick X/Y arrive from XInput as signed 16-bit values in [-32768, 32767].
# We rescale to signed integer percent in [-100, 100] at capture time so the
# downstream measurements (and the suggested-deadzone value the user sees)
# share units with the wrapper's existing stick deadzone slider (0..100).
# Per the measurement contract: "If the app already has configurable deadzone units, report in
# the same units the user sees in the UI."
_XINPUT_AXIS_MAX = 32767
_PCT_AXIS_MAX = 100


def _scale_xinput_axis_to_pct(raw: int) -> int:
    pct = int(round(raw * _PCT_AXIS_MAX / _XINPUT_AXIS_MAX))
    if pct > _PCT_AXIS_MAX:
        return _PCT_AXIS_MAX
    if pct < -_PCT_AXIS_MAX:
        return -_PCT_AXIS_MAX
    return pct


# Optional inter-poll yield. Defaults to 0 for two reasons:
# 1) The measurement contract wants the highest practical poll rate so 1k–8k cadence can be
#    characterized; Windows quantizes sub-millisecond ``time.sleep`` calls to
#    the system timer resolution (~15.6 ms) anyway, which would cap the
#    worker at ~64 Hz and make 1 kHz cadence measurement meaningless.
# 2) Even a tiny ``time.sleep`` call would leak into unrelated tests that
#    patch ``time.sleep`` globally (some tools-side runner tests do this);
#    the daemon worker thread can outlive its test by a few microseconds.
# CPython releases the GIL periodically inside the tight loop, so the UI
# render thread still gets cycles. Tests that need to slow the worker down
# can pass a non-zero value.
_POLL_YIELD_NS_DEFAULT = 0


class SampleCollector:
    """Daemon-thread sample collector for one Health Report run.

    Usage::

        collector = SampleCollector(provider)
        collector.start()
        # ... user performs the test step ...
        samples = collector.stop()

    :meth:`start` and :meth:`stop` are not re-entrant; the collector is
    designed to be re-created per run (cheap; no DLL re-load happens here).

    The provider closure owns the XInput DLL handle in production; the
    collector owns nothing that needs explicit cleanup.
    """

    def __init__(
        self,
        provider: SampleProvider,
        *,
        sample_cap: int = _SAMPLE_CAP_DEFAULT,
        poll_yield_ns: int = _POLL_YIELD_NS_DEFAULT,
    ) -> None:
        self._provider = provider
        self._sample_cap = max(1, int(sample_cap))
        self._poll_yield_ns = max(0, int(poll_yield_ns))
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._samples: list[Sample] = []
        self._lock = threading.Lock()
        self._paused = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("SampleCollector is already running")
        self._stop_event.clear()
        with self._lock:
            self._samples = []
        self._thread = threading.Thread(
            target=self._run_loop,
            name="health-report-sample-collector",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, join_timeout_s: float = 2.0) -> list[Sample]:
        """Signal the worker, join, and return the captured samples."""

        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout_s)
        self._thread = None
        with self._lock:
            return list(self._samples)

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def snapshot_samples(self) -> list[Sample]:
        """Thread-safe peek at the samples captured so far (mid-run)."""

        with self._lock:
            return list(self._samples)

    def pause(self) -> None:
        """Suspend appending to ``self._samples`` without stopping the worker.

        The worker keeps polling XInput (preserving any warm-up state) and
        keeps timestamping samples, but drops them on the floor while paused.
        Call :meth:`resume` to re-enable append. Idempotent and safe to call
        before :meth:`start` or after :meth:`stop`.

        Used by :class:`HealthReportService` to suspend appends during
        operator-paced READY_* pauses so the sample cap isn't burned by
        wall-clock time the user spent reading instructions between steps.
        """

        with self._lock:
            self._paused = True

    def resume(self) -> None:
        """Re-enable appending after :meth:`pause`. Idempotent."""

        with self._lock:
            self._paused = False

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        # Use Event.wait (not time.sleep) so:
        # 1) cancellation is immediate when stop_event fires mid-yield, and
        # 2) the worker never calls time.sleep — which matters because some
        #    unrelated tests patch time.sleep globally, and a daemon worker
        #    that calls sleep can leak its calls into those tests' mocks.
        sleep_seconds = (
            self._poll_yield_ns / 1_000_000_000 if self._poll_yield_ns else 0.0
        )
        while not self._stop_event.is_set():
            try:
                sample = self._provider()
            except Exception:  # noqa: BLE001 — defensive; never crash the test
                logger.exception("Health Report sample provider raised; stopping capture")
                return
            if sample is not None:
                with self._lock:
                    if self._paused:
                        # Drop sample on the floor; keep polling so XInput
                        # stays warm and the next resume() picks up cleanly.
                        pass
                    elif len(self._samples) < self._sample_cap:
                        self._samples.append(sample)
                    else:
                        # At the cap: drop new samples rather than rewriting
                        # earlier history. Tests assert the cap is honoured.
                        return
            if sleep_seconds > 0:
                if self._stop_event.wait(sleep_seconds):
                    return


# ---------------------------------------------------------------------------
# XInput sample provider — production
# ---------------------------------------------------------------------------


def make_xinput_sample_provider(
    *,
    user_index: int = 0,
    dll_loader: Optional[Callable[[], object]] = None,
    time_source_ns: Callable[[], int] = time.perf_counter_ns,
) -> SampleProvider:
    """Build a closure that returns one fresh ``Sample`` per call.

    The DLL is loaded once at construction time and held inside the closure.
    Returns ``None`` if the DLL is unavailable (CI / non-Windows) or the
    controller is disconnected; the collector skips ``None`` samples.

    Tests should *not* use this — pass a list-driven stub to
    :class:`SampleCollector` directly.
    """

    loader = dll_loader or _load_dll
    dll = loader()
    if dll is None:
        logger.warning(
            "XInput DLL unavailable; Health Report sample provider returns None"
        )
        return lambda: None

    state = _XINPUT_STATE()

    def _provider() -> Optional[Sample]:
        try:
            rc = dll.XInputGetState(user_index, ctypes.byref(state))
        except OSError as exc:
            logger.debug("XInputGetState raised OSError: %s", exc)
            return Sample(
                timestamp_ns=time_source_ns(),
                packet_number=0,
                left_stick_x=0, left_stick_y=0,
                right_stick_x=0, right_stick_y=0,
                left_trigger=0, right_trigger=0,
                connected=False,
            )
        ts = time_source_ns()
        if rc == _ERROR_DEVICE_NOT_CONNECTED:
            return Sample(
                timestamp_ns=ts,
                packet_number=0,
                left_stick_x=0, left_stick_y=0,
                right_stick_x=0, right_stick_y=0,
                left_trigger=0, right_trigger=0,
                connected=False,
            )
        if rc != _ERROR_SUCCESS:
            return None
        gp = state.Gamepad
        return Sample(
            timestamp_ns=ts,
            packet_number=int(state.dwPacketNumber),
            left_stick_x=_scale_xinput_axis_to_pct(int(gp.sThumbLX)),
            left_stick_y=_scale_xinput_axis_to_pct(int(gp.sThumbLY)),
            right_stick_x=_scale_xinput_axis_to_pct(int(gp.sThumbRX)),
            right_stick_y=_scale_xinput_axis_to_pct(int(gp.sThumbRY)),
            left_trigger=int(gp.bLeftTrigger),
            right_trigger=int(gp.bRightTrigger),
            connected=True,
        )

    return _provider


__all__ = [
    "SampleCollector",
    "SampleProvider",
    "make_xinput_sample_provider",
    "_scale_xinput_axis_to_pct",
]
