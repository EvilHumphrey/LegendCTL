"""Device discovery and high-level device actions."""

from __future__ import annotations

import ctypes as c
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from ctypes import wintypes as w
from typing import Any

from zd_app.services.xinput import describe_battery_level, get_connected_controllers
from zd_app.i18n import t
from zd_app.models import DeviceClass, DeviceState, utc_now_iso
from zd_app.services.model_fingerprint import collect_model_fingerprint
from zd_app.services.official_app_summary_service import OfficialAppSummary, OfficialAppSummaryService


logger = logging.getLogger(__name__)


from zd_app.services._log_entry import (
    LogEntry,
    _render_log_fmt_args,
    render_log_entry,
    render_log_message,
)
from zd_app.services.path_scrub import scrub_paths


DIGCF_PRESENT = 0x2
# Required whenever SetupDiGetClassDevsW is called with a NULL ClassGuid (as the
# enumeration below does): without DIGCF_ALLCLASSES the call fails with
# ERROR_INVALID_PARAMETER (87) and recognition sees ZERO devices — proven live
# on the operator's machine 2026-07-02 (the mocked-API tests cannot catch this).
DIGCF_ALLCLASSES = 0x4
ERROR_NO_MORE_ITEMS = 259
SPDRP_DEVICEDESC = 0x0
_INVALID_HANDLE_VALUE = w.HANDLE(-1).value
_DEVICE_INSTANCE_ID_BUFFER_CHARS = 512
_DEVICE_DESCRIPTION_BUFFER_CHARS = 512


class _GUID(c.Structure):
    _fields_ = [
        ("Data1", w.DWORD),
        ("Data2", w.WORD),
        ("Data3", w.WORD),
        ("Data4", c.c_ubyte * 8),
    ]


class _SP_DEVINFO_DATA(c.Structure):
    _fields_ = [
        ("cbSize", w.DWORD),
        ("ClassGuid", _GUID),
        ("DevInst", w.DWORD),
        ("Reserved", c.c_void_p),
    ]


class _Win32:
    """Small lazy-bound Win32 surface for locale-independent presence probes."""

    _kernel32 = None
    _setupapi = None

    @classmethod
    def kernel32(cls):
        if cls._kernel32 is None:
            cls._kernel32 = c.windll.kernel32
            cls._kernel32.GetLastError.argtypes = []
            cls._kernel32.GetLastError.restype = w.DWORD
        return cls._kernel32

    @classmethod
    def setupapi(cls):
        if cls._setupapi is None:
            cls._setupapi = c.WinDLL("setupapi")
            cls._setupapi.SetupDiGetClassDevsW.argtypes = [
                c.POINTER(_GUID),
                w.LPCWSTR,
                w.HWND,
                w.DWORD,
            ]
            cls._setupapi.SetupDiGetClassDevsW.restype = c.c_void_p
            cls._setupapi.SetupDiEnumDeviceInfo.argtypes = [
                c.c_void_p,
                w.DWORD,
                c.POINTER(_SP_DEVINFO_DATA),
            ]
            cls._setupapi.SetupDiEnumDeviceInfo.restype = w.BOOL
            cls._setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
                c.c_void_p,
                c.POINTER(_SP_DEVINFO_DATA),
                w.LPWSTR,
                w.DWORD,
                c.POINTER(w.DWORD),
            ]
            cls._setupapi.SetupDiGetDeviceInstanceIdW.restype = w.BOOL
            cls._setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
                c.c_void_p,
                c.POINTER(_SP_DEVINFO_DATA),
                w.DWORD,
                c.POINTER(w.DWORD),
                c.c_void_p,
                w.DWORD,
                c.POINTER(w.DWORD),
            ]
            cls._setupapi.SetupDiGetDeviceRegistryPropertyW.restype = w.BOOL
            cls._setupapi.SetupDiDestroyDeviceInfoList.argtypes = [c.c_void_p]
            cls._setupapi.SetupDiDestroyDeviceInfoList.restype = w.BOOL
        return cls._setupapi


