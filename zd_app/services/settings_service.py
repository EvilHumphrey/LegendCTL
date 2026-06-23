"""Settings protocol service for ZD Gaming Zone 1.2.7 feature reads/writes.

This service targets the stock ``VID_413D&PID_2104`` MI_02 vendor-defined
HID interface. It is independent of the opt-in standalone trigger service:
polling-rate writes do not require ``VID_20BC`` enumeration and do not run a
background keepalive loop.

Protocol source: the documented v1.2.7 vendor HID protocol.
"""

from __future__ import annotations

import ctypes as c
import logging
import threading
import time
from ctypes import wintypes as w
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from typing import Callable, Optional, TypeVar


logger = logging.getLogger(__name__)

_R = TypeVar("_R")


# ---------------------------------------------------------------------------
# VID/PID + interface constants
# ---------------------------------------------------------------------------

PUBLIC_VENDOR_ID = 0x413D
PUBLIC_PRODUCT_ID = 0x2104

# Standard HID device-interface GUID.
MI02_DEVICE_INTERFACE_GUID = "{4d1e55b2-f16f-11cf-88cb-001111000030}"


# ---------------------------------------------------------------------------
# HID feature-report protocol constants
# ---------------------------------------------------------------------------

HID_FEATURE_REPORT_SIZE = 65
HID_REPORT_ID = 0x00
MAGIC_WRITE_PREFIX = bytes.fromhex("1055aa51")
MAGIC_READ_QUERY_PREFIX = bytes.fromhex("1055aa50")
MAGIC_READ_RESPONSE_PREFIX = bytes.fromhex("3055aad0")
MAGIC_WRITE_ACK_PREFIX = bytes.fromhex("3055aad1")
READ_RESPONSE_TIMEOUT_MS = 1000
# Floor applied by _default_read_file when the caller-supplied timeout is
# missing or non-positive. Picked to be slow enough for legit reads
# (typically 10-100ms in normal operation) but fast enough that a wedged
# HID handle doesn't visibly delay startup beyond ~1.5 seconds. See
# refresh_from_controller's startup-hang regression for why this exists.
READ_FILE_TIMEOUT_MS_DEFAULT = 1500
READ_STALE_RESPONSE_MAX_DISCARDS = 64
WRITE_RETRY_DELAY_S = 0.05
DISCONNECT_WIN32_ERRORS = {6, 433, 1167, 1168}
SHORT_WRITE_ERROR_CODE = 299
CATEGORY_POLLING_RATE = 0x11
POLLING_RATE_SUBCOMMAND = 0x00
POLLING_RATE_SUFFIX = bytes.fromhex("0202")
CATEGORY_STEP_SIZE = 0x0D
STEP_SIZE_SUBCOMMAND = 0x00
STEP_SIZE_VALUE_MIN = 1
STEP_SIZE_VALUE_MAX = 255
# fw-1.24 vendor/device default; scale is 1:1 raw 1-255, confirmed 2026-05-30.
STEP_SIZE_VALUE_DEFAULT = 73
CATEGORY_BUTTON_BINDING = 0x02
BUTTON_BINDING_SUBCOMMAND = 0x00
# The 16 sequential per-slot button-binding reads intermittently drop or
# mis-read a single slot when they run inside the big capture / post-restore
# verify read burst -- local testing saw button_bindings report a
# false "mismatch after restore" even though every write landed (the captured
# Restore Point itself recorded only 15 of 16 slots). A steady-state probe read
# all 16 slots 5/5 passes, so it is timing/load sensitivity, not a decode bug.
# Retry a transient read a few times before giving up, mirroring the write-side
# WRITE_RETRY_DELAY_S / _write_payload_with_retry precedent. Kept small so a
# fully-unresponsive device can't balloon get_all_settings (worst case is
# BUTTON_BINDING_READ_ATTEMPTS reads per slot). The delay rides the injected
# self._sleep seam, so tests stub it to a no-op (no real sleep in the suite).
BUTTON_BINDING_READ_ATTEMPTS = 3
BUTTON_BINDING_READ_RETRY_DELAY_S = 0.02
CATEGORY_STICK_DEADZONE = 0x09
DEADZONE_SUBCOMMAND = 0x00
CATEGORY_STICK_SENSITIVITY = 0x06
# Dormant 1.2.9 8-point sensitivity category (= 0x06 | 0x80). Not wired into any
# active write path; see build_sensitivity_curve_payload_8point below.
CATEGORY_STICK_SENSITIVITY_8POINT = 0x86
SENSITIVITY_SUBCOMMAND = 0x00
SENSITIVITY_STICK_LEFT = 0x00
SENSITIVITY_STICK_RIGHT = 0x01
CATEGORY_STICK_INVERSION = 0x07
INVERSION_SUBCOMMAND = 0x00
INVERSION_STICK_LEFT = 0x00
INVERSION_STICK_RIGHT = 0x01
CATEGORY_TRIGGER_SETTINGS = 0x0A
TRIGGER_SUBCOMMAND = 0x00
TRIGGER_SELECTOR_LEFT = 0x00
TRIGGER_SELECTOR_RIGHT = 0x01
TRIGGER_RANGE_MIN_VALUE = 0
TRIGGER_RANGE_MAX_VALUE = 100
CATEGORY_VIBRATION = 0x0C
VIBRATION_SUBCOMMAND = 0x00
VIBRATION_STRENGTH_MIN = 0
VIBRATION_STRENGTH_MAX = 100
CATEGORY_MOTION = 0x0B
MOTION_SUBCOMMAND = 0x00
CATEGORY_LIGHTING = 0x10
LIGHTING_SUBCOMMAND = 0x00
CATEGORY_BACK_PADDLE_PRIMARY = 0x03
CATEGORY_BACK_PADDLE_BINDING = 0x05
CATEGORY_BACK_PADDLE_BULK = 0x12
BACK_PADDLE_SUBCOMMAND = 0x00
BACK_PADDLE_EVENT_INDEX_BINDING = 0x00
BACK_PADDLE_EVENT_INDEX_TERMINATOR = 0x01
BACK_PADDLE_UNBOUND_FLAG = 0x02
BACK_PADDLE_DEFAULT_DURATION_MS = 10
BACK_PADDLE_DEFAULT_CYCLE_MS = 10
LIGHTING_BRIGHTNESS_BYTE_MIN = 0
LIGHTING_BRIGHTNESS_BYTE_MAX = 255
DEADZONE_VALUE_MIN = 0
DEADZONE_VALUE_MAX = 100
SENSITIVITY_VALUE_MIN = 0
SENSITIVITY_VALUE_MAX = 100
SENSITIVITY_ANCHOR_COUNT = 3
SENSITIVITY_ANCHOR_COUNT_8POINT = 8
# Short read timeout used only by the 8-point capability probe. A capable
# fw-1.24 device answers the 0x86 read in ~10-100 ms; this caps the one-time
# penalty on a non-capable (fw-1.18) device that never answers 0x86 — its
# get_all_settings pays this once (the verdict is cached per connection) before
# falling back to the 3-point path. Generous enough to never false-negative a
# healthy capable device; see supports_8point_sensitivity.
SENSITIVITY_8POINT_PROBE_TIMEOUT_MS = 500


class PollingRate(Enum):
    """Confirmed USB polling-rate selector bytes from 1.2.7 captures."""

    HZ_250 = 0x01
    HZ_500 = 0x02
    HZ_1000 = 0x03
    HZ_2000 = 0x04
    HZ_4000 = 0x05
    HZ_8000 = 0x06


SUPPORTED_POLLING_RATES = frozenset(PollingRate)


POLLING_RATE_HZ: dict[PollingRate, int] = {
    PollingRate.HZ_250: 250,
    PollingRate.HZ_500: 500,
    PollingRate.HZ_1000: 1000,
    PollingRate.HZ_2000: 2000,
    PollingRate.HZ_4000: 4000,
    PollingRate.HZ_8000: 8000,
}


class TriggerMode(Enum):
    """Trigger mode selector bytes from wrapper-vendor round-trip verification."""

    LONG = 0x00
    SHORT = 0x01


class TriggerVibrationMode(Enum):
    """Confirmed trigger-vibration mode selector bytes from 1.2.7 captures."""

    NATIVE = 0x00
    STEREO_RESONANCE = 0x01
    TRIGGER_VIBRATION = 0x02


class MotionMappingTarget(Enum):
    """Where the motion gyro maps when active."""

    DISABLED = 0x00
    LEFT_JOYSTICK = 0x01
    RIGHT_JOYSTICK = 0x02


class MotionMappingMode(Enum):
    """How the motion mapping behaves when active."""

    INSTANT = 0x00
    CONTINUOUS = 0x01


class LightingZone(Enum):
    """Capture-confirmed lighting zones from the 1.2.7 Lighting tab."""

    HOME = 0x00
    LEFT_LIGHT = 0x01
    RIGHT_LIGHT = 0x02


class LightingMode(Enum):
    """Capture-confirmed per-zone lighting mode selector bytes."""

    OFF = 0x00
    ALWAYS_ON = 0x01
    BREATH = 0x02
    FADE = 0x03
    FLOW = 0x04


class ButtonSlot(Enum):
    """Confirmed button slot indices from 1.2.7 captures."""

    UP = 0x01
    RIGHT = 0x02
    DOWN = 0x03
    LEFT = 0x04
    A = 0x05
    B = 0x06
    X = 0x07
    Y = 0x08
    LB = 0x09
    RB = 0x0A
    LT = 0x0B
    RT = 0x0C
    BACK = 0x0D
    START = 0x0E
    LS = 0x0F
    RS = 0x10


class ControllerButtonTarget(Enum):
    """Confirmed controller-button target values from 1.2.7 captures."""

    LS = 0x09
    RS = 0x0A
    UP = 0x0B
    RIGHT = 0x0C
    DOWN = 0x0D
    LEFT = 0x0E
    A = 0x0F
    B = 0x10
    X = 0x11
    Y = 0x12
    LB = 0x13
    RB = 0x14
    LT = 0x15
    RT = 0x16
    BACK = 0x17
    START = 0x18


class MacroSlot(Enum):
    """Back-paddle / macro slot indices for the supported firmware."""

    M1 = 0x00
    M2 = 0x01
    M3 = 0x02
    M4 = 0x03
    LM = 0x04
    RM = 0x05
    LK = 0x06
    RK = 0x07


SUPPORTED_BUTTON_SLOTS = frozenset(ButtonSlot)
SUPPORTED_CONTROLLER_BUTTON_TARGETS = frozenset(ControllerButtonTarget)
SUPPORTED_MACRO_SLOTS = frozenset(MacroSlot)


@dataclass(frozen=True)
class ButtonMapping:
    """Button mapping target bytes.

    Supports capture-confirmed controller-button mappings only:
    ``01 00 TT`` where ``TT`` is a confirmed
    :class:`ControllerButtonTarget` value. Keyboard/macro target kinds require
    additional captures.
    """

    target_kind: int = 0x01
    target_low: int = 0x00
    target_value: int = 0x10

    @classmethod
    def controller_button(cls, target: ControllerButtonTarget) -> "ButtonMapping":
        if not isinstance(target, ControllerButtonTarget):
            raise NotImplementedError(f"Unsupported controller-button target: {target!r}")
        return cls(target_kind=0x01, target_low=0x00, target_value=target.value)


CAPTURED_A_TO_B_MAPPING = ButtonMapping.controller_button(ControllerButtonTarget.B)


@dataclass(frozen=True)
class BackPaddleBinding:
    """One-step binding for a back paddle.

    B-arc intentionally supports only one controller-button target per paddle.
    It does not expose keyboard targets, multi-step sequences, or user-tuned
    timing values. ``None`` means the slot is explicitly unbound.
    """

    target: Optional[ControllerButtonTarget]


@dataclass(frozen=True)
class SensitivityAnchor:
    """One sensitivity-curve anchor point from the captured left-stick layout."""

    x: int
    y: int


@dataclass(frozen=True)
class StickDeadzones:
    """All four stick deadzone values carried by one category-0x09 frame."""

    left_center: int
    right_center: int
    left_outer: int
    right_outer: int


@dataclass(frozen=True)
class AxisInversion:
    """Captured per-stick X/Y inversion flags."""

    x_inverted: bool
    y_inverted: bool

    def __post_init__(self) -> None:
        if type(self.x_inverted) is not bool:
            raise TypeError(f"x_inverted must be a bool, got {self.x_inverted!r}")
        if type(self.y_inverted) is not bool:
            raise TypeError(f"y_inverted must be a bool, got {self.y_inverted!r}")


