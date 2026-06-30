"""File-backed local profile storage."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from uuid import uuid4

from zd_app.models import Profile, utc_now_iso
# The DoS guards live in ``_import_guards`` so v1 and v2 Safe Import share one
# source of truth. MAX_IMPORT_* are re-exported here for existing importers.
from zd_app.storage._import_guards import (
    MAX_IMPORT_BYTES,
    MAX_IMPORT_JSON_DEPTH,
    read_guarded_json,
)
from zd_app.storage.settings_store import initialize_user_data_dir
from zd_app.storage.wrapper_profile_store import slugify


logger = logging.getLogger(__name__)


class ProfileStore:
    def __init__(self, base_dir: str | Path | None = None):
        if base_dir is None:
            self.base_dir = initialize_user_data_dir() / "profiles"
        else:
            self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, profile: Profile) -> Path:
        profile.last_modified = utc_now_iso()
        path = self._path_for_id(profile.profile_id)
        # Atomic write (temp-then-replace + best-effort fsync), matching the
        # sibling stores (wrapper_profile_store / restore_point_store /
        # settings_store): a crash mid-write leaves either the previous file or
        # an ignored ``.tmp`` straggler, never a half-written ``<slug>.json``
        # (the ``*.json`` glob in list_profiles never sees the ``.tmp``).
        payload = json.dumps(profile.to_dict(), indent=2)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with open(temp_path, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                logger.debug("fsync unavailable for %s", temp_path, exc_info=True)
        temp_path.replace(path)
        return path

    def load(self, profile_id: str) -> Profile:
        path = self._path_for_id(profile_id)
        return Profile.from_dict(read_guarded_json(path))

    def list_profiles(self) -> list[Profile]:
        profiles: list[Profile] = []
        for path in sorted(self.base_dir.glob("*.json"), reverse=True):
            try:
                profiles.append(Profile.from_dict(read_guarded_json(path)))
            except (OSError, ValueError, RecursionError, json.JSONDecodeError):
                # json.JSONDecodeError is a ValueError; Profile.from_dict now
                # normalizes all wrong-shape errors to ValueError too, so a
                # corrupt or malformed profile file is skipped, not fatal.
                continue
        profiles.sort(key=lambda item: item.last_modified, reverse=True)
        return profiles

    def duplicate(self, profile_id: str, new_name: str) -> Profile:
        source = self.load(profile_id)
        copy_profile = source.clone()
        copy_profile.profile_id = self._unique_profile_id(source.profile_id)
        copy_profile.display_name = new_name
        copy_profile.origin = "desktop"
        self.save(copy_profile)
        return copy_profile

    def rename(self, profile_id: str, new_name: str) -> Profile:
        profile = self.load(profile_id)
        profile.display_name = new_name
        self.save(profile)
        return profile

    def export_profile(self, profile_id: str, export_dir: str | Path | None = None) -> Path:
        export_path = (
            initialize_user_data_dir() / "exports"
            if export_dir is None
            else Path(export_dir)
        )
        export_path.mkdir(parents=True, exist_ok=True)
        profile = self.load(profile_id)
        # display_name and profile_id are both user-controlled; slugify each
        # before composing the export filename so neither can escape export_path.
        filename = f"{slugify(profile.display_name) or 'profile'}-{slugify(profile.profile_id) or 'config'}.json"
        destination = self._contained(export_path, filename)
        destination.write_text(json.dumps(profile.to_dict(), indent=2), encoding="utf-8")
        return destination

    def import_profile(self, path: str) -> Profile:
        payload = read_guarded_json(path)
        profile = Profile.from_dict(payload)
        # Ignore the imported profile_id entirely and mint a fresh, filesystem
        # -safe id. A hostile id such as ``..\\..\\Startup\\evil`` therefore can
        # never reach a path.
        profile.profile_id = self._unique_profile_id(f"profile-{uuid4().hex[:8]}")
        profile.origin = "desktop"
        # No-automation posture: the legacy v1 ButtonMapping carries dormant
        # turbo_enabled / macro_binding fields that no apply path reads. Neutralize
        # them on import so this user-reachable legacy vector can't store — or
        # later re-export — automation state from a foreign / hand-edited file.
        for mapping in profile.button_mappings:
            mapping.turbo_enabled = False
            mapping.macro_binding = None
        self.save(profile)
        return profile

    def _unique_profile_id(self, preferred_id: str) -> str:
        base = slugify(preferred_id) or f"profile-{uuid4().hex[:8]}"
        candidate = base
        while (self.base_dir / f"{candidate}.json").exists():
            candidate = f"{base}-{uuid4().hex[:6]}"
        return candidate

    def _path_for_id(self, profile_id: str) -> Path:
        slug = slugify(profile_id)
        if not slug:
            raise ValueError(f"Invalid profile id: {profile_id!r}")
        return self._contained(self.base_dir, f"{slug}.json")

    @staticmethod
    def _contained(base: Path, filename: str) -> Path:
        # Defense-in-depth: even though ``filename`` is always a bare slug,
        # confirm the resolved path stays directly inside ``base`` before any
        # read or write uses it.
        path = base / filename
        if path.resolve().parent != base.resolve():
            raise ValueError(f"Refusing profile path outside base dir: {filename!r}")
        return path
