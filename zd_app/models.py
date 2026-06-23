"""Shared application models for the ZD Ultimate Legend MVP."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4


ConnectionState = Literal[
    "no_device",
    "connecting",
    "connected",
    "unsupported_firmware",
    "wrong_mode",
    "device_error",
]

DataFreshness = Literal[
    "never_read",
    "reading",
    "fresh",
    "stale",
    "write_pending",
    "write_success",
    "write_failed",
]

CapabilityState = Literal["supported", "unsupported", "unknown_pending_protocol_work"]

SyncStatus = Literal[
    "Disconnected",
    "Connected",
    "Reading",
    "Ready",
    "Unsaved Changes",
    "Applying",
    "Apply Failed",
]

ProfileOrigin = Literal["device", "desktop", "draft"]
SummaryFieldSource = Literal["unknown", "xinput", "official_app_ui", "protocol"]

BUTTON_ACTIONS = [
    "A",
    "B",
    "X",
    "Y",
    "LB",
    "RB",
    "LT",
    "RT",
    "L3",
    "R3",
    "DPad Up",
    "DPad Down",
    "DPad Left",
    "DPad Right",
    "Start",
    "Back",
    "Home",
    "Capture",
    "Paddle 1",
    "Paddle 2",
    "Paddle 3",
    "Paddle 4",
    "Macro Slot 1",
    "Macro Slot 2",
    "Disabled",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ButtonMapping:
    physical_input_id: str
    physical_label: str
    action: str
    default_action: str
    turbo_enabled: bool = False
    macro_binding: str | None = None

    def to_dict(self) -> dict:
        return {
            "physical_input_id": self.physical_input_id,
            "physical_label": self.physical_label,
            "action": self.action,
            "default_action": self.default_action,
            "turbo_enabled": self.turbo_enabled,
            "macro_binding": self.macro_binding,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ButtonMapping":
        return cls(
            physical_input_id=payload["physical_input_id"],
            physical_label=payload["physical_label"],
            action=payload["action"],
            default_action=payload.get("default_action", payload["action"]),
            turbo_enabled=payload.get("turbo_enabled", False),
            macro_binding=payload.get("macro_binding"),
        )


@dataclass
class StickSettings:
    center_deadzone: int = 8
    peripheral_deadzone: int = 95
    deadzone_compensation: int = 0
    curve_preset: str = "Default"
    invert_x: bool = False
    invert_y: bool = False
    report_rate_hz: int | None = None

    def to_dict(self) -> dict:
        return {
            "center_deadzone": self.center_deadzone,
            "peripheral_deadzone": self.peripheral_deadzone,
            "deadzone_compensation": self.deadzone_compensation,
            "curve_preset": self.curve_preset,
            "invert_x": self.invert_x,
            "invert_y": self.invert_y,
            "report_rate_hz": self.report_rate_hz,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "StickSettings":
        return cls(
            center_deadzone=payload.get("center_deadzone", 8),
            peripheral_deadzone=payload.get("peripheral_deadzone", 95),
            deadzone_compensation=payload.get("deadzone_compensation", 0),
            curve_preset=payload.get("curve_preset", "Default"),
            invert_x=payload.get("invert_x", False),
            invert_y=payload.get("invert_y", False),
            report_rate_hz=payload.get("report_rate_hz"),
        )


@dataclass
class TriggerSettings:
    mode: str = "Standard"
    threshold: int = 50
    actuation_point: int = 50
    hair_trigger: bool = False

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "threshold": self.threshold,
            "actuation_point": self.actuation_point,
            "hair_trigger": self.hair_trigger,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "TriggerSettings":
        return cls(
            mode=payload.get("mode", "Standard"),
            threshold=payload.get("threshold", 50),
            actuation_point=payload.get("actuation_point", 50),
            hair_trigger=payload.get("hair_trigger", False),
        )


@dataclass
class Profile:
    profile_id: str
    display_name: str
    origin: ProfileOrigin
    button_mappings: list[ButtonMapping]
    left_stick: StickSettings = field(default_factory=StickSettings)
    right_stick: StickSettings = field(default_factory=StickSettings)
    left_trigger: TriggerSettings = field(default_factory=TriggerSettings)
    right_trigger: TriggerSettings = field(default_factory=TriggerSettings)
    dirty: bool = False
    active_on_controller: bool = False
    last_modified: str = field(default_factory=utc_now_iso)

    def clone(self) -> "Profile":
        return Profile.from_dict(self.to_dict())

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "origin": self.origin,
            "button_mappings": [mapping.to_dict() for mapping in self.button_mappings],
            "left_stick": self.left_stick.to_dict(),
            "right_stick": self.right_stick.to_dict(),
            "left_trigger": self.left_trigger.to_dict(),
            "right_trigger": self.right_trigger.to_dict(),
            "dirty": self.dirty,
            "active_on_controller": self.active_on_controller,
            "last_modified": self.last_modified,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Profile":
        # Normalize wrong-shape input (a non-dict payload, a missing required
        # key, or a button_mappings entry that isn't a dict) to ValueError so
        # callers get one clean failure type to catch. Without this, a malformed
        # legacy "Import Config" file raises a raw KeyError/TypeError/
        # AttributeError that escapes import_profile_from_modal and wedges the
        # UI. Mirrors snapshot_from_dict / ModulePassport.from_dict.
        try:
            return cls(
                profile_id=payload["profile_id"],
                display_name=payload["display_name"],
                origin=payload.get("origin", "draft"),
                button_mappings=[ButtonMapping.from_dict(item) for item in payload.get("button_mappings", [])],
                left_stick=StickSettings.from_dict(payload.get("left_stick", {})),
                right_stick=StickSettings.from_dict(payload.get("right_stick", {})),
                left_trigger=TriggerSettings.from_dict(payload.get("left_trigger", {})),
                right_trigger=TriggerSettings.from_dict(payload.get("right_trigger", {})),
                dirty=payload.get("dirty", False),
                active_on_controller=payload.get("active_on_controller", False),
                last_modified=payload.get("last_modified", utc_now_iso()),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"Invalid profile payload: {exc}") from exc


@dataclass
class WrapperProfile:
    """Named snapshot of wrapper-controlled controller settings."""

    name: str
    snapshot: Any
    description: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    last_modified_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        from zd_app.storage.snapshot_codec import snapshot_to_dict

        return {
            "schema_version": 1,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "last_modified_at": self.last_modified_at,
            "snapshot": snapshot_to_dict(self.snapshot),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WrapperProfile":
        from zd_app.storage.snapshot_codec import snapshot_from_dict

        if payload.get("schema_version") != 1:
            raise ValueError(
                f"Unsupported wrapper-profile schema_version: {payload.get('schema_version')!r}"
            )
        return cls(
            name=payload["name"],
            description=payload.get("description", ""),
            created_at=payload["created_at"],
            last_modified_at=payload["last_modified_at"],
            snapshot=snapshot_from_dict(payload["snapshot"]),
        )


@dataclass
class OnboardSlot:
    slot_id: int
    display_name: str
    status: str = "slot_unknown"
    active: bool = False
    target_selected: bool = False
    source_profile_id: str | None = None


@dataclass
class DeviceState:
    product_name: str = "ZD Ultimate Legend"
    stable_identifier: str = "unknown"
    connection_mode: str = "Unknown"
    firmware_version: str = "Unknown"
    battery_level: str = "Unknown"
    sleep_setting: str = "Unknown"
    active_onboard_profile: int = 1
    sync_status: SyncStatus = "Disconnected"
    connection_state: ConnectionState = "no_device"
    data_freshness: DataFreshness = "never_read"
    supported_capabilities: dict[str, CapabilityState] = field(default_factory=dict)
    summary_sources: dict[str, SummaryFieldSource] = field(
        default_factory=lambda: {
            "battery": "unknown",
            "firmware": "unknown",
            "active_profile": "unknown",
            "sleep": "unknown",
        }
    )
    xinput_slot: int | None = None
    last_read_time: str | None = None
    last_apply_time: str | None = None


@dataclass
class DiagnosticSnapshot:
    timestamp: str
    connection_mode: str
    device_id: str
    firmware_version: str
    active_profile: int
    last_packet_timestamp: str | None
    last_read_duration_ms: float | None
    last_write_duration_ms: float | None
    event_log: list[str]


@dataclass
class AppSettings:
    language: str = "en"
    logging_verbosity: str = "Normal"
    auto_read_on_connect: bool = True
    diagnostics_bundle_dir: str = "zd_data/diagnostics"
    show_legacy_screens: bool = False
    developer_panels_visible: bool = False
    last_reviewed_crash_timestamp: str | None = None
    first_run_acknowledged: bool = False

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "logging_verbosity": self.logging_verbosity,
            "auto_read_on_connect": self.auto_read_on_connect,
            "diagnostics_bundle_dir": self.diagnostics_bundle_dir,
            "show_legacy_screens": self.show_legacy_screens,
            "developer_panels_visible": self.developer_panels_visible,
            "last_reviewed_crash_timestamp": self.last_reviewed_crash_timestamp,
            "first_run_acknowledged": self.first_run_acknowledged,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "AppSettings":
        # Tolerate a stale ``vendor_path`` key in on-disk settings written
        # before v1 became the ship path — it's silently ignored. New
        # writes drop the key (see ``to_dict``).
        # ``developer_panels_visible`` defaults to False for forward-compat
        # with settings.json files written before the UX-cleanup pass.
        # ``last_reviewed_crash_timestamp`` defaults to None for forward-compat
        # with settings.json files written before the crash-review feature landed.
        # ``first_run_acknowledged`` defaults to False for forward-compat with
        # settings.json files written before the first-run acknowledgment gate
        # landed — an existing install with no flag is treated as not-yet
        # acknowledged and sees the gate once on its next launch.
        last_reviewed = payload.get("last_reviewed_crash_timestamp")
        return cls(
            language=_normalize_language_code(payload.get("language", "en")),
            logging_verbosity=payload.get("logging_verbosity", "Normal"),
            auto_read_on_connect=payload.get("auto_read_on_connect", True),
            diagnostics_bundle_dir=payload.get("diagnostics_bundle_dir", "zd_data/diagnostics"),
            show_legacy_screens=bool(payload.get("show_legacy_screens", False)),
            developer_panels_visible=bool(payload.get("developer_panels_visible", False)),
            last_reviewed_crash_timestamp=last_reviewed if isinstance(last_reviewed, str) else None,
            first_run_acknowledged=bool(payload.get("first_run_acknowledged", False)),
        )


def _normalize_language_code(value: str | None) -> str:
    return {
        None: "en",
        "": "en",
        "English": "en",
        "en": "en",
        "Simplified Chinese": "zh-CN",
        "Chinese (Simplified)": "zh-CN",
        "简体中文": "zh-CN",
        "zh": "zh-CN",
        "zh-CN": "zh-CN",
    }.get(value, "en")


def default_button_mappings() -> list[ButtonMapping]:
    defaults = [
        ("a", "A", "A"),
        ("b", "B", "B"),
        ("x", "X", "X"),
        ("y", "Y", "Y"),
        ("lb", "LB", "LB"),
        ("rb", "RB", "RB"),
        ("lt", "LT", "LT"),
        ("rt", "RT", "RT"),
        ("l3", "L3", "L3"),
        ("r3", "R3", "R3"),
        ("dpad_up", "DPad Up", "DPad Up"),
        ("dpad_down", "DPad Down", "DPad Down"),
        ("dpad_left", "DPad Left", "DPad Left"),
        ("dpad_right", "DPad Right", "DPad Right"),
        ("start", "Start", "Start"),
        ("back", "Back", "Back"),
        ("paddle_1", "Paddle 1", "A"),
        ("paddle_2", "Paddle 2", "B"),
        ("paddle_3", "Paddle 3", "X"),
        ("paddle_4", "Paddle 4", "Y"),
        ("rear_1", "Rear Button 1", "LB"),
        ("rear_2", "Rear Button 2", "RB"),
        ("extra_shoulder_l", "Extra Shoulder L", "LT"),
        ("extra_shoulder_r", "Extra Shoulder R", "RT"),
    ]
    return [
        ButtonMapping(
            physical_input_id=input_id,
            physical_label=label,
            action=action,
            default_action=action,
        )
        for input_id, label, action in defaults
    ]


def create_default_profile(name: str = "Unsaved Draft", origin: ProfileOrigin = "draft") -> Profile:
    return Profile(
        profile_id=f"profile-{uuid4().hex[:8]}",
        display_name=name,
        origin=origin,
        button_mappings=default_button_mappings(),
    )


def diff_profile_count(current: Profile, baseline: Profile | None) -> int:
    if baseline is None:
        return 0

    diff_count = 0
    baseline_map = {item.physical_input_id: item for item in baseline.button_mappings}
    for mapping in current.button_mappings:
        previous = baseline_map.get(mapping.physical_input_id)
        if previous is None or previous.action != mapping.action:
            diff_count += 1

    if current.left_stick.to_dict() != baseline.left_stick.to_dict():
        diff_count += 1
    if current.right_stick.to_dict() != baseline.right_stick.to_dict():
        diff_count += 1
    if current.left_trigger.to_dict() != baseline.left_trigger.to_dict():
        diff_count += 1
    if current.right_trigger.to_dict() != baseline.right_trigger.to_dict():
        diff_count += 1

    return diff_count


def deep_copy_profile(profile: Profile | None) -> Profile | None:
    return deepcopy(profile) if profile is not None else None
