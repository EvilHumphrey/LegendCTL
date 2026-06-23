"""Hardware-free tests for profile and storage logic."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zd_app.models import Profile, create_default_profile, diff_profile_count
from zd_app.services.profile_service import ProfileService
from zd_app.storage.profile_store import (
    MAX_IMPORT_BYTES,
    MAX_IMPORT_JSON_DEPTH,
    ProfileStore,
)


class ProfileServiceReadScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        store = ProfileStore(base_dir=self.temp_dir.name)
        self.service = ProfileService(store)

    def test_read_from_controller_marks_verified_active_slot(self) -> None:
        draft = self.service.read_from_controller(2, active_slot_source="Official App UI")

        self.assertEqual(draft.display_name, "Draft aligned to Config 2")
        self.assertIn("Active config 2 confirmed from Official App UI", self.service.last_controller_read_scope)
        self.assertEqual(
            [(slot.slot_id, slot.status, slot.active) for slot in self.service.onboard_slots],
            [
                (1, "slot_not_read", False),
                (2, "slot_active_confirmed", True),
                (3, "slot_not_read", False),
                (4, "slot_not_read", False),
            ],
        )

    def test_read_from_controller_marks_unverified_slot_as_working_assumption(self) -> None:
        draft = self.service.read_from_controller(1, active_slot_source="Not verified")

        self.assertEqual(draft.display_name, "Draft aligned to Config 1")
        self.assertIn("active onboard config is not verified yet", self.service.last_controller_read_scope)
        self.assertEqual(
            [(slot.slot_id, slot.status, slot.active) for slot in self.service.onboard_slots],
            [
                (1, "slot_working_assumption", True),
                (2, "slot_not_read", False),
                (3, "slot_not_read", False),
                (4, "slot_not_read", False),
            ],
        )


class ProfileDiffTests(unittest.TestCase):
    def test_diff_profile_count_tracks_button_and_stick_changes(self) -> None:
        baseline = create_default_profile(name="Baseline")
        current = baseline.clone()
        current.button_mappings[0].action = "B"
        current.left_stick.center_deadzone = 12

        self.assertEqual(diff_profile_count(current, baseline), 2)


class ProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = ProfileStore(base_dir=self.temp_dir.name)

    def test_unique_profile_id_avoids_existing_file(self) -> None:
        existing = create_default_profile(name="Existing", origin="desktop")
        existing.profile_id = "profile-fixed"
        self.store.save(existing)

        duplicate_id = self.store._unique_profile_id(existing.profile_id)

        self.assertNotEqual(duplicate_id, existing.profile_id)
        self.assertTrue(duplicate_id.startswith("profile-fixed-"))

    def test_duplicate_creates_new_desktop_profile(self) -> None:
        source = create_default_profile(name="Tournament", origin="desktop")
        source.profile_id = "profile-source"
        self.store.save(source)

        duplicate = self.store.duplicate(source.profile_id, "Tournament Copy")

        self.assertEqual(duplicate.display_name, "Tournament Copy")
        self.assertEqual(duplicate.origin, "desktop")
        self.assertNotEqual(duplicate.profile_id, source.profile_id)
        self.assertTrue((Path(self.temp_dir.name) / f"{duplicate.profile_id}.json").exists())

    def test_save_writes_complete_json_and_leaves_no_tmp_straggler(self) -> None:
        profile = create_default_profile(name="RoundTrip", origin="desktop")
        profile.profile_id = "profile-roundtrip"
        path = self.store.save(profile)

        self.assertEqual(self.store.load("profile-roundtrip").display_name, "RoundTrip")
        # A successful atomic save replaces the temp file — no .tmp left behind.
        self.assertFalse(path.with_suffix(path.suffix + ".tmp").exists())
        self.assertEqual(list(Path(self.temp_dir.name).glob("*.tmp")), [])

    def test_save_is_atomic_a_crash_at_replace_preserves_the_previous_file(self) -> None:
        # Regression for the pre-fix non-atomic write: a crash mid-save must NOT
        # truncate/corrupt the previously-good profile (matches the sibling
        # stores' temp-then-replace guarantee).
        profile = create_default_profile(name="V1", origin="desktop")
        profile.profile_id = "profile-atomic"
        path = self.store.save(profile)

        profile.display_name = "V2-should-not-land"
        with patch.object(Path, "replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.store.save(profile)

        # The previous version is intact and the final file is still valid JSON
        # (never a half-written <slug>.json).
        self.assertEqual(self.store.load("profile-atomic").display_name, "V1")
        json.loads(path.read_text(encoding="utf-8"))


class ProfileStorePathSafetyTests(unittest.TestCase):
    """H1: imported or looked-up ids must never escape the store base_dir."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        # base_dir is a nested subdirectory so a write "outside base_dir" is
        # observable inside the temp tree.
        self.base_dir = Path(self.temp_dir.name) / "profiles"
        self.store = ProfileStore(base_dir=self.base_dir)

    def _import_source(self, payload: dict) -> Path:
        source = Path(self.temp_dir.name) / "incoming.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        return source

    def test_import_assigns_fresh_safe_id_ignoring_malicious_profile_id(self) -> None:
        payload = create_default_profile(name="Imported").to_dict()
        payload["profile_id"] = r"..\..\..\Startup\evil"

        imported = self.store.import_profile(str(self._import_source(payload)))

        self.assertNotIn("..", imported.profile_id)
        self.assertNotIn("\\", imported.profile_id)
        self.assertNotIn("/", imported.profile_id)
        saved = self.base_dir / f"{imported.profile_id}.json"
        self.assertTrue(saved.exists())
        self.assertEqual(saved.resolve().parent, self.base_dir.resolve())

    def test_import_writes_nothing_outside_base_dir(self) -> None:
        outside = Path(self.temp_dir.name) / "evil.json"
        payload = create_default_profile(name="Imported").to_dict()
        payload["profile_id"] = f"..\\{outside.stem}"

        self.store.import_profile(str(self._import_source(payload)))

        self.assertFalse(outside.exists())
        self.assertEqual(len(list(self.base_dir.glob("*.json"))), 1)

    def test_save_with_traversal_id_stays_contained(self) -> None:
        profile = create_default_profile(name="X")
        profile.profile_id = r"..\..\escape"

        path = self.store.save(profile)

        self.assertEqual(path.resolve().parent, self.base_dir.resolve())
        self.assertNotIn("..", path.name)

    def test_path_for_id_contains_absolute_and_drive_letter_ids(self) -> None:
        for hostile in (
            r"C:\Windows\System32\evil",
            r"\\server\share\payload",
            "/etc/passwd",
            r"..\..\x",
        ):
            with self.subTest(hostile=hostile):
                path = self.store._path_for_id(hostile)
                self.assertEqual(path.resolve().parent, self.base_dir.resolve())
                self.assertTrue(path.name.endswith(".json"))

    def test_path_for_id_rejects_ids_that_sanitize_to_empty(self) -> None:
        for empty in ("...", "///", "\\", "   "):
            with self.subTest(empty=empty):
                with self.assertRaises(ValueError):
                    self.store._path_for_id(empty)

    def test_import_rejects_oversized_file(self) -> None:
        source = Path(self.temp_dir.name) / "big.json"
        source.write_text("x" * (MAX_IMPORT_BYTES + 1), encoding="utf-8")

        with self.assertRaises(ValueError):
            self.store.import_profile(str(source))
        self.assertEqual(list(self.base_dir.glob("*.json")), [])

    def test_import_rejects_deeply_nested_json(self) -> None:
        source = Path(self.temp_dir.name) / "deep.json"
        depth = MAX_IMPORT_JSON_DEPTH + 5
        source.write_text("[" * depth + "]" * depth, encoding="utf-8")

        with self.assertRaises(ValueError):
            self.store.import_profile(str(source))
        self.assertEqual(list(self.base_dir.glob("*.json")), [])

    def test_import_neutralizes_automation_fields(self) -> None:
        # A foreign / hand-edited v1 config can carry the dormant turbo_enabled /
        # macro_binding fields; import strips them so the legacy vector can't
        # store or later re-export automation (no-automation posture).
        profile = create_default_profile(name="Imported")
        profile.button_mappings[0].turbo_enabled = True
        profile.button_mappings[0].macro_binding = "A->B->A->B"
        payload = profile.to_dict()
        self.assertTrue(payload["button_mappings"][0]["turbo_enabled"])  # present in source

        imported = self.store.import_profile(str(self._import_source(payload)))

        self.assertTrue(all(not m.turbo_enabled for m in imported.button_mappings))
        self.assertTrue(all(m.macro_binding is None for m in imported.button_mappings))
        # Persisted copy is neutralized too — it can't be re-exported with automation.
        reloaded = self.store.load(imported.profile_id)
        self.assertTrue(all(not m.turbo_enabled for m in reloaded.button_mappings))
        self.assertTrue(all(m.macro_binding is None for m in reloaded.button_mappings))

    def test_import_malformed_non_dict_raises_clean_value_error(self) -> None:
        # A non-dict JSON payload must surface as ValueError (Profile.from_dict
        # normalizes the shape error) instead of a raw TypeError escaping and
        # wedging the import modal.
        source = Path(self.temp_dir.name) / "bad_shape.json"
        source.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.import_profile(str(source))
        self.assertEqual(list(self.base_dir.glob("*.json")), [])


class ProfileFromDictNormalizationTests(unittest.TestCase):
    """P2d: Profile.from_dict normalizes wrong-shape input to ValueError so the
    legacy import path can't leak a raw KeyError/TypeError/AttributeError."""

    def test_non_dict_payload_raises_value_error(self) -> None:
        for bad in ([1, 2, 3], "hello", 42, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    Profile.from_dict(bad)  # type: ignore[arg-type]

    def test_missing_required_key_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            Profile.from_dict({"display_name": "X"})  # no profile_id

    def test_button_mappings_wrong_shape_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            Profile.from_dict(
                {"profile_id": "p", "display_name": "X", "button_mappings": ["not-a-dict"]}
            )

    def test_valid_payload_still_loads(self) -> None:
        profile = create_default_profile(name="OK")
        self.assertEqual(Profile.from_dict(profile.to_dict()).display_name, "OK")


if __name__ == "__main__":
    unittest.main()
