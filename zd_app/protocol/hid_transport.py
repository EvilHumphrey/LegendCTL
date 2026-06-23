r"""Enumerate HID interface paths through SetupAPI (used by the opt-in StandaloneTriggerService).

This exists because the secondary ZD config interface `VID_20BC&PID_5080`
does not show up through plain `hid.enumerate()` even though the official
Windows app can open it during controller-settings sessions.

Examples:
    .\.venv-zd\Scripts\python -m zd_app.protocol.hid_transport
    .\.venv-zd\Scripts\python -m zd_app.protocol.hid_transport --vendor-id 0x20BC --product-id 0x5080 --watch-seconds 5
"""

from __future__ import annotations

import argparse
import ctypes as c
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from ctypes import wintypes as w
from pathlib import Path
from typing import Callable


__all__ = [
    "CM_GET_DEVICE_INTERFACE_LIST_PRESENT",
    "CM_NOTIFY_ACTION_DEVICEINTERFACEARRIVAL",
    "CM_NOTIFY_ACTION_DEVICEINTERFACEREMOVAL",
    "CM_NOTIFY_EVENT_DATA",
    "CM_NOTIFY_FILTER",
    "CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE",
    "CR_SUCCESS",
    "DIGCF_DEVICEINTERFACE",
    "DIGCF_PRESENT",
    "ERROR_FILE_NOT_FOUND",
    "ERROR_NO_MORE_ITEMS",
    "ERROR_PATH_NOT_FOUND",
    "ERROR_SHARING_VIOLATION",
    "FILE_ATTRIBUTE_NORMAL",
    "FILE_FLAG_OVERLAPPED",
    "FILE_SHARE_READ",
    "FILE_SHARE_WRITE",
    "GENERIC_READ",
    "GENERIC_WRITE",
    "GUID",
    "HIDDEN_BOOTSTRAP_OPEN_PROFILE",
    "HIDDEN_METADATA_PROBE_PROFILE",
    "HIDDEN_READBACK_OPEN_PROFILE",
    "HidOpenProfile",
    "HiddenInterfaceEvent",
    "HiddenInterfaceWatcher",
    "HiddenTransportProbe",
    "INVALID_HANDLE_VALUE",
    "MAX_DEVICE_ID_LEN",
    "OFFICIAL_HIDDEN_OPEN_PROFILES",
    "OPEN_EXISTING",
    "PUBLIC_IDENTIFY_OPEN_PROFILE",
    "RETRYABLE_OPEN_ERRORS",
    "SP_DEVICE_INTERFACE_DATA",
    "candidate_hid_paths",
    "enumerate_hid_paths",
    "event_driven_open_hidden_transport",
    "filter_hid_paths",
    "kernel32",
    "known_hidden_paths_from_logs",
    "main",
    "openable_hid_paths",
    "parse_args",
    "probe_hidden_transport",
    "probe_open_modes",
    "setupapi",
    "try_open_hid_path",
    "try_open_hid_profile",
    "wait_for_hid_paths",
    "wait_for_hidden_transport",
    "wait_for_openable_hid_paths",
]


logger = logging.getLogger(__name__)


DIGCF_PRESENT = 0x2
DIGCF_DEVICEINTERFACE = 0x10
ERROR_NO_MORE_ITEMS = 259
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_SHARING_VIOLATION = 32
# Win32 last-error codes that event_driven_open_hidden_transport retries on
# when anchored on a subsequent PnP ARRIVAL. The share-violation branch is
# the original hypothesis; FILE_NOT_FOUND and PATH_NOT_FOUND
# were added after the event-driven-opener parity test observed the
# actual ZD
# failure mode (transient interface deregistration during vendor
# controller-identification failure), not share-mode conflict. Order does
# not matter — this is a membership test set.
RETRYABLE_OPEN_ERRORS: tuple[int, ...] = (
    ERROR_SHARING_VIOLATION,
    ERROR_FILE_NOT_FOUND,
    ERROR_PATH_NOT_FOUND,
)
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_FLAG_OVERLAPPED = 0x40000000
OPEN_EXISTING = 0x3
INVALID_HANDLE_VALUE = w.HANDLE(-1).value

CR_SUCCESS = 0
CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE = 0
CM_NOTIFY_ACTION_DEVICEINTERFACEARRIVAL = 0
CM_NOTIFY_ACTION_DEVICEINTERFACEREMOVAL = 1
CM_GET_DEVICE_INTERFACE_LIST_PRESENT = 0
MAX_DEVICE_ID_LEN = 200


@dataclass(frozen=True)
class HiddenTransportProbe:
    visible_paths: tuple[str, ...]
    known_candidate_paths: tuple[str, ...]
    reachable_paths: tuple[str, ...]

    @property
    def candidate_paths(self) -> tuple[str, ...]:
        ordered: list[str] = []
        seen: set[str] = set()
        for collection in (self.visible_paths, self.known_candidate_paths):
            for path in collection:
                if path in seen:
                    continue
                seen.add(path)
                ordered.append(path)
        return tuple(ordered)

    def source_for_path(self, path: str | None) -> str:
        if not path:
            return "none"
        in_visible = path in self.visible_paths
        in_known = path in self.known_candidate_paths
        if in_visible and in_known:
            return "setupapi+log_cache"
        if in_visible:
            return "setupapi"
        if in_known:
            return "log_cache"
        return "unknown"