@dataclass(frozen=True)
class TriggerSettings:
    """Captured per-trigger range and trigger-mode values."""

    range_min: int
    range_max: int
    mode: TriggerMode


@dataclass(frozen=True)
class VibrationSettings:
    """All vibration fields carried by one category-0x0c frame."""

    left_grip_strength: int
    right_grip_strength: int
    left_trigger_motor_strength: int
    right_trigger_motor_strength: int
    mode: TriggerVibrationMode


@dataclass(frozen=True)
class MotionSettings:
    """Motion-tab state carried by one category-0x0b frame.

    Decoded from the category-0x0b motion frame. This is read-only
    in the wrapper: profiles preserve it for completeness, but profile apply
    intentionally skips it.
    """

    target: MotionMappingTarget
    trigger_key: int
    mode: MotionMappingMode
    sensitivity: int


@dataclass(frozen=True)
class RgbColor:
    """Raw RGB color bytes carried by a lighting frame."""

    r: int
    g: int
    b: int


@dataclass(frozen=True)
class LightingSettings:
    """Captured per-zone lighting values carried by one category-0x10 frame."""

    light_on: bool
    mode: LightingMode
    brightness_byte: int
    color: RgbColor


# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 0x3
INVALID_HANDLE_VALUE = w.HANDLE(-1).value

DIGCF_PRESENT = 0x2
DIGCF_DEVICEINTERFACE = 0x10
ERROR_NO_MORE_ITEMS = 259

MI02_WRITE_OPEN_DESIRED_ACCESS = GENERIC_WRITE
MI02_WRITE_OPEN_SHARE_MODE = FILE_SHARE_READ | FILE_SHARE_WRITE
MI02_WRITE_OPEN_FLAGS_AND_ATTRIBUTES = 0x0
MI02_READ_WRITE_OPEN_DESIRED_ACCESS = GENERIC_READ | GENERIC_WRITE
MI02_READ_WRITE_OPEN_SHARE_MODE = FILE_SHARE_READ | FILE_SHARE_WRITE
MI02_READ_WRITE_OPEN_FLAGS_AND_ATTRIBUTES = 0x0


# ---------------------------------------------------------------------------
# ctypes structures
# ---------------------------------------------------------------------------

class _GUID(c.Structure):
    _fields_ = [
        ("Data1", w.DWORD),
        ("Data2", w.WORD),
        ("Data3", w.WORD),
        ("Data4", c.c_ubyte * 8),
    ]


class _SP_DEVICE_INTERFACE_DATA(c.Structure):
    _fields_ = [
        ("cbSize", w.DWORD),
        ("InterfaceClassGuid", _GUID),
        ("Flags", w.DWORD),
        ("Reserved", c.c_void_p),
    ]


# ---------------------------------------------------------------------------
# Lazy-bound Win32 surface
# ---------------------------------------------------------------------------

class _Win32:
    """Small Win32 surface for MI_02 HID writes.

    The service-level helpers below are dependency-injectable so tests never
    need to touch real ctypes. This class is intentionally narrow and mirrors
    the v1 service's lazy-binding pattern.
    """

    _kernel32 = None
    _setupapi = None

    @classmethod
    def kernel32(cls):
        if cls._kernel32 is None:
            cls._kernel32 = c.windll.kernel32
            cls._kernel32.CreateFileW.argtypes = [
                w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE,
            ]
            cls._kernel32.CreateFileW.restype = w.HANDLE
            cls._kernel32.CloseHandle.argtypes = [w.HANDLE]
            cls._kernel32.CloseHandle.restype = w.BOOL
            cls._kernel32.WriteFile.argtypes = [
                w.HANDLE, c.c_void_p, w.DWORD,
                c.POINTER(w.DWORD), c.c_void_p,
            ]
            cls._kernel32.WriteFile.restype = w.BOOL
            cls._kernel32.ReadFile.argtypes = [
                w.HANDLE, c.c_void_p, w.DWORD,
                c.POINTER(w.DWORD), c.c_void_p,
            ]
            cls._kernel32.ReadFile.restype = w.BOOL
            cls._kernel32.GetLastError.argtypes = []
            cls._kernel32.GetLastError.restype = w.DWORD
            cls._kernel32.CancelIoEx.argtypes = [w.HANDLE, c.c_void_p]
            cls._kernel32.CancelIoEx.restype = w.BOOL
        return cls._kernel32

    @classmethod
    def setupapi(cls):
        if cls._setupapi is None:
            cls._setupapi = c.WinDLL("setupapi")
            cls._setupapi.SetupDiGetClassDevsW.argtypes = [
                c.POINTER(_GUID), w.LPCWSTR, w.HWND, w.DWORD,
            ]
            cls._setupapi.SetupDiGetClassDevsW.restype = c.c_void_p
            cls._setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
                c.c_void_p, c.c_void_p,
                c.POINTER(_GUID), w.DWORD,
                c.POINTER(_SP_DEVICE_INTERFACE_DATA),
            ]
            cls._setupapi.SetupDiEnumDeviceInterfaces.restype = w.BOOL
            cls._setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
                c.c_void_p, c.POINTER(_SP_DEVICE_INTERFACE_DATA),
                c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p,
            ]
            cls._setupapi.SetupDiGetDeviceInterfaceDetailW.restype = w.BOOL
            cls._setupapi.SetupDiDestroyDeviceInfoList.argtypes = [c.c_void_p]
            cls._setupapi.SetupDiDestroyDeviceInfoList.restype = w.BOOL
        return cls._setupapi


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class WriteOutcome(Enum):
    """One write-path outcome vocabulary shared by every ``set_*`` writer."""

    OK = "ok"
    OK_WITH_RETRY = "ok_with_retry"
    DEVICE_NOT_FOUND = "device_not_found"
    OPEN_FAILED = "open_failed"
    WRITE_FAILED = "write_failed"


# Backwards-compatible aliases — each feature writer historically declared a
# byte-identical outcome enum. Every name below IS WriteOutcome (one class), so
# imports, isinstance checks, and cross-feature comparisons work unchanged.
SetPollingRateOutcome = WriteOutcome
SetButtonBindingOutcome = WriteOutcome
SetBackPaddleBindingOutcome = WriteOutcome
SetDeadzoneOutcome = WriteOutcome
SetStepSizeOutcome = WriteOutcome
SetSensitivityCurveOutcome = WriteOutcome
SetAxisInversionOutcome = WriteOutcome
SetTriggerSettingsOutcome = WriteOutcome
SetVibrationOutcome = WriteOutcome
SetLightingOutcome = WriteOutcome


@dataclass
class SetPollingRateResult:
    outcome: SetPollingRateOutcome
    rate: Optional[PollingRate]
    error_code: Optional[int]
    bytes_written: Optional[int]
    payload_hex: Optional[str]
    elapsed_ms: int


@dataclass
class SetButtonBindingResult:
    outcome: SetButtonBindingOutcome
    slot: Optional[ButtonSlot]
    mapping: Optional[ButtonMapping]
    error_code: Optional[int]
    bytes_written: Optional[int]
    payload_hex: Optional[str]
    elapsed_ms: int


@dataclass
class SetBackPaddleBindingResult:
    outcome: SetBackPaddleBindingOutcome
    slot: Optional[MacroSlot]
    binding: Optional[BackPaddleBinding]
    error_code: Optional[int]
    bytes_written: Optional[int]
    payload_hex: Optional[str]
    elapsed_ms: int


@dataclass
class SetDeadzoneResult:
    outcome: SetDeadzoneOutcome
    deadzones: Optional[StickDeadzones]
    error_code: Optional[int]
    bytes_written: Optional[int]
    payload_hex: Optional[str]
    elapsed_ms: int


@dataclass
class SetStepSizeResult:
    outcome: SetStepSizeOutcome
    value: Optional[int]
    error_code: Optional[int]
    bytes_written: Optional[int]
    payload_hex: Optional[str]
    elapsed_ms: int


SensitivityAnchorTuple = tuple[
    SensitivityAnchor,
    SensitivityAnchor,
    SensitivityAnchor,
]

# Dormant 8-point curve type used only by the (unwired) 1.2.9 0x86 builder.
SensitivityAnchorTuple8 = tuple[
    SensitivityAnchor,
    SensitivityAnchor,
    SensitivityAnchor,
    SensitivityAnchor,
    SensitivityAnchor,
    SensitivityAnchor,
    SensitivityAnchor,
    SensitivityAnchor,
]


@dataclass
class SetSensitivityCurveResult:
    # ``anchors`` carries 3 points for the legacy 0x06 writer and 8 for the
    # 1.2.9 0x86 writer; the result shape is otherwise identical for both.
    outcome: SetSensitivityCurveOutcome
    anchors: Optional[SensitivityAnchorTuple | SensitivityAnchorTuple8]
    error_code: Optional[int]
    bytes_written: Optional[int]
    payload_hex: Optional[str]
    elapsed_ms: int


@dataclass(frozen=True)
class ControllerSnapshot:
    """All controller settings read in one operation for UI hydration."""

    polling_rate: Optional[PollingRate]
    vibration: Optional[VibrationSettings]
    deadzones: Optional[StickDeadzones]
    axis_inversion_left: Optional[AxisInversion]
    axis_inversion_right: Optional[AxisInversion]
    sensitivity_left: Optional[SensitivityAnchorTuple]
    sensitivity_right: Optional[SensitivityAnchorTuple]
    # 1.2.9 / fw-1.24 8-point curves (HID cat 0x86). Carried alongside the
    # 3-point fields above rather than replacing them: both are read straight
    # from the device, so the apply path always has a valid 3-point fallback.
    # Populated by get_all_settings only on devices that pass the cat-0x86
    # capability probe; ``None`` everywhere else. Declared with defaults near
    # the end of the dataclass (see below) to keep the constructor's
    # non-default field order stable for existing positional callers.
    trigger_left: Optional[TriggerSettings]
    trigger_right: Optional[TriggerSettings]
    button_bindings: dict[ButtonSlot, ButtonMapping]
    lighting_zones: dict[LightingZone, LightingSettings]
    motion_settings: Optional[MotionSettings] = None
    back_paddle_bindings: dict[MacroSlot, BackPaddleBinding] = field(default_factory=dict)
    step_size: Optional[int] = None
    sensitivity_left_8point: Optional[SensitivityAnchorTuple8] = None
    sensitivity_right_8point: Optional[SensitivityAnchorTuple8] = None


@dataclass
class SetAxisInversionResult:
    outcome: SetAxisInversionOutcome
    stick: Optional[str]
    inversion: Optional[AxisInversion]
    error_code: Optional[int]
    bytes_written: Optional[int]
    payload_hex: Optional[str]
    elapsed_ms: int


@dataclass
class SetTriggerSettingsResult:
    outcome: SetTriggerSettingsOutcome
    trigger: Optional[str]
    settings: Optional[TriggerSettings]
    error_code: Optional[int]
    bytes_written: Optional[int]
    payload_hex: Optional[str]
    elapsed_ms: int


@dataclass
class SetVibrationResult:
    outcome: SetVibrationOutcome
    settings: Optional[VibrationSettings]
    error_code: Optional[int]
    bytes_written: Optional[int]
    payload_hex: Optional[str]
    elapsed_ms: int


@dataclass
class SetLightingResult:
    outcome: SetLightingOutcome
    zone: Optional[str]
    settings: Optional[LightingSettings]
    error_code: Optional[int]
    bytes_written: Optional[int]
    payload_hex: Optional[str]
    elapsed_ms: int


@dataclass
class _EnsureHandleResult:
    outcome: WriteOutcome
    handle: Optional[int] = None
    target_path: Optional[str] = None
    error_code: Optional[int] = None


@dataclass(frozen=True)
class _WritePayloadResult:
    ok: bool
    err: int
    bytes_written: int
    retried: bool = False


class SettingsServiceError(RuntimeError):
    """Raised for read-from-controller protocol failures."""

    def __init__(self, message: str, *, win32_error: int | None = None):
        super().__init__(message)
        self.win32_error = win32_error


