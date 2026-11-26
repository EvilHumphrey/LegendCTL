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
import re
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from zd_app.services.path_scrub import scrub_paths, scrub_value
from zd_app.services.wear_ledger.models import (
    SCHEMA_VERSION,
    WearLedgerEvent,
)
from zd_app.storage._import_guards import list_guarded_directory
from zd_app.storage._output_guards import (
    GuardedOutputDirectory,
    UnsafeOutputPathError,
    guarded_output_directory,
)


logger = logging.getLogger(__name__)


DEFAULT_ROTATION_BYTES = 5 * 1024 * 1024  # 5MB rotation threshold.
DEFAULT_MAX_ROTATED_ARCHIVES = 10
MAX_READ_LINE_BYTES = 64 * 1024
# 64 MiB covers the full active + 10 rotated archive retention window (~55 MB)
# with headroom while still capping memory against corrupted files.
MAX_READ_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_LEDGER_DIRECTORY_ENTRIES = 1024
ACTIVE_FILENAME = "events.jsonl"
ROTATED_PREFIX = "events-"
ROTATED_SUFFIX = ".jsonl"
_ROTATED_ARCHIVE_RE = re.compile(r"^events-(\d{8}_\d{6})(?:_(\d+))?\.jsonl$")


def _archive_sort_key(name: str) -> tuple[str, int, str]:
    match = _ROTATED_ARCHIVE_RE.fullmatch(name)
    if match is None:
        return ("", -1, name)
    stamp = match.group(1)
    suffix = int(match.group(2)) if match.group(2) is not None else 0
    return (stamp, suffix, name)


