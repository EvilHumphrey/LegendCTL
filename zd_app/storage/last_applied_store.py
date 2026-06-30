"""Single-record store for the last wrapper-profile apply (Phase 2).

Records what the wrapper last *sent* to the controller — the snapshot exactly
as :meth:`AppShell._apply_snapshot_to_controller` received it (post any
device-field filtering), plus the apply outcome's failed-field labels — so the
Device-vs-Profile screen can answer *"has my controller drifted since I applied
last week?"* across app restarts.

Honesty notes (mirrors the apply pipeline's claims, never exceeds them):

* The record is written by the **named-profile apply** and **Safe Import
  apply** paths only. Restore Points and per-field writes deliberately do NOT
  record — post-apply drift (including drift caused by a restore) is exactly
  what the Last-Applied column exists to show.
* ``failed_fields`` carries the apply coordinator's ``setting_label`` tokens
  (``"vibration"``, ``"sens_left"``, ``"binding_A"``, …) verbatim: a field in
  this list was *attempted but not ACKed*, so its recorded value must never be
  presented as on-device truth.
* Storage is **best-effort** at the call sites: a write failure here must never
  affect an apply result (see ``AppShell._record_last_applied_safe``).

On-disk layout: one ``last_applied.json`` in the user-data root (a sibling of
``settings.json`` / ``wrapper_profiles/`` — deliberately NOT inside
``wrapper_profiles/`` where the ``*.json`` profile glob would disclose it as a
skipped file). Atomic temp-then-replace writes mirroring
:meth:`RestorePointStore.save` / :meth:`WrapperProfileStore.save`; a corrupt or
missing file loads as ``None`` with a logged warning, never a crash and never a
disclosure card.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from zd_app.services.settings_service import ControllerSnapshot
from zd_app.storage._import_guards import read_guarded_json
from zd_app.storage.settings_store import initialize_user_data_dir
from zd_app.storage.snapshot_codec import snapshot_from_dict, snapshot_to_dict


logger = logging.getLogger(__name__)


SCHEMA_VERSION = 1
FILENAME = "last_applied.json"
TEMP_SUFFIX = ".tmp"


def utc_now_iso_z() -> str:
    """ISO-8601 UTC with ``Z`` suffix — same shape the Restore Points rows show."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class LastAppliedRecord:
    """One apply event: who, when, and exactly what was sent."""

    profile_name: str
    applied_at: str  # UTC ISO-8601 ``Z`` (see utc_now_iso_z)
    include_device: bool  # device-global fields (polling/step) were part of it
    failed_fields: tuple[str, ...]  # coordinator setting_labels that did not ACK
    snapshot: ControllerSnapshot  # as-applied (post device-field filtering)


def record_to_dict(record: LastAppliedRecord) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_name": record.profile_name,
        "applied_at": record.applied_at,
        "include_device": record.include_device,
        "failed_fields": list(record.failed_fields),
        "snapshot": snapshot_to_dict(record.snapshot),
    }


def record_from_dict(payload: dict) -> LastAppliedRecord:
    """Parse a stored record; raises ``ValueError`` on any shape problem.

    Strict-ish on purpose: the caller (:meth:`LastAppliedStore.load`) maps any
    failure to ``None`` + a warning, so a half-believable file never produces a
    half-believable record.
    """

    if not isinstance(payload, dict):
        raise ValueError(f"last-applied record must be a JSON object, got {type(payload).__name__}")
    profile_name = payload.get("profile_name")
    applied_at = payload.get("applied_at")
    if not isinstance(profile_name, str) or not profile_name:
        raise ValueError("last-applied record has no usable profile_name")
    if not isinstance(applied_at, str) or not applied_at:
        raise ValueError("last-applied record has no usable applied_at")
    failed_raw = payload.get("failed_fields", [])
    if not isinstance(failed_raw, list) or not all(isinstance(f, str) for f in failed_raw):
        raise ValueError("last-applied record failed_fields must be a list of strings")
    snapshot_payload = payload.get("snapshot")
    if not isinstance(snapshot_payload, dict):
        raise ValueError("last-applied record has no snapshot object")
    return LastAppliedRecord(
        profile_name=profile_name,
        applied_at=applied_at,
        include_device=bool(payload.get("include_device", False)),
        failed_fields=tuple(failed_raw),
        snapshot=snapshot_from_dict(snapshot_payload),
    )


class LastAppliedStore:
    """File-backed single-record store (``<user_data_dir>/last_applied.json``)."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        if base_dir is None:
            self.base_dir = initialize_user_data_dir()
        else:
            self.base_dir = Path(base_dir)

    @property
    def path(self) -> Path:
        return self.base_dir / FILENAME

    def save(self, record: LastAppliedRecord) -> Path:
        """Persist (overwrite) the record with an atomic temp-then-replace.

        Mirrors ``RestorePointStore.save``: write to ``last_applied.json.tmp``
        (invisible to every ``*.json`` glob), flush+fsync, then
        ``Path.replace()`` — a crash mid-write leaves either the previous
        record or a ``.tmp`` straggler, never a half-written file.
        """

        self.base_dir.mkdir(parents=True, exist_ok=True)
        final_path = self.path
        temp_path = final_path.with_suffix(final_path.suffix + TEMP_SUFFIX)
        payload = json.dumps(record_to_dict(record), indent=2)
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

    def load(self) -> LastAppliedRecord | None:
        """Return the stored record, or ``None`` when there isn't a usable one.

        Missing file → ``None`` silently (the normal first-run state).
        Unreadable / corrupt / wrong-shape file → ``None`` + a logged warning —
        never a raise, never a UI disclosure (the screen just renders its
        "no apply recorded" hint).
        """

        path = self.path
        try:
            payload = read_guarded_json(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("could not read last-applied record %s: %s", path, exc)
            return None
        except (ValueError, RecursionError, json.JSONDecodeError) as exc:
            logger.warning("corrupt last-applied record %s: %s", path, exc)
            return None
        try:
            return record_from_dict(payload)
        except ValueError as exc:
            logger.warning("corrupt last-applied record %s: %s", path, exc)
            return None


__all__ = [
    "FILENAME",
    "SCHEMA_VERSION",
    "LastAppliedRecord",
    "LastAppliedStore",
    "record_from_dict",
    "record_to_dict",
    "utc_now_iso_z",
]
