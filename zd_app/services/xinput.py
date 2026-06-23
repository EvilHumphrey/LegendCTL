"""XInput ctypes bindings for direct controller access via XInput1_4.dll."""

import ctypes
from ctypes import Structure, c_ubyte, c_uint, c_ushort, c_short, POINTER, byref

# ---------------------------------------------------------------------------
# XInput button bitmask constants
# ---------------------------------------------------------------------------
XINPUT_GAMEPAD_DPAD_UP        = 0x0001
XINPUT_GAMEPAD_DPAD_DOWN      = 0x0002
XINPUT_GAMEPAD_DPAD_LEFT      = 0x0004
XINPUT_GAMEPAD_DPAD_RIGHT     = 0x0008
XINPUT_GAMEPAD_START          = 0x0010
XINPUT_GAMEPAD_BACK           = 0x0020
XINPUT_GAMEPAD_LEFT_THUMB     = 0x0040
XINPUT_GAMEPAD_RIGHT_THUMB    = 0x0080
XINPUT_GAMEPAD_LEFT_SHOULDER  = 0x0100
XINPUT_GAMEPAD_RIGHT_SHOULDER = 0x0200
XINPUT_GAMEPAD_A              = 0x1000
XINPUT_GAMEPAD_B              = 0x2000
XINPUT_GAMEPAD_X              = 0x4000
XINPUT_GAMEPAD_Y              = 0x8000

BUTTON_NAMES = {
    XINPUT_GAMEPAD_DPAD_UP:        "DPad Up",
    XINPUT_GAMEPAD_DPAD_DOWN:      "DPad Down",
    XINPUT_GAMEPAD_DPAD_LEFT:      "DPad Left",
    XINPUT_GAMEPAD_DPAD_RIGHT:     "DPad Right",
    XINPUT_GAMEPAD_START:          "Start",
    XINPUT_GAMEPAD_BACK:           "Back",
    XINPUT_GAMEPAD_LEFT_THUMB:     "L3",
    XINPUT_GAMEPAD_RIGHT_THUMB:    "R3",
    XINPUT_GAMEPAD_LEFT_SHOULDER:  "LB",
    XINPUT_GAMEPAD_RIGHT_SHOULDER: "RB",
    XINPUT_GAMEPAD_A:              "A",
    XINPUT_GAMEPAD_B:              "B",
    XINPUT_GAMEPAD_X:              "X",
    XINPUT_GAMEPAD_Y:              "Y",
}

ERROR_SUCCESS = 0
ERROR_DEVICE_NOT_CONNECTED = 0x048F
XUSER_MAX_COUNT = 4
BATTERY_DEVTYPE_GAMEPAD = 0x00
BATTERY_TYPE_DISCONNECTED = 0x00
BATTERY_TYPE_WIRED = 0x01
BATTERY_TYPE_ALKALINE = 0x02
BATTERY_TYPE_NIMH = 0x03
BATTERY_TYPE_UNKNOWN = 0xFF
BATTERY_LEVEL_EMPTY = 0x00
BATTERY_LEVEL_LOW = 0x01
BATTERY_LEVEL_MEDIUM = 0x02
BATTERY_LEVEL_FULL = 0x03

# ---------------------------------------------------------------------------
# XInput structures
# ---------------------------------------------------------------------------
class XINPUT_GAMEPAD(Structure):
    _fields_ = [
        ("wButtons",     c_ushort),
        ("bLeftTrigger",  c_ubyte),
        ("bRightTrigger", c_ubyte),
        ("sThumbLX",     c_short),
        ("sThumbLY",     c_short),
        ("sThumbRX",     c_short),
        ("sThumbRY",     c_short),
    ]

class XINPUT_STATE(Structure):
    _fields_ = [
        ("dwPacketNumber", c_uint),
        ("Gamepad",        XINPUT_GAMEPAD),
    ]

class XINPUT_CAPABILITIES(Structure):
    class _VIBRATION(Structure):
        _fields_ = [
            ("wLeftMotorSpeed",  c_ushort),
            ("wRightMotorSpeed", c_ushort),
        ]
    _fields_ = [
        ("Type",      c_ubyte),
        ("SubType",   c_ubyte),
        ("Flags",     c_ushort),
        ("Gamepad",   XINPUT_GAMEPAD),
        ("Vibration", _VIBRATION),
    ]


class XINPUT_VIBRATION(Structure):
    _fields_ = [
        ("wLeftMotorSpeed", c_ushort),
        ("wRightMotorSpeed", c_ushort),
    ]


class XINPUT_BATTERY_INFORMATION(Structure):
    _fields_ = [
        ("BatteryType", c_ubyte),
        ("BatteryLevel", c_ubyte),
    ]

