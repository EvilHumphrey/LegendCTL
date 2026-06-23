"""Wear ledger data model.

Events are persisted as line-delimited JSON. Each line is a single
:class:`WearLedgerEvent` serialised via :meth:`WearLedgerEvent.to_dict`.

Schema versioning follows the same pattern as ``restore_point_models``:
:data:`SCHEMA_VERSION` is the current write version and
:data:`SUPPORTED_SCHEMA_VERSIONS` is the read-compatibility set. v1 is the
only version that exists today; the field is recorded so future migrations
can rewrite old lines without losing the original.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from zd_app.services.path_scrub import scrub_paths


SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})


SESSION_START = "session_start"
SESSION_END = "session_end"
PROFILE_APPLY = "profile_apply"
RP_CAPTURE = "rp_capture"
RP_RESTORE = "rp_restore"
RP_DELETE = "rp_delete"
SLIDER_WRITE = "slider_write"
HEALTH_REPORT = "health_report"
READINESS_CHECK = "readiness_check"
SERVICE_NOTE = "service_note"
MODULE_ASSIGNED = "module_assigned"
MODULE_CHARACTERIZED = "module_characterized"
MODULE_NOTES_UPDATED = "module_notes_updated"
MODULE_TREND_FLAGGED = "module_trend_flagged"
DIAGNOSTIC_BUNDLE_GENERATED = "diagnostic_bundle_generated"


EVENT_TYPES: tuple[str, ...] = (
    SESSION_START,
    SESSION_END,
    PROFILE_APPLY,
    RP_CAPTURE,
    RP_RESTORE,
    RP_DELETE,
    SLIDER_WRITE,
    HEALTH_REPORT,
    READINESS_CHECK,
    SERVICE_NOTE,
    MODULE_ASSIGNED,
    MODULE_CHARACTERIZED,
    MODULE_NOTES_UPDATED,
    MODULE_TREND_FLAGGED,
    DIAGNOSTIC_BUNDLE_GENERATED,
)


# Filter-dropdown grouping. The screen renders these in order: "All", then
# the categories below. Each category maps to a set of event_type values so
# the filter knows which events to show.
FILTER_CATEGORY_SESSIONS = "sessions"
FILTER_CATEGORY_PROFILES = "profiles"
FILTER_CATEGORY_RESTORES = "restores"
FILTER_CATEGORY_HEALTH = "health"
FILTER_CATEGORY_SLIDER = "slider"
FILTER_CATEGORY_NOTES = "notes"
FILTER_CATEGORY_MODULES = "modules"
FILTER_CATEGORY_BUNDLES = "bundles"


FILTER_CATEGORY_MEMBERS: dict[str, frozenset[str]] = {
    FILTER_CATEGORY_SESSIONS: frozenset({SESSION_START, SESSION_END}),
    FILTER_CATEGORY_PROFILES: frozenset({PROFILE_APPLY}),
    FILTER_CATEGORY_RESTORES: frozenset({RP_CAPTURE, RP_RESTORE, RP_DELETE}),
    FILTER_CATEGORY_HEALTH: frozenset({HEALTH_REPORT, READINESS_CHECK}),
    FILTER_CATEGORY_SLIDER: frozenset({SLIDER_WRITE}),
    FILTER_CATEGORY_NOTES: frozenset({SERVICE_NOTE}),
    FILTER_CATEGORY_MODULES: frozenset(
        {
            MODULE_ASSIGNED,
            MODULE_CHARACTERIZED,
            MODULE_NOTES_UPDATED,
            MODULE_TREND_FLAGGED,
        }
    ),
    FILTER_CATEGORY_BUNDLES: frozenset({DIAGNOSTIC_BUNDLE_GENERATED}),
}


_EVENT_TYPE_LABEL_KEYS: dict[str, str] = {
    SESSION_START: "wear_ledger.event.session_start",
    SESSION_END: "wear_ledger.event.session_end",
    PROFILE_APPLY: "wear_ledger.event.profile_apply",
    RP_CAPTURE: "wear_ledger.event.rp_capture",
    RP_RESTORE: "wear_ledger.event.rp_restore",
    RP_DELETE: "wear_ledger.event.rp_delete",
    SLIDER_WRITE: "wear_ledger.event.slider_write",
    HEALTH_REPORT: "wear_ledger.event.health_report",
    READINESS_CHECK: "wear_ledger.event.readiness_check",
    SERVICE_NOTE: "wear_ledger.event.service_note",
    MODULE_ASSIGNED: "wear_ledger.event.module_assigned",
    MODULE_CHARACTERIZED: "wear_ledger.event.module_characterized",
    MODULE_NOTES_UPDATED: "wear_ledger.event.module_notes_updated",
    MODULE_TREND_FLAGGED: "wear_ledger.event.module_trend_flagged",
    DIAGNOSTIC_BUNDLE_GENERATED: "wear_ledger.event.diagnostic_bundle_generated",
}


def event_type_label_key(event_type: str) -> str:
    """Return the i18n key for an event type, or a debug placeholder."""

    return _EVENT_TYPE_LABEL_KEYS.get(event_type, f"wear_ledger.event.{event_type}")


# Free-text sanitiser for operator-written service notes. Strips C0 control
# characters except newline + tab, normalises CRLF/CR to LF, collapses runs
# of >2 newlines to 2, trims trailing whitespace on each line, redacts
# absolute filesystem paths, and caps the whole string at MAX_SERVICE_NOTE_LEN
# characters. The wrapper does not currently render Markdown or HTML, but the
# ledger is append-only so a note written today may be rendered by a future
# change — defensive scrubbing now is cheaper than a retroactive sweep over
# thousands of jsonl lines later. The path redaction also keeps the raw
# ``events.jsonl`` artifact free of the operator's home path (the diagnostic
# bundle re-scrubs its preview, but the local file should not bear paths
# either).
MAX_SERVICE_NOTE_LEN = 2000

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def sanitize_service_note(text: str) -> str:
    if not text:
        return ""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _CONTROL_CHARS_RE.sub("", cleaned)
    cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n"))
    cleaned = _MULTI_NEWLINE_RE.sub("\n\n", cleaned)
    cleaned = cleaned.strip()
    # Redact absolute paths (drops the account username after a Users/home
    # root, even a spaced display name) before the length cap, so the cap
    # applies to the final persisted form.
    cleaned = scrub_paths(cleaned)
    if len(cleaned) > MAX_SERVICE_NOTE_LEN:
        cleaned = cleaned[:MAX_SERVICE_NOTE_LEN].rstrip()
    return cleaned


@dataclass(frozen=True)
class WearLedgerEvent:
    """One row in the chronological maintenance log.

    Fields:
        ts: ISO-8601 UTC timestamp with a ``Z`` suffix
            (e.g. ``"2026-05-26T18:42:11Z"``). Matches the restore-point
            timestamp shape so the two log surfaces are mutually readable.
        event_type: One of :data:`EVENT_TYPES`. Used for filter-dropdown
            grouping and for the per-row label lookup.
        summary: A short, pre-formatted one-line string the UI can render
            verbatim. Localised at write-time (service builds the summary
            from i18n keys before persisting) so re-rendering an older
            ledger after a translation change still shows the original
            event description.
        details: Free-form dict of typed metadata for the expand-row view
            and for filters that need more than ``event_type``. Always
            JSON-serialisable; the service refuses non-serialisable values.
        schema_version: Always :data:`SCHEMA_VERSION` for newly written
            events; older lines retain whatever value they were written
            with so a future migration can dispatch on it.
    """

    ts: str
    event_type: str
    summary: str
    details: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "event_type": self.event_type,
            "summary": self.summary,
            "details": dict(self.details),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WearLedgerEvent":
        ts = payload.get("ts")
        event_type = payload.get("event_type")
        summary = payload.get("summary", "")
        details = payload.get("details", {}) or {}
        schema_version = int(payload.get("schema_version", SCHEMA_VERSION))
        if not isinstance(ts, str) or not isinstance(event_type, str):
            raise ValueError("WearLedgerEvent requires str ts and event_type")
        if not isinstance(details, Mapping):
            raise ValueError("WearLedgerEvent.details must be a mapping")
        return cls(
            ts=ts,
            event_type=event_type,
            summary=str(summary),
            details=dict(details),
            schema_version=schema_version,
        )


__all__ = [
    "DIAGNOSTIC_BUNDLE_GENERATED",
    "EVENT_TYPES",
    "FILTER_CATEGORY_BUNDLES",
    "FILTER_CATEGORY_HEALTH",
    "FILTER_CATEGORY_MEMBERS",
    "FILTER_CATEGORY_MODULES",
    "FILTER_CATEGORY_NOTES",
    "FILTER_CATEGORY_PROFILES",
    "FILTER_CATEGORY_RESTORES",
    "FILTER_CATEGORY_SESSIONS",
    "FILTER_CATEGORY_SLIDER",
    "HEALTH_REPORT",
    "MAX_SERVICE_NOTE_LEN",
    "MODULE_ASSIGNED",
    "MODULE_CHARACTERIZED",
    "MODULE_NOTES_UPDATED",
    "MODULE_TREND_FLAGGED",
    "PROFILE_APPLY",
    "READINESS_CHECK",
    "RP_CAPTURE",
    "RP_DELETE",
    "RP_RESTORE",
    "SCHEMA_VERSION",
    "SERVICE_NOTE",
    "SESSION_END",
    "SESSION_START",
    "SLIDER_WRITE",
    "SUPPORTED_SCHEMA_VERSIONS",
    "WearLedgerEvent",
    "event_type_label_key",
    "sanitize_service_note",
]
