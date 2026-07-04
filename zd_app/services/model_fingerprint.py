"""Read-only HID model fingerprint collection.

The collector intentionally uses only descriptor / metadata APIs on the
already-known ZD MI_02 HID path. It never sends vendor reports and never asks
for a write-capable handle.
"""

from __future__ import annotations

import ctypes as c
import hashlib
import logging
import re
from collections.abc import Iterable, Mapping
from ctypes import wintypes as w
from dataclasses import dataclass, field

from zd_app.i18n import t


logger = logging.getLogger(__name__)


FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 0x3
METADATA_OPEN_DESIRED_ACCESS = 0x0
METADATA_OPEN_SHARE_MODE = FILE_SHARE_READ | FILE_SHARE_WRITE
METADATA_OPEN_FLAGS_AND_ATTRIBUTES = 0x0
INVALID_HANDLE_VALUE = w.HANDLE(-1).value
HIDP_STATUS_SUCCESS = 0x00110000
# HIDAPI clamps USB string buffers to exactly 126 WCHAR: a buffer >= 127 makes
# some USB devices return a silently BROKEN string (libusb/hidapi windows/hid.c).
# A max-length string here fails CLOSED to None (honest "not collected") rather
# than risk silent corruption on a trust surface. Do NOT bump this to 127 for a
# NUL terminator (reviewed 2026-07-03; an independent Win32 review rejected the off-by-one "fix").
_HID_STRING_BUFFER_CHARS = 126
_MI_INDEX_RE = re.compile(r"(?:&|#)MI_([0-9A-Fa-f]{2})(?:[&#\\]|$)", re.IGNORECASE)


@dataclass(frozen=True)
class InterfaceInventory:
    count: int
    mi_indices: tuple[int, ...]


@dataclass(frozen=True)
class ModelFingerprint:
    vid: int | None = None
    pid: int | None = None
    version_number: int | None = None
    product_string: str | None = None
    manufacturer_string: str | None = None
    usage_page: int | None = None
    usage: int | None = None
    input_report_len: int | None = None
    output_report_len: int | None = None
    feature_report_len: int | None = None
    button_caps_count: int | None = None
    value_caps_count: int | None = None
    interface_inventory: InterfaceInventory | None = None
    digest: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        digest = _digest_for(self) if _has_observed_field(self) else None
        object.__setattr__(self, "digest", digest)

    @property
    def short_digest(self) -> str | None:
        return self.digest[:12] if self.digest else None


class _HIDD_ATTRIBUTES(c.Structure):
    _fields_ = [
        ("Size", w.ULONG),
        ("VendorID", w.WORD),
        ("ProductID", w.WORD),
        ("VersionNumber", w.WORD),
    ]


class _HIDP_CAPS(c.Structure):
    _fields_ = [
        ("Usage", w.WORD),
        ("UsagePage", w.WORD),
        ("InputReportByteLength", w.WORD),
        ("OutputReportByteLength", w.WORD),
        ("FeatureReportByteLength", w.WORD),
        ("Reserved", w.WORD * 17),
        ("NumberLinkCollectionNodes", w.WORD),
        ("NumberInputButtonCaps", w.WORD),
        ("NumberInputValueCaps", w.WORD),
        ("NumberInputDataIndices", w.WORD),
        ("NumberOutputButtonCaps", w.WORD),
        ("NumberOutputValueCaps", w.WORD),
        ("NumberOutputDataIndices", w.WORD),
        ("NumberFeatureButtonCaps", w.WORD),
        ("NumberFeatureValueCaps", w.WORD),
        ("NumberFeatureDataIndices", w.WORD),
    ]