def _win32_error_from_exception(exc: BaseException) -> int | None:
    explicit = getattr(exc, "win32_error", None)
    if isinstance(explicit, int):
        return explicit

    marker = "Win32 err "
    text = str(exc)
    marker_index = text.find(marker)
    if marker_index < 0:
        return None

    digits_start = marker_index + len(marker)
    digits = []
    for char in text[digits_start:]:
        if not char.isdigit():
            break
        digits.append(char)
    if not digits:
        return None
    return int("".join(digits))


def _is_disconnect_win32_error(error_code: int | None) -> bool:
    return error_code in DISCONNECT_WIN32_ERRORS


# ---------------------------------------------------------------------------
# Path enumeration + low-level helpers
# ---------------------------------------------------------------------------

def _parse_guid_string(guid_str: str) -> _GUID:
    s = guid_str.strip().lstrip("{").rstrip("}")
    parts = s.split("-")
    if len(parts) != 5:
        raise ValueError(f"Invalid GUID format: {guid_str!r}")
    data4_bytes = bytes.fromhex(parts[3] + parts[4])
    if len(data4_bytes) != 8:
        raise ValueError(f"Invalid GUID Data4 length: {guid_str!r}")
    guid = _GUID()
    guid.Data1 = int(parts[0], 16)
    guid.Data2 = int(parts[1], 16)
    guid.Data3 = int(parts[2], 16)
    for i, byte in enumerate(data4_bytes):
        guid.Data4[i] = byte
    return guid


def enumerate_mi02_hid_paths(
    *,
    vendor_id: int = PUBLIC_VENDOR_ID,
    product_id: int = PUBLIC_PRODUCT_ID,
) -> list[str]:
    """Return HID interface paths matching ``VID_413D&PID_2104&MI_02``."""

    try:
        setupapi = _Win32.setupapi()
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover - non-Windows fallback
        return []

    interface_guid = _parse_guid_string(MI02_DEVICE_INTERFACE_GUID)
    info_set = setupapi.SetupDiGetClassDevsW(
        c.byref(interface_guid),
        None,
        None,
        DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
    )
    if not info_set:
        return []

    paths: list[str] = []
    try:
        index = 0
        while True:
            interface_data = _SP_DEVICE_INTERFACE_DATA()
            interface_data.cbSize = c.sizeof(_SP_DEVICE_INTERFACE_DATA)
            ok = bool(
                setupapi.SetupDiEnumDeviceInterfaces(
                    info_set,
                    None,
                    c.byref(interface_guid),
                    index,
                    c.byref(interface_data),
                )
            )
            if not ok:
                if kernel32.GetLastError() == ERROR_NO_MORE_ITEMS:
                    break
                break

            required_size = w.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                info_set,
                c.byref(interface_data),
                None,
                0,
                c.byref(required_size),
                None,
            )
            if not required_size.value:
                index += 1
                continue

            detail = c.create_string_buffer(required_size.value)
            cb_size = 8 if c.sizeof(c.c_void_p) == 8 else 6
            c.cast(detail, c.POINTER(w.DWORD))[0] = cb_size
            ok = bool(
                setupapi.SetupDiGetDeviceInterfaceDetailW(
                    info_set,
                    c.byref(interface_data),
                    detail,
                    required_size,
                    None,
                    None,
                )
            )
            if ok:
                path = c.wstring_at(c.addressof(detail) + c.sizeof(w.DWORD))
                low = path.lower()
                needle = f"vid_{vendor_id:04x}&pid_{product_id:04x}&mi_02"
                if needle in low:
                    paths.append(path)
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info_set)
    return paths


def _choose_mi02_path(paths: list[str]) -> Optional[str]:
    if not paths:
        return None
    for path in paths:
        if path.lower().startswith(r"\\?\hid#"):
            return path
    return paths[0]


