"""Background XInput polling service for the live diagnostics panel.

Polls ``XInputGetState`` for user index 0 on a worker thread and exposes the
latest gamepad state as an ``XInputSnapshot``. Designed for the read-only
"what XInput sees" panel in the Diagnostics screen, which closes the
B-arc verification loop: bind M1 -> A in Controller > Buttons, switch to
Diagnostics, press M1, watch the A indicator light up.

The service is single-user-index by design (multi-controller is out of
scope). DLL fallback walks ``XInput1_4.dll`` -> ``XInput1_3.dll`` ->
``XInput9_1_0.dll``; if none load (e.g. CI without DirectX), the service
stays in ``disconnected()`` forever and the UI shows an "XInput unavailable"
message.
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
    """One sample of XInput state for user index 0."""

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

    @classmethod
    def disconnected(cls, *, dll_available: bool = True) -> "XInputSnapshot":
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

    The service is intentionally simple:

    * One worker thread, one user index, one DLL handle.
    * ``get_snapshot`` is cheap and lock-protected so the UI can call it on
      every frame without coordination cost.
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
        try:
            rc = self._dll.XInputGetState(0, ctypes.byref(state))
        except OSError as exc:
            logger.warning("XInputGetState raised OSError: %s", exc)
            return XInputSnapshot.disconnected()
        if rc == _ERROR_DEVICE_NOT_CONNECTED:
            return XInputSnapshot.disconnected()
        if rc != _ERROR_SUCCESS:
            logger.debug("XInputGetState returned unexpected rc=%s", rc)
            return XInputSnapshot.disconnected()
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