def _is_ordinary_regular_file(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    if getattr(info, "st_file_attributes", 0) & 0x400:
        return False
    return True


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
        max_rotated_archives: int = DEFAULT_MAX_ROTATED_ARCHIVES,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._utc_now = utc_now
        # Floor at 256 bytes — small enough that tests can drive rotation
        # with single-event lines, large enough that an accidental zero or
        # negative ``rotation_bytes`` configuration can't thrash the disk
        # rotating after every write.
        self._rotation_bytes = max(256, int(rotation_bytes))
        self._max_rotated_archives = max(1, int(max_rotated_archives))
        self._last_stamp_counter: dict[str, int] = {}
        # This is deliberately process-local. LegendCTL is a single-window GUI
        # app, so one process serializes normal ledger writes. Separate GUI
        # processes could still race a rotation; that is an accepted best-effort
        # limitation for this non-authoritative wear log.
        self._write_lock = threading.Lock()
        try:
            with guarded_output_directory(
                self._base_dir, self._base_dir, allow_fallback=False
            ):
                pass
        except (OSError, ValueError):
            # Construction never raises — if the directory can't be created,
            # subsequent writes will fail and log, and reads return [].
            logger.exception(
                "WearLedgerService: failed to create base_dir %s", self._base_dir
            )
        logger.debug(
            "WearLedgerService ready: base_dir=%s rotation_bytes=%d max_archives=%d",
            self._base_dir,
            self._rotation_bytes,
            self._max_rotated_archives,
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
            # Both fields can contain operator-authored text (service notes,
            # profile names, module identifiers). Keep failure telemetry
            # structural: exception type and traceback are useful, while
            # echoing either rejected payload would copy private text into the
            # rotating support log.
            logger.exception("WearLedgerService.append failed")
            return None
        return event

    def _write_line(self, event: WearLedgerEvent) -> None:
        # Serialise first so a non-JSONable details value fails before we
        # touch the file. ``ensure_ascii=False`` keeps zh-CN summaries
        # readable in the on-disk file; ``sort_keys=True`` keeps the line
        # diff-friendly across two same-event writes for tests.
        line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        encoded = line.encode("utf-8")
        # The size check, possible rename, and append form one transaction
        # relative to concurrent UI and worker writers. In particular, a
        # second writer must not append through a handle selected before the
        # first writer rotated the active file.
        with self._write_lock:
            with guarded_output_directory(
                self._base_dir,
                self._base_dir,
                allow_fallback=False,
                allow_rename=True,
            ) as output:
                existing_size = 0
                try:
                    with output.open_binary_read(ACTIVE_FILENAME) as existing:
                        existing_size = os.fstat(existing.fileno()).st_size
                except FileNotFoundError:
                    existing_size = 0
                if existing_size > 0 and existing_size + len(encoded) > self._rotation_bytes:
                    self._rotate(output)
                with output.open_binary_append(ACTIVE_FILENAME) as handle:
                    handle.write(encoded)

    def _rotate(self, output: GuardedOutputDirectory) -> None:
        """Atomically rename the active file to a stamped archive and prune old ones.

        Call only while ``_write_lock`` is held by :meth:`_write_line` and
        ``output`` is an active pinned directory with ``allow_rename=True``.
        Same-second rotations monotonically advance the numeric suffix so
        pruning older archives can never cause a newer archive to reuse a
        lower-sorting name and be selected for deletion ahead of older ones.
        """

        stamp = _rotated_stamp(self._utc_now())
        existing_max = self._last_stamp_counter.get(stamp, -1)
        try:
            entries = list_guarded_directory(
                output.path, max_entries=_MAX_LEDGER_DIRECTORY_ENTRIES
            )
        except (OSError, ValueError):
            entries = []
        for entry in entries:
            m = _ROTATED_ARCHIVE_RE.fullmatch(entry.name)
            if m is not None and m.group(1) == stamp:
                num = int(m.group(2)) if m.group(2) is not None else 0
                if num > existing_max:
                    existing_max = num
        next_counter = existing_max + 1
        while True:
            if next_counter == 0:
                target_name = f"{ROTATED_PREFIX}{stamp}{ROTATED_SUFFIX}"
            else:
                target_name = f"{ROTATED_PREFIX}{stamp}_{next_counter}{ROTATED_SUFFIX}"
            try:
                os.lstat(output.path / target_name)
                next_counter += 1
                continue
            except FileNotFoundError:
                break
        try:
            output.rename_noreplace(ACTIVE_FILENAME, target_name)
            self._last_stamp_counter[stamp] = next_counter
            if len(self._last_stamp_counter) > 16:
                oldest = sorted(self._last_stamp_counter.keys())[0]
                if oldest != stamp:
                    self._last_stamp_counter.pop(oldest, None)
            logger.info("WearLedgerService rotated: %s -> %s", ACTIVE_FILENAME, target_name)
            self._prune_rotated_archives(output)
        except (OSError, ValueError):
            logger.exception(
                "WearLedgerService rotation failed: active=%s target=%s",
                ACTIVE_FILENAME,
                target_name,
            )

    def _prune_rotated_archives(self, output: GuardedOutputDirectory) -> None:
        try:
            entries = list_guarded_directory(
                output.path, max_entries=_MAX_LEDGER_DIRECTORY_ENTRIES
            )
        except (OSError, ValueError):
            return
        rotated = sorted(
            (
                entry.name
                for entry in entries
                if entry.name.startswith(ROTATED_PREFIX)
                and entry.name.endswith(ROTATED_SUFFIX)
            ),
            key=_archive_sort_key,
        )
        excess = len(rotated) - self._max_rotated_archives
        if excess <= 0:
            return
        for stale_name in rotated[:excess]:
            try:
                output.unlink_file(stale_name)
            except (OSError, ValueError):
                logger.warning("WearLedgerService: could not prune archive %s", stale_name)

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
        Reads newest files first through pinned no-reparse single-link file
        handles with per-line and total byte caps.
        """

        type_filter: Optional[frozenset[str]] = (
            frozenset(event_types) if event_types is not None else None
        )
        since_str = _format_ts(since) if since is not None else None
        until_str = _format_ts(until) if until is not None else None

        if not self._base_dir.exists():
            return []

        events: list[WearLedgerEvent] = []
        bytes_read = 0
        try:
            with guarded_output_directory(
                self._base_dir, self._base_dir, allow_fallback=False
            ) as output:
                for path in self._all_event_files():
                    if bytes_read >= MAX_READ_TOTAL_BYTES:
                        break
                    try:
                        with output.open_binary_read(path.name) as handle:
                            while bytes_read < MAX_READ_TOTAL_BYTES:
                                raw_line = handle.readline(MAX_READ_LINE_BYTES + 1)
                                if not raw_line:
                                    break
                                bytes_read += len(raw_line)
                                if len(raw_line) > MAX_READ_LINE_BYTES and not raw_line.endswith(b"\n"):
                                    while bytes_read < MAX_READ_TOTAL_BYTES:
                                        chunk = handle.readline(MAX_READ_LINE_BYTES)
                                        if not chunk:
                                            break
                                        bytes_read += len(chunk)
                                        if chunk.endswith(b"\n"):
                                            break
                                    continue
                                line = raw_line.decode("utf-8", errors="replace").strip()
                                if not line:
                                    continue
                                try:
                                    payload = json.loads(line)
                                except (json.JSONDecodeError, RecursionError):
                                    continue
                                try:
                                    event = WearLedgerEvent.from_dict(payload)
                                except (ValueError, TypeError, AttributeError):
                                    continue
                                if type_filter is not None and event.event_type not in type_filter:
                                    continue
                                if since_str is not None and event.ts < since_str:
                                    continue
                                if until_str is not None and event.ts > until_str:
                                    continue
                                events.append(event)
                    except (OSError, ValueError):
                        logger.exception(
                            "WearLedgerService.read_events: failed to read %s", path.name
                        )
        except (OSError, ValueError):
            return []

        events.sort(key=lambda e: e.ts, reverse=True)
        if limit is not None and limit >= 0:
            events = events[:limit]
        return events

    def _all_event_files(self) -> list[Path]:
        """Return active + rotated files newest-first (active first, archives desc)."""

        try:
            entries = list_guarded_directory(
                self._base_dir, max_entries=_MAX_LEDGER_DIRECTORY_ENTRIES
            )
        except (OSError, ValueError):
            return []
        files: list[Path] = []
        rotated: list[Path] = []
        for entry in entries:
            if not _is_ordinary_regular_file(entry):
                continue
            if entry.name == ACTIVE_FILENAME:
                files.append(entry)
            elif entry.name.startswith(ROTATED_PREFIX) and entry.name.endswith(ROTATED_SUFFIX):
                rotated.append(entry)
        rotated.sort(key=lambda p: _archive_sort_key(p.name), reverse=True)
        files.extend(rotated)
        return files

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def count_events(self) -> int:
        """Total events across all files (bounded guarded read). Returns 0 on failure."""

        if not self._base_dir.exists():
            return 0
        total = 0
        bytes_read = 0
        try:
            with guarded_output_directory(
                self._base_dir, self._base_dir, allow_fallback=False
            ) as output:
                for path in self._all_event_files():
                    if bytes_read >= MAX_READ_TOTAL_BYTES:
                        break
                    try:
                        with output.open_binary_read(path.name) as handle:
                            while bytes_read < MAX_READ_TOTAL_BYTES:
                                raw_line = handle.readline(MAX_READ_LINE_BYTES + 1)
                                if not raw_line:
                                    break
                                bytes_read += len(raw_line)
                                if len(raw_line) > MAX_READ_LINE_BYTES and not raw_line.endswith(b"\n"):
                                    while bytes_read < MAX_READ_TOTAL_BYTES:
                                        chunk = handle.readline(MAX_READ_LINE_BYTES)
                                        if not chunk:
                                            break
                                        bytes_read += len(chunk)
                                        if chunk.endswith(b"\n"):
                                            break
                                    continue
                                if raw_line.strip():
                                    total += 1
                    except (OSError, ValueError):
                        continue
        except (OSError, ValueError):
            return 0
        return total


__all__ = [
    "ACTIVE_FILENAME",
    "DEFAULT_MAX_ROTATED_ARCHIVES",
    "DEFAULT_ROTATION_BYTES",
    "MAX_READ_LINE_BYTES",
    "MAX_READ_TOTAL_BYTES",
    "ROTATED_PREFIX",
    "ROTATED_SUFFIX",
    "WearLedgerService",
]
