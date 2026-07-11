"""App settings persistence."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path

from zd_app.models import AppSettings
from zd_app.storage._import_guards import read_guarded_json
from zd_app.version import __app_id__


logger = logging.getLogger(__name__)

SETUP_DRAWER_DISMISSED_KEY = "setup_drawer_dismissed"
MIGRATION_STATE_FILENAME = ".zd_migration_state"
MIGRATION_STATE_STARTED = "started"
MIGRATION_STATE_COMPLETE = "complete"
MIGRATION_TEMP_SUFFIX = ".zdmig.tmp"


def _default_user_data_dir() -> Path:
    """Return the default writable data directory for this run mode."""

    override = os.environ.get("ZDUL_DATA_DIR")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / __app_id__
    return Path("zd_data")


def initialize_user_data_dir() -> Path:
    """Ensure the data directory exists and migrate adjacent frozen data once."""

    target = _default_user_data_dir()
    target.mkdir(parents=True, exist_ok=True)
    if not getattr(sys, "frozen", False):
        return target

    legacy = Path(sys.executable).parent / "zd_data"
    marker_path = target / MIGRATION_STATE_FILENAME
    state = _read_migration_state(marker_path)
    if state == MIGRATION_STATE_COMPLETE:
        return target

    # A prior interrupted copy leaves only these reserved temporary files.
    # Remove them before continuing the one permitted resume path so they
    # cannot accumulate or be mistaken for user data on an unmarked target.
    _remove_stale_migration_temps(target)

    if state is None:
        if not legacy.exists():
            _write_migration_state(target, MIGRATION_STATE_COMPLETE)
            return target
        if _target_has_user_data(target):
            # Preserve the legacy no-clobber behavior for already in-use
            # installs that predate the migration marker.
            _write_migration_state(target, MIGRATION_STATE_COMPLETE)
            return target
        _write_migration_state(target, MIGRATION_STATE_STARTED)

    if legacy.exists():
        _copy_missing_legacy_items(legacy, target)
        logger.info("Migrated legacy zd_data/ -> %s", target)
    _write_migration_state(target, MIGRATION_STATE_COMPLETE)
    return target


def _read_migration_state(marker_path: Path) -> str | None:
    """Return the saved state, failing closed for an invalid marker.

    ``None`` is reserved for an absent marker so callers can distinguish a
    pre-marker install from an interrupted migration. Only a marker that is
    exactly ``{"state": "started"}`` may resume a migration. A corrupt or
    unrecognised marker cannot prove that copying was interrupted, so it is
    normalised to ``complete`` rather than risking resurrection of legacy data
    the user deliberately deleted from the migrated location.
    """

    if not marker_path.exists():
        return None
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _normalise_unreadable_migration_state(marker_path)
    if payload == {"state": MIGRATION_STATE_STARTED}:
        return MIGRATION_STATE_STARTED
    if payload == {"state": MIGRATION_STATE_COMPLETE}:
        return MIGRATION_STATE_COMPLETE
    return _normalise_unreadable_migration_state(marker_path)


def _normalise_unreadable_migration_state(marker_path: Path) -> str:
    """Treat an ambiguous migration state as complete without touching legacy."""

    logger.warning(
        "Frozen-data migration state at %s was unreadable or invalid; treating it "
        "as complete and normalizing the marker",
        marker_path,
    )
    try:
        _write_migration_state(marker_path.parent, MIGRATION_STATE_COMPLETE)
    except OSError:
        # Even if the normalization write fails, return complete: the legacy
        # source remains available for manual recovery and must not be copied
        # automatically from an ambiguous marker.
        logger.warning(
            "Could not normalize unreadable frozen-data migration state at %s; "
            "continuing fail-closed",
            marker_path,
            exc_info=True,
        )
    return MIGRATION_STATE_COMPLETE


def _write_migration_state(target: Path, state: str) -> None:
    """Durably replace the frozen-data migration state marker."""

    marker_path = target / MIGRATION_STATE_FILENAME
    temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
    payload = json.dumps({"state": state})
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                logger.debug("fsync unavailable for %s", temp_path, exc_info=True)
        os.replace(temp_path, marker_path)
    except OSError:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        raise


def _target_has_user_data(target: Path) -> bool:
    """Whether ``target`` has data other than migration bookkeeping."""

    ignored = {
        MIGRATION_STATE_FILENAME,
        f"{MIGRATION_STATE_FILENAME}.tmp",
    }
    return any(
        item.name not in ignored and not item.name.endswith(MIGRATION_TEMP_SUFFIX)
        for item in target.iterdir()
    )


def _remove_stale_migration_temps(target: Path) -> None:
    """Best-effort removal of per-file temporary copies from a prior resume."""

    for temp_path in target.rglob(f"*{MIGRATION_TEMP_SUFFIX}"):
        if temp_path.is_dir():
            continue
        try:
            temp_path.unlink()
        except OSError:
            logger.warning(
                "Could not remove stale frozen-data migration temp %s",
                temp_path,
                exc_info=True,
            )


def _copy_missing_legacy_items(legacy: Path, target: Path) -> None:
    """Resume migration without overwriting files already in ``target``."""

    def _copy_file_if_missing(source: str, destination: str, *args, **kwargs) -> str:
        destination_path = Path(destination)
        if destination_path.exists():
            return str(destination_path)
        temp_path = destination_path.with_name(
            f"{destination_path.name}{MIGRATION_TEMP_SUFFIX}"
        )
        # Publish each file only after the same-directory temporary copy is
        # durable. A process interruption can leave a temp, but never a final
        # path that a later resume mistakes for a completed copy.
        shutil.copy2(source, temp_path, *args, **kwargs)
        with open(temp_path, "rb") as handle:
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                logger.debug("fsync unavailable for %s", temp_path, exc_info=True)
        os.replace(temp_path, destination_path)
        return str(destination_path)

    for item in legacy.iterdir():
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(
                item,
                destination,
                dirs_exist_ok=True,
                copy_function=_copy_file_if_missing,
            )
        elif not destination.exists():
            _copy_file_if_missing(str(item), str(destination))


def _settings_with_default_paths() -> AppSettings:
    settings = AppSettings()
    settings.diagnostics_bundle_dir = str(_default_user_data_dir() / "diagnostics")
    setattr(settings, SETUP_DRAWER_DISMISSED_KEY, False)
    return settings


def _coerce_setup_drawer_dismissed(value) -> bool:
    return bool(value)


class SettingsStore:
    def __init__(self, path: str | Path | None = None):
        if path is None:
            self.path = initialize_user_data_dir() / "settings.json"
        else:
            self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> AppSettings:
        if not self.path.exists():
            return _settings_with_default_paths()
        try:
            payload = read_guarded_json(self.path)
        except (OSError, ValueError, RecursionError, json.JSONDecodeError):
            return _settings_with_default_paths()
        # A parseable-but-non-object root (``null`` / ``[]`` / ``42`` / a bare
        # string) would AttributeError on ``payload.get(...)`` below; bail to
        # defaults so a corrupt/tampered settings.json degrades gracefully
        # instead of bricking launch (matching every sibling store).
        if not isinstance(payload, dict):
            logger.warning(
                "settings.json root is %s, not an object; using defaults",
                type(payload).__name__,
            )
            return _settings_with_default_paths()
        # ``from_dict`` + the diagnostics-dir resolution can still raise on
        # hostile field shapes (e.g. an unhashable ``language`` value trips a
        # TypeError in ``_normalize_language_code``); honour the corrupt->
        # defaults contract rather than aborting every relaunch.
        try:
            settings = AppSettings.from_dict(payload)
            setattr(
                settings,
                SETUP_DRAWER_DISMISSED_KEY,
                _coerce_setup_drawer_dismissed(
                    payload.get(SETUP_DRAWER_DISMISSED_KEY, False)
                ),
            )
            diagnostics_dir = payload.get("diagnostics_bundle_dir")
            if diagnostics_dir is None or Path(diagnostics_dir) == Path("zd_data") / "diagnostics":
                settings.diagnostics_bundle_dir = str(_default_user_data_dir() / "diagnostics")
        except (AttributeError, TypeError, KeyError, ValueError):
            logger.warning(
                "settings.json could not be parsed into AppSettings; using "
                "defaults",
                exc_info=True,
            )
            return _settings_with_default_paths()
        return settings

    def save(self, settings: AppSettings) -> None:
        # Atomic temp-then-replace (per the subsystem's "all writes atomic"
        # invariant, mirroring RestorePointStore/LastAppliedStore/
        # WrapperProfileStore): a crash mid-write leaves either the previous
        # settings.json or a ``.tmp`` straggler, never a half-written final.
        data = settings.to_dict()
        data[SETUP_DRAWER_DISMISSED_KEY] = _coerce_setup_drawer_dismissed(
            getattr(
                settings,
                SETUP_DRAWER_DISMISSED_KEY,
                self.get_setup_drawer_dismissed(),
            )
        )
        payload = json.dumps(data, indent=2)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(temp_path, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                # Best-effort: fsync can be unavailable on some filesystems
                # (e.g. Windows network shares). The replace below still runs.
                logger.debug("fsync unavailable for %s", temp_path, exc_info=True)
        temp_path.replace(self.path)

    def get_setup_drawer_dismissed(self) -> bool:
        if not self.path.exists():
            return False
        try:
            payload = read_guarded_json(self.path)
        except (OSError, ValueError, RecursionError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        return _coerce_setup_drawer_dismissed(
            payload.get(SETUP_DRAWER_DISMISSED_KEY, False)
        )

    def set_setup_drawer_dismissed(self, dismissed: bool) -> None:
        settings = self.load()
        setattr(
            settings,
            SETUP_DRAWER_DISMISSED_KEY,
            _coerce_setup_drawer_dismissed(dismissed),
        )
        self.save(settings)