def _default_open_write_handle(path: str) -> tuple[Optional[int], int]:
    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover - non-Windows fallback
        return None, 0
    handle = kernel32.CreateFileW(
        path,
        MI02_WRITE_OPEN_DESIRED_ACCESS,
        MI02_WRITE_OPEN_SHARE_MODE,
        None,
        OPEN_EXISTING,
        MI02_WRITE_OPEN_FLAGS_AND_ATTRIBUTES,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or not handle:
        return None, kernel32.GetLastError()
    return int(handle), 0


def _default_open_read_write_handle(path: str) -> tuple[Optional[int], int]:
    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover - non-Windows fallback
        return None, 0
    handle = kernel32.CreateFileW(
        path,
        MI02_READ_WRITE_OPEN_DESIRED_ACCESS,
        MI02_READ_WRITE_OPEN_SHARE_MODE,
        None,
        OPEN_EXISTING,
        MI02_READ_WRITE_OPEN_FLAGS_AND_ATTRIBUTES,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or not handle:
        return None, kernel32.GetLastError()
    return int(handle), 0


def _default_write_file(handle: int, payload: bytes) -> tuple[bool, int, int]:
    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover - non-Windows fallback
        return False, 0, 0
    buf = c.create_string_buffer(payload)
    bytes_written = w.DWORD(0)
    ok = bool(
        kernel32.WriteFile(
            handle,
            c.cast(buf, c.c_void_p),
            len(payload),
            c.byref(bytes_written),
            None,
        )
    )
    return ok, 0 if ok else kernel32.GetLastError(), int(bytes_written.value)


def _default_read_file(handle: int, length: int, timeout_ms: int) -> bytes:
    """Read up to ``length`` bytes from a synchronous HID handle with a timeout.

    The handle is opened synchronously (no FILE_FLAG_OVERLAPPED), so plain
    ``kernel32.ReadFile`` has no native timeout — it blocks until the device
    responds. When the device is wedged (e.g. odd HID state after a hot-replug)
    this hangs the entire startup flow because ``refresh_from_controller``
    runs synchronously before ``dpg.create_viewport``.

    Args:
        handle: kernel32 HANDLE opened by ``_default_open_read_write_handle``.
        length: maximum bytes to read (a full feature report is 65 bytes;
            ``_read_payload`` always passes ``HID_FEATURE_REPORT_SIZE``).
        timeout_ms: per-call deadline. ``<=0`` falls back to
            ``READ_FILE_TIMEOUT_MS_DEFAULT`` (1500 ms). Callers in
            ``_read_response`` pass the remaining budget against the
            overall ``READ_RESPONSE_TIMEOUT_MS`` window.

    Strategy: run ``kernel32.ReadFile`` on a worker thread; the main thread
    joins with the caller-supplied ``timeout_ms``. If the join times out we
    call ``kernel32.CancelIoEx(handle, None)`` to unblock the worker, then
    raise ``TimeoutError``. The worker eventually wakes up and exits (its
    result is discarded). Subsequent calls into ``_default_read_file`` get
    a fresh worker; the handle remains usable for retries even after a
    cancelled read.

    How callers should handle the exception:
        Most callers run via ``_read_response`` which already has its own
        retry-on-mismatch loop and a ``SettingsServiceError`` wrapper, so
        a ``TimeoutError`` propagates up to whichever public method was
        invoked (``get_polling_rate``, ``get_all_settings``, etc.). The
        startup-flow caller ``AppShell.refresh_from_controller`` wraps
        ``settings_service.get_all_settings()`` in a broad ``except
        Exception`` (intentional — see the comment at app_shell.py:839)
        and surfaces the failure as a localized status string + log
        entry, then proceeds to viewport creation. New callers should
        either propagate, retry with backoff, or catch + report.

    Raises:
        TimeoutError: device did not respond within ``timeout_ms``.
        SettingsServiceError: ReadFile returned an error or a short read.
    """

    if timeout_ms is None or timeout_ms <= 0:
        timeout_ms = READ_FILE_TIMEOUT_MS_DEFAULT

    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError) as exc:  # pragma: no cover - non-Windows fallback
        raise SettingsServiceError("ReadFile unavailable") from exc

    buf = (c.c_ubyte * length)()
    bytes_read = w.DWORD(0)
    result: list[tuple[bool, int]] = []
    error_box: list[BaseException] = []

    def _reader() -> None:
        try:
            ok = bool(
                kernel32.ReadFile(
                    handle,
                    c.cast(buf, c.c_void_p),
                    length,
                    c.byref(bytes_read),
                    None,
                )
            )
            result.append((ok, 0 if ok else kernel32.GetLastError()))
        except BaseException as exc:  # noqa: BLE001 - re-raised on main thread
            error_box.append(exc)

    reader = threading.Thread(target=_reader, daemon=True, name="hid-read-bounded")
    reader.start()
    reader.join(timeout=timeout_ms / 1000.0)
    if reader.is_alive():
        # Worker is still blocked inside kernel32.ReadFile. CancelIoEx on the
        # shared handle unblocks the synchronous I/O; the worker will exit
        # shortly after and we discard its result.
        try:
            kernel32.CancelIoEx(handle, None)
        except OSError:
            pass
        raise TimeoutError(f"HID read timed out after {timeout_ms}ms")

    if error_box:
        raise error_box[0]
    if not result:  # pragma: no cover - reader thread always populates one of the two
        raise SettingsServiceError("ReadFile reader produced no result")
    ok, err = result[0]
    if not ok:
        raise SettingsServiceError(
            f"ReadFile failed (Win32 err {err})",
            win32_error=err,
        )
    if bytes_read.value < 5:
        raise SettingsServiceError(f"ReadFile returned only {bytes_read.value} bytes")
    return bytes(buf[: bytes_read.value])


def _default_close_handle(handle: int) -> bool:
    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover - non-Windows fallback
        return False
    return bool(kernel32.CloseHandle(handle))


# ---------------------------------------------------------------------------
# Protocol helpers + service
# ---------------------------------------------------------------------------

def build_polling_rate_payload(rate: PollingRate) -> bytes:
    """Build the 65-byte feature-report frame for a confirmed polling rate."""

    if not isinstance(rate, PollingRate) or rate not in SUPPORTED_POLLING_RATES:
        raise ValueError(f"Unsupported polling rate: {rate!r}")
    payload = bytearray(HID_FEATURE_REPORT_SIZE)
    payload[0] = HID_REPORT_ID
    payload[1:5] = MAGIC_WRITE_PREFIX
    payload[5] = CATEGORY_POLLING_RATE
    payload[6] = POLLING_RATE_SUBCOMMAND
    payload[7] = rate.value
    payload[8:10] = POLLING_RATE_SUFFIX
    return bytes(payload)


def build_step_size_payload(value: int) -> bytes:
    """Build the 65-byte feature-report frame for the joystick step-size byte.

    The vendor app's HID traffic for this write breaks down as:
    cat 0x0D, sub 0x00, value byte at offset 7, LE 16-bit echo of the same value
    at offsets 30-31. Vendor app's inline help reads "smaller value = more
    precise stick response" — semantic is stick output quantization.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"step_size must be int; got {type(value).__name__}")
    if value < STEP_SIZE_VALUE_MIN or value > STEP_SIZE_VALUE_MAX:
        raise ValueError(
            f"step_size must be in [{STEP_SIZE_VALUE_MIN}, {STEP_SIZE_VALUE_MAX}]; got {value}"
        )
    payload = bytearray(HID_FEATURE_REPORT_SIZE)
    payload[0] = HID_REPORT_ID
    payload[1:5] = MAGIC_WRITE_PREFIX
    payload[5] = CATEGORY_STEP_SIZE
    payload[6] = STEP_SIZE_SUBCOMMAND
    payload[7] = value
    payload[30:32] = value.to_bytes(2, "little")
    return bytes(payload)


def build_read_query_payload(category: int, sub_cmd: int = 0x00, selector: int = 0x00) -> bytes:
    """Build a 65-byte read-magic query frame."""

    category = _validate_byte_int(category, name="category")
    sub_cmd = _validate_byte_int(sub_cmd, name="sub_cmd")
    selector = _validate_byte_int(selector, name="selector")
    payload = bytearray(HID_FEATURE_REPORT_SIZE)
    payload[0] = HID_REPORT_ID
    payload[1:5] = MAGIC_READ_QUERY_PREFIX
    payload[5] = category
    payload[6] = sub_cmd
    payload[7] = selector
    return bytes(payload)


def build_button_binding_payload(slot: ButtonSlot, mapping: ButtonMapping) -> bytes:
    """Build a 65-byte feature-report frame for confirmed button remaps."""

    if not isinstance(slot, ButtonSlot) or slot not in SUPPORTED_BUTTON_SLOTS:
        raise NotImplementedError(f"Unsupported button slot: {slot!r}")
    if not isinstance(mapping, ButtonMapping):
        raise NotImplementedError(f"Unsupported button mapping: {mapping!r}")
    if mapping.target_kind != 0x01:
        raise NotImplementedError(f"Unsupported button mapping target kind: {mapping!r}")
    if mapping.target_low != 0x00:
        raise NotImplementedError(f"Unsupported button mapping target low byte: {mapping!r}")
    if mapping.target_value not in {target.value for target in SUPPORTED_CONTROLLER_BUTTON_TARGETS}:
        raise NotImplementedError(f"Unsupported button mapping target value: {mapping!r}")

    payload = bytearray(HID_FEATURE_REPORT_SIZE)
    payload[0] = HID_REPORT_ID
    payload[1:5] = MAGIC_WRITE_PREFIX
    payload[5] = CATEGORY_BUTTON_BINDING
    payload[6] = BUTTON_BINDING_SUBCOMMAND
    payload[7] = slot.value
    payload[8] = mapping.target_kind
    payload[9] = mapping.target_low
    payload[10] = mapping.target_value
    payload[11] = 0x00
    return bytes(payload)


def _validate_back_paddle_target(
    target: Optional[ControllerButtonTarget],
) -> Optional[ControllerButtonTarget]:
    if target is None:
        return None
    if not isinstance(target, ControllerButtonTarget):
        raise NotImplementedError(f"Unsupported back-paddle target: {target!r}")
    if target not in SUPPORTED_CONTROLLER_BUTTON_TARGETS:
        raise NotImplementedError(f"Unsupported back-paddle target: {target!r}")
    return target


def _new_back_paddle_payload(slot: MacroSlot) -> bytearray:
    if not isinstance(slot, MacroSlot) or slot not in SUPPORTED_MACRO_SLOTS:
        raise NotImplementedError(f"Unsupported back-paddle slot: {slot!r}")
    payload = bytearray(HID_FEATURE_REPORT_SIZE)
    payload[0] = HID_REPORT_ID
    payload[1:5] = MAGIC_WRITE_PREFIX
    payload[5] = CATEGORY_BACK_PADDLE_BINDING
    payload[6] = BACK_PADDLE_SUBCOMMAND
    payload[8] = slot.value
    payload[48] = slot.value
    return payload


def build_back_paddle_binding_payloads(
    slot: MacroSlot,
    target: Optional[ControllerButtonTarget],
) -> tuple[bytes, ...]:
    """Build the minimal decoded category-0x05 write set for one back paddle."""

    validated_target = _validate_back_paddle_target(target)
    if validated_target is None:
        payload = _new_back_paddle_payload(slot)
        payload[9] = BACK_PADDLE_EVENT_INDEX_BINDING
        payload[15] = BACK_PADDLE_UNBOUND_FLAG
        return (bytes(payload),)

    binding = _new_back_paddle_payload(slot)
    binding[9] = BACK_PADDLE_EVENT_INDEX_BINDING
    binding[10] = validated_target.value
    binding[12] = BACK_PADDLE_DEFAULT_DURATION_MS
    binding[14] = BACK_PADDLE_DEFAULT_CYCLE_MS
    binding[49] = validated_target.value
    binding[57] = BACK_PADDLE_DEFAULT_DURATION_MS

    terminator = _new_back_paddle_payload(slot)
    terminator[9] = BACK_PADDLE_EVENT_INDEX_TERMINATOR
    terminator[45] = BACK_PADDLE_EVENT_INDEX_TERMINATOR
    return (bytes(binding), bytes(terminator))


def _validate_percent_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int in [0, 100], got {value!r}")
    if value < 0 or value > 100:
        raise ValueError(f"{name} must be in [0, 100], got {value!r}")
    return value


def _validate_stick_deadzones(deadzones: object) -> StickDeadzones:
    if not isinstance(deadzones, StickDeadzones):
        raise TypeError(f"deadzones must be a StickDeadzones instance, got {deadzones!r}")
    _validate_percent_int(deadzones.left_center, name="left_center")
    _validate_percent_int(deadzones.right_center, name="right_center")
    _validate_percent_int(deadzones.left_outer, name="left_outer")
    _validate_percent_int(deadzones.right_outer, name="right_outer")
    return deadzones


def build_all_deadzones_payload(deadzones: StickDeadzones) -> bytes:
    """Build the captured all-stick-deadzones feature-report frame."""

    validated = _validate_stick_deadzones(deadzones)
    payload = bytearray(HID_FEATURE_REPORT_SIZE)
    payload[0] = HID_REPORT_ID
    payload[1:5] = MAGIC_WRITE_PREFIX
    payload[5] = CATEGORY_STICK_DEADZONE
    payload[6] = DEADZONE_SUBCOMMAND
    payload[7] = validated.left_center
    payload[8] = validated.right_center
    payload[9] = validated.left_outer
    payload[10] = validated.right_outer
    return bytes(payload)


def _validate_sensitivity_anchors(anchors: object) -> SensitivityAnchorTuple:
    if not isinstance(anchors, tuple):
        raise TypeError("anchors must be a tuple of exactly 3 SensitivityAnchor values")
    if len(anchors) != SENSITIVITY_ANCHOR_COUNT:
        raise TypeError("anchors must be a tuple of exactly 3 SensitivityAnchor values")
    for idx, anchor in enumerate(anchors):
        if not isinstance(anchor, SensitivityAnchor):
            raise TypeError(
                "anchors must be a tuple of exactly 3 SensitivityAnchor values; "
                f"item {idx} was {anchor!r}"
            )
        _validate_percent_int(anchor.x, name=f"anchors[{idx}].x")
        _validate_percent_int(anchor.y, name=f"anchors[{idx}].y")
    return anchors


def build_sensitivity_curve_payload(
    stick: int,
    anchors: SensitivityAnchorTuple,
) -> bytes:
    """Build a captured sensitivity-curve feature-report frame."""

    if not isinstance(stick, int) or isinstance(stick, bool):
        raise TypeError(f"stick selector must be an int, got {stick!r}")
    if stick not in (SENSITIVITY_STICK_LEFT, SENSITIVITY_STICK_RIGHT):
        raise NotImplementedError(f"Unsupported sensitivity stick selector: {stick!r}")
    validated = _validate_sensitivity_anchors(anchors)
    payload = bytearray(HID_FEATURE_REPORT_SIZE)
    payload[0] = HID_REPORT_ID
    payload[1:5] = MAGIC_WRITE_PREFIX
    payload[5] = CATEGORY_STICK_SENSITIVITY
    payload[6] = SENSITIVITY_SUBCOMMAND
    payload[7] = stick
    for i, anchor in enumerate(validated):
        payload[8 + (i * 2)] = anchor.x
        payload[9 + (i * 2)] = anchor.y
    return bytes(payload)


def build_left_stick_sensitivity_curve_payload(
    anchors: SensitivityAnchorTuple,
) -> bytes:
    """Build the captured left-stick sensitivity-curve feature-report frame."""

    return build_sensitivity_curve_payload(SENSITIVITY_STICK_LEFT, anchors)


def build_right_stick_sensitivity_curve_payload(
    anchors: SensitivityAnchorTuple,
) -> bytes:
    """Build the captured right-stick sensitivity-curve feature-report frame."""

    return build_sensitivity_curve_payload(SENSITIVITY_STICK_RIGHT, anchors)


def _validate_sensitivity_anchors_8point(anchors: object) -> SensitivityAnchorTuple8:
    if not isinstance(anchors, tuple):
        raise TypeError("anchors must be a tuple of exactly 8 SensitivityAnchor values")
    if len(anchors) != SENSITIVITY_ANCHOR_COUNT_8POINT:
        raise TypeError("anchors must be a tuple of exactly 8 SensitivityAnchor values")
    for idx, anchor in enumerate(anchors):
        if not isinstance(anchor, SensitivityAnchor):
            raise TypeError(
                "anchors must be a tuple of exactly 8 SensitivityAnchor values; "
                f"item {idx} was {anchor!r}"
            )
        _validate_percent_int(anchor.x, name=f"anchors[{idx}].x")
        _validate_percent_int(anchor.y, name=f"anchors[{idx}].y")
        if idx > 0:
            previous = anchors[idx - 1]
            if anchor.x < previous.x:
                raise ValueError(
                    f"anchors must be non-decreasing in x; anchors[{idx}].x "
                    f"({anchor.x}) < anchors[{idx - 1}].x ({previous.x})"
                )
            if anchor.y < previous.y:
                raise ValueError(
                    f"anchors must be non-decreasing in y; anchors[{idx}].y "
                    f"({anchor.y}) < anchors[{idx - 1}].y ({previous.y})"
                )
    return anchors


def build_sensitivity_curve_payload_8point(
    stick: int,
    anchors: SensitivityAnchorTuple8,
) -> bytes:
    """Build a dormant 8-point (category 0x86) sensitivity-curve frame.

    Mirrors :func:`build_sensitivity_curve_payload` but emits the 1.2.9
    8-point layout: ``b5 = CATEGORY_STICK_SENSITIVITY_8POINT`` and eight
    interleaved ``(X, Y)`` pairs at ``b8..b23`` (``b7`` is the stick selector).

    This is dormant capability. Firmware *acceptance* of the 0x86 category is
    unverified (writes succeed at the HID layer but readback was blocked),
    so no ``SettingsService`` method, apply
    coordinator, or Restore-Point path calls it. The legacy 3-point 0x06 path
    remains the sole active sensitivity writer.
    """

    if not isinstance(stick, int) or isinstance(stick, bool):
        raise TypeError(f"stick selector must be an int, got {stick!r}")
    if stick not in (SENSITIVITY_STICK_LEFT, SENSITIVITY_STICK_RIGHT):
        raise NotImplementedError(f"Unsupported sensitivity stick selector: {stick!r}")
    validated = _validate_sensitivity_anchors_8point(anchors)
    payload = bytearray(HID_FEATURE_REPORT_SIZE)
    payload[0] = HID_REPORT_ID
    payload[1:5] = MAGIC_WRITE_PREFIX
    payload[5] = CATEGORY_STICK_SENSITIVITY_8POINT
    payload[6] = SENSITIVITY_SUBCOMMAND
    payload[7] = stick
    for i, anchor in enumerate(validated):
        payload[8 + (i * 2)] = anchor.x
        payload[9 + (i * 2)] = anchor.y
    return bytes(payload)


def build_left_stick_sensitivity_curve_payload_8point(
    anchors: SensitivityAnchorTuple8,
) -> bytes:
    """Build the dormant left-stick 8-point sensitivity-curve frame."""

    return build_sensitivity_curve_payload_8point(SENSITIVITY_STICK_LEFT, anchors)


def build_right_stick_sensitivity_curve_payload_8point(
    anchors: SensitivityAnchorTuple8,
) -> bytes:
    """Build the dormant right-stick 8-point sensitivity-curve frame."""

    return build_sensitivity_curve_payload_8point(SENSITIVITY_STICK_RIGHT, anchors)


def _decode_sensitivity_curve_8point(
    response: bytes,
) -> Optional[SensitivityAnchorTuple8]:
    """Decode + strictly validate eight ``(X, Y)`` anchors from a cat-0x86 read.

    ``response`` is the raw buffer ``SettingsService._read_response`` returned:
    report ID at ``[0]``, read magic at ``[1:5]``, category at ``[5]``, the
    stick selector at ``[7]``, and the eight pairs at ``[8..23]``. That layout
    is identical to the 0x86 *write* payload because Windows prepends the
    report-ID byte to the 64-byte device frame (``30 55 aa d0 86 00 <stick>
    X1 Y1 ... X8 Y8``), shifting every wire byte up by one.

    Returns ``None`` (never raises) on a short buffer, an out-of-range value,
    or a non-monotonic curve, so callers degrade to the legacy 3-point path.
    Validation mirrors :func:`_validate_sensitivity_anchors_8point`
    (non-decreasing in both axes, every value in ``[0, 100]``); the strictness
    is deliberate — it keeps the capability probe from false-positiving on a
    device that does not actually speak 0x86.
    """

    pairs_start = 8
    needed = pairs_start + SENSITIVITY_ANCHOR_COUNT_8POINT * 2
    if len(response) < needed:
        logger.warning(
            "8-point sensitivity response too short: %d bytes (need >= %d)",
            len(response),
            needed,
        )
        return None

    anchors: list[SensitivityAnchor] = []
    for i in range(SENSITIVITY_ANCHOR_COUNT_8POINT):
        x = response[pairs_start + i * 2]
        y = response[pairs_start + i * 2 + 1]
        if not (SENSITIVITY_VALUE_MIN <= x <= SENSITIVITY_VALUE_MAX):
            logger.warning("8-point sensitivity x out of range at anchor %d: %d", i, x)
            return None
        if not (SENSITIVITY_VALUE_MIN <= y <= SENSITIVITY_VALUE_MAX):
            logger.warning("8-point sensitivity y out of range at anchor %d: %d", i, y)
            return None
        if anchors and (x < anchors[-1].x or y < anchors[-1].y):
            logger.warning(
                "8-point sensitivity not monotonic at anchor %d: (%d, %d) < (%d, %d)",
                i,
                x,
                y,
                anchors[-1].x,
                anchors[-1].y,
            )
            return None
        anchors.append(SensitivityAnchor(x=x, y=y))
    return tuple(anchors)  # type: ignore[return-value]


def _validate_axis_inversion(inversion: object) -> AxisInversion:
    if not isinstance(inversion, AxisInversion):
        raise TypeError(f"inversion must be an AxisInversion instance, got {inversion!r}")
    return inversion


def build_axis_inversion_payload(
    stick_selector: int,
    inversion: AxisInversion,
) -> bytes:
    """Build the captured per-stick axis-inversion feature-report frame."""

    if not isinstance(stick_selector, int) or isinstance(stick_selector, bool):
        raise TypeError(f"stick selector must be an int, got {stick_selector!r}")
    if stick_selector not in (INVERSION_STICK_LEFT, INVERSION_STICK_RIGHT):
        raise NotImplementedError(f"Unsupported inversion stick selector: {stick_selector!r}")
    validated = _validate_axis_inversion(inversion)
    payload = bytearray(HID_FEATURE_REPORT_SIZE)
    payload[0] = HID_REPORT_ID
    payload[1:5] = MAGIC_WRITE_PREFIX
    payload[5] = CATEGORY_STICK_INVERSION
    payload[6] = INVERSION_SUBCOMMAND
    payload[7] = stick_selector
    payload[8] = 0x01 if validated.x_inverted else 0x00
    payload[9] = 0x01 if validated.y_inverted else 0x00
    return bytes(payload)


def _validate_trigger_settings(settings: object) -> TriggerSettings:
    if not isinstance(settings, TriggerSettings):
        raise TypeError(f"settings must be a TriggerSettings instance, got {settings!r}")
    range_min = _validate_percent_int(settings.range_min, name="range_min")
    range_max = _validate_percent_int(settings.range_max, name="range_max")
    if range_min > range_max:
        raise ValueError(
            f"range_min must be less than or equal to range_max, got {range_min} > {range_max}"
        )
    if not isinstance(settings.mode, TriggerMode):
        raise TypeError(f"mode must be a TriggerMode value, got {settings.mode!r}")
    return settings


def build_trigger_settings_payload(
    trigger_selector: int,
    settings: TriggerSettings,
) -> bytes:
    """Build the captured per-trigger settings feature-report frame."""

    if not isinstance(trigger_selector, int) or isinstance(trigger_selector, bool):
        raise TypeError(f"trigger selector must be an int, got {trigger_selector!r}")
    if trigger_selector not in (TRIGGER_SELECTOR_LEFT, TRIGGER_SELECTOR_RIGHT):
        raise NotImplementedError(f"Unsupported trigger selector: {trigger_selector!r}")
    validated = _validate_trigger_settings(settings)
    payload = bytearray(HID_FEATURE_REPORT_SIZE)
    payload[0] = HID_REPORT_ID
    payload[1:5] = MAGIC_WRITE_PREFIX
    payload[5] = CATEGORY_TRIGGER_SETTINGS
    payload[6] = TRIGGER_SUBCOMMAND
    payload[7] = trigger_selector
    payload[8] = validated.range_min
    payload[9] = validated.range_max
    payload[10] = validated.mode.value
    return bytes(payload)


def _validate_vibration_settings(settings: object) -> VibrationSettings:
    if not isinstance(settings, VibrationSettings):
        raise TypeError(f"settings must be a VibrationSettings instance, got {settings!r}")
    _validate_percent_int(settings.left_grip_strength, name="left_grip_strength")
    _validate_percent_int(settings.right_grip_strength, name="right_grip_strength")
    _validate_percent_int(
        settings.left_trigger_motor_strength,
        name="left_trigger_motor_strength",
    )
    _validate_percent_int(
        settings.right_trigger_motor_strength,
        name="right_trigger_motor_strength",
    )
    if not isinstance(settings.mode, TriggerVibrationMode):
        raise TypeError(f"mode must be a TriggerVibrationMode value, got {settings.mode!r}")
    return settings


def build_vibration_payload(settings: VibrationSettings) -> bytes:
    """Build the captured all-vibration-settings feature-report frame."""

    validated = _validate_vibration_settings(settings)
    payload = bytearray(HID_FEATURE_REPORT_SIZE)
    payload[0] = HID_REPORT_ID
    payload[1:5] = MAGIC_WRITE_PREFIX
    payload[5] = CATEGORY_VIBRATION
    payload[6] = VIBRATION_SUBCOMMAND
    payload[7] = validated.left_grip_strength
    payload[8] = validated.right_grip_strength
    payload[9] = validated.left_trigger_motor_strength
    payload[10] = validated.right_trigger_motor_strength
    payload[11] = validated.mode.value
    return bytes(payload)


def _validate_byte_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int in [0, 255], got {value!r}")
    if value < 0 or value > 255:
        raise ValueError(f"{name} must be in [0, 255], got {value!r}")
    return value


def _validate_lighting_settings(settings: object) -> LightingSettings:
    if not isinstance(settings, LightingSettings):
        raise TypeError(f"settings must be a LightingSettings instance, got {settings!r}")
    if type(settings.light_on) is not bool:
        raise TypeError(f"light_on must be a bool, got {settings.light_on!r}")
    if not isinstance(settings.mode, LightingMode):
        raise TypeError(f"mode must be a LightingMode value, got {settings.mode!r}")
    _validate_byte_int(settings.brightness_byte, name="brightness_byte")
    if not isinstance(settings.color, RgbColor):
        raise TypeError(f"color must be an RgbColor instance, got {settings.color!r}")
    _validate_byte_int(settings.color.r, name="r")
    _validate_byte_int(settings.color.g, name="g")
    _validate_byte_int(settings.color.b, name="b")
    return settings


def build_lighting_payload(zone: LightingZone, settings: LightingSettings) -> bytes:
    """Build the captured per-zone lighting feature-report frame.

    ``brightness_byte`` is the raw 0-255 byte captured on the wire. UI percent
    conversion is intentionally left to callers because vendor rounding is not
    fully characterized.
    """

    if not isinstance(zone, LightingZone):
        raise TypeError(f"zone must be a LightingZone value, got {zone!r}")
    validated = _validate_lighting_settings(settings)
    payload = bytearray(HID_FEATURE_REPORT_SIZE)
    payload[0] = HID_REPORT_ID
    payload[1:5] = MAGIC_WRITE_PREFIX
    payload[5] = CATEGORY_LIGHTING
    payload[6] = LIGHTING_SUBCOMMAND
    payload[7] = zone.value
    payload[8] = 0x01 if validated.light_on else 0x00
    payload[9] = validated.mode.value
    payload[10] = validated.brightness_byte
    payload[11] = validated.color.r
    payload[12] = validated.color.g
    payload[13] = validated.color.b
    return bytes(payload)


class SettingsService:
    """One-shot settings writer for the stock VID_413D MI_02 HID interface."""

    def __init__(
        self,
        *,
        enumerate_paths: Callable[[], list[str]] = enumerate_mi02_hid_paths,
        open_write_handle: Callable[[str], tuple[Optional[int], int]] = (
            _default_open_write_handle
        ),
        open_read_write_handle: Callable[[str], tuple[Optional[int], int]] = (
            _default_open_read_write_handle
        ),
        write_file: Callable[[int, bytes], tuple[bool, int, int]] = _default_write_file,
        read_file: Callable[[int, int, int], bytes] = _default_read_file,
        close_handle: Callable[[int], bool] = _default_close_handle,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        batch_read_budget_s: float = 8.0,
    ):
        self._enumerate_paths = enumerate_paths
        self._open_write_handle = open_write_handle
        self._open_read_write_handle = open_read_write_handle
        self._write_file = write_file
        self._read_file = read_file
        self._close_handle = close_handle
        self._clock = clock
        self._sleep = sleep
        # Wall-clock cap for one full get_all_settings batch. Per-read timeouts
        # only bound each getter (~READ_RESPONSE_TIMEOUT_MS); a device that
        # keeps answering with mismatched frames makes every getter burn its
        # full deadline, and the ~30-getter batch (button slots retry up to
        # 3x each) runs synchronously on the UI thread — minutes of freeze
        # without a batch-level cap. 0 or negative disables the budget.
        self._batch_read_budget_s = batch_read_budget_s

        self._write_handle: Optional[int] = None
        self._read_write_handle: Optional[int] = None
        self._target_path: Optional[str] = None
        self._last_open_error: Optional[int] = None
        # Cached 8-point (cat 0x86) capability verdict for the current HID
        # connection. None = not yet probed; reset whenever handles are
        # dropped (stop / disconnect) so a different controller re-probes.
        self._supports_8point: Optional[bool] = None

    @property
    def target_path(self) -> Optional[str]:
        return self._target_path

    @property
    def is_started(self) -> bool:
        return self._write_handle is not None or self._read_write_handle is not None

    def start(self) -> SetPollingRateOutcome:
        """Open and cache the MI_02 write handle if needed."""

        return self._ensure_handle().outcome

    def stop(self) -> None:
        """Close cached MI_02 handles if open."""

        # Capability is per-connection; clear it so the next controller re-probes.
        self._supports_8point = None
        if self._write_handle is None and self._read_write_handle is None:
            self._target_path = None
            self._last_open_error = None
            return
        handles = []
        if self._write_handle is not None:
            handles.append(self._write_handle)
        if (
            self._read_write_handle is not None
            and self._read_write_handle not in handles
        ):
            handles.append(self._read_write_handle)
        self._write_handle = None
        self._read_write_handle = None
        self._target_path = None
        self._last_open_error = None
        for handle in handles:
            try:
                self._close_handle(handle)
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.debug("close_handle(%s) raised", handle, exc_info=True)

    def _invalidate_cached_handles(self, *, error_code: int, context: str) -> None:
        # A dropped/disconnected handle means a possibly-different controller on
        # reconnect, so the cached 0x86 verdict must be re-probed.
        self._supports_8point = None
        handles = []
        if self._write_handle is not None:
            handles.append(self._write_handle)
        if (
            self._read_write_handle is not None
            and self._read_write_handle not in handles
        ):
            handles.append(self._read_write_handle)

        self._write_handle = None
        self._read_write_handle = None
        self._target_path = None
        self._last_open_error = None

        logger.info(
            "invalidating SettingsService cached handle(s) after %s "
            "(Win32 err %s)",
            context,
            error_code,
        )
        for handle in handles:
            try:
                self._close_handle(handle)
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.debug("close_handle(%s) raised", handle, exc_info=True)

    def _write_payload(
        self,
        handle: int,
        payload: bytes,
        *,
        context: str = "payload",
    ) -> tuple[bool, int, int]:
        ok, err, bytes_written = self._write_file(handle, payload)
        if ok and bytes_written != len(payload):
            logger.warning(
                "SettingsService short write for %s: wrote %s of %s bytes",
                context,
                bytes_written,
                len(payload),
            )
            ok = False
            err = SHORT_WRITE_ERROR_CODE
        if not ok and _is_disconnect_win32_error(err):
            self._invalidate_cached_handles(
                error_code=err,
                context="WriteFile failure",
            )
        return ok, err, bytes_written

    def _write_payload_with_retry(
        self,
        handle: int,
        payload: bytes,
        *,
        context: str,
    ) -> _WritePayloadResult:
        ok, err, bytes_written = self._write_payload(handle, payload, context=context)
        if ok:
            return _WritePayloadResult(
                ok=True,
                err=err,
                bytes_written=bytes_written,
                retried=False,
            )

        logger.info(
            "SettingsService write failed for %s (Win32 err %s); retrying once",
            context,
            err,
        )
        self._sleep(WRITE_RETRY_DELAY_S)

        retry_handle = handle
        if self._write_handle is None:
            retry_handle_result = self._ensure_handle()
            if (
                retry_handle_result.outcome != SetPollingRateOutcome.OK
                or retry_handle_result.handle is None
            ):
                retry_err = retry_handle_result.error_code
                if retry_err is None:
                    retry_err = err
                return _WritePayloadResult(
                    ok=False,
                    err=retry_err,
                    bytes_written=bytes_written,
                    retried=True,
                )
            retry_handle = retry_handle_result.handle

        retry_ok, retry_err, retry_bytes_written = self._write_payload(
            retry_handle,
            payload,
            context=context,
        )
        return _WritePayloadResult(
            ok=retry_ok,
            err=retry_err,
            bytes_written=retry_bytes_written,
            retried=True,
        )

    def _execute_write(
        self,
        payload: bytes,
        *,
        context: str,
        make_result: Callable[..., _R],
    ) -> _R:
        """Shared ensure-handle -> write-with-retry scaffold for ``set_*`` writers.

        ``make_result`` (typically a ``functools.partial`` of the feature's result
        dataclass) receives keyword arguments ``outcome``, ``error_code``,
        ``bytes_written``, ``payload_hex`` and ``elapsed_ms``. Feature-specific
        validation and payload building run in the caller, before any Win32 seam.
        """

        start_ts = self._clock()
        handle_result = self._ensure_handle()
        if handle_result.outcome != WriteOutcome.OK or handle_result.handle is None:
            outcome = handle_result.outcome
            if outcome != WriteOutcome.DEVICE_NOT_FOUND:
                outcome = WriteOutcome.OPEN_FAILED
            error_code: Optional[int] = handle_result.error_code
            bytes_written: Optional[int] = None
        else:
            write_result = self._write_payload_with_retry(
                handle_result.handle,
                payload,
                context=context,
            )
            if not write_result.ok:
                outcome = WriteOutcome.WRITE_FAILED
                error_code = write_result.err
            else:
                outcome = WriteOutcome.OK_WITH_RETRY if write_result.retried else WriteOutcome.OK
                error_code = None
            bytes_written = write_result.bytes_written

        return make_result(
            outcome=outcome,
            error_code=error_code,
            bytes_written=bytes_written,
            payload_hex=payload.hex(),
            elapsed_ms=round((self._clock() - start_ts) * 1000),
        )

    def set_polling_rate(self, rate: PollingRate) -> SetPollingRateResult:
        """Write the confirmed polling-rate feature report."""

        return self._execute_write(
            build_polling_rate_payload(rate),
            context=f"polling_rate:{rate.name}",
            make_result=partial(SetPollingRateResult, rate=rate),
        )

    def get_polling_rate(self) -> Optional[PollingRate]:
        """Read the controller's current polling-rate selector."""

        try:
            query = build_read_query_payload(CATEGORY_POLLING_RATE, POLLING_RATE_SUBCOMMAND)
            response = self._read_response(query, expected_cat=CATEGORY_POLLING_RATE)
            rate_byte = response[7]
            try:
                return PollingRate(rate_byte)
            except ValueError:
                logger.warning("unknown polling rate byte: 0x%02x", rate_byte)
                return None
        except SettingsServiceError as exc:
            logger.warning("get_polling_rate failed: %s", exc)
            return None

    def set_step_size(self, value: int) -> SetStepSizeResult:
        """Write the joystick step-size feature report (cat 0x0D byte 7 + LE16 echo)."""

        return self._execute_write(
            build_step_size_payload(value),
            context=f"step_size:{value}",
            make_result=partial(SetStepSizeResult, value=value),
        )

    def get_step_size(self) -> Optional[int]:
        """Read the controller's current joystick step-size byte (1-255)."""

        try:
            query = build_read_query_payload(CATEGORY_STEP_SIZE, STEP_SIZE_SUBCOMMAND)
            response = self._read_response(query, expected_cat=CATEGORY_STEP_SIZE)
            value = response[7]
            if STEP_SIZE_VALUE_MIN <= value <= STEP_SIZE_VALUE_MAX:
                return value
            logger.warning("step_size byte out of range: 0x%02x", value)
            return None
        except SettingsServiceError as exc:
            logger.warning("get_step_size failed: %s", exc)
            return None

    def _decode_button_binding_response(
        self,
        response: bytes,
        slot: ButtonSlot,
    ) -> Optional[ButtonMapping]:
        """Decode one button-binding read response into a ``ButtonMapping``.

        Returns ``None`` when the response is unusable in a way that is worth
        re-reading rather than trusting: a slot/selector mismatch (a stale or
        interleaved response) or an unknown controller-button target (which a
        garbled partial read can fabricate). ``get_button_binding`` retries on a
        ``None`` within a bounded attempt budget, so a persistently unknown
        target still resolves to ``None`` instead of looping forever.
        """

        if response[7] != slot.value:
            logger.warning(
                "slot mismatch in response: queried 0x%02x, got 0x%02x",
                slot.value,
                response[7],
            )
            return None

        target_kind = response[8]
        target_low = response[9]
        target_value = response[10]
        if target_kind == 0x01:
            try:
                target = ControllerButtonTarget(target_value)
            except ValueError:
                logger.warning(
                    "unknown controller-button target: 0x%02x",
                    target_value,
                )
                return None
            return ButtonMapping.controller_button(target)

        return ButtonMapping(
            target_kind=target_kind,
            target_low=target_low,
            target_value=target_value,
        )

    def get_button_binding(self, slot: ButtonSlot) -> Optional[ButtonMapping]:
        """Read one captured button-binding slot from the controller.

        The 16 sequential per-slot reads can intermittently drop or mis-read a
        single slot when they run inside the big capture / post-restore verify
        read burst, yielding a false "mismatch after restore" even though every
        write landed (seen in local testing). Retry a transient read failure
        a few times before giving up so one flaky readback can't masquerade as a
        real change. A clean first read still issues exactly one read; only a
        failure costs a retry. Mirrors the write-side
        ``_write_payload_with_retry`` precedent and is centralized here so
        capture, the post-restore verify read-back, and the normal "Read" button
        all benefit.
        """

        if not isinstance(slot, ButtonSlot):
            raise TypeError(f"slot must be a ButtonSlot, got {slot!r}")

        last_error: Optional[str] = None
        for attempt in range(BUTTON_BINDING_READ_ATTEMPTS):
            if attempt:
                # Only sleep after a failure -- no added latency on the happy
                # path. self._sleep is the injected seam (tests stub it to a
                # no-op), so the suite never really sleeps here.
                self._sleep(BUTTON_BINDING_READ_RETRY_DELAY_S)
            try:
                query = build_read_query_payload(
                    CATEGORY_BUTTON_BINDING, selector=slot.value
                )
                response = self._read_response(
                    query,
                    expected_cat=CATEGORY_BUTTON_BINDING,
                    expected_selector=slot.value,
                )
            except SettingsServiceError as exc:
                last_error = str(exc)
                logger.warning("get_button_binding(slot=%r) failed: %s", slot, exc)
                continue

            mapping = self._decode_button_binding_response(response, slot)
            if mapping is not None:
                return mapping
            # Slot/selector mismatch or unknown target: the helper logged the
            # specific reason; re-read within budget before giving up.
            last_error = "unusable button-binding response"

        if BUTTON_BINDING_READ_ATTEMPTS > 1 and last_error is not None:
            logger.warning(
                "get_button_binding(slot=%r) gave up after %d attempts (%s)",
                slot,
                BUTTON_BINDING_READ_ATTEMPTS,
                last_error,
            )
        return None

    def set_button_binding(
        self,
        slot: ButtonSlot,
        mapping: ButtonMapping,
    ) -> SetButtonBindingResult:
        """Write the captured button-binding frame.

        Supports the 16x16 capture-confirmed controller-button matrix. Unknown
        slots/targets raise ``NotImplementedError`` before any Win32 seam.
        """

        return self._execute_write(
            build_button_binding_payload(slot, mapping),
            context=f"button_binding:{slot.name}",
            make_result=partial(SetButtonBindingResult, slot=slot, mapping=mapping),
        )

    def get_back_paddle_binding(self, slot: MacroSlot) -> Optional[BackPaddleBinding]:
        """Return None because the decoded back-paddle categories are write-only."""

        if not isinstance(slot, MacroSlot):
            raise TypeError(f"slot must be a MacroSlot, got {slot!r}")
        return None

    def get_all_back_paddle_bindings(self) -> dict[MacroSlot, BackPaddleBinding]:
        """Read all back-paddle bindings.

        No read-side response was captured for categories 0x03, 0x05, or 0x12,
        so controller-side state is currently not queryable. The UI preserves
        last-written values in profile snapshots instead.
        """

        return {}

    def set_back_paddle_binding(
        self,
        slot: MacroSlot,
        target: Optional[ControllerButtonTarget],
    ) -> SetBackPaddleBindingResult:
        """Write one 1-step back-paddle binding or explicit unbound state."""

        start_ts = self._clock()
        binding = BackPaddleBinding(target=_validate_back_paddle_target(target))
        payloads = build_back_paddle_binding_payloads(slot, binding.target)
        payload_hex = ";".join(payload.hex() for payload in payloads)

        handle_result = self._ensure_handle()
        if (
            handle_result.outcome != SetPollingRateOutcome.OK
            or handle_result.handle is None
        ):
            outcome = handle_result.outcome
            if outcome != WriteOutcome.DEVICE_NOT_FOUND:
                outcome = WriteOutcome.OPEN_FAILED
            return SetBackPaddleBindingResult(
                outcome=outcome,
                slot=slot,
                binding=binding,
                error_code=handle_result.error_code,
                bytes_written=None,
                payload_hex=payload_hex,
                elapsed_ms=round((self._clock() - start_ts) * 1000),
            )

        def _write_pair() -> tuple[bool, int, bool, Optional[int]]:
            """Write the full (binding[, terminator]) frame sequence once.

            Returns ``(ok, bytes_written, retried, last_error)``. ``ok`` is
            False as soon as a frame permanently fails.
            """

            written = 0
            retried_any = False
            for payload in payloads:
                current_handle = self._write_handle or handle_result.handle
                write_result = self._write_payload_with_retry(
                    current_handle,
                    payload,
                    context=f"back_paddle:{slot.name}",
                )
                written += write_result.bytes_written or 0
                retried_any = retried_any or write_result.retried
                if not write_result.ok:
                    return False, written, retried_any, write_result.err
            return True, written, retried_any, None

        ok, total_written, retried, last_err = _write_pair()
        # Back-paddle categories are write-only (no read-back channel), so a
        # frame that fails AFTER an earlier frame in the pair committed would
        # otherwise leave the firmware half-configured (binding written,
        # terminator missing) with nothing able to detect it. Re-attempt the
        # FULL pair as a UNIT once before giving up: a transient terminator
        # rejection (the firmware in-burst quirk) recovers to a fully-written
        # pair; a permanent failure still returns WRITE_FAILED — never a false
        # OK. Single-frame (unbound) writes already self-retry per frame, so the
        # unit re-attempt only applies to the multi-frame bound case.
        if not ok and len(payloads) > 1:
            retried = True
            ok, retry_written, _, last_err = _write_pair()
            total_written += retry_written

        if not ok:
            return SetBackPaddleBindingResult(
                outcome=SetBackPaddleBindingOutcome.WRITE_FAILED,
                slot=slot,
                binding=binding,
                error_code=last_err,
                bytes_written=total_written,
                payload_hex=payload_hex,
                elapsed_ms=round((self._clock() - start_ts) * 1000),
            )

        return SetBackPaddleBindingResult(
            outcome=(
                SetBackPaddleBindingOutcome.OK_WITH_RETRY
                if retried
                else SetBackPaddleBindingOutcome.OK
            ),
            slot=slot,
            binding=binding,
            error_code=None,
            bytes_written=total_written,
            payload_hex=payload_hex,
            elapsed_ms=round((self._clock() - start_ts) * 1000),
        )

    def set_back_paddle_unbound(self, slot: MacroSlot) -> SetBackPaddleBindingResult:
        """Clear one back-paddle slot."""

        return self.set_back_paddle_binding(slot, None)

    def set_all_deadzones(self, deadzones: StickDeadzones) -> SetDeadzoneResult:
        """Write the captured all-stick-deadzones frame."""

        validated = _validate_stick_deadzones(deadzones)
        return self._execute_write(
            build_all_deadzones_payload(deadzones),
            context="deadzones",
            make_result=partial(SetDeadzoneResult, deadzones=validated),
        )

    def get_deadzones(self) -> Optional[StickDeadzones]:
        """Read all stick deadzone values from the controller."""

        try:
            query = build_read_query_payload(CATEGORY_STICK_DEADZONE, DEADZONE_SUBCOMMAND)
            response = self._read_response(query, expected_cat=CATEGORY_STICK_DEADZONE)
            return StickDeadzones(
                left_center=response[7],
                right_center=response[8],
                left_outer=response[9],
                right_outer=response[10],
            )
        except SettingsServiceError as exc:
            logger.warning("get_deadzones failed: %s", exc)
            return None

    def set_left_stick_sensitivity_curve(
        self,
        anchors: SensitivityAnchorTuple,
    ) -> SetSensitivityCurveResult:
        """Write the captured left-stick sensitivity-curve frame."""

        validated = _validate_sensitivity_anchors(anchors)
        return self._execute_write(
            build_left_stick_sensitivity_curve_payload(anchors),
            context="sensitivity:left",
            make_result=partial(SetSensitivityCurveResult, anchors=validated),
        )

    def set_right_stick_sensitivity_curve(
        self,
        anchors: SensitivityAnchorTuple,
    ) -> SetSensitivityCurveResult:
        """Write the captured right-stick sensitivity-curve frame."""

        validated = _validate_sensitivity_anchors(anchors)
        return self._execute_write(
            build_right_stick_sensitivity_curve_payload(anchors),
            context="sensitivity:right",
            make_result=partial(SetSensitivityCurveResult, anchors=validated),
        )

    def set_left_stick_sensitivity_curve_8point(
        self,
        anchors: SensitivityAnchorTuple8,
    ) -> SetSensitivityCurveResult:
        """Write the 1.2.9 left-stick 8-point (cat 0x86) sensitivity-curve frame."""

        return self._set_sensitivity_curve_8point(
            "left", SENSITIVITY_STICK_LEFT, anchors
        )

    def set_right_stick_sensitivity_curve_8point(
        self,
        anchors: SensitivityAnchorTuple8,
    ) -> SetSensitivityCurveResult:
        """Write the 1.2.9 right-stick 8-point (cat 0x86) sensitivity-curve frame."""

        return self._set_sensitivity_curve_8point(
            "right", SENSITIVITY_STICK_RIGHT, anchors
        )

    def _set_sensitivity_curve_8point(
        self,
        stick: str,
        stick_selector: int,
        anchors: SensitivityAnchorTuple8,
    ) -> SetSensitivityCurveResult:
        # 1.2.9 cat-0x86 8-point variant of the 3-point writers (same scaffold;
        # only the builder/validator and HID category differ). Callers gate on
        # supports_8point_sensitivity(); this method does not re-check capability.
        validated = _validate_sensitivity_anchors_8point(anchors)
        return self._execute_write(
            build_sensitivity_curve_payload_8point(stick_selector, anchors),
            context=f"sensitivity_8point:{stick}",
            make_result=partial(SetSensitivityCurveResult, anchors=validated),
        )

    def get_sensitivity_curve_8point(
        self,
        stick: int,
        *,
        timeout_ms: int = READ_RESPONSE_TIMEOUT_MS,
    ) -> Optional[SensitivityAnchorTuple8]:
        """Read + decode one stick's 1.2.9 8-point (cat 0x86) sensitivity curve.

        Issues a per-stick 0x86 read query (read magic, cat 0x86, sub 0x00,
        stick selector at b7 — the same selector shape the 0x86 *write* uses),
        then hands the response to :func:`_decode_sensitivity_curve_8point`.
        Unlike the legacy 3-point :meth:`get_sensitivity_curves` (which returns
        both sticks from a single selector-less frame), the 0x86 read is
        per-stick and the response echoes the queried selector at b7, so we
        pass ``expected_selector`` and let ``_read_response`` reject a frame
        for the wrong stick.

        Returns ``None`` on any failure — unsupported device (no/garbled/timed-
        out 0x86 answer), wrong stick, or a curve that fails strict decode — so
        callers degrade to the 3-point path. Never raises.
        """

        if stick not in (SENSITIVITY_STICK_LEFT, SENSITIVITY_STICK_RIGHT):
            logger.warning(
                "get_sensitivity_curve_8point: invalid stick selector %r", stick
            )
            return None
        try:
            query = build_read_query_payload(
                CATEGORY_STICK_SENSITIVITY_8POINT,
                SENSITIVITY_SUBCOMMAND,
                selector=stick,
            )
            response = self._read_response(
                query,
                expected_cat=CATEGORY_STICK_SENSITIVITY_8POINT,
                expected_selector=stick,
                timeout_ms=timeout_ms,
            )
        except (SettingsServiceError, TimeoutError) as exc:
            # TimeoutError is the canonical non-capable signal: an fw-1.18 device
            # never answers 0x86, so the read times out. _read_response lets it
            # propagate (only SettingsServiceError is retried there), hence the
            # explicit catch — without it the probe below would raise instead of
            # returning a safe False.
            logger.info(
                "get_sensitivity_curve_8point(stick=0x%02x) failed: %s", stick, exc
            )
            return None
        return _decode_sensitivity_curve_8point(response)

    def supports_8point_sensitivity(self) -> bool:
        """Return whether this controller speaks the 1.2.9 cat-0x86 8-point curve.

        Probes once per connection by reading the LEFT stick's 8-point curve
        with a short timeout; the device is deemed capable iff a fully valid
        8-point curve decodes. The verdict is cached in ``self._supports_8point``
        and reset when handles drop (see ``stop`` / ``_invalidate_cached_handles``),
        so a freshly connected controller re-probes.

        Defaults to ``False`` on any failure. The asymmetry is deliberate: a
        false negative merely keeps the safe legacy 3-point path, whereas a
        false positive would write 0x86 frames to a device that does not
        understand them — so the underlying decode is strict (valid range +
        monotonic) rather than lenient.
        """

        if self._supports_8point is not None:
            return self._supports_8point
        curve = self.get_sensitivity_curve_8point(
            SENSITIVITY_STICK_LEFT,
            timeout_ms=SENSITIVITY_8POINT_PROBE_TIMEOUT_MS,
        )
        self._supports_8point = curve is not None
        return self._supports_8point

    def get_sensitivity_curves(
        self,
    ) -> Optional[tuple[SensitivityAnchorTuple, SensitivityAnchorTuple]]:
        """Read the hypothesized left/right sensitivity curves from the controller."""

        try:
            query = build_read_query_payload(
                CATEGORY_STICK_SENSITIVITY,
                SENSITIVITY_SUBCOMMAND,
            )
            response = self._read_response(
                query,
                expected_cat=CATEGORY_STICK_SENSITIVITY,
            )
            left: SensitivityAnchorTuple = (
                SensitivityAnchor(x=response[7], y=response[8]),
                SensitivityAnchor(x=response[9], y=response[10]),
                SensitivityAnchor(x=response[11], y=response[12]),
            )
            right: SensitivityAnchorTuple = (
                SensitivityAnchor(x=response[13], y=response[14]),
                SensitivityAnchor(x=response[15], y=response[16]),
                SensitivityAnchor(x=response[17], y=response[18]),
            )
            return (left, right)
        except SettingsServiceError as exc:
            logger.warning("get_sensitivity_curves failed: %s", exc)
            return None

    def set_left_stick_inversion(
        self,
        inversion: AxisInversion,
    ) -> SetAxisInversionResult:
        """Write the captured left-stick axis-inversion frame."""

        return self._set_axis_inversion("left", INVERSION_STICK_LEFT, inversion)

    def set_right_stick_inversion(
        self,
        inversion: AxisInversion,
    ) -> SetAxisInversionResult:
        """Write the captured right-stick axis-inversion frame."""

        return self._set_axis_inversion("right", INVERSION_STICK_RIGHT, inversion)

    def get_axis_inversion(self) -> Optional[tuple[AxisInversion, AxisInversion]]:
        """Read both sticks' axis inversion values from a single cat 0x07 query.

        Response layout (verified on hardware via the
        axis-inversion read-offset investigation):

            byte 5  = 0x07 (category)
            byte 6  = 0x00 (sub-cmd)
            byte 7  = LEFT  X-axis inversion flag (0/1)
            byte 8  = LEFT  Y-axis inversion flag (0/1)
            byte 9  = RIGHT X-axis inversion flag (0/1)
            byte 10 = RIGHT Y-axis inversion flag (0/1)
            byte 11+ = zeros

        The selector byte in the query (byte 7) does not affect the response —
        the firmware returns both sticks regardless of the queried selector.
        """

        try:
            query = build_read_query_payload(CATEGORY_STICK_INVERSION, INVERSION_SUBCOMMAND)
            response = self._read_response(query, expected_cat=CATEGORY_STICK_INVERSION)
            left = AxisInversion(
                x_inverted=bool(response[7]),
                y_inverted=bool(response[8]),
            )
            right = AxisInversion(
                x_inverted=bool(response[9]),
                y_inverted=bool(response[10]),
            )
            return (left, right)
        except SettingsServiceError as exc:
            logger.warning("get_axis_inversion failed: %s", exc)
            return None

    def _set_axis_inversion(
        self,
        stick: str,
        stick_selector: int,
        inversion: AxisInversion,
    ) -> SetAxisInversionResult:
        validated = _validate_axis_inversion(inversion)
        return self._execute_write(
            build_axis_inversion_payload(stick_selector, inversion),
            context=f"axis_inversion:{stick}",
            make_result=partial(SetAxisInversionResult, stick=stick, inversion=validated),
        )

    def set_left_trigger_settings(
        self,
        settings: TriggerSettings,
    ) -> SetTriggerSettingsResult:
        """Write the captured left-trigger settings frame."""

        return self._set_trigger_settings("left", TRIGGER_SELECTOR_LEFT, settings)

    def set_right_trigger_settings(
        self,
        settings: TriggerSettings,
    ) -> SetTriggerSettingsResult:
        """Write the captured right-trigger settings frame."""

        return self._set_trigger_settings("right", TRIGGER_SELECTOR_RIGHT, settings)

    def _set_trigger_settings(
        self,
        trigger: str,
        trigger_selector: int,
        settings: TriggerSettings,
    ) -> SetTriggerSettingsResult:
        validated = _validate_trigger_settings(settings)
        return self._execute_write(
            build_trigger_settings_payload(trigger_selector, settings),
            context=f"trigger:{trigger}",
            make_result=partial(SetTriggerSettingsResult, trigger=trigger, settings=validated),
        )

    def get_trigger_settings(self) -> Optional[tuple[TriggerSettings, TriggerSettings]]:
        """Read the hypothesized left/right trigger settings."""

        try:
            query = build_read_query_payload(CATEGORY_TRIGGER_SETTINGS, TRIGGER_SUBCOMMAND)
            response = self._read_response(query, expected_cat=CATEGORY_TRIGGER_SETTINGS)
            try:
                left_mode = TriggerMode(response[11])
                right_mode = TriggerMode(response[12])
            except ValueError as exc:
                logger.warning("unknown trigger mode byte: %s", exc)
                return None
            left = TriggerSettings(
                range_min=response[7],
                range_max=response[10],
                mode=left_mode,
            )
            right = TriggerSettings(
                range_min=response[8],
                range_max=response[9],
                mode=right_mode,
            )
            return (left, right)
        except SettingsServiceError as exc:
            logger.warning("get_trigger_settings failed: %s", exc)
            return None

    def set_vibration(self, settings: VibrationSettings) -> SetVibrationResult:
        """Write the captured all-vibration-settings frame."""

        validated = _validate_vibration_settings(settings)
        return self._execute_write(
            build_vibration_payload(settings),
            context="vibration",
            make_result=partial(SetVibrationResult, settings=validated),
        )

    def get_vibration(self) -> Optional[VibrationSettings]:
        """Read the controller's current vibration settings."""

        try:
            query = build_read_query_payload(CATEGORY_VIBRATION, VIBRATION_SUBCOMMAND)
            response = self._read_response(query, expected_cat=CATEGORY_VIBRATION)
            try:
                mode = TriggerVibrationMode(response[11])
            except ValueError:
                logger.warning("unknown vibration mode byte: 0x%02x", response[11])
                return None
            return VibrationSettings(
                left_grip_strength=response[7],
                right_grip_strength=response[8],
                left_trigger_motor_strength=response[9],
                right_trigger_motor_strength=response[10],
                mode=mode,
            )
        except SettingsServiceError as exc:
            logger.warning("get_vibration failed: %s", exc)
            return None

    def get_motion_settings(self) -> Optional[MotionSettings]:
        """Read the controller's current motion-tab settings.

        The wrapper does not expose a writer for this category. D0 confirmed
        enough read shape for profile completeness, not enough product value
        for a visible edit surface.
        """

        try:
            query = build_read_query_payload(CATEGORY_MOTION, MOTION_SUBCOMMAND)
            response = self._read_response(query, expected_cat=CATEGORY_MOTION)
            try:
                target = MotionMappingTarget(response[7])
            except ValueError:
                logger.warning("unknown motion target byte: 0x%02x", response[7])
                return None
            try:
                mode = MotionMappingMode(response[10])
            except ValueError:
                logger.warning("unknown motion mode byte: 0x%02x", response[10])
                return None
            return MotionSettings(
                target=target,
                trigger_key=response[9],
                mode=mode,
                sensitivity=response[11],
            )
        except SettingsServiceError as exc:
            logger.warning("get_motion_settings failed: %s", exc)
            return None

    def set_zone_lighting(
        self,
        zone: LightingZone,
        settings: LightingSettings,
    ) -> SetLightingResult:
        """Write the captured per-zone lighting frame for confirmed zones."""

        # build_lighting_payload type-checks the zone before the settings validator.
        payload = build_lighting_payload(zone, settings)
        validated = _validate_lighting_settings(settings)
        return self._execute_write(
            payload,
            context=f"lighting:{zone.name}",
            make_result=partial(SetLightingResult, zone=zone.name.lower(), settings=validated),
        )

    def get_zone_lighting(self, zone: LightingZone) -> Optional[LightingSettings]:
        """Read one lighting zone from the controller."""

        if not isinstance(zone, LightingZone):
            raise TypeError(f"zone must be a LightingZone, got {zone!r}")
        try:
            query = build_read_query_payload(CATEGORY_LIGHTING, selector=zone.value)
            response = self._read_response(
                query,
                expected_cat=CATEGORY_LIGHTING,
                expected_selector=zone.value,
            )
            if response[7] != zone.value:
                logger.warning(
                    "zone mismatch in response: queried 0x%02x, got 0x%02x",
                    zone.value,
                    response[7],
                )
                return None
            try:
                mode = LightingMode(response[9])
            except ValueError:
                logger.warning("unknown lighting mode byte: 0x%02x", response[9])
                return None
            return LightingSettings(
                light_on=bool(response[8]),
                mode=mode,
                brightness_byte=response[10],
                color=RgbColor(r=response[11], g=response[12], b=response[13]),
            )
        except SettingsServiceError as exc:
            logger.warning("get_zone_lighting(zone=%r) failed: %s", zone, exc)
            return None

    def get_all_settings(self) -> ControllerSnapshot:
        """Read all decoded settings from the controller in one synchronous batch.

        The batch shares one wall-clock budget (``batch_read_budget_s``
        constructor keyword; 0 or negative disables it). Per-read timeouts
        still bound each getter; the budget caps their sum. Once the deadline
        passes, every remaining getter is skipped — its snapshot field stays
        ``None`` (collection entries absent) — and a single warning summarizes
        how many getter calls were issued vs planned.
        """

        budget_s = self._batch_read_budget_s
        deadline = (self._clock() + budget_s) if budget_s > 0 else None
        issued = 0
        skipped = 0
        exhausted = False

        def _within_budget() -> bool:
            # One guard per getter call (the 8-point capability probe
            # included). Once the deadline passes, stay exhausted without
            # consulting the clock again so the whole tail of the batch is
            # skipped uniformly.
            nonlocal issued, skipped, exhausted
            if not exhausted and deadline is not None and self._clock() >= deadline:
                exhausted = True
            if exhausted:
                skipped += 1
                return False
            issued += 1
            return True

        polling_rate = self.get_polling_rate() if _within_budget() else None
        vibration = self.get_vibration() if _within_budget() else None
        deadzones = self.get_deadzones() if _within_budget() else None

        axis_inv = self.get_axis_inversion() if _within_budget() else None
        axis_left = axis_inv[0] if axis_inv else None
        axis_right = axis_inv[1] if axis_inv else None

        sens = self.get_sensitivity_curves() if _within_budget() else None
        sens_left = sens[0] if sens else None
        sens_right = sens[1] if sens else None

        # 8-point (cat 0x86) curves are carried alongside the 3-point fields,
        # not in place of them, so the apply path always keeps a 3-point
        # fallback. Probe capability once (cached per connection); only read the
        # richer per-stick curves on a device that passes. A non-capable device
        # pays a single short-timeout probe read here and nothing more. The
        # probe and both per-stick reads each count against the batch budget;
        # when the probe itself is skipped, the per-stick reads are not planned
        # (nor counted), exactly like a not-capable verdict.
        sens_left_8point: Optional[SensitivityAnchorTuple8] = None
        sens_right_8point: Optional[SensitivityAnchorTuple8] = None
        if _within_budget() and self.supports_8point_sensitivity():
            if _within_budget():
                sens_left_8point = self.get_sensitivity_curve_8point(
                    SENSITIVITY_STICK_LEFT
                )
            if _within_budget():
                sens_right_8point = self.get_sensitivity_curve_8point(
                    SENSITIVITY_STICK_RIGHT
                )

        trig = self.get_trigger_settings() if _within_budget() else None
        trig_left = trig[0] if trig else None
        trig_right = trig[1] if trig else None

        bindings: dict[ButtonSlot, ButtonMapping] = {}
        for slot in ButtonSlot:
            if not _within_budget():
                continue
            mapping = self.get_button_binding(slot)
            if mapping is not None:
                bindings[slot] = mapping

        lighting: dict[LightingZone, LightingSettings] = {}
        for zone in LightingZone:
            if not _within_budget():
                continue
            zone_settings = self.get_zone_lighting(zone)
            if zone_settings is not None:
                lighting[zone] = zone_settings

        motion = self.get_motion_settings() if _within_budget() else None
        back_paddles = (
            self.get_all_back_paddle_bindings() if _within_budget() else {}
        )
        step_size = self.get_step_size() if _within_budget() else None

        if skipped:
            logger.warning(
                "get_all_settings: batch read budget (%.1fs) exhausted after "
                "%d of %d reads; remaining fields unread",
                budget_s,
                issued,
                issued + skipped,
            )

        return ControllerSnapshot(
            polling_rate=polling_rate,
            vibration=vibration,
            deadzones=deadzones,
            axis_inversion_left=axis_left,
            axis_inversion_right=axis_right,
            sensitivity_left=sens_left,
            sensitivity_right=sens_right,
            trigger_left=trig_left,
            trigger_right=trig_right,
            button_bindings=bindings,
            lighting_zones=lighting,
            motion_settings=motion,
            back_paddle_bindings=back_paddles,
            step_size=step_size,
            sensitivity_left_8point=sens_left_8point,
            sensitivity_right_8point=sens_right_8point,
        )

    def _ensure_handle(self) -> _EnsureHandleResult:
        if self._write_handle is not None:
            return _EnsureHandleResult(
                outcome=SetPollingRateOutcome.OK,
                handle=self._write_handle,
                target_path=self._target_path,
            )

        target_path = _choose_mi02_path(self._enumerate_paths())
        if target_path is None:
            self._target_path = None
            self._last_open_error = None
            return _EnsureHandleResult(outcome=SetPollingRateOutcome.DEVICE_NOT_FOUND)

        handle, err = self._open_write_handle(target_path)
        self._target_path = target_path
        if handle is None:
            self._last_open_error = err
            return _EnsureHandleResult(
                outcome=SetPollingRateOutcome.OPEN_FAILED,
                target_path=target_path,
                error_code=err,
            )

        self._write_handle = handle
        self._last_open_error = None
        return _EnsureHandleResult(
            outcome=SetPollingRateOutcome.OK,
            handle=handle,
            target_path=target_path,
        )

    def _ensure_read_write_handle(self) -> Optional[int]:
        if self._read_write_handle is not None:
            return self._read_write_handle

        target_path = self._target_path or _choose_mi02_path(self._enumerate_paths())
        if target_path is None:
            self._target_path = None
            self._last_open_error = None
            return None

        handle, err = self._open_read_write_handle(target_path)
        self._target_path = target_path
        if handle is None:
            self._last_open_error = err
            return None

        self._read_write_handle = handle
        self._last_open_error = None
        return handle

    def _read_payload(self, handle: int, length: int, timeout_ms: int) -> bytes:
        return self._read_file(handle, length, timeout_ms)

    def _read_response(
        self,
        query_payload: bytes,
        *,
        expected_cat: int,
        expected_selector: Optional[int] = None,
        timeout_ms: int = READ_RESPONSE_TIMEOUT_MS,
    ) -> bytes:
        if len(query_payload) != HID_FEATURE_REPORT_SIZE:
            raise SettingsServiceError(
                f"read query must be {HID_FEATURE_REPORT_SIZE} bytes, "
                f"got {len(query_payload)}"
            )
        expected_cat = _validate_byte_int(expected_cat, name="expected_cat")
        if expected_selector is not None:
            expected_selector = _validate_byte_int(
                expected_selector,
                name="expected_selector",
            )

        handle = self._ensure_read_write_handle()
        if handle is None:
            raise SettingsServiceError("could not open read+write MI_02 handle")

        ok, err, bytes_written = self._write_payload(
            handle,
            query_payload,
            context="read query",
        )
        if not ok:
            raise SettingsServiceError(
                f"read query WriteFile failed (Win32 err {err})",
                win32_error=err,
            )
        if bytes_written != len(query_payload):
            raise SettingsServiceError(
                f"read query WriteFile wrote {bytes_written} of {len(query_payload)} bytes"
            )

        deadline = self._clock() + (timeout_ms / 1000.0)
        last_mismatch: str | None = None
        for attempt in range(READ_STALE_RESPONSE_MAX_DISCARDS + 1):
            if attempt == 0:
                remaining_ms = timeout_ms
            else:
                remaining_ms = max(1, int((deadline - self._clock()) * 1000))
            try:
                response = self._read_payload(
                    handle,
                    HID_FEATURE_REPORT_SIZE,
                    remaining_ms,
                )
            except SettingsServiceError as exc:
                error_code = _win32_error_from_exception(exc)
                if _is_disconnect_win32_error(error_code):
                    self._invalidate_cached_handles(
                        error_code=error_code,
                        context="ReadFile failure",
                    )
                if last_mismatch is not None:
                    raise SettingsServiceError(
                        f"{last_mismatch}; no matching response before timeout"
                    ) from exc
                raise
            if len(response) < 8:
                raise SettingsServiceError(f"read response too short: {len(response)} bytes")
            if response[0] != HID_REPORT_ID:
                raise SettingsServiceError(
                    f"unexpected response magic: {response[:5].hex()}"
                )
            if response[1:5] == MAGIC_WRITE_ACK_PREFIX:
                last_mismatch = "write ACK received before read response"
                logger.debug(
                    "discarding write ACK for category 0x%02x while waiting for read response",
                    response[5],
                )
                if self._clock() >= deadline:
                    break
                continue
            if response[1:5] != MAGIC_READ_RESPONSE_PREFIX:
                raise SettingsServiceError(
                    f"unexpected response magic: {response[:5].hex()}"
                )
            if response[5] != expected_cat:
                last_mismatch = (
                    f"category mismatch: expected 0x{expected_cat:02x}, "
                    f"got 0x{response[5]:02x}"
                )
            elif expected_selector is not None and response[7] != expected_selector:
                last_mismatch = (
                    f"selector mismatch: expected 0x{expected_selector:02x}, "
                    f"got 0x{response[7]:02x}"
                )
            else:
                return response
            if self._clock() >= deadline:
                break

        if last_mismatch is None:
            last_mismatch = "no matching read response"
        raise SettingsServiceError(f"{last_mismatch}; no matching response before timeout")