class _Win32:
    """Lazy-bound Win32 surface for metadata-only HID calls."""

    _kernel32 = None
    _hidsdi = None
    _hid = None

    @classmethod
    def kernel32(cls):
        if cls._kernel32 is None:
            cls._kernel32 = c.windll.kernel32
            cls._kernel32.CreateFileW.argtypes = [
                w.LPCWSTR,
                w.DWORD,
                w.DWORD,
                c.c_void_p,
                w.DWORD,
                w.DWORD,
                w.HANDLE,
            ]
            cls._kernel32.CreateFileW.restype = w.HANDLE
            cls._kernel32.CloseHandle.argtypes = [w.HANDLE]
            cls._kernel32.CloseHandle.restype = w.BOOL
            cls._kernel32.GetLastError.argtypes = []
            cls._kernel32.GetLastError.restype = w.DWORD
        return cls._kernel32

    @classmethod
    def hidsdi(cls):
        if cls._hidsdi is None:
            # HidD_* exports live in hid.dll; "hidsdi" is only the SDK header
            # name (loading WinDLL("hidsdi") raises and silently blanked every
            # HidD-derived field on real hardware — caught by operator smoke).
            cls._hidsdi = c.WinDLL("hid")
            # HidD_* return Windows BOOLEAN (1 byte), NOT BOOL (4 bytes). Binding
            # them as BOOL reads 3 extra return-register bytes whose upper bytes
            # are not guaranteed zero, so a TRUE could misread as FALSE. HidP_Get-
            # Caps (below) stays c_long: it returns an NTSTATUS, not a BOOLEAN.
            cls._hidsdi.HidD_GetAttributes.argtypes = [
                w.HANDLE,
                c.POINTER(_HIDD_ATTRIBUTES),
            ]
            cls._hidsdi.HidD_GetAttributes.restype = w.BOOLEAN
            cls._hidsdi.HidD_GetProductString.argtypes = [
                w.HANDLE,
                c.c_void_p,
                w.ULONG,
            ]
            cls._hidsdi.HidD_GetProductString.restype = w.BOOLEAN
            cls._hidsdi.HidD_GetManufacturerString.argtypes = [
                w.HANDLE,
                c.c_void_p,
                w.ULONG,
            ]
            cls._hidsdi.HidD_GetManufacturerString.restype = w.BOOLEAN
            cls._hidsdi.HidD_GetPreparsedData.argtypes = [
                w.HANDLE,
                c.POINTER(c.c_void_p),
            ]
            cls._hidsdi.HidD_GetPreparsedData.restype = w.BOOLEAN
            cls._hidsdi.HidD_FreePreparsedData.argtypes = [c.c_void_p]
            cls._hidsdi.HidD_FreePreparsedData.restype = w.BOOLEAN
        return cls._hidsdi

    @classmethod
    def hid(cls):
        if cls._hid is None:
            cls._hid = c.WinDLL("hid")
            cls._hid.HidP_GetCaps.argtypes = [
                c.c_void_p,
                c.POINTER(_HIDP_CAPS),
            ]
            cls._hid.HidP_GetCaps.restype = c.c_long
        return cls._hid


def collect_model_fingerprint(
    *,
    device_path: str | None = None,
    pnp_entries: Iterable[Mapping[str, object]] = (),
) -> ModelFingerprint:
    """Collect a best-effort, read-only fingerprint for the ZD MI_02 device.

    Every Win32 failure degrades the affected fields to ``None`` and the
    function never raises to callers.
    """

    inventory = interface_inventory_from_entries(pnp_entries)
    values: dict[str, object] = {"interface_inventory": inventory}
    try:
        path = device_path or _default_mi02_path()
        if not path:
            return ModelFingerprint(**values)
        kernel32 = _Win32.kernel32()
        handle = _open_metadata_handle(kernel32, path)
        if _handle_failed(handle):
            return ModelFingerprint(**values)
        try:
            hidsdi = _Win32.hidsdi()
            hid = _Win32.hid()
            values.update(_read_attributes(hidsdi, handle))
            values["product_string"] = _read_hid_string(
                hidsdi.HidD_GetProductString,
                handle,
            )
            values["manufacturer_string"] = _read_hid_string(
                hidsdi.HidD_GetManufacturerString,
                handle,
            )
            values.update(_read_caps(hidsdi, hid, handle))
        finally:
            _close_handle(kernel32, handle)
    except Exception:  # noqa: BLE001 - collection must not affect device flow
        logger.debug("Model fingerprint collection failed", exc_info=True)
    return ModelFingerprint(**values)


def interface_inventory_from_entries(
    entries: Iterable[Mapping[str, object]],
) -> InterfaceInventory | None:
    indices: set[int] = set()
    for entry in entries:
        for key in ("instance_id", "device_path", "path"):
            value = entry.get(key)
            if not isinstance(value, str):
                continue
            for match in _MI_INDEX_RE.finditer(value):
                indices.add(int(match.group(1), 16))
    if not indices:
        return None
    ordered = tuple(sorted(indices))
    return InterfaceInventory(count=len(ordered), mi_indices=ordered)


