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
    if not legacy.exists() or any(target.iterdir()):
        return target

    for item in legacy.iterdir():
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)
    logger.info("Migrated legacy zd_data/ -> %s", target)
    return target


def _settings_with_default_paths() -> AppSettings:
    settings = AppSettings()
    settings.diagnostics_bundle_dir = str(_default_user_data_dir() / "diagnostics")
    return settings


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
        payload = json.dumps(settings.to_dict(), indent=2)
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
