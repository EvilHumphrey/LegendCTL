"""Tests for file-backed Wrapper Profile storage."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import mock_open, patch

from zd_app.models import WrapperProfile
from zd_app.services.settings_service import (
    AxisInversion,
    ButtonMapping,
    ButtonSlot,
    ControllerButtonTarget,
    ControllerSnapshot,
    LightingMode,
    LightingSettings,
    LightingZone,
    MotionMappingMode,
    MotionMappingTarget,
    MotionSettings,
    PollingRate,
    RgbColor,
    SensitivityAnchor,
    StickDeadzones,
    TriggerMode,
    TriggerSettings,
    TriggerVibrationMode,
    VibrationSettings,
)
from zd_app.storage.wrapper_profile_store import (
    WrapperProfileError,
    WrapperProfileStore,
    slugify,
)


_DEEP_JSON = "[" * 20000 + "]" * 20000


def _full_snapshot(rate: PollingRate = PollingRate.HZ_8000) -> ControllerSnapshot:
    return ControllerSnapshot(
        polling_rate=rate,
        vibration=VibrationSettings(20, 30, 40, 50, TriggerVibrationMode.TRIGGER_VIBRATION),
        deadzones=StickDeadzones(5, 6, 90, 91),
        axis_inversion_left=AxisInversion(True, False),
        axis_inversion_right=AxisInversion(False, True),
        sensitivity_left=(
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 72),
            SensitivityAnchor(100, 100),
        ),
        sensitivity_right=(
            SensitivityAnchor(0, 0),
            SensitivityAnchor(60, 80),
            SensitivityAnchor(100, 100),
        ),
        trigger_left=TriggerSettings(3, 77, TriggerMode.SHORT),
        trigger_right=TriggerSettings(4, 88, TriggerMode.LONG),
        button_bindings={
            ButtonSlot.A: ButtonMapping.controller_button(ControllerButtonTarget.B),
        },
        lighting_zones={
            LightingZone.HOME: LightingSettings(
                True,
                LightingMode.ALWAYS_ON,
                180,
                RgbColor(1, 2, 3),
            ),
        },
        motion_settings=MotionSettings(
            MotionMappingTarget.LEFT_JOYSTICK,
            0x06,
            MotionMappingMode.CONTINUOUS,
            50,
        ),
    )


def _partial_snapshot() -> ControllerSnapshot:
    return ControllerSnapshot(
        polling_rate=None,
        vibration=None,
        deadzones=None,
        axis_inversion_left=None,
        axis_inversion_right=None,
        sensitivity_left=None,
        sensitivity_right=None,
        trigger_left=None,
        trigger_right=None,
        button_bindings={},
        lighting_zones={},
    )


def _profile(name: str, snapshot: ControllerSnapshot | None = None, description: str = "") -> WrapperProfile:
    return WrapperProfile(
        name=name,
        description=description,
        created_at="2026-05-04T17:20:00+00:00",
        last_modified_at="2026-05-04T17:20:00+00:00",
        snapshot=snapshot or _full_snapshot(),
    )


class WrapperProfileStoreTests(unittest.TestCase):
    def test_save_then_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            profile = _profile("Apex Tuned", description="Ranked FPS")

            path = store.save(profile)
            loaded = store.load("Apex Tuned")

            self.assertEqual(path.name, "apex-tuned.json")
            self.assertEqual(loaded.name, "Apex Tuned")
            self.assertEqual(loaded.description, "Ranked FPS")
            self.assertEqual(loaded.snapshot, profile.snapshot)
            profiles, skipped = store.list_profiles()
            self.assertEqual([item.name for item in profiles], ["Apex Tuned"])
            self.assertEqual(skipped, [])

    def test_save_overwrites_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            store.save(_profile("Apex Tuned", _full_snapshot(PollingRate.HZ_250)))
            store.save(_profile("Apex Tuned", _full_snapshot(PollingRate.HZ_8000)))

            loaded = store.load("Apex Tuned")

            self.assertEqual(loaded.snapshot.polling_rate, PollingRate.HZ_8000)
            profiles, skipped = store.list_profiles()
            self.assertEqual(len(profiles), 1)
            self.assertEqual(skipped, [])

    def test_list_profiles_sorted_by_last_modified_desc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            with patch(
                "zd_app.storage.wrapper_profile_store.utc_now_iso",
                side_effect=[
                    "2026-05-04T17:20:01+00:00",
                    "2026-05-04T17:20:02+00:00",
                ],
            ):
                store.save(_profile("A"))
                store.save(_profile("B"))

            profiles, skipped = store.list_profiles()
            self.assertEqual([profile.name for profile in profiles], ["B", "A"])
            self.assertEqual(skipped, [])

    def test_delete_existing_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            store.save(_profile("Apex Tuned"))

            self.assertTrue(store.delete("Apex Tuned"))
            self.assertFalse((Path(tmpdir) / "apex-tuned.json").exists())

    def test_delete_missing_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)

            self.assertFalse(store.delete("Missing"))

    def test_load_missing_raises_wrapper_profile_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)

            with self.assertRaises(WrapperProfileError):
                store.load("Missing")

    def test_save_invalid_name_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            for name in ("", "   ", "!!!"):
                with self.subTest(name=name):
                    with self.assertRaises(WrapperProfileError):
                        store.save(_profile(name))

    def test_slugify_lowercases_and_normalizes_separators(self) -> None:
        cases = {
            "Apex Tuned": "apex-tuned",
            "FPS Default": "fps-default",
            "  Casual  ! ": "casual",
            "": "",
            "   ": "",
            "!!!": "",
        }

        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(slugify(raw), expected)

    def test_slugify_collision_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            store.save(_profile("Apex Tuned", description="first"))
            store.save(_profile("apex-tuned", description="second"))

            loaded = store.load("Apex Tuned")

            self.assertEqual(loaded.name, "apex-tuned")
            self.assertEqual(loaded.description, "second")
            profiles, skipped = store.list_profiles()
            self.assertEqual(len(profiles), 1)
            self.assertEqual(skipped, [])

    def test_rename_moves_file_and_clears_old(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            store.save(_profile("Old Name"))

            renamed = store.rename("Old Name", "New Name")

            self.assertEqual(renamed.name, "New Name")
            self.assertFalse((Path(tmpdir) / "old-name.json").exists())
            self.assertTrue((Path(tmpdir) / "new-name.json").exists())
            self.assertEqual(store.load("New Name").snapshot, _full_snapshot())

    def test_rename_target_collision_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            store.save(_profile("Old Name"))
            store.save(_profile("New Name"))

            with self.assertRaises(WrapperProfileError):
                store.rename("Old Name", "New Name")

    def test_corrupted_json_skipped_in_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            store.save(_profile("Good"))
            bad_path = Path(tmpdir) / "bad.json"
            bad_path.write_text("{not json", encoding="utf-8")

            with self.assertLogs("zd_app.storage.wrapper_profile_store", level="WARNING") as logs:
                profiles, skipped = store.list_profiles()

            self.assertEqual([profile.name for profile in profiles], ["Good"])
            self.assertEqual(skipped, [bad_path])
            self.assertIn("Failed to load profile bad.json", logs.output[0])

    def test_list_profiles_returns_skipped_for_schema_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            bad_path = Path(tmpdir) / "bad.json"
            bad_path.write_text(
                json.dumps(
                    {
                        "schema_version": 99,
                        "name": "Bad",
                        "created_at": "2026-05-04T17:20:00+00:00",
                        "last_modified_at": "2026-05-04T17:20:00+00:00",
                        "snapshot": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertLogs("zd_app.storage.wrapper_profile_store", level="WARNING"):
                profiles, skipped = store.list_profiles()

            self.assertEqual(profiles, [])
            self.assertEqual(skipped, [bad_path])

    def test_list_profiles_continues_past_one_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            store.save(_profile("Good"))
            bad_path = Path(tmpdir) / "bad.json"
            bad_path.write_text("{not json", encoding="utf-8")

            with self.assertLogs("zd_app.storage.wrapper_profile_store", level="WARNING"):
                profiles, skipped = store.list_profiles()

            self.assertEqual([profile.name for profile in profiles], ["Good"])
            self.assertEqual(skipped, [bad_path])

    def test_deeply_nested_profile_is_skipped_in_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            store.save(_profile("Good"))
            deep_path = Path(tmpdir) / "deep.json"
            deep_path.write_text(_DEEP_JSON, encoding="utf-8")

            with self.assertLogs("zd_app.storage.wrapper_profile_store", level="WARNING"):
                profiles, skipped = store.list_profiles()

            self.assertEqual([profile.name for profile in profiles], ["Good"])
            self.assertEqual(skipped, [deep_path])

    def test_deeply_nested_profile_load_raises_wrapper_profile_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            (Path(tmpdir) / "deep.json").write_text(_DEEP_JSON, encoding="utf-8")

            with self.assertRaises(WrapperProfileError):
                store.load("Deep")

    def test_unsupported_schema_version_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            (Path(tmpdir) / "bad.json").write_text(
                json.dumps(
                    {
                        "schema_version": 99,
                        "name": "Bad",
                        "created_at": "2026-05-04T17:20:00+00:00",
                        "last_modified_at": "2026-05-04T17:20:00+00:00",
                        "snapshot": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(WrapperProfileError):
                store.load("Bad")

    def test_partial_snapshot_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            profile = _profile("Partial", _partial_snapshot())

            store.save(profile)
            loaded = store.load("Partial")

            self.assertEqual(loaded.snapshot, profile.snapshot)

    def test_profile_save_load_preserves_motion_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            motion = MotionSettings(
                MotionMappingTarget.RIGHT_JOYSTICK,
                0x06,
                MotionMappingMode.CONTINUOUS,
                100,
            )
            profile = _profile("Motion", _full_snapshot())
            profile.snapshot = replace(profile.snapshot, motion_settings=motion)

            store.save(profile)
            loaded = store.load("Motion")

            self.assertEqual(loaded.snapshot.motion_settings, motion)


class AtomicSaveTests(unittest.TestCase):
    """``save`` mirrors ``RestorePointStore.save``'s temp-file-then-replace.

    The previous bare ``write_text`` could leave a
    half-written profile if the process died mid-write. The atomic dance
    leaves either the previous file or a ``.tmp`` straggler the ``*.json``
    glob never picks up.
    """

    def test_save_writes_temp_then_replaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)

            original_replace = Path.replace
            replace_calls: list[tuple[str, str]] = []

            def _spy(self: Path, target):
                replace_calls.append((self.name, Path(target).name))
                return original_replace(self, target)

            with patch.object(Path, "replace", _spy):
                store.save(_profile("Apex Tuned", description="Ranked FPS"))

            self.assertEqual(replace_calls, [("apex-tuned.json.tmp", "apex-tuned.json")])
            self.assertEqual(list(Path(tmpdir).glob("*.tmp")), [])
            loaded = store.load("Apex Tuned")
            self.assertEqual(loaded.description, "Ranked FPS")
            self.assertEqual(loaded.snapshot, _full_snapshot())

    def test_failed_write_leaves_existing_profile_intact(self) -> None:
        """A write that raises mid-stream must not corrupt the previous file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WrapperProfileStore(tmpdir)
            store.save(_profile("Apex Tuned", description="original"))
            final_path = Path(tmpdir) / "apex-tuned.json"
            before_bytes = final_path.read_bytes()

            exploding = mock_open()
            exploding.return_value.write.side_effect = OSError("disk full mid-write")
            with patch("zd_app.storage.wrapper_profile_store.open", exploding):
                with self.assertRaises(OSError):
                    store.save(_profile("Apex Tuned", description="updated"))

            self.assertEqual(final_path.read_bytes(), before_bytes)
            self.assertEqual(store.load("Apex Tuned").description, "original")

    def test_temp_straggler_is_invisible_to_list_profiles(self) -> None:
        """``*.json`` glob does not match ``.json.tmp`` — a crashed write's
        straggler must not surface as a (corrupt) profile."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "apex-tuned.json.tmp").write_text(
                "half-written", encoding="utf-8"
            )
            store = WrapperProfileStore(tmpdir)

            profiles, skipped = store.list_profiles()

            self.assertEqual(profiles, [])
            self.assertEqual(skipped, [])


if __name__ == "__main__":
    unittest.main()