def canonical_rendering(fingerprint: ModelFingerprint) -> str:
    return "".join(
        f"{key}={_canonical_value(fingerprint, key)}\n"
        for key in _CANONICAL_FIELD_ORDER
    )


def fingerprint_display_rows(
    fingerprint: ModelFingerprint,
) -> tuple[tuple[str, str], ...]:
    missing = t("model_fingerprint.value.not_collected")
    return tuple(
        (label_key, _display_value(fingerprint, attr, missing))
        for label_key, attr in _DISPLAY_FIELDS
    )


def _open_metadata_handle(kernel32, path: str):
    return kernel32.CreateFileW(
        path,
        METADATA_OPEN_DESIRED_ACCESS,
        METADATA_OPEN_SHARE_MODE,
        None,
        OPEN_EXISTING,
        METADATA_OPEN_FLAGS_AND_ATTRIBUTES,
        None,
    )


def _close_handle(kernel32, handle) -> None:
    try:
        kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - best-effort handle cleanup
        logger.debug("Model fingerprint CloseHandle failed", exc_info=True)


def _handle_failed(handle: object) -> bool:
    value = getattr(handle, "value", handle)
    return value in (None, 0, INVALID_HANDLE_VALUE)


def _read_attributes(hidsdi, handle) -> dict[str, int | None]:
    attrs = _HIDD_ATTRIBUTES()
    attrs.Size = c.sizeof(_HIDD_ATTRIBUTES)
    try:
        ok = bool(hidsdi.HidD_GetAttributes(handle, c.byref(attrs)))
    except Exception:  # noqa: BLE001 - field-level best effort
        logger.debug("HidD_GetAttributes failed", exc_info=True)
        ok = False
    if not ok:
        return {"vid": None, "pid": None, "version_number": None}
    return {
        "vid": int(attrs.VendorID),
        "pid": int(attrs.ProductID),
        "version_number": int(attrs.VersionNumber),
    }


def _read_hid_string(func, handle) -> str | None:
    buffer = c.create_unicode_buffer(_HID_STRING_BUFFER_CHARS)
    try:
        ok = bool(func(handle, buffer, c.sizeof(buffer)))
    except Exception:  # noqa: BLE001 - field-level best effort
        logger.debug("HidD string query failed", exc_info=True)
        ok = False
    if not ok:
        return None
    return _clean_hid_string(buffer.value)


def _clean_hid_string(value: object) -> str | None:
    text = str(value).replace("\x00", "")
    text = " ".join(text.replace("\r\n", "\n").replace("\r", "\n").splitlines())
    return text.strip() or None


def _read_caps(hidsdi, hid, handle) -> dict[str, int | None]:
    fields = {
        "usage_page": None,
        "usage": None,
        "input_report_len": None,
        "output_report_len": None,
        "feature_report_len": None,
        "button_caps_count": None,
        "value_caps_count": None,
    }
    preparsed = c.c_void_p()
    try:
        ok = bool(hidsdi.HidD_GetPreparsedData(handle, c.byref(preparsed)))
    except Exception:  # noqa: BLE001 - field-level best effort
        logger.debug("HidD_GetPreparsedData failed", exc_info=True)
        ok = False
    if not ok or not preparsed.value:
        return fields
    try:
        caps = _HIDP_CAPS()
        try:
            status = int(hid.HidP_GetCaps(preparsed, c.byref(caps)))
        except Exception:  # noqa: BLE001 - field-level best effort
            logger.debug("HidP_GetCaps failed", exc_info=True)
            status = -1
        if status != HIDP_STATUS_SUCCESS:
            return fields
        return {
            "usage_page": int(caps.UsagePage),
            "usage": int(caps.Usage),
            "input_report_len": int(caps.InputReportByteLength),
            "output_report_len": int(caps.OutputReportByteLength),
            "feature_report_len": int(caps.FeatureReportByteLength),
            "button_caps_count": (
                int(caps.NumberInputButtonCaps)
                + int(caps.NumberOutputButtonCaps)
                + int(caps.NumberFeatureButtonCaps)
            ),
            "value_caps_count": (
                int(caps.NumberInputValueCaps)
                + int(caps.NumberOutputValueCaps)
                + int(caps.NumberFeatureValueCaps)
            ),
        }
    finally:
        try:
            hidsdi.HidD_FreePreparsedData(preparsed)
        except Exception:  # noqa: BLE001 - best-effort HID cleanup
            logger.debug("HidD_FreePreparsedData failed", exc_info=True)


