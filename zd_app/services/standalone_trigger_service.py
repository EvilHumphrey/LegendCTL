"""Standalone trigger service (opt-in; not auto-run).

Standalone trigger service for v1 wrapper.

The v1 architecture replaces vendor.exe with a pure-Python service that:

1. Fires the captured 7-IOCTL XUSB SET_STATE sequence on the
   ``VID_413D&PID_2104`` MI_00 interface to flip the controller from
   public XInput mode to ``VID_20BC&PID_5080`` dual-mode.
2. Holds two ``VID_20BC`` HID handles (write + read) and runs an active
   keepalive write/read loop so dual-mode persists for the wrapper UI's
   lifetime.

This module owns piece (1): the trigger sequence. The HID keepalive
lifecycle and top-level service wiring are defined later in this module.

Reference patterns used here: lazy-bound ``_Win32`` ctypes class for
hardware-free testability, dependency-injected default helpers, dataclass
result types with explicit Enum outcomes.

Trigger contract source: the documented v1 trigger protocol.
Reliability: ~90% first-attempt + ~100% with one immediate
retry; the retry wrapper is defined later in this module.
"""

from __future__ import annotations

import ctypes as c
import logging
import threading
import time
from ctypes import wintypes as w
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# VID/PID + interface constants
# ---------------------------------------------------------------------------

PUBLIC_VENDOR_ID = 0x413D
PUBLIC_PRODUCT_ID = 0x2104
HIDDEN_VENDOR_ID = 0x20BC
HIDDEN_PRODUCT_ID = 0x5080

# XUSB compat device interface GUID (the captured trace's MI_00 path uses
# this GUID class; it differs from the standard HID class GUID).
XUSB_DEVICE_INTERFACE_GUID = "{ec87f1e3-c13b-4100-b5f7-8b84d54260cb}"


# ---------------------------------------------------------------------------
# Trigger sequence (verified against the documented v1 trigger protocol)
# ---------------------------------------------------------------------------

IOCTL_GET_INFORMATION = 0x80006000
IOCTL_GET_STATE = 0x8000E00C
IOCTL_GET_LED = 0x8000E008
IOCTL_SET_STATE = 0x8000A010

# In-payloads for the 3 metadata IOCTLs that prefix the trigger.
GET_STATE_INPUT = bytes.fromhex("010100")
GET_LED_INPUT = bytes.fromhex("010100")
SET_STATE_PREAMBLE_INPUT = bytes.fromhex("0006000001")

# The 3 trigger SET_STATE payloads. XX bytes 01/AA/23 are
# required-specific; all three other XX-byte variants tried failed
# to enumerate VID_20BC. Hardcode here.
TRIGGER_PAYLOAD_1 = bytes.fromhex("0000550102")
TRIGGER_PAYLOAD_2 = bytes.fromhex("000055aa02")
TRIGGER_PAYLOAD_3 = bytes.fromhex("0000552302")

# Output buffer sizes for the v1 trigger IOCTLs.
GET_INFORMATION_OUT_LEN = 12
GET_STATE_OUT_LEN = 29
GET_LED_OUT_LEN = 3

# Inter-trigger gaps (captured: ~31ms between triggers 1->2 and 2->3).
DEFAULT_INTER_TRIGGER_GAP_S = 0.030


# ---------------------------------------------------------------------------
# Win32 file-access + SetupAPI constants
# ---------------------------------------------------------------------------

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 0x3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = w.HANDLE(-1).value

DIGCF_PRESENT = 0x2
DIGCF_DEVICEINTERFACE = 0x10
ERROR_NO_MORE_ITEMS = 259

DUPLICATE_SAME_ACCESS = 0x2

FILE_FLAG_OVERLAPPED = 0x40000000

# Open-handle shape for the XUSB MI_00 interface.
# Hardcoded: shared read+write, normal attributes.
XUSB_OPEN_DESIRED_ACCESS = GENERIC_READ | GENERIC_WRITE
XUSB_OPEN_SHARE_MODE = FILE_SHARE_READ | FILE_SHARE_WRITE
XUSB_OPEN_FLAGS_AND_ATTRIBUTES = FILE_ATTRIBUTE_NORMAL

# HID-side open shapes for the keepalive lifecycle (vendor uses two
# handles per the active-protocol trace; verified stable over a sustained 30s run).
HID_WRITE_OPEN_DESIRED_ACCESS = GENERIC_WRITE  # 0x40000000
HID_WRITE_OPEN_SHARE_MODE = FILE_SHARE_READ | FILE_SHARE_WRITE
HID_WRITE_OPEN_FLAGS_AND_ATTRIBUTES = 0  # synchronous

HID_READ_OPEN_DESIRED_ACCESS = GENERIC_READ | GENERIC_WRITE  # 0xc0000000
HID_READ_OPEN_SHARE_MODE = FILE_SHARE_READ | FILE_SHARE_WRITE
HID_READ_OPEN_FLAGS_AND_ATTRIBUTES = FILE_FLAG_OVERLAPPED  # 0x40000000

# Standard HID descriptor IOCTLs vendor issues on the write handle right
# after open. Per the documented XUSB22 IOCTL set: 0x000b01a8 =
# IOCTL_HID_GET_COLLECTION_INFORMATION (12-byte response carrying VID/PID),
# 0x000b0193 = IOCTL_HID_GET_COLLECTION_DESCRIPTOR (1636-byte HID report
# descriptor). A minimal-lifecycle run sustained 30s without reissuing
# them, so they're probably NOT load-bearing for keepalive — but vendor
# does them at session open, so include for parity.
IOCTL_HID_GET_COLLECTION_INFORMATION = 0x000B01A8
IOCTL_HID_GET_COLLECTION_DESCRIPTOR = 0x000B0193

HID_COLLECTION_INFORMATION_OUT_LEN = 12
HID_COLLECTION_DESCRIPTOR_OUT_LEN = 1636

# Keepalive payloads (vendor-observed, verified stable). 5-byte magic
# prefix + 59 zero bytes = 64-byte HID report. A sustained 30s run
# with zero padding confirmed the trailing bytes are not load-bearing.
HID_REPORT_SIZE = 64
KEEPALIVE_WRITE_A_PAYLOAD = bytes.fromhex("1055aa5001") + bytes(59)
KEEPALIVE_WRITE_B_PAYLOAD = bytes.fromhex("1055aa500f") + bytes(59)

# Win32 Wait* / overlapped error codes
ERROR_IO_PENDING = 997
ERROR_OPERATION_ABORTED = 995
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x00000102


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


class _OVERLAPPED(c.Structure):
    _fields_ = [
        ("Internal", c.c_void_p),
        ("InternalHigh", c.c_void_p),
        ("Offset", w.DWORD),
        ("OffsetHigh", w.DWORD),
        ("hEvent", w.HANDLE),
    ]


# ---------------------------------------------------------------------------
# Lazy-bound Win32 surface
# ---------------------------------------------------------------------------

class _Win32:
    """Lazy-bound Win32 API surface for the trigger service.

    Wrapped in a class so tests can substitute mocks at the higher-level
    DI seams (the helper functions below) without touching ctypes.
    """

    _kernel32 = None
    _setupapi = None

    @classmethod
    def kernel32(cls):
        if cls._kernel32 is None:
            cls._kernel32 = c.windll.kernel32
            cls._kernel32.CreateFileW.argtypes = [
                w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE
            ]
            cls._kernel32.CreateFileW.restype = w.HANDLE
            cls._kernel32.CloseHandle.argtypes = [w.HANDLE]
            cls._kernel32.CloseHandle.restype = w.BOOL
            cls._kernel32.DeviceIoControl.argtypes = [
                w.HANDLE, w.DWORD,
                c.c_void_p, w.DWORD,
                c.c_void_p, w.DWORD,
                c.POINTER(w.DWORD), c.c_void_p,
            ]
            cls._kernel32.DeviceIoControl.restype = w.BOOL
            cls._kernel32.GetCurrentProcess.argtypes = []
            cls._kernel32.GetCurrentProcess.restype = w.HANDLE
            cls._kernel32.DuplicateHandle.argtypes = [
                w.HANDLE, w.HANDLE, w.HANDLE,
                c.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD,
            ]
            cls._kernel32.DuplicateHandle.restype = w.BOOL
            cls._kernel32.GetLastError.argtypes = []
            cls._kernel32.GetLastError.restype = w.DWORD
            # HID keepalive overlapped-I/O bindings
            cls._kernel32.WriteFile.argtypes = [
                w.HANDLE, c.c_void_p, w.DWORD,
                c.POINTER(w.DWORD), c.c_void_p,
            ]
            cls._kernel32.WriteFile.restype = w.BOOL
            cls._kernel32.ReadFile.argtypes = [
                w.HANDLE, c.c_void_p, w.DWORD,
                c.c_void_p, c.c_void_p,
            ]
            cls._kernel32.ReadFile.restype = w.BOOL
            cls._kernel32.GetOverlappedResult.argtypes = [
                w.HANDLE, c.c_void_p, c.POINTER(w.DWORD), w.BOOL,
            ]
            cls._kernel32.GetOverlappedResult.restype = w.BOOL
            cls._kernel32.CancelIoEx.argtypes = [w.HANDLE, c.c_void_p]
            cls._kernel32.CancelIoEx.restype = w.BOOL
            cls._kernel32.CreateEventW.argtypes = [
                c.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR,
            ]
            cls._kernel32.CreateEventW.restype = w.HANDLE
            cls._kernel32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
            cls._kernel32.WaitForSingleObject.restype = w.DWORD
        return cls._kernel32

    @classmethod
    def setupapi(cls):
        if cls._setupapi is None:
            # Use a private DLL wrapper so our XUSB-class GUID argtypes
            # don't clash with hidden_hid_transport's HID-class argtypes
            # on a shared setupapi handle (a defensive private-wrapper pattern).
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


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class TriggerOutcome(Enum):
    """Outcome of :func:`fire_trigger_sequence`."""

    SUCCESS = "success"  # all 7 IOCTLs returned ok=true AND VID_20BC enumerated
    NO_XUSB_PATH = "no_xusb_path"  # VID_413D MI_00 not present
    OPEN_FAILED = "open_failed"  # CreateFileW on XUSB path failed
    DUPLICATE_FAILED = "duplicate_failed"  # DuplicateHandle failed
    IOCTL_FAILED = "ioctl_failed"  # at least one of the 7 IOCTLs returned ok=false
    HIDDEN_NOT_VISIBLE = "hidden_not_visible"  # all IOCTLs ok, no VID_20BC enum within timeout