@dataclass(frozen=True)
class HidOpenProfile:
    name: str
    desired_access: int
    share_mode: int
    creation_disposition: int
    flags_and_attributes: int


PUBLIC_IDENTIFY_OPEN_PROFILE = HidOpenProfile(
    name="public_identify",
    desired_access=GENERIC_READ | GENERIC_WRITE,
    share_mode=FILE_SHARE_READ | FILE_SHARE_WRITE,
    creation_disposition=OPEN_EXISTING,
    flags_and_attributes=FILE_ATTRIBUTE_NORMAL,
)

HIDDEN_BOOTSTRAP_OPEN_PROFILE = HidOpenProfile(
    name="bootstrap_write",
    desired_access=GENERIC_WRITE,
    share_mode=FILE_SHARE_READ | FILE_SHARE_WRITE,
    creation_disposition=OPEN_EXISTING,
    flags_and_attributes=0,
)

HIDDEN_READBACK_OPEN_PROFILE = HidOpenProfile(
    name="readback_overlapped",
    desired_access=GENERIC_READ | GENERIC_WRITE,
    share_mode=FILE_SHARE_READ | FILE_SHARE_WRITE,
    creation_disposition=OPEN_EXISTING,
    flags_and_attributes=FILE_FLAG_OVERLAPPED,
)

# Metadata-only probe to confirm a path is the right HID TLC before a real
# RW open, avoiding the hidden-open timing window. DesiredAccess = 0
# can succeed against a device handle even when GENERIC_READ / GENERIC_WRITE
# would be denied by an existing sharing conflict, and HidD_* metadata calls
# are explicitly documented to work against that weaker handle. Used by
# event_driven_open_hidden_transport to confirm TLC identity before
# committing to a real RW open.
HIDDEN_METADATA_PROBE_PROFILE = HidOpenProfile(
    name="metadata_probe",
    desired_access=0,
    share_mode=FILE_SHARE_READ | FILE_SHARE_WRITE,
    creation_disposition=OPEN_EXISTING,
    flags_and_attributes=FILE_FLAG_OVERLAPPED,
)

OFFICIAL_HIDDEN_OPEN_PROFILES: tuple[HidOpenProfile, ...] = (
    HIDDEN_BOOTSTRAP_OPEN_PROFILE,
    HIDDEN_READBACK_OPEN_PROFILE,
)


class GUID(c.Structure):
    _fields_ = [
        ("Data1", w.DWORD),
        ("Data2", w.WORD),
        ("Data3", w.WORD),
        ("Data4", c.c_ubyte * 8),
    ]


