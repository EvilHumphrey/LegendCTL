"""WearLedgerService — append-only, best-effort chronological event log.

The service owns one directory (``<user_data_dir>/wear_ledger/``) and writes
events as line-delimited JSON to ``events.jsonl``. When the file would
exceed :data:`DEFAULT_ROTATION_BYTES`, it rotates atomically to a stamped
name (``events-YYYYMMDD_HHMMSS.jsonl``) and a fresh ``events.jsonl`` takes
over. :meth:`read_events` transparently merges the active file and all
rotated files, sorted reverse-chronologically.

All write paths are best-effort: a failure to persist an event is logged
but never raised to the caller. The wear ledger is a personal-utility log,
not a critical system, so a transient disk error must not crash a profile
apply / restore / slider write.

The service is UI-free. Callers in other services build the event payload
(summary string + details dict) and call :meth:`append`; the wear-ledger
screen renders events back via :meth:`read_events`.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from zd_app.services.path_scrub import scrub_paths, scrub_value
from zd_app.services.wear_ledger.models import (
    SCHEMA_VERSION,
    WearLedgerEvent,
)


logger = logging.getLogger(__name__)


DEFAULT_ROTATION_BYTES = 5 * 1024 * 1024  # 5MB rotation threshold.
ACTIVE_FILENAME = "events.jsonl"
ROTATED_PREFIX = "events-"
ROTATED_SUFFIX = ".jsonl"


def _scrub_details(value: Any) -> Any:
    """Recursively scrub path-shaped strings out of a ``details`` payload.

    Walks dicts and lists, running every ``str`` *leaf* through
    :func:`scrub_value` (drops the account username / home path, keeps the
    basename). ``int``/``float``/``bool``/``None`` leaves are returned
    untouched so their JSON types survive a ``read_events`` round-trip — note
    ``bool`` is a subclass of ``int`` but neither matches the ``str`` branch,
    so both pass through unchanged. Mapping *keys* are code-defined field names
    (not user input), so they are left as-is.
    """

    if isinstance(value, Mapping):
        return {key: _scrub_details(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_details(item) for item in value]
    if isinstance(value, str):
        return scrub_value(value)
    return value


def _format_ts(moment: datetime) -> str:
    """ISO-8601 UTC with ``Z`` suffix — matches restore-point timestamps."""

    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rotated_stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")


class WearLedgerService:
    """Owns the wear-ledger directory and exposes append/read methods.

    Designed to be ``None``-tolerant from the caller side: services that
    receive a ``WearLedgerService | None`` constructor parameter should fall
    back to a no-op when the ledger isn't wired (tests, headless tools).
    """

    def __init__(
        self,
        *,
        base_dir: Path,
        utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        rotation_bytes: int = DEFAULT_ROTATION_BYTES,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._utc_now = utc_now
        # Floor at 256 bytes — small enough that tests can drive rotation
        # with single-event lines, large enough that an accidental zero or
        # negative ``rotation_bytes`` configuration can't thrash the disk
        # rotating after every write.
        self._rotation_bytes = max(256, int(rotation_bytes))
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Construction never raises — if the directory can't be created,
            # subsequent writes will fail and log, and reads return [].
            logger.exception(
                "WearLedgerService: failed to create base_dir %r", str(self._base_dir)
            )
        logger.info(
            "WearLedgerService constructed: service_id=%s base_dir=%r rotation_bytes=%d",
            id(self), str(self._base_dir), self._rotation_bytes,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def active_path(self) -> Path:
        return self._base_dir / ACTIVE_FILENAME

    @property
    def rotation_bytes(self) -> int:
        return self._rotation_bytes

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    def append(
        self,
        event_type: str,
        *,
        summary: str,
        details: Optional[Mapping[str, Any]] = None,
        ts: Optional[datetime] = None,
    ) -> Optional[WearLedgerEvent]:
        """Persist one event. Returns the event on success, ``None`` on failure.

        The call never raises. Caller is responsible for forming a localised
        ``summary`` string; ``details`` is an optional dict of typed payload
        for the expand-row view and for richer filtering downstream.
        """

        moment = ts if ts is not None else self._utc_now()
        normalized_details = dict(details) if details else {}
        # Scrub at the single write chokepoint so every caller is covered
        # (profile-apply names, restore-point titles, module IDs, service
        # notes, device_controller_name) without each having to remember to
        # sanitize. The raw events.jsonl must not bear the operator's home
        # path / account username — honouring the intent stated in models.py.
        # Already-scrubbed callers re-scrub harmlessly (idempotent); numeric /
        # bool / None details keep their types for the read_events round-trip.
        event = WearLedgerEvent(
            ts=_format_ts(moment),
            event_type=event_type,
            summary=scrub_paths(summary or ""),
            details=_scrub_details(normalized_details),
            schema_version=SCHEMA_VERSION,
        )
        try:
            self._write_line(event)
        except (OSError, ValueError, TypeError):
            logger.exception(
                "WearLedgerService.append failed: event_type=%r summary=%r",
                event_type, summary,
            )
            return None
        return event

    def _write_line(self, event: WearLedgerEvent) -> None:
        # Serialise first so a non-JSONable details value fails before we
        # touch the file. ``ensure_ascii=False`` keeps zh-CN summaries
        # readable in the on-disk file; ``sort_keys=True`` keeps the line
        # diff-friendly across two same-event writes for tests.
        line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        encoded = line.encode("utf-8")
        self._base_dir.mkdir(parents=True, exist_ok=True)
        active = self.active_path
        existing_size = 0
        try:
            existing_size = active.stat().st_size
        except FileNotFoundError:
            pass
        if existing_size > 0 and existing_size + len(encoded) > self._rotation_bytes:
            self._rotate()
        with open(active, "ab") as handle:
            handle.write(encoded)

    def _rotate(self) -> None:
        """Rename the active file to a stamped archive name.

        On Windows, ``os.replace`` is atomic for same-volume renames; a
        target collision (very-fast rotation within the same second) is
        resolved by appending a suffix counter.
        """

        active = self.active_path
        if not active.exists():
            return
        stamp = _rotated_stamp(self._utc_now())
        target = self._base_dir / f"{ROTATED_PREFIX}{stamp}{ROTATED_SUFFIX}"
        counter = 1
        while target.exists():
            target = self._base_dir / f"{ROTATED_PREFIX}{stamp}_{counter}{ROTATED_SUFFIX}"
            counter += 1
        try:
            os.replace(active, target)
            logger.info("WearLedgerService rotated: %s -> %s", active.name, target.name)
        except OSError:
            logger.exception(
                "WearLedgerService rotation failed: active=%r target=%r",
                str(active), str(target),
            )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read_events(
        self,
        *,
        event_types: Optional[Iterable[str]] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> list[WearLedgerEvent]:
        """Return matching events, most-recent first.

        ``event_types`` filters by exact ``event_type`` membership.
        ``since``/``until`` filter by timestamp (inclusive on both ends).
        ``limit`` caps the returned list size after filtering.
        """

        type_filter: Optional[frozenset[str]] = (
            frozenset(event_types) if event_types is not None else None
        )
        since_str = _format_ts(since) if since is not None else None
        until_str = _format_ts(until) if until is not None else None

        events: list[WearLedgerEvent] = []
        for path in self._all_event_files():
            try:
                with open(path, "rb") as handle:
                    for raw_line in handle:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            # Skip corrupted lines rather than aborting the
                            # entire read — partial recovery beats blank
                            # screen after a crash mid-write.
                            continue
                        try:
                            event = WearLedgerEvent.from_dict(payload)
                        except (ValueError, TypeError):
                            continue
                        if type_filter is not None and event.event_type not in type_filter:
                            continue
                        if since_str is not None and event.ts < since_str:
                            continue
                        if until_str is not None and event.ts > until_str:
                            continue
                        events.append(event)
            except OSError:
                logger.exception(
                    "WearLedgerService.read_events: failed to read %r", str(path)
                )

        events.sort(key=lambda e: e.ts, reverse=True)
        if limit is not None and limit >= 0:
            events = events[:limit]
        return events

    def _all_event_files(self) -> list[Path]:
        """Return active + rotated files in stable order (newest active last)."""

        if not self._base_dir.exists():
            return []
        files: list[Path] = []
        active = self.active_path
        if active.exists():
            files.append(active)
        # Rotated archives — sorted alphabetically, which equals
        # chronologically because the stamp is YYYYMMDD_HHMMSS.
        rotated = sorted(
            self._base_dir.glob(f"{ROTATED_PREFIX}*{ROTATED_SUFFIX}"),
            key=lambda p: p.name,
        )
        files.extend(rotated)
        return files

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def count_events(self) -> int:
        """Total events across all files. Best-effort; returns 0 on failure."""

        total = 0
        for path in self._all_event_files():
            try:
                with open(path, "rb") as handle:
                    for raw_line in handle:
                        if raw_line.strip():
                            total += 1
            except OSError:
                continue
        return total


__all__ = [
    "ACTIVE_FILENAME",
    "DEFAULT_ROTATION_BYTES",
    "ROTATED_PREFIX",
    "ROTATED_SUFFIX",
    "WearLedgerService",
]