@dataclass
class IoctlAttempt:
    step: int
    label: str
    ioctl: int
    in_hex: str
    ok: bool
    last_error: int
    bytes_returned: int
    out_hex: str
    elapsed_ms: int


@dataclass
class TriggerResult:
    outcome: TriggerOutcome
    target_path: Optional[str] = None
    open_ok: bool = False
    open_last_error: int = 0
    duplicate_ok: bool = False
    duplicate_last_error: int = 0
    ioctl_attempts: list[IoctlAttempt] = field(default_factory=list)
    visible_after_ms: Optional[int] = None
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# Default helpers (dependency-injectable for tests)
# ---------------------------------------------------------------------------

def _default_enumerate_hidden_paths() -> list[str]:
    """Return all VID_20BC&PID_5080 HID interface paths."""
    try:
        from zd_app.protocol.hid_transport import filter_hid_paths
    except ImportError:  # pragma: no cover - non-Windows test env fallback
        return []
    return list(filter_hid_paths(HIDDEN_VENDOR_ID, HIDDEN_PRODUCT_ID))


def _default_check_hidden_visible() -> bool:
    """Return True if VID_20BC HID device is enumerable on the host bus."""
    return bool(_default_enumerate_hidden_paths())


def _default_enumerate_xusb_paths() -> list[str]:
    """Return all VID_413D&PID_2104 MI_00 XUSB-compat device interface paths."""
    try:
        setupapi = _Win32.setupapi()
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover - non-Windows
        return []

    interface_guid = _parse_guid_string(XUSB_DEVICE_INTERFACE_GUID)
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
            ok = setupapi.SetupDiEnumDeviceInterfaces(
                info_set,
                None,
                c.byref(interface_guid),
                index,
                c.byref(interface_data),
            )
            if not ok:
                if kernel32.GetLastError() == ERROR_NO_MORE_ITEMS:
                    break
                index += 1
                continue

            needed = w.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                info_set, c.byref(interface_data), None, 0, c.byref(needed), None,
            )

            buffer = c.create_string_buffer(needed.value)
            c.memset(buffer, 0, needed.value)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize:
            # 8 on 64-bit (4-byte cbSize + 4 bytes alignment for WCHAR[1]),
            # 6 on 32-bit (4-byte cbSize + 2-byte WCHAR[1]).
            c.cast(buffer, c.POINTER(w.DWORD)).contents.value = (
                8 if c.sizeof(c.c_void_p) == 8 else 6
            )

            ok = setupapi.SetupDiGetDeviceInterfaceDetailW(
                info_set,
                c.byref(interface_data),
                c.cast(buffer, c.c_void_p),
                needed,
                c.byref(needed),
                None,
            )
            if ok:
                paths.append(c.wstring_at(c.addressof(buffer) + c.sizeof(w.DWORD)))
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info_set)

    wanted_vid = f"vid_{PUBLIC_VENDOR_ID:04x}"
    wanted_pid = f"pid_{PUBLIC_PRODUCT_ID:04x}"
    return [
        path
        for path in paths
        if wanted_vid in path.lower()
        and wanted_pid in path.lower()
        and "mi_00" in path.lower()
    ]


