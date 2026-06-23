"""Wear ledger — chronological maintenance log for the controller.

This is a write-mostly append-only event log that records
wrapper lifecycle events (sessions, profile applies, RP captures/restores,
slider commits, health-report verdicts) plus operator-written service notes.
The UI is a single screen that filters and renders these events in reverse-
chronological order, with a small drift-trend sparkline at the top.
"""

from __future__ import annotations

from zd_app.services.wear_ledger.models import (
    EVENT_TYPES,
    SCHEMA_VERSION,
    WearLedgerEvent,
    event_type_label_key,
    sanitize_service_note,
)
from zd_app.services.wear_ledger.service import (
    DEFAULT_ROTATION_BYTES,
    WearLedgerService,
)


__all__ = [
    "DEFAULT_ROTATION_BYTES",
    "EVENT_TYPES",
    "SCHEMA_VERSION",
    "WearLedgerEvent",
    "WearLedgerService",
    "event_type_label_key",
    "sanitize_service_note",
]
