"""File-backed local Wrapper Profile storage."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from zd_app.models import WrapperProfile, utc_now_iso
from zd_app.storage._import_guards import read_guarded_json
from zd_app.storage.settings_store import initialize_user_data_dir


_SLUG_RE = re.compile(r"[^a-z0-9]+")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_PROFILE_NAME_LEN = 64
DEFAULT_IMPORTED_NAME = "Imported Profile"
TEMP_SUFFIX = ".tmp"
logger = logging.getLogger(__name__)


def slugify(name: str) -> str:
    """Normalize a profile name to a filesystem-safe slug."""

    lowered = name.strip().lower()
    return _SLUG_RE.sub("-", lowered).strip("-")


def sanitize_display_name(name: object) -> str:
    """Strip control characters and cap the length of an untrusted name.

    Identity always rides ``slugify`` (a hostile name can never reach a path),
    but the display name is shown verbatim in the UI, so drop control
    characters (NUL, newlines, terminal escapes) and cap the length. Returns
    ``""`` when the input is unusable; callers pick a fallback.
    """

    if not isinstance(name, str):
        return ""
    cleaned = _CONTROL_CHARS_RE.sub("", name).strip()
    return cleaned[:MAX_PROFILE_NAME_LEN]


def unique_display_name(desired: object, existing_slugs: set[str]) -> str:
    """A sanitized display name whose slug does not collide with existing ones.

    Mirrors ``ProfileStore._unique_profile_id`` (collision suffix) but yields a
    friendly numbered display name rather than a hex id, because the wrapper's
    identity is the display name itself.
    """

    base = sanitize_display_name(desired)
    if not slugify(base):
        base = DEFAULT_IMPORTED_NAME
    candidate = base
    counter = 2
    while slugify(candidate) in existing_slugs:
        suffix = f" ({counter})"
        candidate = f"{base[:MAX_PROFILE_NAME_LEN - len(suffix)]}{suffix}"
        counter += 1
    return candidate


class WrapperProfileError(Exception):
    """Raised by WrapperProfileStore for invalid names and profile IO errors."""


def _contained(base: Path, filename: str) -> Path:
    """Confirm ``base / filename`` resolves to a direct child of ``base``.

    Defense-in-depth mirror of ``ProfileStore._contained``: even though
    ``filename`` is always a bare slug, verify the resolved path stays inside
    ``base`` before any write uses it.
    """

    path = base / filename
    if path.resolve().parent != base.resolve():
        raise WrapperProfileError(f"Refusing profile path outside base dir: {filename!r}")
    return path


class WrapperProfileStore:
    def __init__(self, base_dir: str | Path | None = None):
        if base_dir is None:
            self.base_dir = initialize_user_data_dir() / "wrapper_profiles"
        else:
            self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def list_profiles(self) -> tuple[list[WrapperProfile], list[Path]]:
        """Return readable profiles and skipped files sorted by last-modified time."""

        profiles: list[WrapperProfile] = []
        skipped: list[Path] = []
        for path in self.base_dir.glob("*.json"):
            try:
                profiles.append(
                    WrapperProfile.from_dict(json.loads(path.read_text(encoding="utf-8")))
                )
            except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
                logger.warning("Failed to load profile %s: %s", path.name, exc)
                skipped.append(path)
        profiles.sort(key=lambda profile: profile.last_modified_at, reverse=True)
        return profiles, skipped

    def exists(self, name: str) -> bool:
        slug = slugify(name)
        if not slug:
            return False
        return self._path_for_slug(slug).exists()

    def load(self, name: str) -> WrapperProfile:
        slug = self._slug_or_raise(name)
        path = self._path_for_slug(slug)
        if not path.exists():
            raise WrapperProfileError(f"Profile not found: {name!r}")
        try:
            return WrapperProfile.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise WrapperProfileError(f"Profile {name!r} is corrupted: {exc}") from exc

    def save(self, profile: WrapperProfile) -> Path:
        """Persist or overwrite a profile and update its last-modified time.

        Atomic write mirroring ``RestorePointStore.save`` — write to
        ``<slug>.json.tmp``, flush+fsync, then ``Path.replace()`` so a crash
        mid-write leaves either the previous file or a ``.tmp`` straggler
        (invisible to ``list_profiles``'s ``*.json`` glob), never a
        half-written profile.
        """

        slug = self._slug_or_raise(profile.name)
        profile.last_modified_at = utc_now_iso()
        path = self._path_for_slug(slug)
        temp_path = path.with_suffix(path.suffix + TEMP_SUFFIX)
        payload = json.dumps(profile.to_dict(), indent=2)
        with open(temp_path, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                # Best-effort: fsync can be unavailable on some filesystems
                # (e.g. Windows network shares). Replace below still happens.
                logger.debug("fsync unavailable for %s", temp_path, exc_info=True)
        temp_path.replace(path)
        return path

    def delete(self, name: str) -> bool:
        """Remove a profile by name. Returns True if a file was removed."""

        slug = slugify(name)
        if not slug:
            return False
        path = self._path_for_slug(slug)
        if not path.exists():
            return False
        path.unlink()
        return True

    def rename(self, old_name: str, new_name: str) -> WrapperProfile:
        """Rename a profile, rejecting missing sources and target collisions."""

        old_slug = self._slug_or_raise(old_name)
        new_slug = self._slug_or_raise(new_name)
        if old_slug == new_slug:
            return self.load(old_name)
        old_path = self._path_for_slug(old_slug)
        if not old_path.exists():
            raise WrapperProfileError(f"Profile not found: {old_name!r}")
        if self._path_for_slug(new_slug).exists():
            raise WrapperProfileError(f"Profile already exists: {new_name!r}")

        profile = self.load(old_name)
        profile.name = new_name.strip()
        self.save(profile)
        old_path.unlink()
        return profile

    def import_from_file(self, path: str | Path) -> WrapperProfile:
        """Validate an untrusted wrapper-profile file and return it un-saved.

        Enforces the size/depth guards before parsing, rejects a non-object
        root, validates schema + field ranges via ``WrapperProfile.from_dict``
        (riding the snapshot codec), and replaces the imported name with a
        sanitized, collision-free display name. Does **not** save: the Safe-Import UI
        chooses save-as-new vs apply.
        """

        try:
            payload = read_guarded_json(path)
        except (OSError, ValueError) as exc:
            raise WrapperProfileError(f"Could not read import file {path!r}: {exc}") from exc
        if not isinstance(payload, dict):
            raise WrapperProfileError(f"Import file is not a JSON object: {path!r}")
        try:
            profile = WrapperProfile.from_dict(payload)
        except (KeyError, ValueError, TypeError) as exc:
            raise WrapperProfileError(
                f"Import file is not a valid wrapper profile: {exc}"
            ) from exc
        existing_slugs = {file.stem for file in self.base_dir.glob("*.json")}
        profile.name = unique_display_name(payload.get("name"), existing_slugs)
        return profile

    def export_to_file(self, name: str, dest_dir: str | Path) -> Path:
        """Write a stored profile's JSON into ``dest_dir`` and return the path.

        The filename is ``slugify(name)`` plus a numeric disambiguator so the
        export can never escape ``dest_dir`` (``_contained``) and never
        silently clobbers an unrelated file already in a shared destination.
        """

        profile = self.load(name)
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        base = slugify(profile.name) or "profile"
        filename = f"{base}.json"
        counter = 2
        while (dest / filename).exists():
            filename = f"{base}-{counter}.json"
            counter += 1
        destination = _contained(dest, filename)
        destination.write_text(json.dumps(profile.to_dict(), indent=2), encoding="utf-8")
        return destination

    def _path_for_slug(self, slug: str) -> Path:
        return self.base_dir / f"{slug}.json"

    def _slug_or_raise(self, name: str) -> str:
        slug = slugify(name)
        if not slug:
            raise WrapperProfileError(f"Invalid profile name: {name!r}")
        return slug