# ---------------------------------------------------------------------------
# Load XInput DLL
# ---------------------------------------------------------------------------
def _load_xinput():
    """Walk the XInput DLL fallback chain; return the loaded DLL or ``None``.

    Mirrors ``xinput_poll_service._load_dll``: on a system where none of the
    XInput DLLs load — or off-Windows, where ``ctypes`` has no ``windll`` — this
    returns ``None`` so import succeeds and the public functions degrade
    gracefully instead of aborting launch (this module is imported
    unconditionally at startup) with a raw traceback. (Never reached on the
    shipped Win8+ target since ``xinput1_4.dll`` ships in-box — robustness only.)
    """
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return getattr(windll, name)
        except OSError:
            continue
    return None


_xinput = _load_xinput()

# Set up function signatures (only when a DLL actually loaded).
if _xinput is not None:
    _xinput.XInputGetState.argtypes = [c_uint, POINTER(XINPUT_STATE)]
    _xinput.XInputGetState.restype = c_uint

    _xinput.XInputGetCapabilities.argtypes = [c_uint, c_uint, POINTER(XINPUT_CAPABILITIES)]
    _xinput.XInputGetCapabilities.restype = c_uint

    _xinput.XInputSetState.argtypes = [c_uint, POINTER(XINPUT_VIBRATION)]
    _xinput.XInputSetState.restype = c_uint

    if hasattr(_xinput, "XInputGetBatteryInformation"):
        _xinput.XInputGetBatteryInformation.argtypes = [c_uint, c_ubyte, POINTER(XINPUT_BATTERY_INFORMATION)]
        _xinput.XInputGetBatteryInformation.restype = c_uint

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_state(controller_index: int) -> tuple[int, XINPUT_STATE]:
    """Read current controller state. Returns (error_code, state)."""
    state = XINPUT_STATE()
    if _xinput is None:
        return ERROR_DEVICE_NOT_CONNECTED, state
    result = _xinput.XInputGetState(controller_index, byref(state))
    return result, state

def get_capabilities(controller_index: int) -> tuple[int, XINPUT_CAPABILITIES]:
    """Read controller capabilities. Returns (error_code, caps)."""
    caps = XINPUT_CAPABILITIES()
    if _xinput is None:
        return ERROR_DEVICE_NOT_CONNECTED, caps
    result = _xinput.XInputGetCapabilities(controller_index, 0, byref(caps))
    return result, caps

def get_connected_controllers() -> list[int]:
    """Return list of indices for connected controllers (0-3)."""
    connected = []
    for i in range(XUSER_MAX_COUNT):
        result, _ = get_state(i)
        if result == ERROR_SUCCESS:
            connected.append(i)
    return connected

def decode_buttons(bitmask: int) -> list[str]:
    """Decode button bitmask into list of pressed button names."""
    return [name for bit, name in BUTTON_NAMES.items() if bitmask & bit]


def set_vibration(controller_index: int, left_motor: int, right_motor: int) -> int:
    """Set vibration state for a controller.

    Motor values are clamped to the XInput range of 0-65535.
    """
    vibration = XINPUT_VIBRATION(
        wLeftMotorSpeed=max(0, min(65535, int(left_motor))),
        wRightMotorSpeed=max(0, min(65535, int(right_motor))),
    )
    if _xinput is None:
        return ERROR_DEVICE_NOT_CONNECTED
    return _xinput.XInputSetState(controller_index, byref(vibration))


def get_battery_information(controller_index: int) -> tuple[int, XINPUT_BATTERY_INFORMATION | None]:
    """Read battery information for a controller when supported by the DLL."""
    if _xinput is None or not hasattr(_xinput, "XInputGetBatteryInformation"):
        return ERROR_DEVICE_NOT_CONNECTED, None
    battery = XINPUT_BATTERY_INFORMATION()
    result = _xinput.XInputGetBatteryInformation(controller_index, BATTERY_DEVTYPE_GAMEPAD, byref(battery))
    return result, battery


def describe_battery_level(controller_index: int) -> str:
    """Return a small user-facing battery summary."""
    result, battery = get_battery_information(controller_index)
    if result != ERROR_SUCCESS or battery is None:
        return "Unknown"
    if battery.BatteryType == BATTERY_TYPE_WIRED:
        return "Wired"
    if battery.BatteryType in {BATTERY_TYPE_DISCONNECTED, BATTERY_TYPE_UNKNOWN}:
        return "Unknown"
    return {
        BATTERY_LEVEL_EMPTY: "Empty",
        BATTERY_LEVEL_LOW: "Low",
        BATTERY_LEVEL_MEDIUM: "Medium",
        BATTERY_LEVEL_FULL: "Full",
    }.get(battery.BatteryLevel, "Unknown")
