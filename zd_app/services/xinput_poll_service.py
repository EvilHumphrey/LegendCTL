"""Background XInput polling service for the live diagnostics panel.

Scans ``XInputGetState`` across the four XInput user indices (0-3) on a worker
thread and exposes the selected pad's latest gamepad state as an
``XInputSnapshot``. Designed for the read-only "what XInput sees" panel in the
Diagnostics screen, which closes the B-arc verification loop: bind M1 -> A in
Controller > Buttons, switch to Diagnostics, press M1, watch the A indicator
light up.

Slot selection lives ENTIRELY here (the UI never reasons about XInput indices):

* AUTO (default): pick the FIRST connected slot and stick to it. The selection
  never flips to another pad mid-session while it stays connected; if it
  disconnects, the worker re-scans 0-3 and re-selects the first connected slot.
  When nothing is connected the service serves a clean "no pad" snapshot.
* MANUAL: ``select_slot(i)`` pins one index; ``select_auto()`` resumes the scan.
  A pinned slot keeps its identity even while disconnected, so the UI can keep
  showing "Player N" for the operator's explicit choice.

The selected index rides on every snapshot (``XInputSnapshot.slot``) and is also
exposed via ``active_slot`` so the UI can label it "Player N" (1-based). This is
the fix for the multi-pad bench: when the ZD pad enumerates as player 2-4
(common when other controllers are connected), the live tester and Readiness now
find it instead of staring at an empty slot 0.

DLL fallback walks ``XInput1_4.dll`` -> ``XInput1_3.dll`` -> ``XInput9_1_0.dll``;
if none load (e.g. CI without DirectX), the service stays in ``unavailable()``
forever and the UI shows an "XInput unavailable" message.
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from zd_app.services.settings_service import ControllerButtonTarget


logger = logging.getLogger(__name__)


# XInput error codes
_ERROR_SUCCESS = 0
_ERROR_DEVICE_NOT_CONNECTED = 1167

# XInput supports up to four controllers (user indices 0-3 == XUSER_MAX_COUNT).
_XUSER_MAX_COUNT = 4

# Selection modes, surfaced verbatim by ``XInputPollService.selection_mode``.
_MODE_AUTO = "auto"
_MODE_MANUAL = "manual"


# XInput button bitmask -> internal ControllerButtonTarget enum.
# XInput uses different bit values than our wrapper's enum (which mirrors the
# controller's HID byte values), so this is an explicit translation table.
_XINPUT_BUTTON_BITS: dict[int, ControllerButtonTarget] = {
    0x1000: ControllerButtonTarget.A,
    0x2000: ControllerButtonTarget.B,
    0x4000: ControllerButtonTarget.X,
    0x8000: ControllerButtonTarget.Y,
    0x0100: ControllerButtonTarget.LB,
    0x0200: ControllerButtonTarget.RB,
    0x0020: ControllerButtonTarget.BACK,
    0x0010: ControllerButtonTarget.START,
    0x0040: ControllerButtonTarget.LS,
    0x0080: ControllerButtonTarget.RS,
    0x0001: ControllerButtonTarget.UP,
    0x0002: ControllerButtonTarget.DOWN,
    0x0004: ControllerButtonTarget.LEFT,
    0x0008: ControllerButtonTarget.RIGHT,
}


_DLL_FALLBACK_ORDER = ("XInput1_4.dll", "XInput1_3.dll", "XInput9_1_0.dll")


class _XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class _XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_ulong),
        ("Gamepad", _XINPUT_GAMEPAD),
    ]


@dataclass(frozen=True)
class XInputSnapshot:
    """One sample of XInput state for the selected user index.

    ``slot`` is the XInput user index (0-3) this sample belongs to, or ``None``
    when no pad is selected (AUTO with nothing connected). A manually-pinned
    slot keeps its index here even while disconnected, so the UI can keep
    labelling the operator's explicit choice "Player N".
    """

    connected: bool
    dll_available: bool
    packet_number: int
    buttons: frozenset[ControllerButtonTarget]
    left_trigger: int
    right_trigger: int
    left_stick_x: int
    left_stick_y: int
    right_stick_x: int
    right_stick_y: int
    slot: Optional[int] = None

    @classmethod
    def disconnected(
        cls, *, dll_available: bool = True, slot: Optional[int] = None
    ) -> "XInputSnapshot":
        return cls(
            connected=False,
            dll_available=dll_available,
            packet_number=0,
            buttons=frozenset(),
            left_trigger=0,
            right_trigger=0,
            left_stick_x=0,
            left_stick_y=0,
            right_stick_x=0,
            right_stick_y=0,
            slot=slot,
        )

    @classmethod
    def unavailable(cls) -> "XInputSnapshot":
        return cls.disconnected(dll_available=False)

    @property
    def left_stick_normalized(self) -> tuple[float, float]:
        """Return ``(x, y)`` clamped to ``[-1.0, 1.0]``.

        XInput's SHORT range is asymmetric (-32768 .. 32767); dividing by
        32768 gives a value just barely outside [-1.0, 1.0] at the extreme
        positive end. The clamp keeps downstream UI scaling honest.
        """
        return (
            _clamp_unit(self.left_stick_x / 32768.0),
            _clamp_unit(self.left_stick_y / 32768.0),
        )

    @property
    def right_stick_normalized(self) -> tuple[float, float]:
        return (
            _clamp_unit(self.right_stick_x / 32768.0),
            _clamp_unit(self.right_stick_y / 32768.0),
        )


def _clamp_unit(value: float) -> float:
    if value < -1.0:
        return -1.0
    if value > 1.0:
        return 1.0
    return value


def buttons_from_mask(mask: int) -> frozenset[ControllerButtonTarget]:
    """Translate an XInput ``wButtons`` bitmask into our internal enum set.

    Exposed at module level for test access without instantiating the service.
    """
    return frozenset(
        target for bit, target in _XINPUT_BUTTON_BITS.items() if mask & bit
    )


class XInputPollService:
    """Polls ``XInputGetState`` on a worker thread; serves the latest snapshot.

    The service owns ALL slot-selection logic (the UI never reasons about XInput
    indices):

    * One worker thread, one DLL handle. Each poll resolves WHICH of the four
      user indices to read — AUTO picks/sticks to the first connected slot and
      re-scans on loss; MANUAL pins a chosen slot.
    * ``get_snapshot`` is cheap and lock-protected so the UI can call it on
      every frame without coordination cost.
    * ``select_slot`` / ``select_auto`` flip the selection from the UI thread;
      ``active_slot`` / ``selection_mode`` expose the current pick.
    * ``start`` and ``stop`` are idempotent.
    * If the DLL cannot be loaded (e.g. CI without DirectX), the service
      stays in the ``unavailable`` state forever; ``start`` is still callable
      but the worker thread exits immediately so the lifecycle stays clean.
    """

    def __init__(
        self,
        *,
        poll_interval_s: float = 1 / 120,
        dll_loader=None,
    ) -> None:
        self._poll_interval_s = max(poll_interval_s, 1 / 250)
        self._lock = threading.Lock()
        # Guards the selection state (mode + selected slot), which the UI thread
        # mutates via select_slot/select_auto while the worker reads it. Kept
        # separate from _lock (which guards the hot get_snapshot read) so a poll
        # scan never blocks a UI frame's snapshot read.
        self._sel_lock = threading.Lock()
        self._mode = _MODE_AUTO
        # AUTO: the sticky selection (None until a scan finds a pad). MANUAL: the
        # operator-pinned index. None means "no pad selected".
        self._selected_slot: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._dll = (dll_loader or _load_dll)()
        self._latest = (
            XInputSnapshot.disconnected() if self._dll is not None
            else XInputSnapshot.unavailable()
        )

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Idempotent. Spawns the worker thread on first call."""
        if self._thread is not None and self._thread.is_alive():
            return
        if self._dll is None:
            logger.info("XInput DLL unavailable; service stays in unavailable state.")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="xinput-poll",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, join_timeout_s: float = 1.0) -> None:
        """Idempotent. Signals the worker thread and joins."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout_s)
        self._thread = None

    # -- Read API ------------------------------------------------------------

    def get_snapshot(self) -> XInputSnapshot:
        """Return the most-recent snapshot. Cheap; UI calls this every frame."""
        with self._lock:
            return self._latest

    @property
    def dll_available(self) -> bool:
        return self._dll is not None

    # -- Selection API -------------------------------------------------------

    def select_slot(self, index: int) -> None:
        """Pin the live tester to XInput user index ``index`` (0-3).

        Switches to MANUAL mode: the worker polls only this slot and never
        auto-flips away from it, even while it is disconnected (the UI keeps
        showing the operator's explicit "Player N").
        """

        if not 0 <= index < _XUSER_MAX_COUNT:
            raise ValueError(
                f"XInput slot must be 0..{_XUSER_MAX_COUNT - 1}, got {index!r}"
            )
        with self._sel_lock:
            self._mode = _MODE_MANUAL
            self._selected_slot = index

    def select_auto(self) -> None:
        """Resume AUTO selection: re-scan 0-3 and stick to the first connected."""

        with self._sel_lock:
            self._mode = _MODE_AUTO
            # Drop any pin so the next poll runs a fresh scan from slot 0.
            self._selected_slot = None

    @property
    def active_slot(self) -> Optional[int]:
        """Selected XInput user index (0-3), or ``None`` when no pad is selected
        (AUTO with nothing connected)."""

        with self._sel_lock:
            return self._selected_slot

    @property
    def selection_mode(self) -> str:
        """``"auto"`` or ``"manual"``."""

        with self._sel_lock:
            return self._mode

    # -- Internals -----------------------------------------------------------

    def _run_loop(self) -> None:
        state = _XINPUT_STATE()
        while not self._stop.is_set():
            snapshot = self._poll_once(state)
            with self._lock:
                self._latest = snapshot
            # Use Event.wait so a stop() during sleep returns promptly.
            if self._stop.wait(self._poll_interval_s):
                break

    def _poll_once(self, state: _XINPUT_STATE) -> XInputSnapshot:
        with self._sel_lock:
            mode = self._mode
            slot = self._selected_slot
        try:
            snapshot, resolved_slot = self._select_and_poll(mode, slot, state)
        except OSError as exc:
            logger.warning("XInputGetState raised OSError: %s", exc)
            return XInputSnapshot.disconnected(slot=None)
        # Persist an AUTO (re-)selection so the next poll sticks to it — but only
        # if the UI hasn't switched modes underneath us; a manual pin wins.
        if mode == _MODE_AUTO:
            with self._sel_lock:
                if self._mode == _MODE_AUTO:
                    self._selected_slot = resolved_slot
        return snapshot

    def _select_and_poll(self, mode, slot, state):
        """Resolve which slot to read; return ``(snapshot, resolved_slot)``.

        AUTO is sticky: a still-connected selection is re-read with a SINGLE
        XInput call; only a lost (or absent) selection triggers a 0-3 re-scan
        that picks the first connected slot. MANUAL reads exactly the pinned
        slot and keeps its identity even while disconnected.
        """

        if mode == _MODE_MANUAL:
            snap = self._query_slot(slot, state)
            if snap is not None:
                return snap, slot
            return XInputSnapshot.disconnected(slot=slot), slot
        # AUTO — keep the current pick while it stays connected (sticky).
        if slot is not None:
            snap = self._query_slot(slot, state)
            if snap is not None:
                return snap, slot
            # The sticky slot dropped: fall through to a fresh scan.
        for candidate in range(_XUSER_MAX_COUNT):
            snap = self._query_slot(candidate, state)
            if snap is not None:
                return snap, candidate
        return XInputSnapshot.disconnected(slot=None), None

    def _query_slot(self, slot: int, state: _XINPUT_STATE) -> Optional[XInputSnapshot]:
        """Poll one XInput user index; return a connected snapshot or ``None``.

        ``None`` means "not connected" — ``ERROR_DEVICE_NOT_CONNECTED`` or any
        other non-success rc. ``OSError`` from the DLL propagates to the single
        handler in ``_poll_once`` (a DLL-level failure is identical for every
        slot, so there is no point catching it per slot).
        """

        rc = self._dll.XInputGetState(slot, ctypes.byref(state))
        if rc != _ERROR_SUCCESS:
            if rc != _ERROR_DEVICE_NOT_CONNECTED:
                logger.debug("XInputGetState slot=%s unexpected rc=%s", slot, rc)
            return None
        gp = state.Gamepad
        return XInputSnapshot(
            connected=True,
            dll_available=True,
            packet_number=int(state.dwPacketNumber),
            buttons=buttons_from_mask(int(gp.wButtons)),
            left_trigger=int(gp.bLeftTrigger),
            right_trigger=int(gp.bRightTrigger),
            left_stick_x=int(gp.sThumbLX),
            left_stick_y=int(gp.sThumbLY),
            right_stick_x=int(gp.sThumbRX),
            right_stick_y=int(gp.sThumbRY),
            slot=slot,
        )


def _load_dll():
    """Try the XInput DLL fallback chain; return a ``ctypes.WinDLL`` or ``None``.

    Walks 1_4 -> 1_3 -> 9_1_0 in order. On non-Windows or systems without
    DirectX, ``WinDLL`` is unavailable / the loads fail; we return ``None``
    so the service sits in the unavailable state without crashing.
    """
    WinDLL = getattr(ctypes, "WinDLL", None)
    if WinDLL is None:
        return None
    for name in _DLL_FALLBACK_ORDER:
        try:
            dll = WinDLL(name)
        except OSError as exc:
            logger.debug("XInput DLL %s not loadable: %s", name, exc)
            continue
        try:
            dll.XInputGetState.argtypes = [ctypes.c_uint, ctypes.POINTER(_XINPUT_STATE)]
            dll.XInputGetState.restype = ctypes.c_uint
        except AttributeError:
            logger.debug("XInput DLL %s missing XInputGetState symbol", name)
            continue
        logger.info("Loaded XInput DLL: %s", name)
        return dll
    return None