# Allowlist of USB ``VID&PID`` needles (lowercase) for controllers verified as a
# ZD Ultimate Legend — the only hardware whose HID settings/write protocol this
# app implements. Matched as a substring against the PnP instance id. A
# controller NOT on this allowlist is treated as a generic XInput pad: the
# read-only live tester works, but every HID write/settings surface stays gated
# and labeled "unverified" (see ``DeviceState.write_supported``). This is a tuple
# (not a single literal) so a future verified ZD revision can be added by id
# without touching the detection logic. The MI_02 HID write transport in
# ``settings_service`` is independently scoped to the same VID/PID, so widening
# this list is the ONLY place a new device id becomes write-eligible.
ZD_ULTIMATE_LEGEND_DEVICE_IDS: tuple[str, ...] = ("vid_413d&pid_2104",)


def instance_id_is_allowlisted_zd(instance_id: str) -> bool:
    """True if ``instance_id`` belongs to an allowlisted ZD Ultimate Legend.

    Case-insensitive substring match against :data:`ZD_ULTIMATE_LEGEND_DEVICE_IDS`
    (the PnP instance id embeds the ``VID_xxxx&PID_xxxx`` needle). The
    single hard-coded ``vid_413d&pid_2104`` filter this replaces matched exactly
    this way, so behaviour is unchanged for today's lone id.
    """

    lowered = instance_id.lower()
    return any(needle in lowered for needle in ZD_ULTIMATE_LEGEND_DEVICE_IDS)


def _setupdi_handle_failed(info_set: object) -> bool:
    return info_set in (None, 0, _INVALID_HANDLE_VALUE)


def _setupdi_device_instance_id(setupapi, info_set, device_info: _SP_DEVINFO_DATA) -> str:
    required_size = w.DWORD(0)
    buffer = c.create_unicode_buffer(_DEVICE_INSTANCE_ID_BUFFER_CHARS)
    ok = bool(
        setupapi.SetupDiGetDeviceInstanceIdW(
            info_set,
            c.byref(device_info),
            buffer,
            len(buffer),
            c.byref(required_size),
        )
    )
    if not ok and required_size.value > len(buffer):
        buffer = c.create_unicode_buffer(required_size.value)
        ok = bool(
            setupapi.SetupDiGetDeviceInstanceIdW(
                info_set,
                c.byref(device_info),
                buffer,
                len(buffer),
                c.byref(required_size),
            )
        )
    if not ok:
        return ""
    return buffer.value.strip()