class SP_DEVICE_INTERFACE_DATA(c.Structure):
    _fields_ = [
        ("cbSize", w.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", w.DWORD),
        ("Reserved", c.c_void_p),
    ]


setupapi = c.windll.setupapi
hid_dll = c.windll.hid
kernel32 = c.windll.kernel32

setupapi.SetupDiGetClassDevsW.argtypes = [c.POINTER(GUID), w.LPCWSTR, w.HWND, w.DWORD]
setupapi.SetupDiGetClassDevsW.restype = c.c_void_p
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    c.c_void_p,
    c.c_void_p,
    c.POINTER(GUID),
    w.DWORD,
    c.POINTER(SP_DEVICE_INTERFACE_DATA),
]
setupapi.SetupDiEnumDeviceInterfaces.restype = w.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    c.c_void_p,
    c.POINTER(SP_DEVICE_INTERFACE_DATA),
    c.c_void_p,
    w.DWORD,
    c.POINTER(w.DWORD),
    c.c_void_p,
]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = w.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [c.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.restype = w.BOOL
kernel32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
kernel32.CreateFileW.restype = w.HANDLE
kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.CloseHandle.restype = w.BOOL


def enumerate_hid_paths() -> list[str]:
    hid_guid = GUID()
    hid_dll.HidD_GetHidGuid(c.byref(hid_guid))
    info_set = setupapi.SetupDiGetClassDevsW(c.byref(hid_guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not info_set:
        return []

    paths: list[str] = []
    try:
        index = 0
        while True:
            interface_data = SP_DEVICE_INTERFACE_DATA()
            interface_data.cbSize = c.sizeof(SP_DEVICE_INTERFACE_DATA)
            ok = setupapi.SetupDiEnumDeviceInterfaces(info_set, None, c.byref(hid_guid), index, c.byref(interface_data))
            if not ok:
                if kernel32.GetLastError() == ERROR_NO_MORE_ITEMS:
                    break
                index += 1
                continue

            needed = w.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                info_set,
                c.byref(interface_data),
                None,
                0,
                c.byref(needed),
                None,
            )

            buffer = c.create_string_buffer(needed.value)
            c.memset(buffer, 0, needed.value)
            c.cast(buffer, c.POINTER(w.DWORD)).contents.value = 8 if c.sizeof(c.c_void_p) == 8 else 6

            ok = setupapi.SetupDiGetDeviceInterfaceDetailW(
                info_set,
                c.byref(interface_data),
                c.cast(buffer, c.c_void_p),
                needed,
                c.byref(needed),
                None,
            )
            if ok:
                path = c.wstring_at(c.addressof(buffer) + c.sizeof(w.DWORD))
                paths.append(path)
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info_set)

    return paths


def filter_hid_paths(vendor_id: int | None = None, product_id: int | None = None) -> list[str]:
    vendor_token = f"vid_{vendor_id:04x}" if vendor_id is not None else None
    product_token = f"pid_{product_id:04x}" if product_id is not None else None
    matches: list[str] = []
    for path in enumerate_hid_paths():
        lower = path.lower()
        if vendor_token and vendor_token not in lower:
            continue
        if product_token and product_token not in lower:
            continue
        matches.append(path)
    return matches


def known_hidden_paths_from_logs(
    log_dir: str | Path = "logs",
    vendor_id: int | None = None,
    product_id: int | None = None,
) -> list[str]:
    vendor_token = f"vid_{vendor_id:04x}" if vendor_id is not None else None
    product_token = f"pid_{product_id:04x}" if product_id is not None else None
    matches: list[str] = []
    seen: set[str] = set()
    base = Path(log_dir)
    if not base.exists():
        return []

    for capture_path in sorted(base.glob("*.jsonl")):
        try:
            lines = capture_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            if not raw_line.startswith("{"):
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            path = payload.get("path")
            if not isinstance(path, str):
                continue
            lower = path.lower()
            if vendor_token and vendor_token not in lower:
                continue
            if product_token and product_token not in lower:
                continue
            if path in seen:
                continue
            seen.add(path)
            matches.append(path)
    return matches


def candidate_hid_paths(
    vendor_id: int | None = None,
    product_id: int | None = None,
    log_dir: str | Path = "logs",
) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for source in (
        filter_hid_paths(vendor_id=vendor_id, product_id=product_id),
        known_hidden_paths_from_logs(log_dir=log_dir, vendor_id=vendor_id, product_id=product_id),
    ):
        for path in source:
            if path in seen:
                continue
            seen.add(path)
            candidates.append(path)
    return candidates


def probe_hidden_transport(
    vendor_id: int | None = None,
    product_id: int | None = None,
    log_dir: str | Path = "logs",
    desired_access: int = GENERIC_READ | GENERIC_WRITE,
) -> HiddenTransportProbe:
    visible_paths = tuple(filter_hid_paths(vendor_id=vendor_id, product_id=product_id))
    known_candidate_paths = tuple(known_hidden_paths_from_logs(log_dir=log_dir, vendor_id=vendor_id, product_id=product_id))
    reachable_paths = tuple(openable_hid_paths(list(_merge_unique_paths(visible_paths, known_candidate_paths)), desired_access=desired_access))
    return HiddenTransportProbe(
        visible_paths=visible_paths,
        known_candidate_paths=known_candidate_paths,
        reachable_paths=reachable_paths,
    )


def wait_for_hidden_transport(
    vendor_id: int,
    product_id: int,
    timeout_s: float,
    poll_interval_s: float = 0.25,
    log_dir: str | Path = "logs",
    desired_access: int = GENERIC_READ | GENERIC_WRITE,
) -> HiddenTransportProbe:
    deadline = time.time() + max(0.0, timeout_s)
    last_probe = probe_hidden_transport(
        vendor_id=vendor_id,
        product_id=product_id,
        log_dir=log_dir,
        desired_access=desired_access,
    )
    while True:
        if last_probe.reachable_paths:
            return last_probe
        if time.time() >= deadline:
            return last_probe
        time.sleep(poll_interval_s)
        last_probe = probe_hidden_transport(
            vendor_id=vendor_id,
            product_id=product_id,
            log_dir=log_dir,
            desired_access=desired_access,
        )


def wait_for_hid_paths(
    vendor_id: int,
    product_id: int,
    timeout_s: float,
    poll_interval_s: float = 0.25,
) -> list[str]:
    deadline = time.time() + max(0.0, timeout_s)
    while True:
        matches = filter_hid_paths(vendor_id=vendor_id, product_id=product_id)
        if matches:
            return matches
        if time.time() >= deadline:
            return []
        time.sleep(poll_interval_s)


def _merge_unique_paths(*collections: tuple[str, ...] | list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for collection in collections:
        for path in collection:
            if path in seen:
                continue
            seen.add(path)
            merged.append(path)
    return merged


def try_open_hid_path(
    path: str,
    desired_access: int,
    share_mode: int = FILE_SHARE_READ | FILE_SHARE_WRITE,
    creation_disposition: int = OPEN_EXISTING,
    flags_and_attributes: int = 0,
) -> dict[str, int | bool]:
    handle = kernel32.CreateFileW(
        path,
        desired_access,
        share_mode,
        None,
        creation_disposition,
        flags_and_attributes,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        return {"ok": False, "last_error": kernel32.GetLastError()}

    try:
        return {"ok": True, "last_error": 0}
    finally:
        kernel32.CloseHandle(handle)


def try_open_hid_profile(path: str, profile: HidOpenProfile) -> dict[str, int | bool | str]:
    result = try_open_hid_path(
        path,
        desired_access=profile.desired_access,
        share_mode=profile.share_mode,
        creation_disposition=profile.creation_disposition,
        flags_and_attributes=profile.flags_and_attributes,
    )
    return {
        **result,
        "profile": profile.name,
        "desired_access": profile.desired_access,
        "share_mode": profile.share_mode,
        "creation_disposition": profile.creation_disposition,
        "flags_and_attributes": profile.flags_and_attributes,
    }


def openable_hid_paths(paths: list[str], desired_access: int = GENERIC_READ | GENERIC_WRITE) -> list[str]:
    matches: list[str] = []
    for path in paths:
        result = try_open_hid_path(path, desired_access)
        if result.get("ok"):
            matches.append(path)
    return matches


def wait_for_openable_hid_paths(
    vendor_id: int,
    product_id: int,
    timeout_s: float,
    poll_interval_s: float = 0.25,
    log_dir: str | Path = "logs",
    desired_access: int = GENERIC_READ | GENERIC_WRITE,
) -> list[str]:
    return list(
        wait_for_hidden_transport(
            vendor_id=vendor_id,
            product_id=product_id,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            log_dir=log_dir,
            desired_access=desired_access,
        ).reachable_paths
    )


def probe_open_modes(paths: list[str]) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for path in paths:
        results.append(
            {
                "path": path,
                "open_read_write": try_open_hid_path(path, GENERIC_READ | GENERIC_WRITE),
                "open_read_only": try_open_hid_path(path, GENERIC_READ),
                "open_write_only": try_open_hid_path(path, GENERIC_WRITE),
            }
        )
    return results


# ----------------------------------------------------------------------
# Event-driven opener path (CM_Register_Notification).
#
# This block is the event-driven opener implementation. The polling path
# above is preserved
# unchanged and remains the default; the event-driven path is opt-in via
# ZD_HIDDEN_OPENER=event.
#
# Key design constraints honored here:
#   - HIDDEN_BOOTSTRAP_OPEN_PROFILE numerics are not modified (vendor
#     ground truth from the decoded DeviceIoControl behavior).
#   - The readiness ladder candidate/reachable/ready/verified is preserved;
#     a successful RW open via this path still only means reachable.
#   - The callback runs on a system thread; all state mutation is
#     thread-safe, exceptions are swallowed, and the callback returns
#     ERROR_SUCCESS unconditionally (no query-veto semantics are wanted).
#   - The WINFUNCTYPE callback is stored as an instance attribute with a
#     strong reference for the lifetime of the registration, so it is not
#     garbage-collected while the OS still holds a pointer to it.
# ----------------------------------------------------------------------


try:
    cfgmgr32 = c.windll.cfgmgr32
except OSError:  # pragma: no cover - older Windows without cfgmgr32
    cfgmgr32 = None


def _cfgmgr32_available() -> bool:
    if cfgmgr32 is None:
        return False
    required = (
        "CM_Register_Notification",
        "CM_Unregister_Notification",
        "CM_Get_Device_Interface_List_SizeW",
        "CM_Get_Device_Interface_ListW",
    )
    return all(getattr(cfgmgr32, name, None) is not None for name in required)


if cfgmgr32 is not None:
    if getattr(cfgmgr32, "CM_Register_Notification", None) is not None:
        cfgmgr32.CM_Register_Notification.restype = w.DWORD
    if getattr(cfgmgr32, "CM_Unregister_Notification", None) is not None:
        cfgmgr32.CM_Unregister_Notification.argtypes = [c.c_void_p]
        cfgmgr32.CM_Unregister_Notification.restype = w.DWORD
    if getattr(cfgmgr32, "CM_Get_Device_Interface_List_SizeW", None) is not None:
        cfgmgr32.CM_Get_Device_Interface_List_SizeW.argtypes = [
            c.POINTER(w.ULONG),
            c.POINTER(GUID),
            w.LPCWSTR,
            w.ULONG,
        ]
        cfgmgr32.CM_Get_Device_Interface_List_SizeW.restype = w.DWORD
    if getattr(cfgmgr32, "CM_Get_Device_Interface_ListW", None) is not None:
        cfgmgr32.CM_Get_Device_Interface_ListW.argtypes = [
            c.POINTER(GUID),
            w.LPCWSTR,
            c.c_void_p,
            w.ULONG,
            w.ULONG,
        ]
        cfgmgr32.CM_Get_Device_Interface_ListW.restype = w.DWORD


class _CM_NOTIFY_FILTER_DEVICEINTERFACE(c.Structure):
    _fields_ = [("ClassGuid", GUID)]


class _CM_NOTIFY_FILTER_DEVICEHANDLE(c.Structure):
    _fields_ = [("hTarget", w.HANDLE)]


class _CM_NOTIFY_FILTER_DEVICEINSTANCE(c.Structure):
    _fields_ = [("InstanceId", c.c_wchar * MAX_DEVICE_ID_LEN)]


class _CM_NOTIFY_FILTER_UNION(c.Union):
    _fields_ = [
        ("DeviceInterface", _CM_NOTIFY_FILTER_DEVICEINTERFACE),
        ("DeviceHandle", _CM_NOTIFY_FILTER_DEVICEHANDLE),
        ("DeviceInstance", _CM_NOTIFY_FILTER_DEVICEINSTANCE),
    ]


class CM_NOTIFY_FILTER(c.Structure):
    _fields_ = [
        ("cbSize", w.DWORD),
        ("Flags", w.DWORD),
        ("FilterType", c.c_int),
        ("Reserved", w.DWORD),
        ("u", _CM_NOTIFY_FILTER_UNION),
    ]


class _CM_NOTIFY_EVENT_DATA_DEVICEINTERFACE_PREFIX(c.Structure):
    # The SymbolicLink WCHAR array is an ANYSIZE flexible member and is NOT
    # represented here. The suffix is parsed from the raw buffer using the
    # EventDataSize callback parameter as the authoritative length, not a
    # fixed struct size.
    _fields_ = [("ClassGuid", GUID)]


class _CM_NOTIFY_EVENT_DATA_DEVICEHANDLE_PREFIX(c.Structure):
    _fields_ = [
        ("EventGuid", GUID),
        ("NameOffset", c.c_long),
        ("DataSize", w.DWORD),
    ]


class _CM_NOTIFY_EVENT_DATA_UNION_PREFIX(c.Union):
    _fields_ = [
        ("DeviceInterface", _CM_NOTIFY_EVENT_DATA_DEVICEINTERFACE_PREFIX),
        ("DeviceHandle", _CM_NOTIFY_EVENT_DATA_DEVICEHANDLE_PREFIX),
    ]


class CM_NOTIFY_EVENT_DATA(c.Structure):
    _fields_ = [
        ("FilterType", c.c_int),
        ("Reserved", w.DWORD),
        ("u", _CM_NOTIFY_EVENT_DATA_UNION_PREFIX),
    ]


# Byte offset at which the flexible SymbolicLink WCHAR array begins inside a
# CM_NOTIFY_EVENT_DATA buffer for DeviceInterface events:
# FilterType (DWORD) + Reserved (DWORD) + ClassGuid (GUID)
_CM_NOTIFY_EVENT_DATA_SYMLINK_OFFSET = (
    c.sizeof(c.c_int) + c.sizeof(w.DWORD) + c.sizeof(GUID)
)


# Callback signature per Microsoft CfgMgr32 documentation:
#   DWORD CALLBACK CM_NOTIFY_CALLBACK(
#       HCMNOTIFICATION       hNotify,
#       PVOID                 Context,
#       CM_NOTIFY_ACTION      Action,
#       PCM_NOTIFY_EVENT_DATA EventData,
#       DWORD                 EventDataSize);
CM_NOTIFY_CALLBACK = c.WINFUNCTYPE(
    w.DWORD,
    c.c_void_p,
    c.c_void_p,
    c.c_int,
    c.POINTER(CM_NOTIFY_EVENT_DATA),
    w.DWORD,
)


@dataclass(frozen=True)
class HiddenInterfaceEvent:
    action: str  # "arrival" | "removal"
    path: str
    ts: float  # time.monotonic() at dispatch


class HiddenInterfaceWatcher:
    """Event-driven watcher over HID-interface-class PnP notifications.

    Registers a CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE callback against the
    HID class GUID, seeds its path snapshot via CM_Get_Device_Interface_ListW,
    and exposes a thread-safe queue of arrival / removal events filtered to
    a specific vendor/product pair.

    Thread model:
      - The CfgMgr32 callback runs on a system-provided thread. It pushes
        events into an internal queue.Queue under a lock. It does not call
        CreateFile and does not block.
      - Consumers call matching_paths() / wait_for_event() / drain() from
        the main thread. All three are thread-safe.

    Lifecycle:
      - __init__ does NOT call Windows. Safe to construct without elevated
        privileges or while the CfgMgr32 DLL is unavailable.
      - start() is where registration happens. Raises RuntimeError if the
        required entry points are missing.
      - stop() is idempotent and safe to call in a finally block.

    Testability:
      - The internal seam _on_event(action, path) is what the real callback
        invokes. Tests can call it directly with synthetic (action, path)
        tuples without exercising Windows.
    """

    def __init__(
        self,
        vendor_id: int,
        product_id: int,
        *,
        queue_maxsize: int = 256,
    ):
        self._vendor_token = f"vid_{vendor_id:04x}"
        self._product_token = f"pid_{product_id:04x}"
        self._events: "queue.Queue[HiddenInterfaceEvent]" = queue.Queue(
            maxsize=max(1, queue_maxsize)
        )
        self._paths_lock = threading.Lock()
        self._paths: set[str] = set()
        self._callback: object | None = None
        self._handle: int | None = None
        self._started = False
        self._overflow_warned = False

    def start(self) -> None:
        if self._started:
            return
        if not _cfgmgr32_available():
            raise RuntimeError(
                "cfgmgr32 notification entry points are not available on this Windows build"
            )

        hid_guid = GUID()
        hid_dll.HidD_GetHidGuid(c.byref(hid_guid))

        notify_filter = CM_NOTIFY_FILTER()
        c.memset(c.byref(notify_filter), 0, c.sizeof(CM_NOTIFY_FILTER))
        notify_filter.cbSize = c.sizeof(CM_NOTIFY_FILTER)
        notify_filter.Flags = 0
        notify_filter.FilterType = CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE
        notify_filter.u.DeviceInterface.ClassGuid = hid_guid

        # Strong reference required: garbage-collecting this while Windows
        # still holds the pointer is a process-level crash, not a warning.
        self._callback = CM_NOTIFY_CALLBACK(self._dispatch_callback)

        notify_handle = c.c_void_p(0)
        cr = cfgmgr32.CM_Register_Notification(
            c.byref(notify_filter),
            None,
            self._callback,
            c.byref(notify_handle),
        )
        if cr != CR_SUCCESS:
            self._callback = None
            raise RuntimeError(
                f"CM_Register_Notification failed with CONFIGRET {cr}"
            )

        self._handle = notify_handle.value
        self._started = True

        for path in self._enumerate_hid_paths(hid_guid):
            if self._matches(path):
                with self._paths_lock:
                    self._paths.add(path)

    def stop(self) -> None:
        if not self._started:
            return
        if self._handle and cfgmgr32 is not None:
            try:
                cfgmgr32.CM_Unregister_Notification(self._handle)
            except Exception:  # pragma: no cover - defensive
                pass
        self._handle = None
        self._callback = None
        self._started = False

    def matching_paths(self) -> tuple[str, ...]:
        with self._paths_lock:
            return tuple(sorted(self._paths))

    def wait_for_event(self, timeout_s: float) -> HiddenInterfaceEvent | None:
        try:
            return self._events.get(timeout=max(0.0, timeout_s))
        except queue.Empty:
            return None

    def drain(self) -> list[HiddenInterfaceEvent]:
        drained: list[HiddenInterfaceEvent] = []
        while True:
            try:
                drained.append(self._events.get_nowait())
            except queue.Empty:
                return drained

    def _matches(self, path: str) -> bool:
        if not path:
            return False
        lower = path.lower()
        return self._vendor_token in lower and self._product_token in lower

    def _on_event(self, action: str, path: str) -> None:
        """Internal seam. Called by the real callback and by unit tests."""
        if not self._matches(path):
            return
        if action == "arrival":
            with self._paths_lock:
                self._paths.add(path)
        elif action == "removal":
            with self._paths_lock:
                self._paths.discard(path)
        else:
            return
        event = HiddenInterfaceEvent(action=action, path=path, ts=time.monotonic())
        try:
            self._events.put_nowait(event)
            return
        except queue.Full:
            pass
        # Overflow policy: drop oldest, keep newest, warn once.
        try:
            self._events.get_nowait()
        except queue.Empty:
            pass
        try:
            self._events.put_nowait(event)
        except queue.Full:  # pragma: no cover - defensive
            pass
        if not self._overflow_warned:
            logger.warning(
                "HiddenInterfaceWatcher event queue full (maxsize=%d); dropped oldest event",
                self._events.maxsize,
            )
            self._overflow_warned = True

    def _dispatch_callback(
        self,
        _notify_handle,
        _context,
        action,
        event_data_ptr,
        event_data_size,
    ):
        # Runs on a system thread. Must not raise back into Windows, must
        # not block, and must always return ERROR_SUCCESS.
        try:
            if action == CM_NOTIFY_ACTION_DEVICEINTERFACEARRIVAL:
                action_name = "arrival"
            elif action == CM_NOTIFY_ACTION_DEVICEINTERFACEREMOVAL:
                action_name = "removal"
            else:
                return 0  # ERROR_SUCCESS

            if (
                not event_data_ptr
                or int(event_data_size or 0) <= _CM_NOTIFY_EVENT_DATA_SYMLINK_OFFSET
            ):
                return 0

            symlink_bytes = int(event_data_size) - _CM_NOTIFY_EVENT_DATA_SYMLINK_OFFSET
            if symlink_bytes <= 0:
                return 0
            base_address = (
                c.addressof(event_data_ptr.contents)
                + _CM_NOTIFY_EVENT_DATA_SYMLINK_OFFSET
            )
            wchar_count = symlink_bytes // c.sizeof(c.c_wchar)
            if wchar_count <= 0:
                return 0
            path = c.wstring_at(base_address, wchar_count).rstrip("\x00")
            self._on_event(action_name, path)
        except Exception:  # pragma: no cover - defensive swallow
            pass
        return 0

    def _enumerate_hid_paths(self, class_guid: GUID) -> list[str]:
        if cfgmgr32 is None:
            return []
        buffer_size = w.ULONG(0)
        cr = cfgmgr32.CM_Get_Device_Interface_List_SizeW(
            c.byref(buffer_size),
            c.byref(class_guid),
            None,
            CM_GET_DEVICE_INTERFACE_LIST_PRESENT,
        )
        if cr != CR_SUCCESS or buffer_size.value <= 1:
            return []

        buffer = c.create_unicode_buffer(buffer_size.value)
        cr = cfgmgr32.CM_Get_Device_Interface_ListW(
            c.byref(class_guid),
            None,
            buffer,
            buffer_size.value,
            CM_GET_DEVICE_INTERFACE_LIST_PRESENT,
        )
        if cr != CR_SUCCESS:
            return []

        return _split_multi_sz_wchar_buffer(buffer, buffer_size.value)


def _split_multi_sz_wchar_buffer(buffer, total_wchar_count: int) -> list[str]:
    """Parse a doubly-null-terminated WCHAR buffer into a list of strings."""
    result: list[str] = []
    start = 0
    i = 0
    while i < total_wchar_count:
        if buffer[i] == "\x00":
            if start < i:
                result.append(buffer[start:i])
            if i + 1 < total_wchar_count and buffer[i + 1] == "\x00":
                break
            start = i + 1
        i += 1
    return result


def _init_opener_telemetry(telemetry: dict | None) -> None:
    if telemetry is None:
        return
    telemetry.setdefault("attempts", [])
    telemetry.setdefault(
        "retry_stats",
        {
            "retries_fired": 0,
            "retries_converted": 0,
            "retry_errors_observed": [],
        },
    )
    telemetry.setdefault("trigger_source", None)
    telemetry.setdefault("final_outcome", None)


def _record_opener_attempt(
    telemetry: dict | None,
    profile: HidOpenProfile,
    result: dict,
    is_retry: bool,
    retry_anchor_event: str | None,
    started_at: float,
) -> None:
    if telemetry is None:
        return
    telemetry["attempts"].append(
        {
            "profile": profile.name,
            "desired_access": profile.desired_access,
            "share_mode": profile.share_mode,
            "creation_disposition": profile.creation_disposition,
            "flags_and_attributes": profile.flags_and_attributes,
            "is_retry": bool(is_retry),
            "retry_anchor_event": retry_anchor_event,
            "ok": bool(result.get("ok")),
            "last_error": int(result.get("last_error") or 0),
            "elapsed_ms": round((time.monotonic() - started_at) * 1000),
        }
    )


def event_driven_open_hidden_transport(
    watcher: HiddenInterfaceWatcher,
    profile_bootstrap: HidOpenProfile = HIDDEN_BOOTSTRAP_OPEN_PROFILE,
    rw_retry_window_s: float = 5.0,
    initial_wait_s: float = 2.0,
    open_fn: Callable[[str, HidOpenProfile], dict] = try_open_hid_profile,
    telemetry: dict | None = None,
) -> HiddenTransportProbe:
    """Event-anchored two-stage opener.

    Waits for the watcher to surface a matching path (either already seeded in
    its snapshot or via an arrival event within ``initial_wait_s``). Performs
    a one-shot metadata-only probe (DesiredAccess = 0, overlapped) to confirm
    the path is a well-formed HID TLC, then attempts the bootstrap RW open.
    On any of the errors in ``RETRYABLE_OPEN_ERRORS`` — currently
    ``ERROR_SHARING_VIOLATION`` plus ``ERROR_FILE_NOT_FOUND`` and
    ``ERROR_PATH_NOT_FOUND`` — the retry is
    anchored on the watcher's next arrival event, bounded by
    ``rw_retry_window_s``. All other open errors stop the ladder immediately.

    Returns a HiddenTransportProbe shaped for compatibility with the polling
    path. A successful RW open populates ``reachable_paths`` only; readiness
    (``ready`` / ``verified``) remains gated on
    ``race_hidden_two_handle_ready.observe_readback_until_ready`` and on the
    matched ``0xd0 0x0f`` target acknowledgement.

    If ``telemetry`` is supplied as a mutable dict it is populated with
    per-attempt telemetry:

    - ``attempts``: list of per-call dicts with ``profile``, ``desired_access``,
      ``share_mode``, ``creation_disposition``, ``flags_and_attributes``,
      ``is_retry``, ``retry_anchor_event``, ``ok``, ``last_error``,
      ``elapsed_ms``.
    - ``retry_stats``: dict with ``retries_fired`` (int),
      ``retries_converted`` (int), ``retry_errors_observed`` (list[int]).
    - ``trigger_source``: ``"seed"`` when the path came from the watcher's
      initial enumeration snapshot, ``"arrival_event"`` when it came from a
      subsequent PnP arrival.
    - ``final_outcome``: one of ``"reached"``, ``"blocked_timeout"``,
      ``"blocked_exhausted"``. ``"blocked_timeout"`` covers "the retry
      window expired with no further arrival event"; ``"blocked_exhausted"``
      covers every other non-success (no initial path, metadata probe
      failure, non-retryable RW error).

    When ``telemetry`` is ``None`` (default), behavior is identical to the
    pre-telemetry version.
    """
    _init_opener_telemetry(telemetry)
    started_at = time.monotonic()

    paths = list(watcher.matching_paths())
    trigger_source = "seed"
    if not paths:
        event = watcher.wait_for_event(max(0.0, initial_wait_s))
        if event is None or event.action != "arrival":
            if telemetry is not None:
                telemetry["final_outcome"] = "blocked_exhausted"
            return HiddenTransportProbe(
                visible_paths=(),
                known_candidate_paths=(),
                reachable_paths=(),
            )
        paths = [event.path]
        trigger_source = "arrival_event"

    if telemetry is not None:
        telemetry["trigger_source"] = trigger_source

    target_path = paths[0]

    meta_result = open_fn(target_path, HIDDEN_METADATA_PROBE_PROFILE)
    _record_opener_attempt(
        telemetry,
        HIDDEN_METADATA_PROBE_PROFILE,
        meta_result,
        is_retry=False,
        retry_anchor_event=None,
        started_at=started_at,
    )
    if not meta_result.get("ok"):
        if telemetry is not None:
            telemetry["final_outcome"] = "blocked_exhausted"
        return HiddenTransportProbe(
            visible_paths=(target_path,),
            known_candidate_paths=(),
            reachable_paths=(),
        )

    deadline = time.monotonic() + max(0.0, rw_retry_window_s)
    retry_anchor: str | None = None
    is_retry = False
    while True:
        rw_result = open_fn(target_path, profile_bootstrap)
        _record_opener_attempt(
            telemetry,
            profile_bootstrap,
            rw_result,
            is_retry=is_retry,
            retry_anchor_event=retry_anchor,
            started_at=started_at,
        )
        if rw_result.get("ok"):
            if telemetry is not None:
                telemetry["final_outcome"] = "reached"
                if is_retry:
                    telemetry["retry_stats"]["retries_converted"] += 1
            return HiddenTransportProbe(
                visible_paths=(target_path,),
                known_candidate_paths=(),
                reachable_paths=(target_path,),
            )
        last_error = int(rw_result.get("last_error") or 0)
        if last_error not in RETRYABLE_OPEN_ERRORS:
            if telemetry is not None:
                telemetry["final_outcome"] = "blocked_exhausted"
            break
        if telemetry is not None:
            telemetry["retry_stats"]["retry_errors_observed"].append(last_error)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if telemetry is not None:
                telemetry["final_outcome"] = "blocked_timeout"
            break
        event = watcher.wait_for_event(remaining)
        if event is None or event.action != "arrival":
            if telemetry is not None:
                telemetry["final_outcome"] = "blocked_timeout"
            break
        if telemetry is not None:
            telemetry["retry_stats"]["retries_fired"] += 1
        retry_anchor = f"{event.action}:{event.path}"
        is_retry = True

    return HiddenTransportProbe(
        visible_paths=(target_path,),
        known_candidate_paths=(),
        reachable_paths=(),
    )


def _parse_int(value: str) -> int:
    return int(value, 0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enumerate HID interface paths through SetupAPI.")
    parser.add_argument("--vendor-id", type=_parse_int, help="Optional vendor id filter, e.g. 0x20BC")
    parser.add_argument("--product-id", type=_parse_int, help="Optional product id filter, e.g. 0x5080")
    parser.add_argument(
        "--watch-seconds",
        type=float,
        default=0.0,
        help="Poll for matching paths for this many seconds before printing results.",
    )
    parser.add_argument(
        "--probe-open",
        action="store_true",
        help="Attempt direct CreateFileW opens on the resolved SetupAPI paths.",
    )
    parser.add_argument(
        "--include-log-cache",
        action="store_true",
        help="Include secondary HID paths previously recorded in the local log directory.",
    )
    parser.add_argument(
        "--log-dir",
        default="logs",
        help="Directory containing local capture JSONL files for --include-log-cache.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.watch_seconds and args.vendor_id is not None and args.product_id is not None and args.include_log_cache:
        paths = wait_for_openable_hid_paths(
            args.vendor_id,
            args.product_id,
            args.watch_seconds,
            log_dir=args.log_dir,
        )
    elif args.watch_seconds and args.vendor_id is not None and args.product_id is not None:
        paths = wait_for_hid_paths(args.vendor_id, args.product_id, args.watch_seconds)
    elif args.include_log_cache:
        paths = candidate_hid_paths(args.vendor_id, args.product_id, log_dir=args.log_dir)
    else:
        paths = filter_hid_paths(args.vendor_id, args.product_id)
    if args.probe_open:
        print(json.dumps(probe_open_modes(paths), indent=2))
        return
    print(json.dumps(paths, indent=2))


if __name__ == "__main__":
    main()
