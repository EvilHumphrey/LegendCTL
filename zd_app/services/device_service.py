"""Device discovery and high-level device actions."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from zd_app.services.xinput import describe_battery_level, get_connected_controllers
from zd_app.i18n import t
from zd_app.models import DeviceClass, DeviceState, utc_now_iso
from zd_app.services._subprocess_helpers import silent_run
from zd_app.services.official_app_summary_service import OfficialAppSummary, OfficialAppSummaryService


logger = logging.getLogger(__name__)


from zd_app.services._log_entry import (
    LogEntry,
    _render_log_fmt_args,
    render_log_entry,
    render_log_message,
)
from zd_app.services.path_scrub import scrub_paths


# Absolute path to pnputil.exe (avoids a PATH search-order hijack).
_PNPUTIL_EXE = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"), "System32", "pnputil.exe"
)


# Allowlist of USB ``VID&PID`` needles (lowercase) for controllers verified as a
# ZD Ultimate Legend: the only hardware whose HID settings/write protocol this
# app implements. A non-allowlisted controller remains eligible for read-only
# XInput live verification, but all HID write/settings surfaces stay gated.
ZD_ULTIMATE_LEGEND_DEVICE_IDS: tuple[str, ...] = ("vid_413d&pid_2104",)


def instance_id_is_allowlisted_zd(instance_id: str) -> bool:
    """True if ``instance_id`` belongs to an allowlisted ZD Ultimate Legend."""

    lowered = instance_id.lower()
    return any(needle in lowered for needle in ZD_ULTIMATE_LEGEND_DEVICE_IDS)


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
        self._last_presence_signature: tuple[str, int | None] = ("", None)
        self._clock = clock or time.monotonic
        self._presence_cache_ttl_connected_seconds = max(0.0, presence_cache_ttl_connected_seconds)
        self._presence_cache_ttl_disconnected_seconds = max(0.0, presence_cache_ttl_disconnected_seconds)
        self._cached_zd_entries: list[dict[str, str]] = []
        self._has_cached_zd_entries = False
        self._last_zd_probe_at = 0.0
        # The presence probe shells out to ``pnputil`` (~60-70ms on a typical
        # machine). When that ran inline on the Dear PyGui render thread it
        # produced a periodic multi-frame hitch the operator felt as nav
        # stutter. The cache fields above are now read/written under this lock
        # so a background primer thread can refresh them off the UI thread
        # while ``_tick`` reads them without ever blocking on the subprocess.
        self._presence_cache_lock = threading.Lock()
        self._presence_primer_thread: threading.Thread | None = None
        self._presence_primer_stop = threading.Event()
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
        never launches the ``pnputil`` subprocess on the calling thread. XInput
        enumeration (~0.2ms) still runs so connect/disconnect is detected
        promptly; the slower ZD-specific identification rides the primed cache.
        ``force_probe`` (startup / reconnect / explicit Read) always probes.
        """
        started = time.perf_counter()
        pnp_entries = self._find_zd_entries(force_refresh=force_probe, allow_probe=allow_probe)
        xinput_slots = get_connected_controllers()

        slot = xinput_slots[0] if xinput_slots else None
        battery_level = describe_battery_level(slot) if slot is not None else "Unknown"
        was_connected = self.state.connection_state == "connected"
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

        signature = (stable_identifier, slot)
        if signature != self._last_presence_signature:
            self._last_presence_signature = signature
            if connection_state == "connected":
                self.log_i18n_event(
                    "log.controller.detected",
                    product_name=product_name,
                    connection_mode=connection_mode,
                )
            elif not background:
                self.log_i18n_event("log.controller.not_detected")

        self.state.product_name = product_name
        self.state.device_class = device_class
        self.state.stable_identifier = stable_identifier
        self.state.connection_mode = connection_mode
        self.state.connection_state = connection_state
        if self.state.summary_sources.get("battery") != "official_app_ui":
            self.state.battery_level = battery_level
            self.state.summary_sources["battery"] = "xinput" if battery_level != "Unknown" else "unknown"
        self.state.sync_status = sync_status
        self.state.xinput_slot = slot
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
                # than block this thread on the pnputil subprocess.
                return [entry.copy() for entry in self._cached_zd_entries]

        # The subprocess runs OUTSIDE the lock so a concurrent UI-thread cache
        # read is never stalled for the ~60ms pnputil enumeration.
        try:
            result = silent_run(
                [_PNPUTIL_EXE, "/enum-devices", "/connected"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            with self._presence_cache_lock:
                if self._has_cached_zd_entries:
                    return [entry.copy() for entry in self._cached_zd_entries]
            return []

        entries: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                if current:
                    entries.append(current)
                    current = {}
                continue
            if ":" in line:
                key, value = line.split(":", 1)
                current[key.strip().lower()] = value.strip()
        if current:
            entries.append(current)

        matches = [
            entry for entry in entries
            if instance_id_is_allowlisted_zd(entry.get("instance id", ""))
        ]
        normalized = []
        for entry in matches:
            normalized.append(
                {
                    "instance_id": entry.get("instance id", "unknown"),
                    "description": entry.get("device description", "Unknown device"),
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
        """Refresh the pnputil presence cache, honoring the configured TTL.

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
        the cache only when its TTL has expired, so the pnputil probe cadence
        is unchanged from the inline version — it just no longer runs on the
        render thread. No-op if already running.
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
    def _infer_transport(instance_id: str) -> str:
        upper = instance_id.upper()
        if upper.startswith("USB\\"):
            return "USB"
        if upper.startswith("BTH\\"):
            return "Bluetooth"
        return "Unknown"


