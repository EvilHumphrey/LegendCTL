"""ModulePassportService — owns the per-side passport directory.

Layout under ``base_dir`` (default ``<user_data_dir>/module_passport/``)::

    base_dir/
        left.json                                       active left passport
        right.json                                      active right passport
        archive/
            left_STOCK_BASELINE_LEFT_20260526T184211Z.json
            right_K_SILVER_RIGHT_20260601T032144Z.json

Writes are atomic temp-file-then-replace (mirrors the restore-point store).
The service is best-effort at the storage boundary: a disk failure logs at
exception level and returns ``None`` from the affected call. Construction
never raises — if the directory can't be created, subsequent reads return
``None`` and writes fail gracefully (the wear-ledger event still fires when
the service is provided, so the user-visible trail isn't broken).

Every write emits a ``wear_ledger`` event when a ledger is wired:

- :data:`zd_app.services.wear_ledger.models.MODULE_ASSIGNED` on
  :meth:`ModulePassportService.assign` — first or replacement.
- :data:`MODULE_CHARACTERIZED` on :meth:`append_fingerprint`.
- :data:`MODULE_NOTES_UPDATED` on :meth:`update_notes` (only when the
  sanitised text actually changed).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from zd_app.services.wear_ledger import WearLedgerService
from zd_app.services.wear_ledger.models import (
    MODULE_ASSIGNED,
    MODULE_CHARACTERIZED,
    MODULE_NOTES_UPDATED,
    MODULE_TREND_FLAGGED,
)
from zd_app.storage._import_guards import read_guarded_json
from zd_app.storage.module_passport_models import (
    SCHEMA_VERSION,
    SIDES,
    ModuleFingerprint,
    ModulePassport,
    module_passport_from_dict,
    sanitize_module_id,
    sanitize_module_notes,
)


logger = logging.getLogger(__name__)


ARCHIVE_DIRNAME = "archive"
TEMP_SUFFIX = ".tmp"
PASSPORT_SUFFIX = ".json"


# Read-size ceiling for a passport file. Unlike an untrusted *import*, a passport
# is an app-owned, append-only store: one fingerprint is appended per ~60s
# characterization run, so a heavy user's passport grows unboundedly over its
# lifetime and can legitimately pass the 1 MiB untrusted-import cap (~1,900
# fingerprints). Capping a trusted passport at the import limit makes get()
# return None for that user — silently dropping the side from the diagnostic
# bundle. 16 MiB (~30,000 fingerprints) keeps the guard against a corrupt/runaway
# file while never rejecting a real one. The depth guard still applies.
MAX_PASSPORT_BYTES = 16 * 1024 * 1024


# Archive-filename sanitiser: keep only filesystem-friendly characters,
# replace everything else with underscore. Cap the module_id segment so an
# operator pasting a long string doesn't blow MAX_PATH on Windows.
_ARCHIVE_MODULE_ID_CHARSET_RE = re.compile(r"[^A-Za-z0-9._-]+")
_ARCHIVE_MODULE_ID_CAP = 60


# Match a trailing archive stamp of the form ``_YYYYMMDDTHHMMSSZ`` optionally
# followed by ``_<n>`` collision counter. Used by list_archive_entries to pull
# the archived_at timestamp back out of the filename for UI display.
_ARCHIVE_STAMP_RE = re.compile(
    r"_(?P<stamp>\d{8}T\d{6}Z)(?:_\d+)?$"
)


def _archive_safe_module_id(module_id: str) -> str:
    cleaned = _ARCHIVE_MODULE_ID_CHARSET_RE.sub("_", module_id).strip("_") or "module"
    if len(cleaned) > _ARCHIVE_MODULE_ID_CAP:
        cleaned = cleaned[:_ARCHIVE_MODULE_ID_CAP]
    return cleaned


def _format_utc_now(now_fn: Callable[[], datetime]) -> str:
    return now_fn().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _archive_stamp(now_fn: Callable[[], datetime]) -> str:
    return now_fn().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_archived_at(stem: str) -> str:
    """Decode the trailing archive stamp on a filename stem back into ISO-8601.

    The archive filename pattern is ``<side>_<safe_id>_<YYYYMMDDTHHMMSSZ>``
    with an optional ``_<n>`` collision counter. Returns an empty string when
    the stem doesn't carry a stamp — the caller renders the entry but skips
    the archived-at column.
    """

    match = _ARCHIVE_STAMP_RE.search(stem)
    if match is None:
        return ""
    stamp = match.group("stamp")
    # Stamp is "YYYYMMDDTHHMMSSZ" — reformat to "YYYY-MM-DDTHH:MM:SSZ" so it
    # matches the assigned_at_utc shape that the UI already renders.
    return (
        f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T"
        f"{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}Z"
    )


class ModulePassportService:
    """Per-side passport reader/writer with optional wear-ledger emission.

    The service is ``None``-tolerant for the wear-ledger dependency so it
    can be constructed in tests without one; callers that don't pass a
    ledger see normal JSON I/O without lifecycle events being emitted.
    """

    def __init__(
        self,
        *,
        base_dir: Path,
        wear_ledger: Optional[WearLedgerService] = None,
        utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._base_dir = Path(base_dir)
        self._wear_ledger = wear_ledger
        self._utc_now = utc_now
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            (self._base_dir / ARCHIVE_DIRNAME).mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception(
                "ModulePassportService: failed to create base_dir %r",
                str(self._base_dir),
            )
        logger.info(
            "ModulePassportService constructed: service_id=%s base_dir=%r",
            id(self),
            str(self._base_dir),
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def archive_dir(self) -> Path:
        return self._base_dir / ARCHIVE_DIRNAME

    def path_for(self, side: str) -> Path:
        _ensure_side(side)
        return self._base_dir / f"{side}{PASSPORT_SUFFIX}"

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, side: str) -> Optional[ModulePassport]:
        """Read the active passport for ``side`` or ``None`` if none assigned."""

        _ensure_side(side)
        path = self.path_for(side)
        if not path.exists():
            return None
        try:
            # read_guarded_json caps size/depth and raises ValueError before
            # json.loads can recurse; a deep/oversize local or tampered passport
            # would otherwise raise RecursionError (escaping the old except). The
            # size cap is raised to MAX_PASSPORT_BYTES: a passport is trusted and
            # append-only, so it can outgrow the 1 MiB untrusted-import default.
            payload = read_guarded_json(path, max_bytes=MAX_PASSPORT_BYTES)
            return module_passport_from_dict(payload)
        except (OSError, ValueError, RecursionError, json.JSONDecodeError):
            logger.exception(
                "ModulePassportService.get: failed to load %r", str(path)
            )
            return None

    def list_archive(self, side: Optional[str] = None) -> list[ModulePassport]:
        """Return archived passports (optionally filtered to one side).

        Newest first, by ``assigned_at_utc`` descending. Used by the UI's
        detail view to show prior modules; unreadable files are skipped.
        """

        results: list[ModulePassport] = []
        if not self.archive_dir.exists():
            return results
        for path in sorted(self.archive_dir.glob(f"*{PASSPORT_SUFFIX}")):
            try:
                payload = read_guarded_json(path, max_bytes=MAX_PASSPORT_BYTES)
                passport = module_passport_from_dict(payload)
            except (OSError, ValueError, RecursionError, json.JSONDecodeError):
                logger.warning(
                    "ModulePassportService.list_archive: skipping unreadable %s",
                    path.name,
                )
                continue
            if side is not None and passport.side != side:
                continue
            results.append(passport)
        results.sort(key=lambda p: p.assigned_at_utc, reverse=True)
        return results

    def list_archive_entries(
        self, side: Optional[str] = None
    ) -> list[tuple[ModulePassport, str]]:
        """Return archived passports paired with the ``archived_at_utc``.

        The archived-at timestamp is parsed from the filename stem (the
        passport JSON itself doesn't carry it — it's only encoded into the
        archive filename when ``_archive_existing`` runs). Returns
        ``(passport, archived_at_utc)`` tuples in archived-at-descending
        order (so the most-recently-swapped module surfaces first). When
        the filename can't be parsed the entry still renders, with an empty
        archived_at_utc.
        """

        entries: list[tuple[ModulePassport, str]] = []
        if not self.archive_dir.exists():
            return entries
        for path in sorted(self.archive_dir.glob(f"*{PASSPORT_SUFFIX}")):
            try:
                payload = read_guarded_json(path, max_bytes=MAX_PASSPORT_BYTES)
                passport = module_passport_from_dict(payload)
            except (OSError, ValueError, RecursionError, json.JSONDecodeError):
                logger.warning(
                    "ModulePassportService.list_archive_entries: skipping unreadable %s",
                    path.name,
                )
                continue
            if side is not None and passport.side != side:
                continue
            archived_at = _parse_archived_at(path.stem)
            entries.append((passport, archived_at))
        # Newest archived first: parsed timestamp sorts lexicographically
        # since it's ISO-8601 UTC. Empty strings sink to the bottom.
        entries.sort(key=lambda item: item[1], reverse=True)
        return entries

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def assign(
        self,
        side: str,
        module_id: str,
        notes: str = "",
    ) -> Optional[ModulePassport]:
        """Create a fresh passport for ``side``, archiving any prior one.

        Returns the new passport on success. Returns ``None`` if the
        sanitised ``module_id`` is empty (we never write an unidentified
        passport) or if the on-disk write failed.
        """

        _ensure_side(side)
        cleaned_id = sanitize_module_id(module_id)
        cleaned_notes = sanitize_module_notes(notes)
        if not cleaned_id:
            logger.warning(
                "ModulePassportService.assign: empty module_id after sanitise (side=%s)",
                side,
            )
            return None

        existing = self.get(side)
        replaced_module_id: Optional[str] = None
        if existing is not None:
            replaced_module_id = existing.module_id
            self._archive_existing(existing)

        passport = ModulePassport(
            side=side,
            module_id=cleaned_id,
            assigned_at_utc=_format_utc_now(self._utc_now),
            notes=cleaned_notes,
            fingerprints=tuple(),
            schema_version=SCHEMA_VERSION,
        )
        if not self._persist(passport):
            return None
        self._emit_event(
            MODULE_ASSIGNED,
            summary=f"Module assigned ({side}): {cleaned_id}",
            details={
                "side": side,
                "module_id": cleaned_id,
                "replaced_module_id": replaced_module_id,
            },
        )
        return passport

    def append_fingerprint(
        self,
        side: str,
        fingerprint: ModuleFingerprint,
    ) -> Optional[ModulePassport]:
        """Append a fingerprint to the side's active passport.

        Returns the updated passport on success. Returns ``None`` if the
        side has no active passport yet, the fingerprint's ``side`` field
        disagrees with ``side``, or the write failed.

        Also emits a ``module_trend_flagged`` event when the new
        fingerprint flips the passport-level trend status FROM
        ``stable`` / ``insufficient_data`` TO ``drifting`` or
        ``investigate``. Improvement transitions (drifting → stable) do
        not emit — there's nothing the operator needs to be nudged about
        when things get better.
        """

        # Local import avoids a circular dependency at module import time:
        # trend_analysis imports characterize, which lives in the same
        # package; the service in turn needs trend_analysis only at call
        # time, so the dependency is local.
        from zd_app.services.module_passport.trend_analysis import (
            TREND_STATUS_DRIFTING,
            TREND_STATUS_INVESTIGATE,
            summarize_passport_trends,
        )

        _ensure_side(side)
        if fingerprint.side != side:
            logger.warning(
                "ModulePassportService.append_fingerprint: side mismatch "
                "(arg=%s fingerprint.side=%s)",
                side,
                fingerprint.side,
            )
            return None
        existing = self.get(side)
        if existing is None:
            logger.warning(
                "ModulePassportService.append_fingerprint: no active passport for side=%s",
                side,
            )
            return None
        # Snapshot the pre-append trend status BEFORE we mutate so the
        # delta-on-flip event can name the actual prior status.
        previous_summary = summarize_passport_trends(
            existing, now=self._utc_now()
        )
        updated = existing.with_fingerprint(fingerprint)
        if not self._persist(updated):
            return None
        self._emit_event(
            MODULE_CHARACTERIZED,
            summary=(
                f"Module characterized ({side}): {existing.module_id} "
                f"-> {fingerprint.overall_status}"
            ),
            details={
                "side": side,
                "module_id": existing.module_id,
                "overall_status": fingerprint.overall_status,
                "duration_ms": int(fingerprint.duration_ms),
                "samples_count": int(fingerprint.samples_count),
                "noise_floor_percent": float(fingerprint.noise_floor_percent),
                "circularity_coverage_percent": float(
                    fingerprint.circularity_coverage_percent
                ),
                "asymmetry_score": float(fingerprint.asymmetry_score),
                "bitness_observed": int(fingerprint.bitness_observed),
                "tremor_metric": float(fingerprint.tremor_metric),
                "linearity_score": float(fingerprint.linearity_score),
            },
        )
        new_summary = summarize_passport_trends(updated, now=self._utc_now())
        if (
            new_summary.status != previous_summary.status
            and new_summary.status
            in (TREND_STATUS_DRIFTING, TREND_STATUS_INVESTIGATE)
        ):
            self._emit_event(
                MODULE_TREND_FLAGGED,
                summary=(
                    f"Module trend flagged ({side}): {existing.module_id} "
                    f"-> {new_summary.status}"
                ),
                details={
                    "side": side,
                    "module_id": existing.module_id,
                    "previous_status": previous_summary.status,
                    "new_status": new_summary.status,
                    "attention_metrics": list(new_summary.attention_metrics),
                },
            )
        return updated

    def compute_passport_trend(self, side: str):
        """Return the per-side trend summary or ``None`` if not assigned.

        Pure read path — wraps :func:`summarize_passport_trends` so the
        screen layer only needs a service reference. Returns ``None``
        when no active passport exists for ``side``.
        """

        from zd_app.services.module_passport.trend_analysis import (
            summarize_passport_trends,
        )

        _ensure_side(side)
        passport = self.get(side)
        if passport is None:
            return None
        return summarize_passport_trends(passport, now=self._utc_now())

    def update_notes(self, side: str, notes: str) -> Optional[ModulePassport]:
        """Replace the notes field on the active passport for ``side``.

        Returns the updated passport. Returns ``None`` if no active
        passport exists. If the sanitised text matches the existing notes
        exactly, the write is skipped (and no wear-ledger event fires) so
        the ledger doesn't fill with no-op edits.
        """

        _ensure_side(side)
        existing = self.get(side)
        if existing is None:
            logger.warning(
                "ModulePassportService.update_notes: no active passport for side=%s",
                side,
            )
            return None
        cleaned_notes = sanitize_module_notes(notes)
        if cleaned_notes == existing.notes:
            return existing
        updated = existing.with_notes(cleaned_notes)
        if not self._persist(updated):
            return None
        self._emit_event(
            MODULE_NOTES_UPDATED,
            summary=f"Module notes updated ({side}): {existing.module_id}",
            details={
                "side": side,
                "module_id": existing.module_id,
                "notes_length": len(cleaned_notes),
            },
        )
        return updated

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _persist(self, passport: ModulePassport) -> bool:
        """Atomic write of one passport. Returns True on success."""

        path = self.path_for(passport.side)
        temp_path = path.with_suffix(path.suffix + TEMP_SUFFIX)
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                passport.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            with open(temp_path, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    logger.debug(
                        "ModulePassportService: fsync unavailable for %s",
                        temp_path,
                        exc_info=True,
                    )
            os.replace(temp_path, path)
            return True
        except (OSError, ValueError, TypeError):
            logger.exception(
                "ModulePassportService: failed to persist passport (side=%s)",
                passport.side,
            )
            # Clean up partial temp file when possible.
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
            return False

    def _archive_existing(self, passport: ModulePassport) -> None:
        # Defense-in-depth: ``side`` and ``module_id`` both flow into the archive
        # filename. ``module_id`` is sanitised by ``_archive_safe_module_id``;
        # validate ``side`` here too (the load boundary already rejects a bad
        # side, so this never fires for a real passport) before it builds a path.
        _ensure_side(passport.side)
        try:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception(
                "ModulePassportService: failed to create archive dir %r",
                str(self.archive_dir),
            )
            return
        stamp = _archive_stamp(self._utc_now)
        safe_id = _archive_safe_module_id(passport.module_id)
        target = self.archive_dir / (
            f"{passport.side}_{safe_id}_{stamp}{PASSPORT_SUFFIX}"
        )
        counter = 1
        while target.exists():
            target = self.archive_dir / (
                f"{passport.side}_{safe_id}_{stamp}_{counter}{PASSPORT_SUFFIX}"
            )
            counter += 1
        # Final containment guard: the resolved target must stay directly inside
        # the archive dir. Unreachable given the validated side + sanitised id
        # above, but turns any future sanitisation gap into a refusal rather than
        # an out-of-tree write.
        if target.resolve().parent != self.archive_dir.resolve():
            logger.error(
                "ModulePassportService: refusing archive write outside archive dir (target=%r)",
                str(target),
            )
            return
        try:
            payload = json.dumps(
                passport.to_dict(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            target.write_text(payload, encoding="utf-8")
        except OSError:
            logger.exception(
                "ModulePassportService: failed to archive passport (side=%s, target=%r)",
                passport.side,
                str(target),
            )

    def _emit_event(
        self,
        event_type: str,
        *,
        summary: str,
        details: dict,
    ) -> None:
        if self._wear_ledger is None:
            return
        try:
            self._wear_ledger.append(event_type, summary=summary, details=details)
        except Exception:  # noqa: BLE001 — wear ledger never crashes producer
            logger.exception(
                "ModulePassportService: wear_ledger.append failed (event_type=%s)",
                event_type,
            )


def _ensure_side(side: str) -> None:
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}; got {side!r}")


__all__ = [
    "ARCHIVE_DIRNAME",
    "ModulePassportService",
]