def _setupdi_device_description(setupapi, info_set, device_info: _SP_DEVINFO_DATA) -> str:
    reg_type = w.DWORD(0)
    required_size = w.DWORD(0)
    buffer = c.create_unicode_buffer(_DEVICE_DESCRIPTION_BUFFER_CHARS)
    ok = bool(
        setupapi.SetupDiGetDeviceRegistryPropertyW(
            info_set,
            c.byref(device_info),
            SPDRP_DEVICEDESC,
            c.byref(reg_type),
            buffer,
            c.sizeof(buffer),
            c.byref(required_size),
        )
    )
    if not ok and required_size.value > c.sizeof(buffer):
        buffer = c.create_unicode_buffer((required_size.value // c.sizeof(w.WCHAR)) + 1)
        ok = bool(
            setupapi.SetupDiGetDeviceRegistryPropertyW(
                info_set,
                c.byref(device_info),
                SPDRP_DEVICEDESC,
                c.byref(reg_type),
                buffer,
                c.sizeof(buffer),
                c.byref(required_size),
            )
        )
    if not ok:
        return "Unknown device"
    return buffer.value.strip() or "Unknown device"


def _enumerate_present_device_entries_for(enumerator: str | None) -> list[dict[str, str]]:
    try:
        setupapi = _Win32.setupapi()
        kernel32 = _Win32.kernel32()
    except (OSError, AttributeError):  # pragma: no cover - non-Windows fallback
        return []

    info_set = setupapi.SetupDiGetClassDevsW(
        None, enumerator, None, DIGCF_PRESENT | DIGCF_ALLCLASSES
    )
    if _setupdi_handle_failed(info_set):
        return []

    entries: list[dict[str, str]] = []
    try:
        index = 0
        while True:
            device_info = _SP_DEVINFO_DATA()
            device_info.cbSize = c.sizeof(_SP_DEVINFO_DATA)
            ok = bool(setupapi.SetupDiEnumDeviceInfo(info_set, index, c.byref(device_info)))
            if not ok:
                if kernel32.GetLastError() == ERROR_NO_MORE_ITEMS:
                    break
                index += 1
                continue

            instance_id = _setupdi_device_instance_id(setupapi, info_set, device_info)
            if instance_id:
                entries.append(
                    {
                        "instance_id": instance_id,
                        "description": _setupdi_device_description(
                            setupapi, info_set, device_info
                        ),
                        "status": "Present",
                    }
                )
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info_set)
    return entries


def _enumerate_present_device_entries() -> list[dict[str, str]]:
    """Enumerate present device instance ids without localized text parsing."""

    usb_entries = _enumerate_present_device_entries_for("USB")
    if any(instance_id_is_allowlisted_zd(entry["instance_id"]) for entry in usb_entries):
        return usb_entries

    all_present_entries = _enumerate_present_device_entries_for(None)
    if not usb_entries:
        return all_present_entries

    combined: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in [*usb_entries, *all_present_entries]:
        key = entry["instance_id"].lower()
        if key in seen:
            continue
        seen.add(key)
        combined.append(entry)
    return combined


def _entries_have_setupdi_shape(entries: list[dict[str, str]]) -> bool:
    return any(
        entry.get("instance_id")
        and "description" in entry
        and "status" in entry
        for entry in entries
    )


def _entry_instance_ids(entries: list[dict[str, str]]) -> list[str]:
    return [
        entry["instance_id"]
        for entry in entries
        if isinstance(entry.get("instance_id"), str)
    ]


class DeviceService:
    UNKNOWN_SUMMARY_SOURCE_KEY = "device.summary.source.unknown_fallback"
    SUMMARY_SOURCE_KEY_FOR = {
        "unknown": "device.summary.source.unknown",
        "xinput": "device.summary.source.xinput",
        "official_app_ui": "device.summary.source.official_app_ui",
        "protocol": "device.summary.source.protocol",
    }
    SUMMARY_FIELD_KEY_FOR = {
        "battery": "device.summary.field.battery",
        "firmware": "device.summary.field.firmware",
        "active_profile": "device.summary.field.active_profile",
        "sleep": "device.summary.field.sleep",
    }
    # Canonical battery_level / firmware values stay Latin in state for
    # discriminator stability (see _apply_official_app_summary, summary_sources
    # branching, etc.); the formatter methods below route them through
    # locale-aware keys for display only.
    BATTERY_LEVEL_KEY_FOR = {
        "Unknown": "device.battery.unknown",
        "Wired": "device.battery.wired",
        "Empty": "device.battery.empty",
        "Low": "device.battery.low",
        "Medium": "device.battery.medium",
        "Full": "device.battery.full",
    }

    def __init__(
        self,
        clock=None,
        presence_cache_ttl_connected_seconds: float = 10.0,
        presence_cache_ttl_disconnected_seconds: float = 2.0,
    ):
        self.state = DeviceState(
            supported_capabilities={
                "Buttons": "supported",
                "Sticks": "supported",
                "Triggers": "supported",
                "Profiles": "supported",
                "Diagnostics": "supported",
                "Lighting": "supported",
                "Gyro": "unknown_pending_protocol_work",
                "Macros": "unknown_pending_protocol_work",
                "Firmware": "unknown_pending_protocol_work",
            }
        )
        self.event_log: deque[str | LogEntry] = deque(maxlen=80)
        self.last_apply_result: str | LogEntry | None = None
        self.last_read_duration_ms: float | None = None
        self.last_write_duration_ms: float | None = None
        self._last_presence_signature: tuple[str, str] = ("", "")
        self._last_connected_identifier: str = ""
        self._clock = clock or time.monotonic
        self._presence_cache_ttl_connected_seconds = max(0.0, presence_cache_ttl_connected_seconds)
        self._presence_cache_ttl_disconnected_seconds = max(0.0, presence_cache_ttl_disconnected_seconds)
        self._cached_zd_entries: list[dict[str, str]] = []
        self._has_cached_zd_entries = False
        self._last_zd_probe_at = 0.0
        # The SetupDi presence probe can still take long enough to be felt if it
        # runs inline on the Dear PyGui render thread. The cache fields above are
        # read/written under this lock so a background primer thread can refresh
        # them off the UI thread while ``_tick`` reads the warmed result.
        self._presence_cache_lock = threading.Lock()
        self._presence_primer_thread: threading.Thread | None = None
        self._presence_primer_stop = threading.Event()
        self._model_fingerprint_lock = threading.Lock()
        self._model_fingerprint_thread: threading.Thread | None = None
        self._model_fingerprint_request_key: tuple[str, tuple[str, ...]] | None = None
        self._last_logged_model_fingerprint: tuple[str, str] | None = None
        self.official_app_summary_service = OfficialAppSummaryService()

    def refresh_state(
        self,
        background: bool = False,
        force_probe: bool = False,
        allow_probe: bool = True,
    ) -> DeviceState:
        """Refresh connection/presence state from the OS.

        ``allow_probe=False`` is the non-blocking path used by the per-frame UI
        tick: it reads whatever the background presence primer last cached and
        never launches the SetupDi presence probe on the calling thread. XInput
        enumeration (~0.2ms) still runs so connect/disconnect is detected
        promptly; the ZD-specific identification rides the primed cache.
        ``force_probe`` (startup / reconnect / explicit Read) always probes.
        """
        started = time.perf_counter()
        pnp_entries = self._find_zd_entries(force_refresh=force_probe, allow_probe=allow_probe)
        xinput_slots = get_connected_controllers()

        slot = xinput_slots[0] if xinput_slots else None
        battery_level = describe_battery_level(slot) if slot is not None else "Unknown"
        was_connected = self.state.connection_state == "connected"
        previous_stable_identifier = self.state.stable_identifier
        if pnp_entries:
            chosen = pnp_entries[0]
            product_name = "ZD Ultimate Legend"
            device_class: DeviceClass = "zd_ultimate_legend"
            stable_identifier = chosen["instance_id"]
            connection_mode = self._infer_transport(chosen["instance_id"])
            connection_state = "connected"
            sync_status = self.state.sync_status if was_connected and self.state.last_read_time else "Connected"
        elif xinput_slots:
            product_name = "Xbox-compatible controller"
            device_class = "generic_xinput"
            stable_identifier = f"xinput-slot-{slot}"
            connection_mode = "XInput"
            connection_state = "connected"
            sync_status = self.state.sync_status if was_connected and self.state.last_read_time else "Connected"
        else:
            product_name = "No controller detected"
            device_class = "none"
            stable_identifier = "unknown"
            connection_mode = "Unknown"
            connection_state = "no_device"
            sync_status = "Disconnected"

        signature = (connection_state, stable_identifier)
        if signature != self._last_presence_signature:
            self._last_presence_signature = signature
            if connection_state == "connected":
                identity_changed_while_connected = (
                    was_connected and stable_identifier != previous_stable_identifier
                )
                if not was_connected or identity_changed_while_connected:
                    self.log_i18n_event(
                        "log.controller.detected",
                        product_name=product_name,
                        connection_mode=connection_mode,
                    )
            elif not background:
                self.log_i18n_event("log.controller.not_detected")

        identity_changed = False
        if connection_state == "connected" and self._known_device_identifier(
            stable_identifier
        ):
            previous_identifier = self._last_connected_identifier
            if not previous_identifier and self._known_device_identifier(
                self.state.stable_identifier
            ):
                previous_identifier = self.state.stable_identifier
            if previous_identifier and stable_identifier != previous_identifier:
                self._clear_retained_read_state_for_identity_change()
                identity_changed = True
            self._last_connected_identifier = stable_identifier

        self.state.product_name = product_name
        self.state.device_class = device_class
        self.state.stable_identifier = stable_identifier
        self.state.connection_mode = connection_mode
        self.state.connection_state = connection_state
        if self.state.summary_sources.get("battery") != "official_app_ui":
            self.state.battery_level = battery_level
            self.state.summary_sources["battery"] = "xinput" if battery_level != "Unknown" else "unknown"
        self.state.sync_status = "Connected" if identity_changed else sync_status
        self.state.xinput_slot = slot
        if connection_state == "no_device" and was_connected:
            self.state.data_freshness = "stale"
        if connection_state == "connected" and device_class == "zd_ultimate_legend":
            self._schedule_model_fingerprint_collection(stable_identifier, pnp_entries)
        else:
            self._clear_model_fingerprint()
        self.last_read_duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return self.state

    def read_device_state(self) -> DeviceState:
        self.state.sync_status = "Reading"
        self.state.data_freshness = "reading"
        started = time.perf_counter()
        state = self.refresh_state(background=False, force_probe=True)
        summary = self.official_app_summary_service.read_summary(force_refresh=True)
        retained_protocol_active_profile = False
        if summary is not None:
            retained_protocol_active_profile = self._apply_official_app_summary(summary)
        if state.connection_state == "connected":
            state.last_read_time = utc_now_iso()
            state.data_freshness = "fresh"
            state.sync_status = "Ready"
            if summary is not None:
                if retained_protocol_active_profile:
                    self.log_i18n_event(
                        "log.read.official_summary_protocol_retained"
                    )
                else:
                    self.log_i18n_event("log.read.official_summary")
            else:
                self.log_i18n_event("log.read.success")
        elif summary is not None:
            state.last_read_time = utc_now_iso()
            state.data_freshness = "stale"
            state.sync_status = "Disconnected"
            if retained_protocol_active_profile:
                self.log_i18n_event(
                    "log.read.official_summary_disconnected_protocol_retained"
                )
            else:
                self.log_i18n_event("log.read.official_summary_disconnected")
        else:
            state.data_freshness = "never_read"
            state.sync_status = "Disconnected"
        self.last_read_duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return state

    def restore_safe_defaults(self) -> str:
        message = "Restored local safe defaults. Device write remains explicit."
        self.log_event(message)
        return message

    def record_apply_result(self, success: bool, message: str | LogEntry) -> None:
        self.last_apply_result = message
        self.last_write_duration_ms = 0.0
        self.state.last_apply_time = utc_now_iso()
        self.state.data_freshness = "write_success" if success else "write_failed"
        self.state.sync_status = "Ready" if success else "Apply Failed"
        if isinstance(message, LogEntry):
            self.event_log.appendleft(message)
        else:
            self.log_event(message)

    def record_protocol_active_profile(self, slot_id: int) -> None:
        self.state.active_onboard_profile = slot_id
        self.state.summary_sources["active_profile"] = "protocol"

    def log_event(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.event_log.appendleft(f"{timestamp}  {message}")

    def log_i18n_event(self, key: str, **fmt_args: Any) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.event_log.appendleft(
            LogEntry(timestamp=timestamp, key=key, fmt_args=dict(fmt_args))
        )
        # Feed the crash reporter's parallel rolling buffer so a later crash
        # can ship Recent Activity context. Only the key flows through;
        # fmt_args are intentionally dropped at the crash_reporter boundary
        # to avoid leaking user input (profile names, paths) into reports.
        try:
            from zd_app.services import crash_reporter

            crash_reporter.record_log_entry(key, fmt_args=fmt_args or None)
        except Exception:  # noqa: BLE001 - never let logging side effects crash device flow
            logger.debug("crash_reporter.record_log_entry failed", exc_info=True)

    def _clear_model_fingerprint(self) -> None:
        with self._model_fingerprint_lock:
            self._model_fingerprint_request_key = None
            self._last_logged_model_fingerprint = None
            if hasattr(self.state, "model_fingerprint"):
                self.state.model_fingerprint = None

    def _schedule_model_fingerprint_collection(
        self,
        stable_identifier: str,
        pnp_entries: list[dict[str, str]],
    ) -> None:
        if not _entries_have_setupdi_shape(pnp_entries):
            return
        request_key = (
            stable_identifier,
            tuple(sorted(_entry_instance_ids(pnp_entries))),
        )
        with self._model_fingerprint_lock:
            thread = self._model_fingerprint_thread
            current = getattr(self.state, "model_fingerprint", None)
            # Only short-circuit when we already hold a COMPLETE fingerprint
            # (vid populated). An inventory-only result (first-connect HID-
            # interface lag, or F2's ambiguous-path abstain) is non-None but
            # incomplete; it must re-collect on the next ~2.5s refresh rather
            # than cache "not collected" for the whole session.
            if (
                self._model_fingerprint_request_key == request_key
                and (
                    getattr(current, "vid", None) is not None
                    or (thread is not None and thread.is_alive())
                )
            ):
                return
            self._model_fingerprint_request_key = request_key
            self.state.model_fingerprint = None
            entries = [entry.copy() for entry in pnp_entries]
            thread = threading.Thread(
                target=self._collect_model_fingerprint_worker,
                args=(stable_identifier, request_key, entries),
                name="zd-model-fingerprint",
                daemon=True,
            )
            self._model_fingerprint_thread = thread
        thread.start()

    def _collect_model_fingerprint_worker(
        self,
        stable_identifier: str,
        request_key: tuple[str, tuple[str, ...]],
        pnp_entries: list[dict[str, str]],
    ) -> None:
        try:
            fingerprint = collect_model_fingerprint(pnp_entries=pnp_entries)
        except Exception:  # noqa: BLE001 - never let fingerprinting affect connection state
            logger.debug("Model fingerprint worker failed", exc_info=True)
            return
        with self._model_fingerprint_lock:
            if (
                self.state.stable_identifier != stable_identifier
                or self.state.device_class != "zd_ultimate_legend"
                or self._model_fingerprint_request_key != request_key
            ):
                return
            self.state.model_fingerprint = fingerprint
        self._log_model_fingerprint(stable_identifier, fingerprint)

    def _log_model_fingerprint(self, stable_identifier: str, fingerprint: object) -> None:
        digest = getattr(fingerprint, "short_digest", None)
        if not digest:
            return
        product_string = scrub_paths(
            getattr(fingerprint, "product_string", None)
            or t("model_fingerprint.value.not_collected")
        )
        log_key = (stable_identifier, str(digest))
        with self._model_fingerprint_lock:
            if self._last_logged_model_fingerprint == log_key:
                return
            self._last_logged_model_fingerprint = log_key
        entry = LogEntry(
            timestamp="",
            key="log.model_fingerprint.collected",
            fmt_args={"digest": digest, "product_string": product_string},
        )
        logger.info(render_log_message(entry))
        self.log_i18n_event(
            "log.model_fingerprint.collected",
            digest=digest,
            product_string=product_string,
        )

    def recent_events(self, limit: int = 8) -> list[str]:
        return [scrub_paths(render_log_entry(entry)) for entry in list(self.event_log)[:limit]]

    def clear_event_log(self) -> None:
        self.event_log.clear()

    def summary_source_summary(self) -> str:
        grouped: dict[str, list[str]] = {}
        for field_name in ("battery", "firmware", "active_profile", "sleep"):
            source = self.state.summary_sources.get(field_name, "unknown")
            if source == "unknown":
                continue
            grouped.setdefault(source, []).append(self._summary_field_label(field_name))

        if not grouped:
            return self._summary_source_label("unknown")

        if len(grouped) == 1:
            source, fields = next(iter(grouped.items()))
            label = self._summary_source_label(source)
            if len(fields) == 4:
                return label
            return t(
                "diagnostics.summary_source.source_for",
                source=label,
                fields=", ".join(fields),
            )

        parts = []
        for source, fields in grouped.items():
            label = self._summary_source_label(source)
            parts.append(f"{', '.join(fields)}: {label}")
        return "; ".join(parts)

    def summary_fields_from_source(self, source_name: str) -> list[str]:
        return [
            self._summary_field_label(field_name)
            for field_name in ("battery", "firmware", "active_profile", "sleep")
            if self.state.summary_sources.get(field_name) == source_name
        ]

    def summary_source_label_for(self, field_name: str) -> str:
        source = self.state.summary_sources.get(field_name, "unknown")
        return self._summary_source_label(source)

    def _summary_source_label(self, source: str) -> str:
        key = self.SUMMARY_SOURCE_KEY_FOR.get(source)
        if key is not None:
            return t(key)
        logger.warning("Unmapped summary source label requested: %s", source)
        return t(self.UNKNOWN_SUMMARY_SOURCE_KEY)

    def format_battery_level(self) -> str:
        """Return the localized display string for the current ``battery_level``.

        ``state.battery_level`` stays canonical (Latin "Unknown" / "Wired" /
        "Empty" / "Low" / "Medium" / "Full") so callers can branch on it as
        a stable discriminator. UI rendering paths route through this method
        so the displayed string localizes for zh-CN.
        """
        canonical = self.state.battery_level
        key = self.BATTERY_LEVEL_KEY_FOR.get(canonical)
        if key is not None:
            return t(key)
        # Unmapped values pass through verbatim (forward-compat for
        # battery states added by future XInput SDK versions).
        return canonical

    def format_firmware_version(self) -> str:
        """Return the localized display string for the current ``firmware_version``.

        Numeric versions ("1.18", etc.) pass through unchanged; the
        sentinel "Unknown" / empty value routes through the locale.
        """
        canonical = self.state.firmware_version
        if not canonical or canonical == "Unknown":
            return t("device.firmware.unknown")
        return canonical

    def _summary_field_label(self, field_name: str) -> str:
        key = self.SUMMARY_FIELD_KEY_FOR.get(field_name)
        if key is None:
            logger.warning("Unmapped summary field label requested: %s", field_name)
            return field_name
        return t(key)

    def _find_zd_entries(
        self, force_refresh: bool = False, allow_probe: bool = True
    ) -> list[dict[str, str]]:
        now = self._clock()
        with self._presence_cache_lock:
            ttl_seconds = (
                self._presence_cache_ttl_connected_seconds
                if self._cached_zd_entries
                else self._presence_cache_ttl_disconnected_seconds
            )
            cache_fresh = (
                self._has_cached_zd_entries
                and (now - self._last_zd_probe_at) < ttl_seconds
            )
            if not force_refresh and cache_fresh:
                return [entry.copy() for entry in self._cached_zd_entries]
            if not force_refresh and not allow_probe:
                # Non-blocking UI-tick path: hand back whatever the background
                # presence primer last cached (possibly stale or empty) rather
                # than block this thread on the SetupDi presence probe.
                return [entry.copy() for entry in self._cached_zd_entries]

        # SetupDi enumeration runs OUTSIDE the lock so a concurrent UI-thread
        # cache read is never stalled by OS device-tree traversal.
        try:
            entries = _enumerate_present_device_entries()
        except (OSError, AttributeError):
            with self._presence_cache_lock:
                if self._has_cached_zd_entries:
                    return [entry.copy() for entry in self._cached_zd_entries]
            return []

        matches = [
            entry for entry in entries
            if instance_id_is_allowlisted_zd(entry.get("instance_id", ""))
        ]
        normalized = []
        for entry in matches:
            normalized.append(
                {
                    "instance_id": entry.get("instance_id", "unknown"),
                    "description": entry.get("description", "Unknown device"),
                    "status": entry.get("status", "Unknown"),
                }
            )
        normalized.sort(
            key=lambda item: (
                not item["instance_id"].lower().startswith("usb\\"),
                item["instance_id"],
            )
        )
        with self._presence_cache_lock:
            self._cached_zd_entries = [entry.copy() for entry in normalized]
            self._has_cached_zd_entries = True
            self._last_zd_probe_at = now
        return [entry.copy() for entry in normalized]

    def refresh_presence_cache(self) -> None:
        """Refresh the SetupDi presence cache, honoring the configured TTL.

        Side-effect only: updates the lock-guarded cache fields and never
        touches ``self.state`` or the event log, so it is safe to call from a
        background thread. The UI tick then reads the warmed cache via
        ``refresh_state(..., allow_probe=False)`` without blocking.
        """
        try:
            self._find_zd_entries(force_refresh=False, allow_probe=True)
        except Exception:  # pragma: no cover - defensive; never kill the primer
            logger.debug("Presence cache refresh failed", exc_info=True)

    def start_presence_primer(self, interval_seconds: float = 1.0) -> None:
        """Start a daemon thread that keeps the presence cache warm off-thread.

        Idempotent. The thread wakes every ``interval_seconds`` and refreshes
        the cache only when its TTL has expired, so the presence-probe cadence
        stays bounded. No-op if already running.
        """
        if self._presence_primer_thread is not None and self._presence_primer_thread.is_alive():
            return
        self._presence_primer_stop.clear()
        thread = threading.Thread(
            target=self._presence_primer_loop,
            args=(max(0.1, interval_seconds),),
            name="zd-presence-primer",
            daemon=True,
        )
        self._presence_primer_thread = thread
        thread.start()

    def stop_presence_primer(self, timeout: float = 2.0) -> None:
        """Signal the presence primer to stop and join it (best-effort)."""
        self._presence_primer_stop.set()
        thread = self._presence_primer_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._presence_primer_thread = None

    def _presence_primer_loop(self, interval_seconds: float) -> None:
        # Prime once immediately so the cache is warm before the first UI tick
        # that runs with allow_probe=False, then refresh on the interval until
        # asked to stop.
        self.refresh_presence_cache()
        while not self._presence_primer_stop.wait(interval_seconds):
            self.refresh_presence_cache()

    def _apply_official_app_summary(self, summary: OfficialAppSummary) -> bool:
        retained_protocol_active_profile = False
        if summary.battery_level:
            self.state.battery_level = summary.battery_level
            self.state.summary_sources["battery"] = "official_app_ui"
        if summary.firmware_version:
            self.state.firmware_version = summary.firmware_version
            self.state.summary_sources["firmware"] = "official_app_ui"
        if summary.active_onboard_profile is not None:
            if self.state.summary_sources.get("active_profile") == "protocol":
                retained_protocol_active_profile = True
            else:
                self.state.active_onboard_profile = summary.active_onboard_profile
                self.state.summary_sources["active_profile"] = "official_app_ui"
        if summary.sleep_setting:
            self.state.sleep_setting = summary.sleep_setting
            self.state.summary_sources["sleep"] = "official_app_ui"
        return retained_protocol_active_profile

    @staticmethod
    def _known_device_identifier(value: str | None) -> bool:
        text = (value or "").strip()
        return bool(text and text.lower() != "unknown")

    def _clear_retained_read_state_for_identity_change(self) -> None:
        self.state.firmware_version = "Unknown"
        self.state.battery_level = "Unknown"
        self.state.sleep_setting = "Unknown"
        self.state.active_onboard_profile = 1
        self.state.last_read_time = None
        self.state.data_freshness = "never_read"
        for field_name in ("battery", "firmware", "active_profile", "sleep"):
            self.state.summary_sources[field_name] = "unknown"

    @staticmethod
    def _infer_transport(instance_id: str) -> str:
        upper = instance_id.upper()
        if upper.startswith("USB\\"):
            return "USB"
        if upper.startswith("BTH\\"):
            return "Bluetooth"
        return "Unknown"


