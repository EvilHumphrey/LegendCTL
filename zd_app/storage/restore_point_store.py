"""File-per-snapshot restore-point store with atomic writes + retention.

The "Storage model" rule mandates a separate store from
:class:`~zd_app.storage.wrapper_profile_store.WrapperProfileStore` — restore
points are audit/recovery artifacts, not user-authored profile tuning data,
and mixing them pollutes the profile list. This module owns:

- the on-disk layout (``<user_data_dir>/restore_points/<filename>.json``);
- atomic write via temp-file-then-replace (stronger guarantee than
  ``WrapperProfileStore.save`` because recovery artifacts deserve it);
- low-panic listing (parse errors don't crash; they surface as
  :class:`~zd_app.storage.restore_point_models.SkippedFile`);
- retention with the §"Retention" rules: oldest-auto-created first, keep
  newest ``first_readable_connect`` per device identity, never prune the
  ``protect`` set, enforce both ``max_count`` and ``max_disk_mb``.

UI is out of scope (restore-point UI-integration follow-up work).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Iterable, Optional

from zd_app.storage._import_guards import read_guarded_json
from zd_app.storage.restore_point_models import (
    KIND,
    RestorePoint,
    RestorePointParseError,
    RestorePointSchemaError,
    SkippedFile,
    restore_point_from_dict,
    restore_point_to_dict,
)
from zd_app.storage.settings_store import initialize_user_data_dir


logger = logging.getLogger(__name__)


DEFAULT_MAX_COUNT = 50
DEFAULT_MAX_DISK_MB = 25
FILENAME_PATTERN = re.compile(
    r"^(?P<ts>\d{8}-\d{6})_(?P<trigger>[a-z0-9_]+)_(?P<suffix>[0-9a-f]{6})\.json$"
)
TEMP_SUFFIX = ".tmp"
_TRIGGER_SANITIZE_RE = re.compile(r"[^a-z0-9_]+")


class RestorePointStoreError(Exception):
    """Raised by :class:`RestorePointStore` for IO + validation failures."""


def filename_for(rp: RestorePoint) -> str:
    """Compute the canonical filename for a restore point.

    Pattern: ``YYYYMMDD-HHMMSS_<trigger>_<6char>.json`` per the
    "Filename / identity" rule. The 6-character suffix is the last 6 chars of
    the id's hex tail; the id is expected to look like
    ``rp_YYYYMMDD_HHMMSS_<6char>``.
    """

    ts = rp.created_at
    ts_compact = (
        ts.replace("-", "").replace(":", "").replace("T", "-").rstrip("Z")[:15]
    )
    trigger_token = _sanitize_trigger(rp.trigger.type)
    suffix = _short_suffix_from_id(rp.id)
    return f"{ts_compact}_{trigger_token}_{suffix}.json"


class RestorePointStore:
    """File-per-record restore-point store.

    ``base_dir`` defaults to ``<user_data_dir>/restore_points/`` (a sibling of
    ``wrapper_profiles/`` and ``diagnostics/``) so the wrapper's existing
    user-data root is reused without changes.
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        if base_dir is None:
            self.base_dir = initialize_user_data_dir() / "restore_points"
        else:
            self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # Construction-identity logging while we investigate the load-by-id
        # bug behind the "service unavailable" / "not found" wrapper
        # symptom. The id + base_dir tuple lets us correlate _safe_load
        # invocations against which store instance they hit.
        logger.info(
            "RestorePointStore constructed: store_id=%s base_dir=%r",
            id(self), str(self.base_dir),
        )

    # -- write --------------------------------------------------------------

    def save(self, rp: RestorePoint) -> Path:
        """Persist a restore point with an atomic temp-file-then-replace.

        Per the "Atomic write and corruption behavior" rule — write to
        ``<filename>.tmp``, flush+close, then ``Path.replace()`` so a crash
        mid-write leaves either the previous file or a ``.tmp`` straggler
        (filtered out by :meth:`list`), never a half-written final file.
        """

        filename = filename_for(rp)
        final_path = self._contained(filename)
        temp_path = final_path.with_suffix(final_path.suffix + TEMP_SUFFIX)
        payload = json.dumps(restore_point_to_dict(rp), indent=2)

        # Open/write/flush/fsync the temp before the replace so the final
        # path swap exposes only fully-written bytes.
        with open(temp_path, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                # Best-effort: fsync can be unavailable on some filesystems
                # (e.g. Windows network shares). Replace below still happens.
                logger.debug("fsync unavailable for %s", temp_path, exc_info=True)
        temp_path.replace(final_path)
        return final_path

    # -- read ---------------------------------------------------------------

    def list(self) -> tuple[list[RestorePoint], list[SkippedFile]]:
        """Return ``(valid, skipped)`` — valid restore points newest-first;
        unreadable / wrong-kind / corrupt files in ``skipped`` for low-panic
        UI disclosure per the "Atomic write and corruption behavior" rule.
        """

        valid: list[RestorePoint] = []
        skipped: list[SkippedFile] = []
        for path in self.base_dir.glob("*.json"):
            try:
                # read_guarded_json caps size/depth and raises ValueError before
                # json.loads can recurse — a deep/oversize local or tampered file
                # would otherwise raise RecursionError, which escaped the old
                # except and crashed the whole listing (defeating low-panic
                # skip-on-bad-file). ValueError/RecursionError are caught below.
                payload = read_guarded_json(path)
                rp = restore_point_from_dict(payload)
            except FileNotFoundError:
                # Vanished between glob and read: a worker-thread prune /
                # delete won the race (captures run as HID jobs now). Not a
                # corrupt-file disclosure — skip silently.
                logger.debug(
                    "restore point %s vanished during list; skipping", path.name
                )
                continue
            except (
                OSError,
                ValueError,
                RecursionError,
                json.JSONDecodeError,
                RestorePointParseError,
            ) as exc:
                logger.warning("could not load restore point %s: %s", path.name, exc)
                skipped.append(SkippedFile(path=str(path), error=str(exc)))
                continue
            valid.append(rp)
        valid.sort(key=lambda rp: rp.created_at, reverse=True)
        return valid, skipped

    def load(self, rp_id: str) -> RestorePoint:
        """Look up a restore point by its ``id`` field.

        Raises :class:`FileNotFoundError` if no file with this id exists, and
        re-raises :class:`RestorePointParseError` /
        :class:`RestorePointSchemaError` for unreadable files.
        """

        for path in self.base_dir.glob("*.json"):
            try:
                payload = read_guarded_json(path)
            except (OSError, ValueError, RecursionError, json.JSONDecodeError) as exc:
                logger.debug("skipping unreadable file %s while searching: %s", path, exc)
                continue
            if payload.get("id") != rp_id:
                continue
            return restore_point_from_dict(payload)
        raise FileNotFoundError(f"restore point not found: {rp_id!r}")

    # -- delete + prune -----------------------------------------------------

    def delete(self, rp_id: str) -> bool:
        """Remove a restore point by id. Returns True if a file was removed."""

        for path in self.base_dir.glob("*.json"):
            try:
                payload = read_guarded_json(path)
            except (OSError, ValueError, RecursionError, json.JSONDecodeError):
                continue
            if payload.get("id") == rp_id:
                path.unlink()
                return True
        return False

    def prune(
        self,
        *,
        max_count: int = DEFAULT_MAX_COUNT,
        max_disk_mb: int = DEFAULT_MAX_DISK_MB,
        protect: Iterable[str] = (),
    ) -> list[str]:
        """Apply the "Retention" rules. Returns ids of pruned files.

        Order:
        1. Always keep ids in ``protect``.
        2. Always keep the newest ``first_readable_connect`` per device
           ``product_string``.
        3. Prefer pruning auto-created snapshots (trigger.type != ``manual``)
           oldest-first to satisfy ``max_count``.
        4. If ``max_disk_mb`` is still exceeded, allow pruning manual
           snapshots oldest-first.
        """

        protect_set = set(protect)
        valid, _ = self.list()
        # Map id -> path so we can unlink without re-glob'ing.
        id_to_path: dict[str, Path] = {}
        for path in self.base_dir.glob("*.json"):
            try:
                payload = read_guarded_json(path)
            except (OSError, ValueError, RecursionError, json.JSONDecodeError):
                continue
            payload_id = payload.get("id")
            if isinstance(payload_id, str):
                id_to_path[payload_id] = path

        # Compute "newest first_readable_connect per device" protection.
        baseline_protect: set[str] = set()
        baseline_seen: dict[str, str] = {}   # product_string -> rp.created_at
        for rp in valid:
            if rp.trigger.type != "first_readable_connect":
                continue
            key = rp.device_identity.product_string or "__unknown__"
            prior = baseline_seen.get(key)
            if prior is None or rp.created_at > prior:
                baseline_seen[key] = rp.created_at
        for rp in valid:
            if rp.trigger.type != "first_readable_connect":
                continue
            key = rp.device_identity.product_string or "__unknown__"
            if baseline_seen.get(key) == rp.created_at:
                baseline_protect.add(rp.id)

        full_protect = protect_set | baseline_protect

        # Auto-created candidates, oldest-first. Manual snapshots are pruned
        # only by disk cap below.
        auto_oldest_first: list[RestorePoint] = sorted(
            (rp for rp in valid if rp.trigger.type != "manual"),
            key=lambda rp: rp.created_at,
        )
        manual_oldest_first: list[RestorePoint] = sorted(
            (rp for rp in valid if rp.trigger.type == "manual"),
            key=lambda rp: rp.created_at,
        )

        pruned: list[str] = []
        remaining = list(valid)

        def _prune_one(rp: RestorePoint) -> None:
            path = id_to_path.get(rp.id)
            if path is None or not path.exists():
                return
            path.unlink()
            pruned.append(rp.id)
            remaining.remove(rp)

        # Phase 1: max_count via auto-oldest-first.
        for rp in auto_oldest_first:
            if len(remaining) <= max_count:
                break
            if rp.id in full_protect:
                continue
            _prune_one(rp)

        # Phase 2: max_disk_mb. Drop more auto first, then manual.
        budget_bytes = max_disk_mb * 1024 * 1024
        def _current_bytes() -> int:
            total = 0
            for rp in remaining:
                path = id_to_path.get(rp.id)
                if path is not None and path.exists():
                    try:
                        total += path.stat().st_size
                    except OSError:
                        continue
            return total

        auto_remaining_oldest_first = [rp for rp in auto_oldest_first if rp in remaining]
        for rp in auto_remaining_oldest_first:
            if _current_bytes() <= budget_bytes:
                break
            if rp.id in full_protect:
                continue
            _prune_one(rp)
        for rp in manual_oldest_first:
            if _current_bytes() <= budget_bytes:
                break
            if rp.id in full_protect:
                continue
            logger.warning(
                "pruning manual restore point %s due to %d MB disk cap",
                rp.id,
                max_disk_mb,
            )
            _prune_one(rp)

        return pruned

    # -- helpers ------------------------------------------------------------

    def _contained(self, filename: str) -> Path:
        path = self.base_dir / filename
        if path.resolve().parent != self.base_dir.resolve():
            raise RestorePointStoreError(
                f"refusing restore-point path outside base dir: {filename!r}"
            )
        return path


def _sanitize_trigger(token: object) -> str:
    """Reduce a free-form trigger string to filesystem-safe lowercase."""

    if not isinstance(token, str):
        return "unknown"
    lowered = token.strip().lower()
    cleaned = _TRIGGER_SANITIZE_RE.sub("_", lowered).strip("_")
    return cleaned or "unknown"


def _short_suffix_from_id(rp_id: object) -> str:
    """Pull a 6-char hex suffix off the id; fall back to ``000000``."""

    if not isinstance(rp_id, str):
        return "000000"
    tail = rp_id.rsplit("_", 1)[-1]
    if re.fullmatch(r"[0-9a-fA-F]{6}", tail):
        return tail.lower()
    sanitized = re.sub(r"[^0-9a-f]", "0", tail.lower())[-6:].rjust(6, "0")
    return sanitized


__all__ = [
    "DEFAULT_MAX_COUNT",
    "DEFAULT_MAX_DISK_MB",
    "FILENAME_PATTERN",
    "RestorePointStore",
    "RestorePointStoreError",
    "filename_for",
]
