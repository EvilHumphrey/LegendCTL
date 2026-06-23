"""Tests for WrapperProfileStore safe import + export (Safe Import)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from zd_app.models import WrapperProfile
from zd_app.services.settings_service import (
    AxisInversion,
    BackPaddleBinding,
    ButtonMapping,
    ButtonSlot,
    ControllerButtonTarget,
    ControllerSnapshot,
    LightingMode,
    LightingSettings,
    LightingZone,
    MacroSlot,
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
from zd_app.storage._import_guards import MAX_IMPORT_BYTES, MAX_IMPORT_JSON_DEPTH
from zd_app.storage.wrapper_profile_store import (
    MAX_PROFILE_NAME_LEN,
    WrapperProfileError,
    WrapperProfileStore,
    sanitize_display_name,
    slugify,
    unique_display_name,
)


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
            LightingZone.HOME: LightingSettings(True, LightingMode.ALWAYS_ON, 180, RgbColor(1, 2, 3)),
        },
        motion_settings=MotionSettings(
            MotionMappingTarget.LEFT_JOYSTICK, 0x06, MotionMappingMode.CONTINUOUS, 50
        ),
        back_paddle_bindings={MacroSlot.M1: BackPaddleBinding(ControllerButtonTarget.A)},
    )


def _profile(name: str, snapshot: ControllerSnapshot | None = None, description: str = "") -> WrapperProfile:
    return WrapperProfile(
        name=name,
        description=description,
        created_at="2026-05-22T00:00:00+00:00",
        last_modified_at="2026-05-22T00:00:00+00:00",
        snapshot=snapshot or _full_snapshot(),
    )


def _valid_payload(name: str = "Imported", snapshot: ControllerSnapshot | None = None) -> dict:
    return _profile(name, snapshot).to_dict()


class ImportFromFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        # base_dir is nested so a write "outside base_dir" is observable.
        self.base_dir = Path(self.temp_dir.name) / "wrapper_profiles"
        self.store = WrapperProfileStore(self.base_dir)

    def _write(self, payload, name: str = "incoming.json") -> Path:
        source = Path(self.temp_dir.name) / name
        if isinstance(payload, (dict, list)):
            source.write_text(json.dumps(payload), encoding="utf-8")
        else:
            source.write_text(payload, encoding="utf-8")
        return source

    def test_happy_path_round_trip(self) -> None:
        source = self._write(_valid_payload("Apex Tuned"))

        profile = self.store.import_from_file(str(source))

        self.assertEqual(profile.name, "Apex Tuned")
        self.assertEqual(profile.snapshot, _full_snapshot())

    def test_import_does_not_auto_save(self) -> None:
        source = self._write(_valid_payload("Whatever"))

        self.store.import_from_file(str(source))

        self.assertEqual(self.store.list_profiles(), ([], []))
        self.assertEqual(list(self.base_dir.glob("*.json")), [])

    def test_traversal_name_slugifies_safely(self) -> None:
        source = self._write(_valid_payload(r"..\..\evil"))

        profile = self.store.import_from_file(str(source))

        self.assertNotIn("..", slugify(profile.name))
        self.assertEqual(slugify(profile.name), "evil")

    def test_imported_traversal_name_saves_contained(self) -> None:
        source = self._write(_valid_payload(r"..\..\..\Startup\evil"))

        profile = self.store.import_from_file(str(source))
        path = self.store.save(profile)

        self.assertEqual(path.resolve().parent, self.base_dir.resolve())
        self.assertNotIn("..", path.name)

    def test_absolute_and_drive_letter_names_save_contained(self) -> None:
        for hostile in (r"C:\Windows\System32\evil", "/etc/passwd", r"\\server\share\x"):
            with self.subTest(hostile=hostile):
                source = self._write(_valid_payload(hostile), name="incoming2.json")

                profile = self.store.import_from_file(str(source))
                path = self.store.save(profile)

                self.assertEqual(path.resolve().parent, self.base_dir.resolve())

    def test_control_chars_and_long_name_sanitized(self) -> None:
        hostile = "A" * 10000 + "\x00\x07\x1b[31m"
        source = self._write(_valid_payload(hostile))

        profile = self.store.import_from_file(str(source))

        self.assertLessEqual(len(profile.name), MAX_PROFILE_NAME_LEN)
        self.assertNotIn("\x00", profile.name)
        self.assertNotIn("\x1b", profile.name)

    def test_uniquifies_name_against_existing(self) -> None:
        self.store.save(_profile("Apex Tuned"))
        source = self._write(_valid_payload("Apex Tuned"))

        profile = self.store.import_from_file(str(source))

        self.assertNotEqual(slugify(profile.name), "apex-tuned")
        self.assertTrue(profile.name.startswith("Apex Tuned"))

    def test_non_object_root_rejected(self) -> None:
        source = self._write([1, 2, 3])

        with self.assertRaises(WrapperProfileError):
            self.store.import_from_file(str(source))

    def test_invalid_json_rejected(self) -> None:
        source = self._write("{not json")

        with self.assertRaises(WrapperProfileError):
            self.store.import_from_file(str(source))

    def test_bad_schema_version_rejected(self) -> None:
        payload = _valid_payload("X")
        payload["schema_version"] = 99
        source = self._write(payload)

        with self.assertRaises(WrapperProfileError):
            self.store.import_from_file(str(source))

    def test_out_of_range_field_rejected(self) -> None:
        payload = _valid_payload("X")
        payload["snapshot"]["deadzones"]["left_center"] = 99999
        source = self._write(payload)

        with self.assertRaises(WrapperProfileError):
            self.store.import_from_file(str(source))

    def test_trigger_min_greater_than_max_rejected(self) -> None:
        payload = _valid_payload("X")
        payload["snapshot"]["trigger_left"]["range_min"] = 80
        payload["snapshot"]["trigger_left"]["range_max"] = 10
        source = self._write(payload)

        with self.assertRaises(WrapperProfileError):
            self.store.import_from_file(str(source))

    def test_bad_enum_rejected(self) -> None:
        payload = _valid_payload("X")
        payload["snapshot"]["polling_rate"] = 999
        source = self._write(payload)

        with self.assertRaises(WrapperProfileError):
            self.store.import_from_file(str(source))

    def test_oversized_rejected_before_parse(self) -> None:
        source = Path(self.temp_dir.name) / "big.json"
        source.write_text("x" * (MAX_IMPORT_BYTES + 1), encoding="utf-8")

        with self.assertRaises(WrapperProfileError):
            self.store.import_from_file(str(source))
        self.assertEqual(list(self.base_dir.glob("*.json")), [])

    def test_deeply_nested_rejected_before_parse(self) -> None:
        depth = MAX_IMPORT_JSON_DEPTH + 5
        source = Path(self.temp_dir.name) / "deep.json"
        source.write_text("[" * depth + "]" * depth, encoding="utf-8")

        with self.assertRaises(WrapperProfileError):
            self.store.import_from_file(str(source))
        self.assertEqual(list(self.base_dir.glob("*.json")), [])

    def test_missing_file_rejected(self) -> None:
        with self.assertRaises(WrapperProfileError):
            self.store.import_from_file(str(Path(self.temp_dir.name) / "nope.json"))

    def test_import_save_export_discards_foreign_automation(self) -> None:
        # The codec preserves only known fields, so foreign automation keys are
        # dropped on import and never reach the saved or exported file.
        payload = _valid_payload("Auto")
        payload["snapshot"]["turbo"] = {"enabled": True}
        slot_key = next(iter(payload["snapshot"]["button_bindings"]))
        payload["snapshot"]["button_bindings"][slot_key]["macro_binding"] = "A->B"
        source = self._write(payload)

        profile = self.store.import_from_file(str(source))
        self.assertNotIn("turbo", json.dumps(profile.to_dict()))
        self.assertNotIn("macro_binding", json.dumps(profile.to_dict()))

        self.store.save(profile)
        dest = Path(self.temp_dir.name) / "exp"
        exported = self.store.export_to_file(profile.name, dest)
        text = exported.read_text(encoding="utf-8")
        self.assertNotIn("turbo", text)
        self.assertNotIn("macro_binding", text)


class ExportToFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base_dir = Path(self.temp_dir.name) / "wrapper_profiles"
        self.store = WrapperProfileStore(self.base_dir)
        self.dest = Path(self.temp_dir.name) / "exports"

    def test_export_writes_slugified_contained_file(self) -> None:
        self.store.save(_profile("Apex Tuned"))

        path = self.store.export_to_file("Apex Tuned", self.dest)

        self.assertEqual(path.name, "apex-tuned.json")
        self.assertEqual(path.resolve().parent, self.dest.resolve())
        self.assertTrue(path.exists())

    def test_export_missing_profile_raises(self) -> None:
        with self.assertRaises(WrapperProfileError):
            self.store.export_to_file("Nope", self.dest)

    def test_export_disambiguates_existing_file(self) -> None:
        self.store.save(_profile("Apex Tuned"))

        first = self.store.export_to_file("Apex Tuned", self.dest)
        second = self.store.export_to_file("Apex Tuned", self.dest)

        self.assertNotEqual(first, second)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_export_then_import_round_trip(self) -> None:
        original = _profile("Round Trip", _full_snapshot(PollingRate.HZ_4000))
        self.store.save(original)

        exported = self.store.export_to_file("Round Trip", self.dest)
        other = WrapperProfileStore(Path(self.temp_dir.name) / "other")
        imported = other.import_from_file(str(exported))

        self.assertEqual(imported.name, "Round Trip")
        self.assertEqual(imported.snapshot, original.snapshot)


class NameHelperTests(unittest.TestCase):
    def test_sanitize_strips_control_and_caps(self) -> None:
        self.assertEqual(sanitize_display_name("hi\x00\x1bthere"), "hithere")
        self.assertEqual(len(sanitize_display_name("z" * 100)), MAX_PROFILE_NAME_LEN)

    def test_sanitize_rejects_non_strings(self) -> None:
        self.assertEqual(sanitize_display_name(123), "")
        self.assertEqual(sanitize_display_name(None), "")
        self.assertEqual(sanitize_display_name({"a": 1}), "")

    def test_unique_default_when_unusable(self) -> None:
        self.assertEqual(unique_display_name("!!!", set()), "Imported Profile")
        self.assertEqual(unique_display_name("", set()), "Imported Profile")
        self.assertEqual(unique_display_name(None, set()), "Imported Profile")

    def test_unique_appends_suffix_on_collision(self) -> None:
        out = unique_display_name("Apex Tuned", {"apex-tuned"})

        self.assertEqual(slugify(out), "apex-tuned-2")
        self.assertEqual(out, "Apex Tuned (2)")

    def test_unique_skips_multiple_collisions(self) -> None:
        out = unique_display_name("Apex Tuned", {"apex-tuned", "apex-tuned-2"})

        self.assertEqual(slugify(out), "apex-tuned-3")


if __name__ == "__main__":
    unittest.main()
