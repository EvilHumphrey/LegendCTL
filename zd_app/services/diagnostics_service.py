"""Diagnostics service: event log plus device-state snapshot / bundle export.

Historically this also hosted a live input poller + analyzer (packet-rate /
button readout) feeding the Diagnostics "Live Input" card. That continuous
real-time monitoring was removed — the wrapper is a configuration tool, not a
live gamepad tester. What remains is the diagnostics event log and the
device-state snapshot used by the Diagnostics screen's health / connection
cards and the shareable bundle.
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from zd_app.i18n import t
from zd_app.models import DeviceState, DiagnosticSnapshot, utc_now_iso
from zd_app.services._log_entry import LogEntry, render_log_entry
from zd_app.services.path_scrub import scrub_paths
from zd_app.storage.settings_store import _default_user_data_dir


logger = logging.getLogger(__name__)


# Fields of DeviceState that are safe to write into a user-shareable bundle.
# An explicit allowlist (not __dict__) so a future DeviceState field can't
# silently leak into a file users are nudged to share.
_BUNDLE_DEVICE_STATE_FIELDS = (
    "product_name",
    "stable_identifier",
    "connection_mode",
    "firmware_version",
    "battery_level",
    "sleep_setting",
    "active_onboard_profile",
    "sync_status",
    "connection_state",
    "data_freshness",
    "supported_capabilities",
    "summary_sources",
    "xinput_slot",
    "last_read_time",
    "last_apply_time",
)

_PNP_VID_PID_RE = re.compile(
    r"^(?:[^\\]+\\)?VID_[0-9A-Fa-f]{4}&PID_[0-9A-Fa-f]{4}",
    re.IGNORECASE,
)


def _redact_instance_id(identifier: str | None) -> str:
    """Reduce a Windows PnP instance ID to its VID/PID prefix.

    The instance-ID tail is a per-unit hardware fingerprint; the VID/PID
    prefix identifies only the product model (shared across every unit), so it
    is safe to keep in a bundle users are nudged to share. Identifiers without
    a VID/PID (``xinput-slot-N``, ``unknown``) are not unit-stable fingerprints
    and pass through unchanged.
    """
    text = identifier or ""
    match = _PNP_VID_PID_RE.match(text)
    return match.group(0) if match else text


def _redact_paths(text: str) -> str:
    """Reduce absolute filesystem paths in event-log text to a privacy-safe form.

    Event-log messages routinely carry absolute paths (e.g. an
    "Attempted path: C:\\Users\\<name>\\..." entry, config file paths, or the
    exported-bundle path the app logs on every export). The on-screen log feeds
    the shareable diagnostic bundle, so the OS username / home directory must
    never ship in it. Delegates to the shared scrubber
    (:func:`zd_app.services.path_scrub.scrub_paths`) so this surface and the
    diagnostic bundle stay in lock-step: app-data paths collapse to
    ``<APP_DATA>``, the username after a ``Users``/``home`` root is dropped
    (even a spaced display name), and any other path is reduced to its
    basename. Non-path text is returned unchanged.
    """

    return scrub_paths(text)


class DiagnosticsService:
    def __init__(self):
        self.event_log: deque[str | LogEntry] = deque(maxlen=120)

    def log_event(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.event_log.appendleft(f"{timestamp}  {message}")

    def log_i18n_event(self, key: str, **fmt_args: Any) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.event_log.appendleft(
            LogEntry(timestamp=timestamp, key=key, fmt_args=dict(fmt_args))
        )

    def clear_event_log(self) -> None:
        self.event_log.clear()

    def build_snapshot(self, device_state: DeviceState, last_read_ms: float | None, last_write_ms: float | None) -> DiagnosticSnapshot:
        # ``last_packet_timestamp`` was historically stamped by the live input
        # poller on each drained sample. With live monitoring removed, the
        # closest honest "we have current data" signal is the last successful
        # controller read, so the health summary keys off that instead.
        return DiagnosticSnapshot(
            timestamp=utc_now_iso(),
            connection_mode=device_state.connection_mode,
            device_id=device_state.stable_identifier,
            firmware_version=device_state.firmware_version,
            active_profile=device_state.active_onboard_profile,
            last_packet_timestamp=device_state.last_read_time,
            last_read_duration_ms=last_read_ms,
            last_write_duration_ms=last_write_ms,
            event_log=[_redact_paths(render_log_entry(entry)) for entry in list(self.event_log)[:25]],
        )

    def _bundle_device_state(self, device_state: DeviceState) -> dict[str, Any]:
        payload = {name: getattr(device_state, name) for name in _BUNDLE_DEVICE_STATE_FIELDS}
        payload["stable_identifier"] = _redact_instance_id(device_state.stable_identifier)
        return payload

    def _safe_output_dir(self, output_dir: str) -> Path:
        """Constrain the bundle output dir to within the user-data root.

        ``diagnostics_bundle_dir`` is settings-controlled; a tampered or
        traversal value must not let the export write outside the app's own
        data directory. If the configured path escapes the allowed root, fall
        back to the default ``<user-data>/diagnostics`` location.
        """
        allowed_root = _default_user_data_dir().resolve()
        resolved = Path(output_dir).resolve()
        if resolved == allowed_root or allowed_root in resolved.parents:
            return resolved
        logger.warning(
            "diagnostics_bundle_dir %r escaped the user-data root; "
            "writing the bundle to the default location instead.",
            output_dir,
        )
        return allowed_root / "diagnostics"

    def export_bundle(self, device_state: DeviceState, last_read_ms: float | None, last_write_ms: float | None, output_dir: str) -> Path:
        snapshot = self.build_snapshot(device_state, last_read_ms, last_write_ms)
        output_path = self._safe_output_dir(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        bundle_path = output_path / f"diagnostic_bundle_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        bundle_path.write_text(
            json.dumps(
                {
                    "device_state": self._bundle_device_state(device_state),
                    "snapshot": {
                        "timestamp": snapshot.timestamp,
                        "connection_mode": snapshot.connection_mode,
                        "device_id": _redact_instance_id(snapshot.device_id),
                        "firmware_version": snapshot.firmware_version,
                        "active_profile": snapshot.active_profile,
                        "last_packet_timestamp": snapshot.last_packet_timestamp,
                        "last_read_duration_ms": snapshot.last_read_duration_ms,
                        "last_write_duration_ms": snapshot.last_write_duration_ms,
                        "event_log": snapshot.event_log,
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self.log_i18n_event("log.diagnostics.exported_bundle", path=bundle_path)
        return bundle_path


