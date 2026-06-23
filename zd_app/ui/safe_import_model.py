"""UI-facing Safe Import contract, backed by the import risk classifier.

The services module ``zd_app.services.import_classifier`` is the source of truth
for foreign-key risk classification — the deep, pattern-based scan that flags
automation / safety-sensitive / unknown keys at any depth. This module adapts
that classifier to the shape the preview UI consumes and owns the parts the
services contract deliberately does not carry:

* ``RiskCategory`` is re-exported from the services module (identical values),
  so the UI and the classifier share one enum identity.
* ``ImportResult`` / ``ImportAudit`` / ``FieldChange`` keep the UI shape: a
  mutable ``FieldChange`` (the UI fills ``current_value`` for the diff), an
  ``ImportAudit`` the apply flow mutates, and an ``ImportResult`` carrying
  ``error_key`` / ``error_detail`` for hard-fail display plus the convenience
  members the screens read.
* the category map, display helpers, name generation, and the file-loading glue
  (``prepare_import``) are UI-owned and stay here.

``classify_import`` is the adapter: it delegates blocked / unknown / automation
detection to the services classifier, then presents the outcome in the UI
contract. A literal re-export is not possible — the services ``ImportResult``
reports ``ok=False`` whenever any key is blocked (so any automation key fails the
import outright), whereas the UI treats a blocked-but-otherwise-valid profile as
a successful import with the automation stripped and discarded.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zd_app.i18n import t
from zd_app.models import WrapperProfile
from zd_app.services import import_classifier
from zd_app.services.import_classifier import DEVICE_SETTING_KEYS, RiskCategory
from zd_app.storage.snapshot_codec import snapshot_to_dict
from zd_app.storage.wrapper_profile_store import slugify

logger = logging.getLogger(__name__)

# True now that ``classify_import`` delegates risk classification to the
# ``zd_app.services.import_classifier`` module and ``RiskCategory`` is re-exported from it.
USING_RISK_CLASSIFIER = True


# --- Import guards (size / depth) -------------------------------------------
# Prefer the classifier's promoted shared module, then v1's profile_store, then inline
# constants. All three are equivalent; the fallback only matters before the classifier
# promotes the guards into ``_import_guards``.
try:  # pragma: no cover - exercised only once the guards are promoted into _import_guards
    from zd_app.storage._import_guards import (  # type: ignore
        MAX_IMPORT_BYTES,
        MAX_IMPORT_JSON_DEPTH,
        _max_json_depth as _max_json_depth_text,
    )
except ImportError:
    from zd_app.storage.profile_store import (  # type: ignore
        MAX_IMPORT_BYTES,
        MAX_IMPORT_JSON_DEPTH,
        _max_json_depth as _max_json_depth_text,
    )


# Categories the user can choose to import (Automation/Blocked are never
# importable — they are discarded). Device is supported but unchecked by
# default for external imports.
SELECTABLE_CATEGORIES = (
    RiskCategory.FEEL,
    RiskCategory.LAYOUT,
    RiskCategory.COSMETIC,
    RiskCategory.DEVICE,
)
DEFAULT_CHECKED_CATEGORIES = (
    RiskCategory.FEEL,
    RiskCategory.LAYOUT,
    RiskCategory.COSMETIC,
)


# Serialized snapshot top-level key -> risk category (resolved map).
# FEEL = deadzones, sensitivity curves, axis inversion, trigger range/mode,
# motion sensitivity; LAYOUT = button bindings + back-paddle single-button;
# COSMETIC = lighting + vibration; DEVICE = polling rate + step-size.
SNAPSHOT_FIELD_CATEGORY: dict[str, RiskCategory] = {
    "polling_rate": RiskCategory.DEVICE,
    "step_size": RiskCategory.DEVICE,
    "vibration": RiskCategory.COSMETIC,
    "deadzones": RiskCategory.FEEL,
    "axis_inversion_left": RiskCategory.FEEL,
    "axis_inversion_right": RiskCategory.FEEL,
    "sensitivity_left": RiskCategory.FEEL,
    "sensitivity_right": RiskCategory.FEEL,
    # 1.2.9 / fw-1.24 8-point curves (HID cat 0x86): the same per-stick feel
    # surface as the 3-point curves above, so they share the FEEL bucket
    # (mirrors ``import_classifier._CATEGORY_MAP``). Absent from this map they
    # were invisible in the preview AND survived FEEL deselection in
    # ``filtered_snapshot`` — guarded by tests/test_field_registry_drift.py.
    "sensitivity_left_8point": RiskCategory.FEEL,
    "sensitivity_right_8point": RiskCategory.FEEL,
    "trigger_left": RiskCategory.FEEL,
    "trigger_right": RiskCategory.FEEL,
    "button_bindings": RiskCategory.LAYOUT,
    "lighting_zones": RiskCategory.COSMETIC,
    "motion_settings": RiskCategory.FEEL,
    "back_paddle_bindings": RiskCategory.LAYOUT,
}

# Serialized snapshot fields backed by a dict (empty == absent).
_DICT_SNAPSHOT_FIELDS = ("button_bindings", "lighting_zones", "back_paddle_bindings")

# i18n label keys for each known field (used by the diff + result screens).
FIELD_LABEL_KEYS: dict[str, str] = {
    key: f"safe_import.field.{key}" for key in SNAPSHOT_FIELD_CATEGORY
}

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_DISPLAY_NAME_LEN = 64

_POLLING_HZ = {
    1: "250Hz",
    2: "500Hz",
    3: "1000Hz",
    4: "2000Hz",
    5: "4000Hz",
    6: "8000Hz",
}


@dataclass
class FieldChange:
    """A single recognized setting change surfaced in the diff."""

    category: RiskCategory
    key: str
    label_key: str
    imported_value: Any
    current_value: Any = None  # filled by the UI against the live profile


@dataclass
class ImportAudit:
    """Audit trail for an import, rendered on the result screen.

    Partially filled by the classifier (source filename, generated id, blocked
    field names/counts) and completed by the UI when the user applies (selected
    / skipped categories, controller-write status).
    """

    source_filename: str = ""
    generated_profile_id: str = ""
    selected_categories: list[RiskCategory] = field(default_factory=list)
    skipped_categories: list[RiskCategory] = field(default_factory=list)
    blocked_field_names: list[str] = field(default_factory=list)
    blocked_automation_count: int = 0
    controller_write: str = "not_performed"  # not_performed | sent | verified
    verified: bool = False
    restore_point_name: str | None = None
    # Post-apply read-back verification detail, filled by the apply flow when
    # every write was ACKed: "verified" is earned only by a clean read-back
    # comparison (both lists empty); otherwise the result modal renders the
    # "sent" reason from these. ``verify_read_failed`` means the read-back
    # itself raised, so no comparison happened at all.
    verify_mismatched: list[str] = field(default_factory=list)
    verify_unverifiable: list[str] = field(default_factory=list)
    verify_read_failed: bool = False


@dataclass
class ImportResult:
    """Structured, UI-ready result of classifying a candidate profile."""

    ok: bool
    profile: WrapperProfile | None = None
    generated_name: str = ""
    categories: dict[RiskCategory, list[FieldChange]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    blocked_fields: list[str] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    audit: ImportAudit = field(default_factory=ImportAudit)
    # Hard-fail display (None when ok). error_detail is a raw exception string.
    error_key: str | None = None
    error_detail: str = ""

    @property
    def blocked_automation_count(self) -> int:
        return self.audit.blocked_automation_count

    @property
    def has_automation(self) -> bool:
        return self.audit.blocked_automation_count > 0

    @property
    def has_device_changes(self) -> bool:
        return bool(self.categories.get(RiskCategory.DEVICE))

    def selectable_present(self) -> list[RiskCategory]:
        return [c for c in SELECTABLE_CATEGORIES if self.categories.get(c)]


def _clean_display_name(raw_name: Any) -> str:
    text = raw_name if isinstance(raw_name, str) else ""
    text = _CONTROL_CHARS_RE.sub("", text).strip()
    if len(text) > MAX_DISPLAY_NAME_LEN:
        text = text[:MAX_DISPLAY_NAME_LEN].strip()
    return text


def _unique_slug(preferred: str, existing_slugs: set[str]) -> str:
    base = slugify(preferred) or "imported-profile"
    candidate = base
    suffix = 2
    while candidate in existing_slugs:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _display_field_name(qualified: str) -> str:
    """Name an import-classifier field path the way the UI surfaces it.

    The classifier reports snapshot-level foreign keys as ``snapshot.<key>`` (and deeper
    dotted paths for nested hits); the UI names them where the user sees them, so
    the redundant ``snapshot.`` prefix is dropped for display + audit.
    """

    return qualified.removeprefix("snapshot.")


def classify_import(raw_payload: dict, *, existing_names: set[str]) -> ImportResult:
    """Bucket a candidate profile dict into risk categories for the preview UI.

    Foreign-key risk detection (blocked / unknown / automation, scanned at any
    depth) is delegated to the :mod:`zd_app.services.import_classifier` classifier;
    this adapter presents the outcome in the UI contract. Hard-fails (``ok=False``
    + ``error_key``) on a non-object root, bad ``schema_version``, or core
    settings that fail validation. Automation / safety-sensitive keys are recorded
    by name + count and discarded, but a valid core profile still imports
    (``ok=True``). Never raises on hostile input.
    """

    if not isinstance(raw_payload, dict):
        return ImportResult(ok=False, error_key="safe_import.error.not_object")

    risk = import_classifier.classify_import(raw_payload, existing_names=existing_names)
    blocked = [_display_field_name(name) for name in risk.blocked_fields]
    blocked_set = set(blocked)
    # The classifier also lists blocked keys under unknown_fields; the UI's "needs review"
    # signal means benign-ignored keys only, so drop the ones already blocked.
    unknown = [
        name
        for name in (_display_field_name(n) for n in risk.unknown_fields)
        if name not in blocked_set
    ]
    automation_count = risk.blocked_automation_count

    audit = ImportAudit(
        blocked_field_names=blocked,
        blocked_automation_count=automation_count,
    )

    if raw_payload.get("schema_version") != 1:
        return ImportResult(
            ok=False,
            blocked_fields=blocked,
            unknown_fields=unknown,
            audit=audit,
            error_key="safe_import.error.schema",
            error_detail=str(raw_payload.get("schema_version")),
        )

    try:
        profile = WrapperProfile.from_dict(raw_payload)
    except (KeyError, ValueError, TypeError) as exc:
        return ImportResult(
            ok=False,
            blocked_fields=blocked,
            unknown_fields=unknown,
            audit=audit,
            error_key="safe_import.error.invalid",
            error_detail=str(exc),
        )

    display_name = _clean_display_name(raw_payload.get("name")) or t(
        "safe_import.default_name"
    )
    existing_slugs = {slugify(name) for name in existing_names if slugify(name)}
    audit.generated_profile_id = _unique_slug(display_name, existing_slugs)

    categories = _bucket_snapshot(profile)

    warnings: list[str] = []
    if unknown:
        warnings.append(t("safe_import.warning.unknown_ignored", count=len(unknown)))
    if automation_count:
        warnings.append(
            t("safe_import.warning.automation_blocked", count=automation_count)
        )

    return ImportResult(
        ok=True,
        profile=profile,
        generated_name=display_name,
        categories=categories,
        warnings=warnings,
        blocked_fields=blocked,
        unknown_fields=unknown,
        audit=audit,
    )


def _bucket_snapshot(profile: WrapperProfile) -> dict[RiskCategory, list[FieldChange]]:
    serialized = snapshot_to_dict(profile.snapshot)
    categories: dict[RiskCategory, list[FieldChange]] = {}
    for key, category in SNAPSHOT_FIELD_CATEGORY.items():
        value = serialized.get(key)
        if value is None:
            continue
        if key in _DICT_SNAPSHOT_FIELDS and not value:
            continue
        change = FieldChange(
            category=category,
            key=key,
            label_key=FIELD_LABEL_KEYS[key],
            imported_value=value,
        )
        categories.setdefault(category, []).append(change)
    return categories


# --- UI-owned glue (stays here regardless of the classifier) ----------------


def prepare_import(path: str, *, existing_names: set[str]) -> ImportResult:
    """Read + guard + parse a file, then classify it.

    Enforces the size + depth guards BEFORE parsing (DoS protection), mirroring
    v1 ``ProfileStore.import_profile``. Returns an ``ok=False`` result with an
    ``error_key`` for any read/parse failure; the classifier handles validation.
    """

    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError:
        return _failed("safe_import.error.unreadable", source_filename=source.name)
    if size > MAX_IMPORT_BYTES:
        return _failed("safe_import.error.too_large", source_filename=source.name)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return _failed("safe_import.error.unreadable", source_filename=source.name)
    except UnicodeDecodeError:
        return _failed("safe_import.error.invalid_json", source_filename=source.name)
    if _max_json_depth_text(text) > MAX_IMPORT_JSON_DEPTH:
        return _failed("safe_import.error.too_deep", source_filename=source.name)
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return _failed(
            "safe_import.error.invalid_json",
            source_filename=source.name,
            detail=str(exc),
        )

    result = classify_import(raw, existing_names=existing_names)
    result.audit.source_filename = source.name
    return result


def _failed(error_key: str, *, source_filename: str = "", detail: str = "") -> ImportResult:
    return ImportResult(
        ok=False,
        audit=ImportAudit(source_filename=source_filename),
        error_key=error_key,
        error_detail=detail,
    )


def filtered_snapshot(snapshot: Any, selected: set[RiskCategory]) -> Any:
    """Return a copy of ``snapshot`` with unselected-category fields cleared.

    Deselecting a category (e.g. Device/polling, unchecked by default) must
    mean those settings are neither saved nor applied. Cleared fields become
    ``None`` (or ``{}`` for the dict-backed fields), which the apply coordinator
    and codec treat as "no change".
    """

    cleared = {
        key: ({} if key in _DICT_SNAPSHOT_FIELDS else None)
        for key, category in SNAPSHOT_FIELD_CATEGORY.items()
        if category not in selected
    }
    # ControllerSnapshot is a frozen dataclass — rebuild via replace().
    return dataclasses.replace(copy.deepcopy(snapshot), **cleared)


# Categories that stay when Device overrides are dropped (Save As default /
# "Apply profile only"). Derived so it can't drift from the field map.
_NON_DEVICE_CATEGORIES = frozenset(
    category for category in SNAPSHOT_FIELD_CATEGORY.values()
    if category is not RiskCategory.DEVICE
)


def has_device_settings(snapshot: Any) -> bool:
    """True if ``snapshot`` carries any Device override (polling rate / step-size).

    Tolerant of objects missing those attributes (e.g. test stand-ins) so the
    profile-card badge never raises on an unexpected shape.
    """

    return any(getattr(snapshot, key, None) is not None for key in DEVICE_SETTING_KEYS)


def without_device_settings(snapshot: Any) -> Any:
    """Copy of ``snapshot`` with Device-category fields (polling rate, step-size)
    cleared to ``None`` — the "Profile settings only" save/apply path."""

    return filtered_snapshot(snapshot, _NON_DEVICE_CATEGORIES)


def summarize_field(change: FieldChange) -> str:
    """A compact, human-readable summary of an imported field value."""

    return summarize_value(change.key, change.imported_value)


def summarize_value(key: str, value: Any) -> str:
    """Compact, human-readable summary of a serialized snapshot field value."""

    if value is None:
        return t("safe_import.diff.not_set")
    try:
        if key == "polling_rate":
            return _POLLING_HZ.get(value, str(value))
        if key == "deadzones":
            return (
                f"L {value['left_center']}/{value['left_outer']}, "
                f"R {value['right_center']}/{value['right_outer']}"
            )
        if key == "vibration":
            return (
                f"L{value['left_grip_strength']} / R{value['right_grip_strength']}"
            )
        if key in ("axis_inversion_left", "axis_inversion_right"):
            flags = [
                axis
                for axis, on in (("X", value.get("x_inverted")), ("Y", value.get("y_inverted")))
                if on
            ]
            return "+".join(flags) if flags else t("safe_import.value.none")
        if key in (
            "sensitivity_left",
            "sensitivity_right",
            "sensitivity_left_8point",
            "sensitivity_right_8point",
        ):
            return t("safe_import.value.points", count=len(value))
        if key in ("trigger_left", "trigger_right"):
            return f"{value['range_min']}–{value['range_max']}"
        if key == "button_bindings":
            return t("safe_import.value.remaps", count=len(value))
        if key == "lighting_zones":
            return t("safe_import.value.zones", count=len(value))
        if key == "back_paddle_bindings":
            return t("safe_import.value.paddles", count=len(value))
        if key == "motion_settings":
            return t("safe_import.value.sensitivity", value=value.get("sensitivity"))
    except (KeyError, TypeError, IndexError):
        logger.debug("summarize_field fallback for %s", key, exc_info=True)
    return str(value)


def category_label_key(category: RiskCategory) -> str:
    return f"safe_import.categories.{category.value}"