def _default_mi02_path() -> str | None:
    try:
        from zd_app.services.settings_service import enumerate_mi02_hid_paths

        paths = enumerate_mi02_hid_paths()
    except Exception:  # noqa: BLE001 - no path means metadata fields stay None
        logger.debug("MI_02 path enumeration failed", exc_info=True)
        return None
    return _choose_mi02_path(paths)


def _choose_mi02_path(paths: Iterable[str]) -> str | None:
    candidates = tuple(dict.fromkeys(path for path in paths if isinstance(path, str) and path))
    if len(candidates) != 1:
        if len(candidates) > 1:
            logger.debug(
                "Multiple MI_02 HID paths found for model fingerprint; abstaining"
            )
        return None
    return candidates[0]


_CANONICAL_FIELD_ORDER = (
    "vid",
    "pid",
    "version_number",
    "product_string",
    "manufacturer_string",
    "usage_page",
    "usage",
    "input_report_len",
    "output_report_len",
    "feature_report_len",
    "button_caps_count",
    "value_caps_count",
    "interface_inventory",
)

_DISPLAY_FIELDS = (
    ("model_fingerprint.digest_label", "short_digest"),
    ("model_fingerprint.vid_label", "vid"),
    ("model_fingerprint.pid_label", "pid"),
    ("model_fingerprint.version_number_label", "version_number"),
    ("model_fingerprint.product_string_label", "product_string"),
    ("model_fingerprint.manufacturer_string_label", "manufacturer_string"),
    ("model_fingerprint.usage_page_label", "usage_page"),
    ("model_fingerprint.usage_label", "usage"),
    ("model_fingerprint.input_report_len_label", "input_report_len"),
    ("model_fingerprint.output_report_len_label", "output_report_len"),
    ("model_fingerprint.feature_report_len_label", "feature_report_len"),
    ("model_fingerprint.button_caps_count_label", "button_caps_count"),
    ("model_fingerprint.value_caps_count_label", "value_caps_count"),
    ("model_fingerprint.interface_inventory_label", "interface_inventory"),
)


def _has_observed_field(fingerprint: ModelFingerprint) -> bool:
    return any(
        getattr(fingerprint, key) is not None
        for key in _CANONICAL_FIELD_ORDER
    )


def _digest_for(fingerprint: ModelFingerprint) -> str:
    payload = canonical_rendering(fingerprint).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_value(fingerprint: ModelFingerprint, key: str) -> str:
    value = getattr(fingerprint, key)
    if value is None:
        return "-"
    if isinstance(value, InterfaceInventory):
        indices = ",".join(f"{index:02x}" for index in value.mi_indices)
        return f"count={value.count};mi={indices or '-'}"
    if isinstance(value, int):
        if key in {"vid", "pid", "version_number", "usage_page", "usage"}:
            return f"{value:04x}"
        return str(value)
    return _clean_hid_string(value) or "-"


def _display_value(fingerprint: ModelFingerprint, attr: str, missing: str) -> str:
    if attr == "short_digest":
        return fingerprint.short_digest or missing
    value = getattr(fingerprint, attr)
    if value is None:
        return missing
    if isinstance(value, InterfaceInventory):
        indices = ", ".join(f"MI_{index:02X}" for index in value.mi_indices)
        return t(
            "model_fingerprint.interface_inventory_value",
            indices=indices or missing,
            count=value.count,
        )
    if isinstance(value, int):
        if attr in {"vid", "pid", "version_number", "usage_page", "usage"}:
            return f"0x{value:04X}"
        return str(value)
    return _clean_hid_string(value) or missing


__all__ = [
    "HIDP_STATUS_SUCCESS",
    "METADATA_OPEN_DESIRED_ACCESS",
    "METADATA_OPEN_FLAGS_AND_ATTRIBUTES",
    "METADATA_OPEN_SHARE_MODE",
    "ModelFingerprint",
    "InterfaceInventory",
    "canonical_rendering",
    "collect_model_fingerprint",
    "fingerprint_display_rows",
    "interface_inventory_from_entries",
]