def _default_open_xusb_handle(path: str) -> tuple[Optional[int], int]:
    """Open the XUSB device path. Returns ``(handle_or_None, last_error)``."""
    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover - non-Windows
        return None, 0
    handle = kernel32.CreateFileW(
        path,
        XUSB_OPEN_DESIRED_ACCESS,
        XUSB_OPEN_SHARE_MODE,
        None,
        OPEN_EXISTING,
        XUSB_OPEN_FLAGS_AND_ATTRIBUTES,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or not handle:
        return None, kernel32.GetLastError()
    return int(handle), 0


def _default_duplicate_handle(source_handle: int) -> tuple[Optional[int], int]:
    """Duplicate a handle in the current process with DUPLICATE_SAME_ACCESS.

    The captured trace + replay used DuplicateHandle with options=0x2 and
    desired_access=0; subsequent IOCTLs use the duplicate. Returns
    ``(dup_handle_or_None, last_error)``.
    """
    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover
        return None, 0
    current_process = kernel32.GetCurrentProcess()
    dup = w.HANDLE()
    ok = bool(
        kernel32.DuplicateHandle(
            current_process,
            source_handle,
            current_process,
            c.byref(dup),
            0,
            False,
            DUPLICATE_SAME_ACCESS,
        )
    )
    if not ok or not dup.value:
        return None, kernel32.GetLastError()
    return int(dup.value), 0


def _default_device_io_control(
    handle: int,
    ioctl: int,
    in_bytes: bytes,
    out_len: int,
) -> tuple[bool, int, int, str]:
    """Issue a DeviceIoControl. Returns ``(ok, last_error, bytes_returned, out_hex)``.

    ``out_hex`` is the hex of the output buffer truncated to ``bytes_returned``.
    """
    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover
        return False, 0, 0, ""
    in_buf = c.create_string_buffer(in_bytes) if in_bytes else None
    out_buf = c.create_string_buffer(out_len) if out_len else None
    bytes_returned = w.DWORD(0)
    ok = bool(
        kernel32.DeviceIoControl(
            handle,
            ioctl,
            c.cast(in_buf, c.c_void_p) if in_buf is not None else None,
            len(in_bytes),
            c.cast(out_buf, c.c_void_p) if out_buf is not None else None,
            out_len,
            c.byref(bytes_returned),
            None,
        )
    )
    last_error = 0 if ok else kernel32.GetLastError()
    if out_buf is not None and bytes_returned.value:
        out_hex = bytes(out_buf.raw[: bytes_returned.value]).hex()
    else:
        out_hex = ""
    return ok, last_error, bytes_returned.value, out_hex


def _default_open_hid_write_handle(path: str) -> tuple[Optional[int], int]:
    """Open the VID_20BC HID path for synchronous writes (vendor's write
    handle shape: GENERIC_WRITE, shared, no overlapped flag).
    """
    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover - non-Windows
        return None, 0
    handle = kernel32.CreateFileW(
        path,
        HID_WRITE_OPEN_DESIRED_ACCESS,
        HID_WRITE_OPEN_SHARE_MODE,
        None,
        OPEN_EXISTING,
        HID_WRITE_OPEN_FLAGS_AND_ATTRIBUTES,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or not handle:
        return None, kernel32.GetLastError()
    return int(handle), 0


def _default_open_hid_read_handle(path: str) -> tuple[Optional[int], int]:
    """Open the VID_20BC HID path for overlapped reads (vendor's read
    handle shape: GENERIC_READ|GENERIC_WRITE, shared, FILE_FLAG_OVERLAPPED).
    """
    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover - non-Windows
        return None, 0
    handle = kernel32.CreateFileW(
        path,
        HID_READ_OPEN_DESIRED_ACCESS,
        HID_READ_OPEN_SHARE_MODE,
        None,
        OPEN_EXISTING,
        HID_READ_OPEN_FLAGS_AND_ATTRIBUTES,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or not handle:
        return None, kernel32.GetLastError()
    return int(handle), 0


def _default_close_handle(handle: int) -> bool:
    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover
        return False
    return bool(kernel32.CloseHandle(handle))


# ---------------------------------------------------------------------------
# Path selection
# ---------------------------------------------------------------------------

def _choose_xusb_path(paths: list[str]) -> Optional[str]:
    """Pick the canonical USB-bus path among enumerated XUSB MI_00 paths.

    The captured vendor trace used a ``\\\\?\\usb#`` prefix; prefer that
    over any composite-bus aliases.
    """
    if not paths:
        return None
    return sorted(
        paths,
        key=lambda p: (0 if p.lower().startswith(r"\\?\usb#") else 1, len(p), p.lower()),
    )[0]


# ---------------------------------------------------------------------------
# Public function: fire the 7-IOCTL trigger sequence
# ---------------------------------------------------------------------------

def fire_trigger_sequence(
    *,
    enumerate_xusb_paths: Callable[[], list[str]] = _default_enumerate_xusb_paths,
    open_xusb_handle: Callable[[str], tuple[Optional[int], int]] = _default_open_xusb_handle,
    duplicate_handle: Callable[[int], tuple[Optional[int], int]] = _default_duplicate_handle,
    device_io_control: Callable[[int, int, bytes, int], tuple[bool, int, int, str]] = _default_device_io_control,
    close_handle: Callable[[int], bool] = _default_close_handle,
    check_hidden_visible: Callable[[], bool] = _default_check_hidden_visible,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    inter_trigger_gap_s: float = DEFAULT_INTER_TRIGGER_GAP_S,
    poll_timeout_s: float = 2.0,
    poll_interval_s: float = 0.100,
    explicit_path: Optional[str] = None,
) -> TriggerResult:
    """Fire the captured 7-IOCTL XUSB trigger and poll for VID_20BC enumeration.

    Sequence (all on VID_413D&PID_2104 MI_00 XUSB-compat interface):

    1. ``IOCTL_GET_INFORMATION`` (0x80006000) on direct handle, no input,
       expect 12-byte identify response.
    2. ``DuplicateHandle`` direct -> dup with ``DUPLICATE_SAME_ACCESS``.
    3. ``IOCTL_GET_STATE`` (0x8000E00C) on dup, in=``010100``, expect 29 bytes.
    4. ``IOCTL_GET_LED`` (0x8000E008) on dup, in=``010100``, expect 3 bytes.
    5. ``IOCTL_SET_STATE`` (0x8000A010) on dup, in=``0006000001`` (preamble).
    6. ``IOCTL_SET_STATE`` (0x8000A010) on dup, in=``0000550102`` (trigger 1).
    7. sleep ``inter_trigger_gap_s``.
    8. ``IOCTL_SET_STATE`` (0x8000A010) on dup, in=``000055aa02`` (trigger 2).
    9. sleep ``inter_trigger_gap_s``.
    10. ``IOCTL_SET_STATE`` (0x8000A010) on dup, in=``0000552302`` (trigger 3).
    11. Poll for VID_20BC enumeration every ``poll_interval_s`` up to
        ``poll_timeout_s``.

    Direct + dup handles are closed before returning, regardless of outcome.

    All Win32 calls are reachable via DI-injectable kwargs so the function
    is fully testable on any platform without a real controller.

    Returns a ``TriggerResult`` whose ``outcome`` is one of:

    - ``SUCCESS``: all 7 IOCTLs ok and VID_20BC enumerated within the poll.
    - ``NO_XUSB_PATH``: ``enumerate_xusb_paths`` returned no candidates.
    - ``OPEN_FAILED``: ``open_xusb_handle`` failed.
    - ``DUPLICATE_FAILED``: ``duplicate_handle`` failed.
    - ``IOCTL_FAILED``: at least one IOCTL returned ok=false.
    - ``HIDDEN_NOT_VISIBLE``: all IOCTLs ok but VID_20BC did not appear.
    """
    result = TriggerResult(outcome=TriggerOutcome.SUCCESS)

    # 0. Pick the target path
    target_path = explicit_path or _choose_xusb_path(enumerate_xusb_paths())
    result.target_path = target_path
    if not target_path:
        result.outcome = TriggerOutcome.NO_XUSB_PATH
        result.error_message = (
            "No VID_413D&PID_2104 MI_00 XUSB interface found. "
            "Is the controller connected and in public XInput mode?"
        )
        return result

    # 1. Open direct handle
    direct_handle, open_err = open_xusb_handle(target_path)
    if direct_handle is None:
        result.open_ok = False
        result.open_last_error = open_err
        result.outcome = TriggerOutcome.OPEN_FAILED
        result.error_message = (
            f"CreateFileW on {target_path!r} failed (last_error={open_err})."
        )
        return result
    result.open_ok = True

    dup_handle: Optional[int] = None
    direct_closed = False
    t0 = clock()
    try:
        # Step 1: identify on direct handle
        ok, err, n, out_hex = device_io_control(
            direct_handle, IOCTL_GET_INFORMATION, b"", GET_INFORMATION_OUT_LEN,
        )
        result.ioctl_attempts.append(IoctlAttempt(
            step=1, label="identify", ioctl=IOCTL_GET_INFORMATION,
            in_hex="", ok=ok, last_error=err, bytes_returned=n, out_hex=out_hex,
            elapsed_ms=round((clock() - t0) * 1000),
        ))
        if not ok:
            result.outcome = TriggerOutcome.IOCTL_FAILED
            result.error_message = (
                f"IOCTL_GET_INFORMATION failed at step 1 (last_error={err})."
            )
            return result

        # Step 2: duplicate handle (subsequent IOCTLs use the duplicate)
        dup_handle, dup_err = duplicate_handle(direct_handle)
        if dup_handle is None:
            result.duplicate_ok = False
            result.duplicate_last_error = dup_err
            result.outcome = TriggerOutcome.DUPLICATE_FAILED
            result.error_message = (
                f"DuplicateHandle failed (last_error={dup_err})."
            )
            return result
        result.duplicate_ok = True

        # Pre-trigger metadata IOCTLs on dup: GET_STATE, GET_LED, SET_STATE preamble.
        pre_trigger_steps: list[tuple[int, str, int, bytes, int]] = [
            (2, "get_state", IOCTL_GET_STATE, GET_STATE_INPUT, GET_STATE_OUT_LEN),
            (3, "get_led", IOCTL_GET_LED, GET_LED_INPUT, GET_LED_OUT_LEN),
            (4, "set_state_preamble", IOCTL_SET_STATE, SET_STATE_PREAMBLE_INPUT, 0),
        ]
        for step, label, ioctl, in_bytes, out_len in pre_trigger_steps:
            ok, err, n, out_hex = device_io_control(dup_handle, ioctl, in_bytes, out_len)
            result.ioctl_attempts.append(IoctlAttempt(
                step=step, label=label, ioctl=ioctl,
                in_hex=in_bytes.hex(), ok=ok, last_error=err,
                bytes_returned=n, out_hex=out_hex,
                elapsed_ms=round((clock() - t0) * 1000),
            ))
            if not ok:
                result.outcome = TriggerOutcome.IOCTL_FAILED
                result.error_message = (
                    f"IOCTL {ioctl:#010x} ({label}) failed at step {step} "
                    f"(last_error={err})."
                )
                return result

        # Close direct handle BEFORE the trigger packets (matches vendor's
        # captured pattern; the replay script + every successful
        # verification run used this ordering. Late-close has zero
        # supporting data points — paranoid parity is cheap insurance
        # against an uncharacterized failure mode.
        if close_handle(direct_handle):
            direct_closed = True

        # Trigger packets (all SET_STATE on dup, with ~30ms inter-packet gaps).
        trigger_steps: list[tuple[int, str, bytes, float]] = [
            (5, "trigger_1", TRIGGER_PAYLOAD_1, 0.0),
            (6, "trigger_2", TRIGGER_PAYLOAD_2, inter_trigger_gap_s),
            (7, "trigger_3", TRIGGER_PAYLOAD_3, inter_trigger_gap_s),
        ]
        for step, label, payload, gap_before in trigger_steps:
            if gap_before > 0:
                sleep(gap_before)
            ok, err, n, out_hex = device_io_control(
                dup_handle, IOCTL_SET_STATE, payload, 0,
            )
            result.ioctl_attempts.append(IoctlAttempt(
                step=step, label=label, ioctl=IOCTL_SET_STATE,
                in_hex=payload.hex(), ok=ok, last_error=err,
                bytes_returned=n, out_hex=out_hex,
                elapsed_ms=round((clock() - t0) * 1000),
            ))
            if not ok:
                result.outcome = TriggerOutcome.IOCTL_FAILED
                result.error_message = (
                    f"IOCTL {IOCTL_SET_STATE:#010x} ({label}) failed at step "
                    f"{step} (last_error={err})."
                )
                return result

        # Step 8: poll for VID_20BC enumeration
        poll_start = clock()
        poll_deadline = poll_start + max(0.0, poll_timeout_s)
        visible = False
        while True:
            if check_hidden_visible():
                visible = True
                break
            if clock() >= poll_deadline:
                break
            sleep(poll_interval_s)

        if visible:
            result.outcome = TriggerOutcome.SUCCESS
            result.visible_after_ms = round((clock() - poll_start) * 1000)
        else:
            result.outcome = TriggerOutcome.HIDDEN_NOT_VISIBLE
            result.error_message = (
                f"All 7 IOCTLs returned ok=true but VID_20BC&PID_5080 did not "
                f"enumerate within {poll_timeout_s:.1f}s. Likely a transient "
                f"USB enumeration race; retry recommended."
            )

        return result
    finally:
        if dup_handle is not None:
            try:
                close_handle(dup_handle)
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.debug("close_handle(dup_handle) raised", exc_info=True)
        if not direct_closed:
            try:
                close_handle(direct_handle)
            except Exception:  # pragma: no cover
                logger.debug("close_handle(direct_handle) raised", exc_info=True)


# ---------------------------------------------------------------------------
# Retry wrapper: ensure_dual_mode
# ---------------------------------------------------------------------------

DEFAULT_INTER_ATTEMPT_PAUSE_S = 0.0  # Probe 4: zero wait recovered ~100% of cases
DEFAULT_FINAL_RETRY_PAUSE_S = 30.0  # last-resort wait for persistent stuck-state
DEFAULT_MAX_RETRIES = 2  # 2 retries on top of initial = 3 total attempts (~99.9%)


class EnsureDualModeStatus(Enum):
    """Outcome of :func:`ensure_dual_mode` (retry-wrapped trigger)."""

    ESTABLISHED = "established"  # VID_20BC enumerated on some attempt
    NO_XUSB_PATH = "no_xusb_path"  # propagated hard failure
    OPEN_FAILED = "open_failed"  # propagated hard failure
    DUPLICATE_FAILED = "duplicate_failed"  # propagated hard failure
    IOCTL_FAILED = "ioctl_failed"  # propagated hard failure
    EXHAUSTED_RETRIES = "exhausted_retries"  # all attempts had HIDDEN_NOT_VISIBLE


@dataclass
class EnsureDualModeResult:
    status: EnsureDualModeStatus
    error_message: Optional[str] = None
    target_path: Optional[str] = None
    attempts: list[TriggerResult] = field(default_factory=list)
    visible_after_ms: Optional[int] = None  # set on ESTABLISHED


_HARD_FAILURE_STATUS_BY_OUTCOME: dict[TriggerOutcome, EnsureDualModeStatus] = {
    TriggerOutcome.NO_XUSB_PATH: EnsureDualModeStatus.NO_XUSB_PATH,
    TriggerOutcome.OPEN_FAILED: EnsureDualModeStatus.OPEN_FAILED,
    TriggerOutcome.DUPLICATE_FAILED: EnsureDualModeStatus.DUPLICATE_FAILED,
    TriggerOutcome.IOCTL_FAILED: EnsureDualModeStatus.IOCTL_FAILED,
}


def ensure_dual_mode(
    *,
    fire_trigger: Callable[..., TriggerResult] = fire_trigger_sequence,
    sleep: Callable[[float], None] = time.sleep,
    max_retries: int = DEFAULT_MAX_RETRIES,
    inter_attempt_pause_s: float = DEFAULT_INTER_ATTEMPT_PAUSE_S,
    final_retry_pause_s: float = DEFAULT_FINAL_RETRY_PAUSE_S,
    trigger_kwargs: Optional[dict] = None,
) -> EnsureDualModeResult:
    """Fire the trigger; retry on transient ``HIDDEN_NOT_VISIBLE`` failures.

    Schedule (with default ``max_retries=2``, total 3 attempts):

    1. Attempt 1: fire the trigger.
    2. If outcome is ``HIDDEN_NOT_VISIBLE``, sleep ``inter_attempt_pause_s``
       (default 0.0 — Probe 4 showed zero-wait immediate retry recovers
       ~100%) and try again.
    3. If still ``HIDDEN_NOT_VISIBLE`` and this is the final retry, sleep
       ``final_retry_pause_s`` (default 30.0s — for the rare persistent
       stuck-state case) and try again.
    4. After all attempts exhausted with ``HIDDEN_NOT_VISIBLE``, return
       ``EXHAUSTED_RETRIES``.

    Hard failures (``NO_XUSB_PATH``, ``OPEN_FAILED``, ``DUPLICATE_FAILED``,
    ``IOCTL_FAILED``) short-circuit on the first occurrence — they're not
    transient enumeration races and retrying won't help.

    All ``TriggerResult`` instances from each attempt are recorded in the
    ``attempts`` list for telemetry / debugging.

    ``trigger_kwargs`` is forwarded as kwargs to the underlying
    ``fire_trigger`` callable (which defaults to ``fire_trigger_sequence``);
    use it to inject DI seams in tests.
    """
    trigger_kwargs = trigger_kwargs or {}
    attempts: list[TriggerResult] = []
    total_attempts = max_retries + 1

    for attempt_idx in range(total_attempts):
        if attempt_idx > 0:
            is_final_retry = attempt_idx == max_retries
            pause = final_retry_pause_s if is_final_retry else inter_attempt_pause_s
            if pause > 0:
                logger.info(
                    "Trigger retry pause: sleeping %.1fs before attempt %d/%d "
                    "(prior attempt was HIDDEN_NOT_VISIBLE)",
                    pause, attempt_idx + 1, total_attempts,
                )
            sleep(pause)

        result = fire_trigger(**trigger_kwargs)
        attempts.append(result)

        if result.outcome == TriggerOutcome.SUCCESS:
            logger.info(
                "Dual-mode established on attempt %d/%d (visible_after=%sms)",
                attempt_idx + 1, total_attempts, result.visible_after_ms,
            )
            return EnsureDualModeResult(
                status=EnsureDualModeStatus.ESTABLISHED,
                target_path=result.target_path,
                attempts=attempts,
                visible_after_ms=result.visible_after_ms,
            )

        if result.outcome != TriggerOutcome.HIDDEN_NOT_VISIBLE:
            # Hard failure — propagate immediately, don't retry.
            logger.warning(
                "Trigger hard failure on attempt %d/%d: %s — not retrying",
                attempt_idx + 1, total_attempts, result.outcome.value,
            )
            return EnsureDualModeResult(
                status=_HARD_FAILURE_STATUS_BY_OUTCOME[result.outcome],
                error_message=result.error_message,
                target_path=result.target_path,
                attempts=attempts,
            )

        logger.warning(
            "Trigger transient failure on attempt %d/%d: HIDDEN_NOT_VISIBLE "
            "(all 7 IOCTLs ok but VID_20BC not enumerated)",
            attempt_idx + 1, total_attempts,
        )

    # All attempts exhausted with HIDDEN_NOT_VISIBLE.
    last_path = attempts[-1].target_path if attempts else None
    return EnsureDualModeResult(
        status=EnsureDualModeStatus.EXHAUSTED_RETRIES,
        error_message=(
            f"VID_20BC&PID_5080 did not enumerate after {total_attempts} "
            "trigger attempts. The controller may be in a stuck state — try "
            "unplugging and replugging it."
        ),
        target_path=last_path,
        attempts=attempts,
    )


# ---------------------------------------------------------------------------
# HID session: open write + read handles, issue descriptor IOCTLs
# ---------------------------------------------------------------------------

class HidSessionStatus(Enum):
    """Outcome of :func:`open_hid_session`."""

    OPENED = "opened"  # both handles opened; descriptor IOCTLs may have failed but handles are usable
    NO_HIDDEN_PATH = "no_hidden_path"  # VID_20BC HID path not enumerated
    WRITE_HANDLE_OPEN_FAILED = "write_handle_open_failed"  # CreateFileW on write shape failed
    READ_HANDLE_OPEN_FAILED = "read_handle_open_failed"  # write opened but read shape failed; write closed before return


@dataclass
class HidOpenAttempt:
    role: str  # "write" or "read"
    path: str
    ok: bool
    last_error: int
    desired_access: int
    share_mode: int
    flags_and_attributes: int


@dataclass
class HidSessionResult:
    """Result of opening a VID_20BC HID session (write + read handles).

    On ``OPENED``, the caller OWNS ``write_handle`` and ``read_handle`` —
    they remain open and the caller is responsible for closing them at
    shutdown via :func:`_default_close_handle` (or whatever close seam was
    in use). On any non-``OPENED`` status, both handles are guaranteed to
    be ``None`` and any partially-opened handle was closed before return.
    """

    status: HidSessionStatus
    target_path: Optional[str] = None
    write_handle: Optional[int] = None
    read_handle: Optional[int] = None
    write_open: Optional[HidOpenAttempt] = None
    read_open: Optional[HidOpenAttempt] = None
    descriptor_attempts: list[IoctlAttempt] = field(default_factory=list)
    error_message: Optional[str] = None


def _choose_hidden_path(paths: list[str]) -> Optional[str]:
    """Pick the canonical HID path among enumerated VID_20BC interfaces.

    The captured trace's path uses a ``\\\\?\\hid#`` prefix; prefer that.
    """
    if not paths:
        return None
    return sorted(
        paths,
        key=lambda p: (0 if p.lower().startswith(r"\\?\hid#") else 1, len(p), p.lower()),
    )[0]


def open_hid_session(
    *,
    enumerate_hidden_paths: Callable[[], list[str]] = _default_enumerate_hidden_paths,
    open_hid_write_handle: Callable[[str], tuple[Optional[int], int]] = _default_open_hid_write_handle,
    open_hid_read_handle: Callable[[str], tuple[Optional[int], int]] = _default_open_hid_read_handle,
    device_io_control: Callable[[int, int, bytes, int], tuple[bool, int, int, str]] = _default_device_io_control,
    close_handle: Callable[[int], bool] = _default_close_handle,
    explicit_path: Optional[str] = None,
) -> HidSessionResult:
    """Open the VID_20BC HID write+read handles + issue descriptor IOCTLs.

    Sequence:

    1. Enumerate VID_20BC&PID_5080 HID paths; pick canonical via
       :func:`_choose_hidden_path`. If none, return ``NO_HIDDEN_PATH``.
    2. Open the write handle (GENERIC_WRITE, shared, synchronous). On
       failure, return ``WRITE_HANDLE_OPEN_FAILED``.
    3. Open the read handle (GENERIC_READ|GENERIC_WRITE, shared,
       FILE_FLAG_OVERLAPPED). On failure, close the write handle and
       return ``READ_HANDLE_OPEN_FAILED``.
    4. Issue descriptor IOCTL ``IOCTL_HID_GET_COLLECTION_INFORMATION``
       (0x000B01A8) on the write handle, no input, expect 12 bytes.
    5. Issue descriptor IOCTL ``IOCTL_HID_GET_COLLECTION_DESCRIPTOR``
       (0x000B0193) on the write handle, no input, expect 1636 bytes.
    6. Return ``OPENED`` with both handles + descriptor attempts.

    A minimal-lifecycle run sustained 30s without these descriptor
    IOCTLs, so they're probably NOT load-bearing for keepalive — failures
    here do NOT invalidate the session. Both handles are still returned.

    On ``OPENED``, the caller takes ownership of both handles and is
    responsible for closing them on shutdown.
    """
    result = HidSessionResult(status=HidSessionStatus.OPENED)

    target_path = explicit_path or _choose_hidden_path(enumerate_hidden_paths())
    result.target_path = target_path
    if not target_path:
        result.status = HidSessionStatus.NO_HIDDEN_PATH
        result.error_message = (
            "No VID_20BC&PID_5080 HID interface found. The controller may "
            "not be in dual-mode yet — fire the trigger first."
        )
        return result

    write_handle, write_err = open_hid_write_handle(target_path)
    result.write_open = HidOpenAttempt(
        role="write",
        path=target_path,
        ok=write_handle is not None,
        last_error=write_err,
        desired_access=HID_WRITE_OPEN_DESIRED_ACCESS,
        share_mode=HID_WRITE_OPEN_SHARE_MODE,
        flags_and_attributes=HID_WRITE_OPEN_FLAGS_AND_ATTRIBUTES,
    )
    if write_handle is None:
        result.status = HidSessionStatus.WRITE_HANDLE_OPEN_FAILED
        result.error_message = (
            f"CreateFileW on HID path {target_path!r} failed in write mode "
            f"(last_error={write_err})."
        )
        return result

    read_handle, read_err = open_hid_read_handle(target_path)
    result.read_open = HidOpenAttempt(
        role="read",
        path=target_path,
        ok=read_handle is not None,
        last_error=read_err,
        desired_access=HID_READ_OPEN_DESIRED_ACCESS,
        share_mode=HID_READ_OPEN_SHARE_MODE,
        flags_and_attributes=HID_READ_OPEN_FLAGS_AND_ATTRIBUTES,
    )
    if read_handle is None:
        # Write opened but read failed — clean up the write handle so we
        # don't leak it; surface the failure.
        try:
            close_handle(write_handle)
        except Exception:  # pragma: no cover - best-effort cleanup
            logger.debug("close_handle(write_handle) raised", exc_info=True)
        result.status = HidSessionStatus.READ_HANDLE_OPEN_FAILED
        result.error_message = (
            f"CreateFileW on HID path {target_path!r} failed in read mode "
            f"(last_error={read_err}). Write handle was opened then closed."
        )
        return result

    # Both handles open — issue descriptor IOCTLs on write handle for
    # vendor parity. Failures here are non-fatal; we still return OPENED.
    descriptor_steps: list[tuple[str, int, int]] = [
        ("hid_get_collection_information", IOCTL_HID_GET_COLLECTION_INFORMATION, HID_COLLECTION_INFORMATION_OUT_LEN),
        ("hid_get_collection_descriptor", IOCTL_HID_GET_COLLECTION_DESCRIPTOR, HID_COLLECTION_DESCRIPTOR_OUT_LEN),
    ]
    for step_idx, (label, ioctl, out_len) in enumerate(descriptor_steps, start=1):
        ok, err, n, out_hex = device_io_control(write_handle, ioctl, b"", out_len)
        result.descriptor_attempts.append(IoctlAttempt(
            step=step_idx, label=label, ioctl=ioctl,
            in_hex="", ok=ok, last_error=err,
            bytes_returned=n, out_hex=out_hex,
            elapsed_ms=0,  # descriptor IOCTLs are fast; cumulative timing not informative here
        ))
        if not ok:
            logger.warning(
                "HID descriptor IOCTL %s (%#010x) failed (last_error=%d) — "
                "non-fatal, continuing.",
                label, ioctl, err,
            )

    result.write_handle = write_handle
    result.read_handle = read_handle
    return result


# ---------------------------------------------------------------------------
# Keepalive lifecycle: KeepaliveLoop
# ---------------------------------------------------------------------------

DEFAULT_KEEPALIVE_CADENCE_S = 7.5  # ~7.5-8.0s between cycles per vendor active-protocol trace
DEFAULT_KEEPALIVE_INTER_WRITE_DELAY_S = 0.011  # ~10-12ms inter-write gap (vendor cadence)
DEFAULT_KEEPALIVE_READ_TIMEOUT_MS = 500  # vendor saw 30-39ms; 500ms is conservative
DEFAULT_KEEPALIVE_INITIAL_CYCLE_DELAY_S = 0.5  # delay before first cycle's writes (matches the observed keepalive pattern)


@dataclass
class OverlappedReadContext:
    """State for a single pending overlapped ReadFile.

    In production, ``overlapped`` is a ctypes ``_OVERLAPPED`` struct,
    ``buffer`` is a ctypes 64-byte string buffer, and ``event_handle`` is
    a Win32 manual-reset event handle from ``CreateEventW``. In tests,
    these can be sentinels (``object()`` etc.) — only the
    ``sequence_num`` and ``completed`` fields are inspected by tests.
    """

    overlapped: Any = None
    event_handle: Optional[int] = None
    buffer: Any = None
    sequence_num: int = 0
    completed: bool = False
    create_last_error: int = 0


@dataclass
class KeepaliveCycleRecord:
    cycle_num: int
    write_a_ok: bool
    write_a_last_error: int
    write_a_bytes: int
    write_b_ok: bool
    write_b_last_error: int
    write_b_bytes: int
    read_ok: Optional[bool]
    read_last_error: Optional[int]
    read_bytes: Optional[int]
    read_response_prefix: Optional[str]
    read_reposted: bool
    read_repost_last_error: Optional[int]


@dataclass
class KeepaliveSnapshot:
    is_running: bool
    cycle_count: int
    successful_write_pairs: int
    successful_reads: int
    write_errors: int
    read_errors: int
    last_cycle_started_ts: Optional[float]
    # Populated when the loop thread exits via an unhandled exception.
    # Format: ``f"{type(exc).__name__}: {exc}"``. Callers (e.g.,
    # main_zd's monitor logic) can read this alongside ``is_running ==
    # False`` to surface a status-bar / dialog message and prompt the
    # operator to restart the wrapper.
    last_error: Optional[str] = None


# ---------------------------------------------------------------------------
# Default keepalive helpers
# ---------------------------------------------------------------------------

def _default_write_hid(handle: int, payload: bytes) -> tuple[bool, int, int]:
    """Synchronous WriteFile. Returns ``(ok, last_error, bytes_written)``."""
    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover - non-Windows
        return False, 0, 0
    buffer = c.create_string_buffer(payload)
    written = w.DWORD(0)
    ok = bool(
        kernel32.WriteFile(
            handle,
            c.cast(buffer, c.c_void_p),
            len(payload),
            c.byref(written),
            None,
        )
    )
    return ok, 0 if ok else kernel32.GetLastError(), written.value


def _default_create_overlapped_context() -> OverlappedReadContext:
    """Allocate an OVERLAPPED struct + manual-reset event + 64-byte buffer."""
    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover - non-Windows
        return OverlappedReadContext()
    ctx = OverlappedReadContext()
    event_handle = kernel32.CreateEventW(None, True, False, None)
    if not event_handle:
        ctx.create_last_error = kernel32.GetLastError()
        return ctx
    ctx.event_handle = int(event_handle)
    ctx.overlapped = _OVERLAPPED()
    c.memset(c.byref(ctx.overlapped), 0, c.sizeof(ctx.overlapped))
    ctx.overlapped.hEvent = event_handle
    ctx.buffer = c.create_string_buffer(HID_REPORT_SIZE)
    return ctx


def _default_post_overlapped_read(
    handle: int, ctx: OverlappedReadContext,
) -> tuple[bool, int]:
    """Issue a 64-byte overlapped ``ReadFile`` against ``handle``.

    A successful overlapped post returns either ``ok=True`` (rare —
    immediate completion) or ``ok=False`` with ``GetLastError() ==
    ERROR_IO_PENDING`` (the normal pending case). Both are valid; the
    caller treats either as "post succeeded, will wait via
    GetOverlappedResult". Returns ``(post_succeeded_or_pending, last_error)``.
    """
    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover - non-Windows
        return False, 0
    if ctx.event_handle is None or ctx.overlapped is None or ctx.buffer is None:
        return False, ctx.create_last_error
    # Reset the OVERLAPPED struct's Internal/Offset fields between posts;
    # only hEvent must remain valid.
    saved_event = ctx.overlapped.hEvent
    c.memset(c.byref(ctx.overlapped), 0, c.sizeof(ctx.overlapped))
    ctx.overlapped.hEvent = saved_event
    ctx.completed = False
    ok = bool(
        kernel32.ReadFile(
            handle,
            c.cast(ctx.buffer, c.c_void_p),
            HID_REPORT_SIZE,
            None,
            c.byref(ctx.overlapped),
        )
    )
    if ok:
        return True, 0
    err = kernel32.GetLastError()
    return err == ERROR_IO_PENDING, err


def _default_wait_for_overlapped(
    ctx: OverlappedReadContext, timeout_ms: int,
) -> tuple[bool, int, int, str]:
    """Wait for a pending overlapped read, then GetOverlappedResult.

    Returns ``(ok, last_error_or_wait_status, bytes_read, response_hex)``.
    The error code on failure is either a Win32 GetLastError() value or
    ``WAIT_TIMEOUT`` (``0x00000102``) if the wait timed out.
    """
    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover - non-Windows
        return False, 0, 0, ""
    if ctx.event_handle is None or ctx.overlapped is None:
        return False, ctx.create_last_error, 0, ""
    wait_result = kernel32.WaitForSingleObject(ctx.event_handle, timeout_ms)
    if wait_result != WAIT_OBJECT_0:
        return False, int(wait_result), 0, ""
    bytes_read = w.DWORD(0)
    ok = bool(
        kernel32.GetOverlappedResult(
            None,  # handle is encoded inside OVERLAPPED.Internal; pass NULL
            c.byref(ctx.overlapped),
            c.byref(bytes_read),
            False,
        )
    )
    if not ok:
        return False, kernel32.GetLastError(), 0, ""
    ctx.completed = True
    response_hex = bytes(ctx.buffer.raw[: bytes_read.value]).hex()
    return True, 0, bytes_read.value, response_hex


def _default_cancel_overlapped(
    handle: int, ctx: OverlappedReadContext,
) -> tuple[bool, int]:
    """Cancel a pending overlapped read + drain via WaitForSingleObject."""
    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover - non-Windows
        return False, 0
    if ctx.event_handle is None or ctx.overlapped is None:
        return False, ctx.create_last_error
    if ctx.completed:
        return True, 0
    ok = bool(kernel32.CancelIoEx(handle, c.byref(ctx.overlapped)))
    last_error = 0 if ok else kernel32.GetLastError()
    # Drain so the kernel can release the OVERLAPPED struct safely.
    kernel32.WaitForSingleObject(ctx.event_handle, 1000)
    bytes_read = w.DWORD(0)
    kernel32.GetOverlappedResult(
        None, c.byref(ctx.overlapped), c.byref(bytes_read), False,
    )
    return ok, last_error


def _default_destroy_overlapped_context(ctx: OverlappedReadContext) -> None:
    """Close the event handle held by an overlapped context."""
    try:
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover - non-Windows
        return
    if ctx.event_handle is not None:
        try:
            kernel32.CloseHandle(ctx.event_handle)
        except Exception:  # pragma: no cover
            logger.debug("CloseHandle(event) raised", exc_info=True)
        ctx.event_handle = None


@dataclass
class HidKeepaliveIo:
    """Dependency-injection container for HID-side keepalive operations.

    Grouping these 6 callables (vs adding them as ~6 narrow kwargs to
    ``KeepaliveLoop.__init__``) keeps the class signature manageable.
    Tests can construct an instance with substituted callables to
    exercise the loop without ctypes.
    """

    write: Callable[[int, bytes], tuple[bool, int, int]] = field(
        default_factory=lambda: _default_write_hid
    )
    create_overlapped: Callable[[], OverlappedReadContext] = field(
        default_factory=lambda: _default_create_overlapped_context
    )
    post_read: Callable[[int, OverlappedReadContext], tuple[bool, int]] = field(
        default_factory=lambda: _default_post_overlapped_read
    )
    wait_read: Callable[[OverlappedReadContext, int], tuple[bool, int, int, str]] = field(
        default_factory=lambda: _default_wait_for_overlapped
    )
    cancel_read: Callable[[int, OverlappedReadContext], tuple[bool, int]] = field(
        default_factory=lambda: _default_cancel_overlapped
    )
    destroy_overlapped: Callable[[OverlappedReadContext], None] = field(
        default_factory=lambda: _default_destroy_overlapped_context
    )


class KeepaliveLoop:
    """Background thread that sustains VID_20BC dual-mode via the
    vendor-observed write/read keepalive pattern.

    Lifecycle:

    1. ``__init__`` sets up state but does not start the thread.
    2. ``start(write_handle, read_handle)`` posts an initial overlapped
       ReadFile and launches the loop thread. The handles are NOT owned
       by the loop — the caller must keep them open for the loop's
       lifetime and close them after ``stop()`` returns.
    3. The loop runs cycles every ``cadence_s`` until ``stop()`` is called
       or the thread sees a write failure exceeding any caller policy
       (current default: continue on errors, just record counters).
    4. ``stop()`` signals the stop event, joins the thread, cancels the
       outstanding overlapped read, and destroys the overlapped context.
       Idempotent: safe to call multiple times.

    Per-cycle sequence (mirrors vendor's active-protocol trace):

    - ``write(write_handle, KEEPALIVE_WRITE_A_PAYLOAD)`` (64 bytes)
    - ``sleep(inter_write_delay_s)`` (~11ms)
    - ``write(write_handle, KEEPALIVE_WRITE_B_PAYLOAD)`` (64 bytes)
    - ``wait_read(ctx, read_timeout_ms)`` for outstanding overlapped read
      (vendor saw 30-39ms; we use 500ms timeout)
    - ``post_read(read_handle, new_ctx)`` to re-arm the next cycle's read
    """

    def __init__(
        self,
        *,
        io: Optional[HidKeepaliveIo] = None,
        clock: Callable[[], float] = time.monotonic,
        cadence_s: float = DEFAULT_KEEPALIVE_CADENCE_S,
        inter_write_delay_s: float = DEFAULT_KEEPALIVE_INTER_WRITE_DELAY_S,
        read_timeout_ms: int = DEFAULT_KEEPALIVE_READ_TIMEOUT_MS,
        initial_cycle_delay_s: float = DEFAULT_KEEPALIVE_INITIAL_CYCLE_DELAY_S,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._io = io if io is not None else HidKeepaliveIo()
        self._clock = clock
        self._sleep = sleep
        self._cadence_s = cadence_s
        self._inter_write_delay_s = inter_write_delay_s
        self._read_timeout_ms = read_timeout_ms
        self._initial_cycle_delay_s = initial_cycle_delay_s

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._write_handle: Optional[int] = None
        self._read_handle: Optional[int] = None
        self._pending_read: Optional[OverlappedReadContext] = None
        self._cycles: list[KeepaliveCycleRecord] = []
        self._successful_write_pairs = 0
        self._successful_reads = 0
        self._write_errors = 0
        self._read_errors = 0
        self._last_cycle_started_ts: Optional[float] = None
        self._next_read_seq = 1
        self._last_error: Optional[str] = None

    # ----- public lifecycle --------------------------------------------

    def start(self, write_handle: int, read_handle: int) -> None:
        """Post initial overlapped read + launch the loop thread.

        The handles are borrowed; caller closes them after ``stop()``.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._write_handle = write_handle
        self._read_handle = read_handle
        self._cycles = []
        self._successful_write_pairs = 0
        self._successful_reads = 0
        self._write_errors = 0
        self._read_errors = 0
        self._last_cycle_started_ts = None
        self._next_read_seq = 1
        self._last_error = None

        # Post the initial overlapped ReadFile BEFORE launching the
        # thread so the first cycle's wait_read has something to wait on.
        ctx = self._io.create_overlapped()
        ctx.sequence_num = self._next_read_seq
        post_ok, post_err = self._io.post_read(read_handle, ctx)
        if not post_ok:
            logger.warning(
                "Keepalive initial overlapped read post failed (last_error=%d)",
                post_err,
            )
            self._read_errors += 1
        self._pending_read = ctx
        self._next_read_seq += 1

        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="StandaloneTriggerKeepalive",
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal stop, join the thread, cancel + destroy the pending read.

        Idempotent. Does NOT close the write/read handles — those belong
        to the caller.
        """
        if self._thread is None:
            # Edge case: start() was never called, OR start() ran but the
            # thread never spawned. Still drain pending_read if any.
            self._cleanup_pending_read()
            return
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0 + self._read_timeout_ms / 1000.0)
        self._thread = None
        self._cleanup_pending_read()

    def _cleanup_pending_read(self) -> None:
        if self._pending_read is None:
            return
        if self._read_handle is not None and not self._pending_read.completed:
            try:
                self._io.cancel_read(self._read_handle, self._pending_read)
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.debug("cancel_read raised", exc_info=True)
        try:
            self._io.destroy_overlapped(self._pending_read)
        except Exception:  # pragma: no cover
            logger.debug("destroy_overlapped raised", exc_info=True)
        self._pending_read = None

    def health_snapshot(self) -> KeepaliveSnapshot:
        return KeepaliveSnapshot(
            is_running=self._thread is not None and self._thread.is_alive(),
            cycle_count=len(self._cycles),
            successful_write_pairs=self._successful_write_pairs,
            successful_reads=self._successful_reads,
            write_errors=self._write_errors,
            read_errors=self._read_errors,
            last_cycle_started_ts=self._last_cycle_started_ts,
            last_error=self._last_error,
        )

    @property
    def cycles(self) -> list[KeepaliveCycleRecord]:
        """Read-only view of recorded cycles. Useful for tests + telemetry."""
        return list(self._cycles)

    # ----- loop body (private) -----------------------------------------

    def _run_loop(self) -> None:
        # Wrap the entire loop body so any unhandled exception (e.g.,
        # from a misbehaving DI seam, an OS-side surprise inside the
        # default ctypes helpers, or a race we didn't anticipate) is
        # captured into ``self._last_error`` and surfaced via
        # ``health_snapshot()``. The thread exits cleanly; no auto-
        # restart (auto-healing hides bugs). Caller (typically the
        # service class's monitor) sees ``is_running=False`` +
        # ``last_error="..."`` and decides what to do.
        try:
            # Initial cycle delay before first writes (matches the observed keepalive pattern;
            # gives the controller a moment to settle after handle open).
            if self._stop_event.wait(self._initial_cycle_delay_s):
                return
            while not self._stop_event.is_set():
                self._run_one_cycle()
                # Inter-cycle wait: stop_event.wait returns True if signaled.
                if self._stop_event.wait(self._cadence_s):
                    return
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("KeepaliveLoop _run_loop crashed; thread exiting")

    def _run_one_cycle(self) -> None:
        cycle_num = len(self._cycles) + 1
        self._last_cycle_started_ts = self._clock()
        write_handle = self._write_handle
        read_handle = self._read_handle

        # Write A
        a_ok, a_err, a_bytes = self._io.write(write_handle, KEEPALIVE_WRITE_A_PAYLOAD)
        if not a_ok:
            self._write_errors += 1
            logger.warning(
                "Keepalive cycle %d: write_a failed (last_error=%d)",
                cycle_num, a_err,
            )

        # Inter-write gap
        self._sleep(self._inter_write_delay_s)

        # Write B
        b_ok, b_err, b_bytes = self._io.write(write_handle, KEEPALIVE_WRITE_B_PAYLOAD)
        if not b_ok:
            self._write_errors += 1
            logger.warning(
                "Keepalive cycle %d: write_b failed (last_error=%d)",
                cycle_num, b_err,
            )
        if a_ok and b_ok and a_bytes == HID_REPORT_SIZE and b_bytes == HID_REPORT_SIZE:
            self._successful_write_pairs += 1

        # Wait for outstanding overlapped read
        read_ok: Optional[bool] = None
        read_err: Optional[int] = None
        read_bytes: Optional[int] = None
        read_prefix: Optional[str] = None
        if self._pending_read is not None:
            r_ok, r_err, r_bytes, r_hex = self._io.wait_read(
                self._pending_read, self._read_timeout_ms,
            )
            read_ok, read_err, read_bytes = r_ok, r_err, r_bytes
            read_prefix = r_hex[:32] if r_hex else ""
            if r_ok:
                self._successful_reads += 1
            else:
                self._read_errors += 1
                logger.debug(
                    "Keepalive cycle %d: read wait failed (last_error/wait=%d)",
                    cycle_num, r_err,
                )

        # Re-post next overlapped read for the NEXT cycle to wait on.
        repost_ok = False
        repost_err: Optional[int] = None
        # Free the old context safely. destroy_overlapped only CLOSES the event
        # handle — if the read never completed (WAIT_TIMEOUT / GetOverlappedResult
        # failure left ctx.completed False), the kernel may still write into the
        # OVERLAPPED struct + buffer. Cancel + drain FIRST so the kernel releases
        # them before they're freed (overwriting _pending_read below), avoiding a
        # use-after-free. Mirrors _cleanup_pending_read's guard.
        old_ctx = self._pending_read
        if old_ctx is not None:
            if read_handle is not None and not old_ctx.completed:
                try:
                    self._io.cancel_read(read_handle, old_ctx)
                except Exception:  # pragma: no cover - best-effort cleanup
                    logger.debug("cancel_read raised on repost", exc_info=True)
            try:
                self._io.destroy_overlapped(old_ctx)
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.debug("destroy_overlapped raised on repost", exc_info=True)
        new_ctx = self._io.create_overlapped()
        new_ctx.sequence_num = self._next_read_seq
        self._next_read_seq += 1
        post_ok, post_err = self._io.post_read(read_handle, new_ctx)
        repost_ok = post_ok
        repost_err = post_err
        if not post_ok:
            self._read_errors += 1
            logger.warning(
                "Keepalive cycle %d: read repost failed (last_error=%d)",
                cycle_num, post_err,
            )
        self._pending_read = new_ctx

        self._cycles.append(KeepaliveCycleRecord(
            cycle_num=cycle_num,
            write_a_ok=a_ok, write_a_last_error=a_err, write_a_bytes=a_bytes,
            write_b_ok=b_ok, write_b_last_error=b_err, write_b_bytes=b_bytes,
            read_ok=read_ok, read_last_error=read_err, read_bytes=read_bytes,
            read_response_prefix=read_prefix,
            read_reposted=repost_ok, read_repost_last_error=repost_err,
        ))


# ---------------------------------------------------------------------------
# StandaloneTriggerService: top-level orchestration
# ---------------------------------------------------------------------------

# Shared across the v0 and v1 wrapper services — only one wrapper instance
# (regardless of v0/v1 selection) runs at a time. Conceptually they're "the
# wrapper"; different implementations, mutually exclusive at runtime.
SINGLETON_MUTEX_NAME = r"Local\ZDUltimateLegendWrapper"

ERROR_ALREADY_EXISTS = 183


def _try_acquire_singleton_mutex(name: str) -> tuple[Optional[int], bool]:
    """Try to acquire a named Win32 mutex.

    Returns ``(handle, is_first_instance)``. ``is_first_instance=False``
    means another wrapper instance already holds the mutex.
    """
    try:
        kernel32 = _Win32.kernel32()
        kernel32.CreateMutexW.argtypes = [c.c_void_p, w.BOOL, w.LPCWSTR]
        kernel32.CreateMutexW.restype = w.HANDLE
    except (OSError, AttributeError):  # pragma: no cover - non-Windows
        return None, True
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None, True
    last_error = kernel32.GetLastError()
    return int(handle), last_error != ERROR_ALREADY_EXISTS


def _release_singleton_mutex(handle: Optional[int]) -> None:
    if handle is None:
        return
    try:
        _Win32.kernel32().CloseHandle(handle)
    except (OSError, AttributeError):  # pragma: no cover
        pass


class StandaloneStartStatus(Enum):
    """Outcome of :meth:`StandaloneTriggerService.start`."""

    STARTED = "started"  # trigger fired, dual-mode established, HID session open, keepalive running
    SINGLETON_CONFLICT = "singleton_conflict"  # another wrapper instance already running
    NO_XUSB_PATH = "no_xusb_path"  # propagated from trigger
    OPEN_FAILED = "open_failed"  # propagated from trigger
    DUPLICATE_FAILED = "duplicate_failed"  # propagated from trigger
    IOCTL_FAILED = "ioctl_failed"  # propagated from trigger
    EXHAUSTED_RETRIES = "exhausted_retries"  # propagated from retry wrapper
    HID_SESSION_OPEN_FAILED = "hid_session_open_failed"  # post-trigger HID open failed


@dataclass
class StandaloneStartResult:
    status: StandaloneStartStatus
    error_message: Optional[str] = None
    target_xusb_path: Optional[str] = None
    target_hid_path: Optional[str] = None
    trigger_attempts: list[TriggerResult] = field(default_factory=list)
    descriptor_attempts: list[IoctlAttempt] = field(default_factory=list)


@dataclass
class StandaloneServiceSnapshot:
    is_started: bool
    is_keepalive_running: bool
    keepalive: Optional[KeepaliveSnapshot]
    started_xusb_path: Optional[str]
    started_hid_path: Optional[str]


_START_STATUS_BY_ENSURE: dict[EnsureDualModeStatus, StandaloneStartStatus] = {
    EnsureDualModeStatus.NO_XUSB_PATH: StandaloneStartStatus.NO_XUSB_PATH,
    EnsureDualModeStatus.OPEN_FAILED: StandaloneStartStatus.OPEN_FAILED,
    EnsureDualModeStatus.DUPLICATE_FAILED: StandaloneStartStatus.DUPLICATE_FAILED,
    EnsureDualModeStatus.IOCTL_FAILED: StandaloneStartStatus.IOCTL_FAILED,
    EnsureDualModeStatus.EXHAUSTED_RETRIES: StandaloneStartStatus.EXHAUSTED_RETRIES,
}


class StandaloneTriggerService:
    """Top-level v1 wrapper service: trigger → HID session → keepalive.

    Exposes the v0/v1-common public surface (``acquire_singleton``,
    ``start``, ``stop``, ``health_snapshot``) so ``main_zd.py`` can swap
    v0 and v1 with
    minimal call-site changes.

    Composition (instead of reimplementation): each primitive is
    composed via injectable function references. Tests substitute fakes
    at the function level (``ensure_dual_mode_fn``, ``open_hid_session_fn``,
    ``keepalive_loop_factory``) — no need to mock 17 individual ctypes
    seams.

    Lifecycle:

    1. ``start()`` -- acquires singleton mutex, runs trigger+retry, opens
       HID session, starts the keepalive loop. Returns
       ``StandaloneStartResult`` with status detailing which step (if
       any) failed.
    2. ``stop()`` -- stops the keepalive loop, closes HID handles,
       releases singleton. Idempotent.
    3. ``health_snapshot()`` -- inspect current state without side effects.
    """

    SINGLETON_MUTEX_NAME = SINGLETON_MUTEX_NAME

    def __init__(
        self,
        *,
        # Trigger + retry config
        max_retries: int = DEFAULT_MAX_RETRIES,
        inter_attempt_pause_s: float = DEFAULT_INTER_ATTEMPT_PAUSE_S,
        final_retry_pause_s: float = DEFAULT_FINAL_RETRY_PAUSE_S,
        trigger_kwargs: Optional[dict] = None,
        # Keepalive config
        keepalive_io: Optional[HidKeepaliveIo] = None,
        keepalive_cadence_s: float = DEFAULT_KEEPALIVE_CADENCE_S,
        keepalive_inter_write_delay_s: float = DEFAULT_KEEPALIVE_INTER_WRITE_DELAY_S,
        keepalive_read_timeout_ms: int = DEFAULT_KEEPALIVE_READ_TIMEOUT_MS,
        keepalive_initial_cycle_delay_s: float = DEFAULT_KEEPALIVE_INITIAL_CYCLE_DELAY_S,
        # DI seams (function-level — easy to fake in tests)
        ensure_dual_mode_fn: Callable[..., EnsureDualModeResult] = ensure_dual_mode,
        open_hid_session_fn: Callable[..., HidSessionResult] = open_hid_session,
        keepalive_loop_factory: Callable[..., "KeepaliveLoop"] = KeepaliveLoop,
        close_handle: Callable[[int], bool] = _default_close_handle,
        check_hidden_visible: Callable[[], bool] = _default_check_hidden_visible,
        # Control
        enable_singleton_mutex: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._max_retries = max_retries
        self._inter_attempt_pause_s = inter_attempt_pause_s
        self._final_retry_pause_s = final_retry_pause_s
        self._trigger_kwargs = trigger_kwargs

        self._keepalive_io = keepalive_io
        self._keepalive_cadence_s = keepalive_cadence_s
        self._keepalive_inter_write_delay_s = keepalive_inter_write_delay_s
        self._keepalive_read_timeout_ms = keepalive_read_timeout_ms
        self._keepalive_initial_cycle_delay_s = keepalive_initial_cycle_delay_s

        self._ensure_dual_mode_fn = ensure_dual_mode_fn
        self._open_hid_session_fn = open_hid_session_fn
        self._keepalive_loop_factory = keepalive_loop_factory
        self._close_handle = close_handle
        self._check_hidden_visible = check_hidden_visible

        self._enable_singleton_mutex = enable_singleton_mutex
        self._clock = clock

        self._mutex_handle: Optional[int] = None
        self._keepalive_loop: Optional[KeepaliveLoop] = None
        self._write_handle: Optional[int] = None
        self._read_handle: Optional[int] = None
        self._target_xusb_path: Optional[str] = None
        self._target_hid_path: Optional[str] = None
        self._is_started: bool = False

    # ----- singleton mutex --------------------------------------------

    def acquire_singleton(self) -> bool:
        """Try to acquire the wrapper's singleton mutex. Returns True if first."""
        if not self._enable_singleton_mutex:
            return True
        handle, is_first = _try_acquire_singleton_mutex(self.SINGLETON_MUTEX_NAME)
        self._mutex_handle = handle
        return is_first

    def release_singleton(self) -> None:
        _release_singleton_mutex(self._mutex_handle)
        self._mutex_handle = None

    # ----- start / stop -----------------------------------------------

    def start(self) -> StandaloneStartResult:
        """Establish dual-mode + start the keepalive loop.

        On success, ``status == STARTED``; the service holds the HID
        write+read handles and the background keepalive thread is
        running. Caller should later call :meth:`stop` to clean up.

        On any failure, all partial state is cleaned up before returning
        — handles are closed, singleton is released, no zombies.
        """
        # 1. Singleton check
        if self._enable_singleton_mutex and self._mutex_handle is None:
            if not self.acquire_singleton():
                # CreateMutexW hands back a valid handle even when the mutex
                # already exists, so acquire_singleton stored one despite the
                # conflict. The service never started, so no stop() will ever
                # release it — release it here or leak a kernel handle per
                # conflicting start() (contradicting the "no zombies" contract).
                self.release_singleton()
                return StandaloneStartResult(
                    status=StandaloneStartStatus.SINGLETON_CONFLICT,
                    error_message=(
                        "Another wrapper instance is already running. "
                        "Close the other instance before starting this one."
                    ),
                )

        # 2. Pre-trigger visibility check (crash-recovery path).
        # If the controller is already in dual-mode (e.g., a prior
        # wrapper session crashed without cleaning up its HID handles
        # and the OS held VID_20BC enumerated), skip the trigger sequence
        # entirely and go straight to opening the HID session. Without
        # this, a wrapper crash + restart would fail NO_XUSB_PATH because
        # VID_413D MI_00 isn't enumerated while the controller is in
        # dual-mode.
        target_xusb_path: Optional[str] = None
        trigger_attempts: list[TriggerResult] = []
        if self._check_hidden_visible():
            logger.info(
                "VID_20BC&PID_5080 already enumerated at start; skipping "
                "trigger sequence (crash-recovery path)",
            )
        else:
            # 2b. Trigger + retry
            ensure_result = self._ensure_dual_mode_fn(
                max_retries=self._max_retries,
                inter_attempt_pause_s=self._inter_attempt_pause_s,
                final_retry_pause_s=self._final_retry_pause_s,
                trigger_kwargs=self._trigger_kwargs,
            )
            target_xusb_path = ensure_result.target_path
            trigger_attempts = ensure_result.attempts

            if ensure_result.status != EnsureDualModeStatus.ESTABLISHED:
                self.release_singleton()
                return StandaloneStartResult(
                    status=_START_STATUS_BY_ENSURE[ensure_result.status],
                    error_message=ensure_result.error_message,
                    target_xusb_path=target_xusb_path,
                    trigger_attempts=trigger_attempts,
                )

        # 3. Open HID session
        session_result = self._open_hid_session_fn()

        if session_result.status != HidSessionStatus.OPENED:
            self.release_singleton()
            return StandaloneStartResult(
                status=StandaloneStartStatus.HID_SESSION_OPEN_FAILED,
                error_message=session_result.error_message,
                target_xusb_path=target_xusb_path,
                target_hid_path=session_result.target_path,
                trigger_attempts=trigger_attempts,
                descriptor_attempts=session_result.descriptor_attempts,
            )

        # Capture state for cleanup
        self._write_handle = session_result.write_handle
        self._read_handle = session_result.read_handle
        self._target_xusb_path = target_xusb_path
        self._target_hid_path = session_result.target_path

        # 4. Build + start keepalive loop
        try:
            self._keepalive_loop = self._keepalive_loop_factory(
                io=self._keepalive_io if self._keepalive_io is not None else HidKeepaliveIo(),
                clock=self._clock,
                cadence_s=self._keepalive_cadence_s,
                inter_write_delay_s=self._keepalive_inter_write_delay_s,
                read_timeout_ms=self._keepalive_read_timeout_ms,
                initial_cycle_delay_s=self._keepalive_initial_cycle_delay_s,
            )
            self._keepalive_loop.start(self._write_handle, self._read_handle)
        except Exception as exc:
            # Cleanup HID handles + singleton on factory/start failure
            logger.exception("KeepaliveLoop start failed; cleaning up")
            self._close_hid_handles_safely()
            self._keepalive_loop = None
            self.release_singleton()
            return StandaloneStartResult(
                status=StandaloneStartStatus.HID_SESSION_OPEN_FAILED,
                error_message=f"KeepaliveLoop start failed: {exc}",
                target_xusb_path=target_xusb_path,
                target_hid_path=session_result.target_path,
                trigger_attempts=trigger_attempts,
                descriptor_attempts=session_result.descriptor_attempts,
            )

        self._is_started = True
        logger.info(
            "StandaloneTriggerService started: VID_413D path=%s, VID_20BC path=%s%s",
            target_xusb_path, session_result.target_path,
            " (trigger skipped — already in dual-mode)" if not trigger_attempts else "",
        )
        return StandaloneStartResult(
            status=StandaloneStartStatus.STARTED,
            target_xusb_path=target_xusb_path,
            target_hid_path=session_result.target_path,
            trigger_attempts=trigger_attempts,
            descriptor_attempts=session_result.descriptor_attempts,
        )

    def stop(self) -> None:
        """Stop the keepalive loop, close HID handles, release singleton.

        Idempotent. Safe to call without a prior :meth:`start`.
        """
        if self._keepalive_loop is not None:
            try:
                self._keepalive_loop.stop()
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.debug("keepalive_loop.stop raised", exc_info=True)
            self._keepalive_loop = None

        self._close_hid_handles_safely()

        self._target_xusb_path = None
        self._target_hid_path = None
        self._is_started = False
        self.release_singleton()

    def _close_hid_handles_safely(self) -> None:
        if self._write_handle is not None:
            try:
                self._close_handle(self._write_handle)
            except Exception:  # pragma: no cover
                logger.debug("close_handle(write) raised", exc_info=True)
            self._write_handle = None
        if self._read_handle is not None:
            try:
                self._close_handle(self._read_handle)
            except Exception:  # pragma: no cover
                logger.debug("close_handle(read) raised", exc_info=True)
            self._read_handle = None

    # ----- health -----------------------------------------------------

    def health_snapshot(self) -> StandaloneServiceSnapshot:
        keepalive_snap = None
        is_keepalive_running = False
        if self._keepalive_loop is not None:
            try:
                keepalive_snap = self._keepalive_loop.health_snapshot()
                is_keepalive_running = keepalive_snap.is_running
            except Exception:  # pragma: no cover
                logger.debug("keepalive health_snapshot raised", exc_info=True)
        return StandaloneServiceSnapshot(
            is_started=self._is_started,
            is_keepalive_running=is_keepalive_running,
            keepalive=keepalive_snap,
            started_xusb_path=self._target_xusb_path,
            started_hid_path=self._target_hid_path,
        )
