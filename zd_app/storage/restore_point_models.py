"""Dataclasses for Restore Points (Device State Vault).

Implements the restore-point storage data model (the snapshot JSON shape).
Restore Points are a separate
audit/recovery artifact distinct from wrapper profiles: a deliberate
"separate restore-point store, not profiles" design decision.

Forbidden field names (per the "Never say Factory Backup" rule): no ``factory_*``,
``backup_*``, ``image_*``, ``clone_*``, ``firmware_image_*``,
``rollback_guaranteed``, ``complete_*``. The forbidden-phrase test in
``tests/test_restore_point_forbidden_phrases.py`` scans dataclass attribute
names AND JSON exporter output keys to enforce this.

JSON codec lives in :func:`restore_point_to_dict` / :func:`restore_point_from_dict`
and reuses :mod:`zd_app.storage.snapshot_codec` for the embedded
:class:`~zd_app.services.settings_service.ControllerSnapshot`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional

from zd_app.services.restore_points.boundary import (
    CLAIM_BOUNDARY_PARAGRAPH,
    CLAIM_BOUNDARY_SHORT_UI,
)
from zd_app.services.settings_service import ControllerSnapshot
from zd_app.storage.snapshot_codec import snapshot_from_dict, snapshot_to_dict


SCHEMA_VERSION = 2
KIND = "zd_restore_point"
# Schema versions this codec can read. v1 files predate the
# ``claim_boundary_*`` fields and synthesize them from the module constants
# on load; v2 files include them verbatim. Future bumps extend this set.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})

# Canonical restore-point id shape, exactly what ``_generate_id`` mints:
# ``rp_YYYYMMDD_HHMMSS_<6 lowercase hex>``. Enforced at load so an id can never
# be anything but a safe filename stem (no separators, no ``..``).
_RESTORE_POINT_ID_RE = re.compile(r"rp_\d{8}_\d{6}_[0-9a-f]{6}")


class CoverageState(str, Enum):
    """Per-field coverage state. Per the "Snapshot JSON shape" rule — null/non-null
    is NOT sufficient because some fields are legitimately ``None`` (e.g.
    ``step_size`` on profiles predating device settings). The state must come from a positive
    read-success signal.
    """

    CAPTURED = "captured"
    PARTIAL = "partial"
    NOT_CAPTURED = "not_captured"
    METADATA_ONLY = "metadata_only"
    UNSUPPORTED = "unsupported"


class CoverageCategory(str, Enum):
    """Field category per the "Field category model" rule. Separate from
    coverage state: category describes what kind of setting it is; state
    describes what happened in this snapshot.
    """

    DEVICE = "device"
    FEEL = "feel"
    LAYOUT = "layout"
    COSMETIC = "cosmetic"
    METADATA = "metadata"
    UNSUPPORTED = "unsupported"


class CaptureSource(str, Enum):
    """Where the snapshot data came from for this capture pass. Per the
    "Fresh-read rule": fresh read is preferred; a stale cached snapshot
    counts only with an explicit flag.
    """

    FRESH_READ = "fresh_read"
    CACHED_SNAPSHOT = "cached_snapshot"
    PARTIAL_READ = "partial_read"
    FAILED_READ_NO_SNAPSHOT = "failed_read_no_snapshot"


class IdentityConfidence(str, Enum):
    """Confidence in the device-identity tuple captured with this restore point."""

    READABLE = "readable"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class RestoreResultLabel(str, Enum):
    """Outcome of a restore() call. Per the "Post-restore verification" rule."""

    VERIFIED = "verified"
    RESTORED_WITH_WARNINGS = "restored_with_warnings"
    PARTIALLY_RESTORED = "partially_restored"
    MISMATCH_AFTER_RESTORE = "mismatch_after_restore"
    RESTORE_FAILED = "restore_failed"


@dataclass(frozen=True)
class RestorePointTrigger:
    """Why this restore point was created. Per the "Trigger model" rule.

    ``type`` is the canonical trigger token (e.g. ``first_readable_connect``,
    ``before_safe_import_apply``, ``before_profile_apply_with_device_settings``,
    ``before_manual_device_setting_write``, ``before_restore``, ``manual``).
    """

    type: str
    source_label: str
    reason: str


@dataclass(frozen=True)
class DeviceIdentity:
    """Device-identity tuple as observed at capture time. Per the
    "Snapshot JSON shape" rule.

    ``vid``/``pid``/``product_string`` are hex-or-string identifiers from the
    HID enumeration; ``firmware_version`` is the controller-reported firmware
    string if available. ``identity_confidence`` reflects how complete the
    identity tuple is.
    """

    vid: Optional[str]
    pid: Optional[str]
    product_string: Optional[str]
    firmware_version: Optional[str]
    identity_confidence: IdentityConfidence


@dataclass(frozen=True)
class FieldCoverage:
    """One field's coverage record. ``state`` is the capture outcome,
    ``category`` is the static classification, ``readable`` is True/False/
    'intermittent' per the denial-aware example, and ``writable`` says
    whether restore should attempt to write this field.
    """

    state: CoverageState
    readable: bool | str
    writable: bool
    category: CoverageCategory
    note: Optional[str] = None


@dataclass(frozen=True)
class RestorePointCoverage:
    """Aggregate coverage map. ``fields`` is keyed by snapshot attribute name
    (``polling_rate``, ``vibration``, ...). ``captured_supported_count`` and
    ``total_supported_count`` exclude UNSUPPORTED and METADATA_ONLY fields so
    the UI can say e.g. "captured 10 of 12 supported settings".
    """

    captured_supported_count: int
    total_supported_count: int
    fields: Mapping[str, FieldCoverage]
    capture_source: CaptureSource


@dataclass(frozen=True)
class RestoreFieldOutcome:
    """Per-field detail row in a :class:`RestoreResult`. Captures both the
    write outcome (succeeded/failed) and the read-back verification outcome
    (matched/mismatched/could_not_verify). The "Post-restore verification" rule
    keeps these states distinct so the UI can show counts honestly.

    ``expected_value`` / ``observed_value`` are populated only when
    ``verify_matched is False`` so the result page can render the actual
    values inline without forcing the user to click through to the controller
    tabs and Read again. Both are stored as ``str()`` representations to
    avoid serialization headaches with dataclasses/enums/dicts and to keep
    the result JSON shape flat. Default-None on success keeps the result
    payload small.
    """

    field_name: str
    write_succeeded: bool
    write_error: Optional[str]
    verify_matched: Optional[bool]    # None == could not verify (no read-back)
    verify_note: Optional[str] = None
    expected_value: Optional[str] = None
    observed_value: Optional[str] = None


@dataclass(frozen=True)
class RestoreResult:
    """Outcome of one :meth:`RestorePointService.restore` call. Per the
    "Post-restore verification" rule — counts first, details second.
    """

    label: RestoreResultLabel
    attempted: int
    wrote_succeeded: int
    write_failed: int
    verified_matched: int
    could_not_verify: int
    mismatched: int
    fields: tuple[RestoreFieldOutcome, ...]
    before_restore_point_id: Optional[str]
    completed_at: str   # ISO-8601 UTC


@dataclass(frozen=True)
class RestoreFieldDelta:
    """One field's pre-restore comparison between the captured RP value and
    the live controller value. Computed by
    :meth:`RestorePointService.compute_restore_preview` so the CONFIRM
    modal can show what will change BEFORE the user commits to the restore
    — symmetric to the per-field diagnostic line the result page shows
    AFTER a restore (from the restore-result-enrichment work).

    ``current_value`` is ``None`` when the device read failed or returned
    nothing; in that case ``note`` carries a short ``"device read failed:
    <ExceptionType>: <msg>"`` (mirroring the restore-result-enrichment pattern)
    or a plain ``"device not readable"`` fallback. ``will_change`` is
    conservatively ``False`` on unreadable fields — we cannot prove a
    change without a current value.
    """

    field_name: str
    will_change: bool
    current_value: Optional[str]
    target_value: Optional[str]
    note: Optional[str] = None


@dataclass(frozen=True)
class RestorePreview:
    """Aggregate pre-restore preview. The CONFIRM modal renders only
    ``fields`` whose ``will_change`` is True; the count fields drive the
    summary sentences below the list (e.g. "(N fields couldn't be read)"
    or "No changes detected").

    Not persisted to disk — this is a service-layer-only computation used
    by the UI to render the CONFIRM modal. No SCHEMA_VERSION bump needed.
    """

    fields: tuple[RestoreFieldDelta, ...]
    fields_changing: int
    fields_unchanged: int
    fields_unreadable: int


@dataclass(frozen=True)
class RestoreAttemptRecord:
    """Persisted-onto-the-restore-point summary of the most recent restore.
    Per the "Apply coordinator interaction" rule — restore attempts go into
    the event log AND optionally onto the restore point metadata.
    """

    attempted_at: str   # ISO-8601 UTC
    label: RestoreResultLabel
    attempted: int
    wrote_succeeded: int
    write_failed: int
    verified_matched: int
    could_not_verify: int
    mismatched: int


@dataclass(frozen=True)
class RestorePoint:
    """One restore point. Reuses the existing :class:`ControllerSnapshot`
    dataclass for the captured settings payload.

    ``claim_boundary_paragraph`` + ``claim_boundary_short`` were added in
    schema v2 so the verbatim denial language travels with every
    JSON export (per the "Required claim-boundary statement" rule: "Put this in
    the Restore UI, detail view, and every export."). Defaults come from
    :mod:`zd_app.services.restore_points.boundary` so existing call sites
    that don't pass them still get the load-bearing copy.
    """

    schema_version: int
    kind: str
    id: str
    created_at: str         # ISO-8601 UTC
    app_version: str
    app_build_commit: Optional[str]
    title: str
    trigger: RestorePointTrigger
    device_identity: DeviceIdentity
    snapshot: ControllerSnapshot
    coverage: RestorePointCoverage
    last_restore_attempt: Optional[RestoreAttemptRecord] = None
    claim_boundary_paragraph: str = CLAIM_BOUNDARY_PARAGRAPH
    claim_boundary_short: str = CLAIM_BOUNDARY_SHORT_UI


@dataclass(frozen=True)
class SkippedFile:
    """A restore-point file the store could not parse. Surfaced via
    :meth:`RestorePointStore.list` for low-panic disclosure per the
    "Atomic write and corruption behavior" rule.
    """

    path: str
    error: str


# ---------------------------------------------------------------------------
# JSON codec
# ---------------------------------------------------------------------------


class RestorePointParseError(ValueError):
    """Raised when a restore-point file cannot be loaded.

    Subclasses :class:`ValueError` so that callers catching the broad codec
    error stay correct; the store also catches :class:`OSError` /
    :class:`json.JSONDecodeError` separately for low-panic disclosure.
    """


class RestorePointSchemaError(RestorePointParseError):
    """Raised when a restore-point JSON has an unknown ``schema_version``.

    Distinct subclass so the UI can render a "from an unsupported version"
    message rather than a generic parse failure. Per the "Atomic write
    and corruption behavior" — corrupt and unknown-schema should not crash
    the app, and ideally should be distinguishable.
    """


def restore_point_to_dict(rp: RestorePoint) -> dict[str, Any]:
    """Serialize a :class:`RestorePoint` to the JSON-ready dict shape."""

    return {
        "schema_version": rp.schema_version,
        "kind": rp.kind,
        "id": rp.id,
        "created_at": rp.created_at,
        "app_version": rp.app_version,
        "app_build_commit": rp.app_build_commit,
        "title": rp.title,
        "trigger": {
            "type": rp.trigger.type,
            "source_label": rp.trigger.source_label,
            "reason": rp.trigger.reason,
        },
        "device_identity": {
            "vid": rp.device_identity.vid,
            "pid": rp.device_identity.pid,
            "product_string": rp.device_identity.product_string,
            "firmware_version": rp.device_identity.firmware_version,
            "identity_confidence": rp.device_identity.identity_confidence.value,
        },
        "snapshot": snapshot_to_dict(rp.snapshot),
        "coverage": {
            "captured_supported_count": rp.coverage.captured_supported_count,
            "total_supported_count": rp.coverage.total_supported_count,
            "capture_source": rp.coverage.capture_source.value,
            "fields": {
                name: _field_coverage_to_dict(coverage)
                for name, coverage in rp.coverage.fields.items()
            },
        },
        "last_restore_attempt": (
            _restore_attempt_to_dict(rp.last_restore_attempt)
            if rp.last_restore_attempt is not None
            else None
        ),
        "claim_boundary_paragraph": rp.claim_boundary_paragraph,
        "claim_boundary_short": rp.claim_boundary_short,
    }


def restore_point_from_dict(payload: dict[str, Any]) -> RestorePoint:
    """Deserialize a JSON dict into a :class:`RestorePoint`.

    Raises :class:`RestorePointSchemaError` on unknown ``schema_version`` and
    :class:`RestorePointParseError` on any other shape/value error.
    """

    if not isinstance(payload, dict):
        raise RestorePointParseError(
            f"restore point JSON must be an object, got {type(payload).__name__}"
        )

    raw_kind = payload.get("kind")
    if raw_kind != KIND:
        raise RestorePointParseError(f"unexpected kind: {raw_kind!r}")

    raw_schema = payload.get("schema_version")
    if raw_schema not in SUPPORTED_SCHEMA_VERSIONS:
        raise RestorePointSchemaError(f"unsupported schema_version: {raw_schema!r}")

    # The id is used verbatim as the export filename stem (``<id>.json``), so it
    # must be the exact canonical shape the generator mints (rp_YYYYMMDD_HHMMSS_
    # <6 hex>) — never a path. A planted file whose ``id`` carries separators or
    # ``..`` is rejected at load (store.list/load surface it as a parse error),
    # so it can never reach the export writer. Validated outside the try below so
    # this RestorePointParseError isn't re-wrapped by the value-error handler.
    raw_id = payload.get("id")
    if not isinstance(raw_id, str) or not _RESTORE_POINT_ID_RE.fullmatch(raw_id):
        raise RestorePointParseError(f"invalid restore point id: {raw_id!r}")

    # v1 files predate the claim-boundary fields; synthesize from the module
    # constants so the on-disk artifact still travels with the verbatim
    # denial language at read time. v2+ files carry the strings directly.
    claim_paragraph = payload.get("claim_boundary_paragraph")
    if claim_paragraph is None:
        claim_paragraph = CLAIM_BOUNDARY_PARAGRAPH
    claim_short = payload.get("claim_boundary_short")
    if claim_short is None:
        claim_short = CLAIM_BOUNDARY_SHORT_UI

    try:
        trigger_payload = payload["trigger"]
        identity_payload = payload["device_identity"]
        coverage_payload = payload["coverage"]
        snapshot_payload = payload["snapshot"]
        return RestorePoint(
            schema_version=int(raw_schema),
            kind=KIND,
            id=raw_id,
            created_at=str(payload["created_at"]),
            app_version=str(payload["app_version"]),
            app_build_commit=_optional_str(payload.get("app_build_commit")),
            title=str(payload["title"]),
            trigger=RestorePointTrigger(
                type=str(trigger_payload["type"]),
                source_label=str(trigger_payload["source_label"]),
                reason=str(trigger_payload["reason"]),
            ),
            device_identity=DeviceIdentity(
                vid=_optional_str(identity_payload.get("vid")),
                pid=_optional_str(identity_payload.get("pid")),
                product_string=_optional_str(identity_payload.get("product_string")),
                firmware_version=_optional_str(identity_payload.get("firmware_version")),
                identity_confidence=IdentityConfidence(
                    identity_payload.get("identity_confidence", "unknown")
                ),
            ),
            snapshot=snapshot_from_dict(snapshot_payload),
            coverage=RestorePointCoverage(
                captured_supported_count=int(coverage_payload["captured_supported_count"]),
                total_supported_count=int(coverage_payload["total_supported_count"]),
                capture_source=CaptureSource(coverage_payload["capture_source"]),
                fields={
                    str(name): _field_coverage_from_dict(value)
                    for name, value in (coverage_payload.get("fields") or {}).items()
                },
            ),
            last_restore_attempt=(
                _restore_attempt_from_dict(payload["last_restore_attempt"])
                if payload.get("last_restore_attempt") is not None
                else None
            ),
            claim_boundary_paragraph=str(claim_paragraph),
            claim_boundary_short=str(claim_short),
        )
    except RestorePointSchemaError:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise RestorePointParseError(f"invalid restore point payload: {exc}") from exc


def _field_coverage_to_dict(coverage: FieldCoverage) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": coverage.state.value,
        "readable": coverage.readable,
        "writable": coverage.writable,
        "category": coverage.category.value,
    }
    if coverage.note is not None:
        result["note"] = coverage.note
    return result


def _field_coverage_from_dict(payload: Mapping[str, Any]) -> FieldCoverage:
    readable = payload["readable"]
    if not isinstance(readable, (bool, str)):
        raise ValueError(f"readable must be bool or str, got {readable!r}")
    return FieldCoverage(
        state=CoverageState(payload["state"]),
        readable=readable,
        writable=bool(payload["writable"]),
        category=CoverageCategory(payload["category"]),
        note=_optional_str(payload.get("note")),
    )


def _restore_attempt_to_dict(record: RestoreAttemptRecord) -> dict[str, Any]:
    return {
        "attempted_at": record.attempted_at,
        "label": record.label.value,
        "attempted": record.attempted,
        "wrote_succeeded": record.wrote_succeeded,
        "write_failed": record.write_failed,
        "verified_matched": record.verified_matched,
        "could_not_verify": record.could_not_verify,
        "mismatched": record.mismatched,
    }


def _restore_attempt_from_dict(payload: Mapping[str, Any]) -> RestoreAttemptRecord:
    return RestoreAttemptRecord(
        attempted_at=str(payload["attempted_at"]),
        label=RestoreResultLabel(payload["label"]),
        attempted=int(payload["attempted"]),
        wrote_succeeded=int(payload["wrote_succeeded"]),
        write_failed=int(payload["write_failed"]),
        verified_matched=int(payload["verified_matched"]),
        could_not_verify=int(payload["could_not_verify"]),
        mismatched=int(payload["mismatched"]),
    )


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "KIND",
    "CaptureSource",
    "CoverageCategory",
    "CoverageState",
    "DeviceIdentity",
    "FieldCoverage",
    "IdentityConfidence",
    "RestoreAttemptRecord",
    "RestoreFieldDelta",
    "RestoreFieldOutcome",
    "RestorePoint",
    "RestorePointCoverage",
    "RestorePointParseError",
    "RestorePointSchemaError",
    "RestorePointTrigger",
    "RestorePreview",
    "RestoreResult",
    "RestoreResultLabel",
    "SkippedFile",
    "restore_point_from_dict",
    "restore_point_to_dict",
]
