"""Unit tests for the single-record Last-Applied store (Phase 2).

Pins the storage contract the Device-vs-Profile screen and the apply-pipeline
hooks rely on: codec round-trip fidelity, atomic temp-then-replace naming that
no ``*.json`` glob can see, and the never-crash load policy (missing file →
``None`` silently; corrupt / wrong-shape file → ``None`` + a logged warning).

Timestamp fixtures are now-relative (time-rot rule) — never a
hard-coded calendar date.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from zd_app.services.settings_service import (
    BackPaddleBinding,
    ButtonMapping,
    ButtonSlot,
    ControllerButtonTarget,
    ControllerSnapshot,
    MacroSlot,
    PollingRate,
    SensitivityAnchor,
    TriggerVibrationMode,
    VibrationSettings,
)
from zd_app.storage.last_applied_store import (
    FILENAME,
    SCHEMA_VERSION,
    LastAppliedRecord,
    LastAppliedStore,
    record_from_dict,
    record_to_dict,
)


_DEEP_JSON = "[" * 20000 + "]" * 20000


def _snap(**kw) -> ControllerSnapshot:
    base = dict(
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
    base.update(kw)
    return ControllerSnapshot(**base)


def _ts(hours_ago: int = 1) -> str:
    moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(**overrides) -> LastAppliedRecord:
    payload = dict(
        profile_name="race day",
        applied_at=_ts(),
        include_device=True,
        failed_fields=("vibration", "binding_A"),
        snapshot=_snap(
            polling_rate=PollingRate.HZ_8000,
            step_size=146,
            vibration=VibrationSettings(10, 20, 30, 40, TriggerVibrationMode.NATIVE),
            sensitivity_left_8point=tuple(
                SensitivityAnchor(i * 10, i * 10) for i in range(8)
            ),
            # 01 00 TT remap with a real target (TT=0x0A -> RS): the codec
            # validates the target value at decode, so 0x05 (not a button target)
            # would now be rejected on load.
            button_bindings={ButtonSlot.A: ButtonMapping(0x01, 0x00, 0x0A)},
            back_paddle_bindings={
                MacroSlot.M1: BackPaddleBinding(target=ControllerButtonTarget.A)
            },
        ),
    )
    payload.update(overrides)
    return LastAppliedRecord(**payload)


class RoundTripTests(unittest.TestCase):
    def test_save_then_load_round_trips_every_field(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            original = _record()
            path = store.save(original)
            self.assertEqual(path.name, FILENAME)
            loaded = store.load()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.profile_name, original.profile_name)
        self.assertEqual(loaded.applied_at, original.applied_at)
        self.assertTrue(loaded.include_device)
        self.assertEqual(loaded.failed_fields, ("vibration", "binding_A"))
        # The snapshot rides the codec — frozen-dataclass equality covers the
        # nested enums / tuples / dicts.
        self.assertEqual(loaded.snapshot, original.snapshot)

    def test_save_overwrites_single_record(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            store.save(_record(profile_name="first"))
            store.save(_record(profile_name="second", failed_fields=()))
            loaded = store.load()
            json_files = list(Path(tmp).glob("*.json"))
        self.assertEqual(loaded.profile_name, "second")
        self.assertEqual(loaded.failed_fields, ())
        self.assertEqual(len(json_files), 1)

    def test_schema_version_written(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            store.save(_record())
            payload = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)


class AtomicWriteTests(unittest.TestCase):
    def test_no_tmp_straggler_after_save(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            store.save(_record())
            names = sorted(p.name for p in Path(tmp).iterdir())
        self.assertEqual(names, [FILENAME])

    def test_tmp_name_invisible_to_json_globs(self) -> None:
        # A crash mid-write leaves last_applied.json.tmp — that name must not
        # match the ``*.json`` globs other stores (and this one) use.
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            temp_path = store.path.with_suffix(store.path.suffix + ".tmp")
            temp_path.write_text("{}", encoding="utf-8")
            visible = list(Path(tmp).glob("*.json"))
        self.assertEqual(visible, [])


class LoadFailurePolicyTests(unittest.TestCase):
    def test_missing_file_loads_none(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            self.assertIsNone(store.load())

    def test_missing_base_dir_loads_none_without_raise(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=Path(tmp) / "never" / "made")
            self.assertIsNone(store.load())

    def test_save_creates_missing_base_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "data"
            store = LastAppliedStore(base_dir=target)
            store.save(_record())
            self.assertIsNotNone(store.load())

    def test_corrupt_json_loads_none_with_warning(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            store.path.write_text("{not json", encoding="utf-8")
            with self.assertLogs("zd_app.storage.last_applied_store", level="WARNING"):
                self.assertIsNone(store.load())

    def test_deeply_nested_json_loads_none_with_warning(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            store.path.write_text(_DEEP_JSON, encoding="utf-8")
            with self.assertLogs("zd_app.storage.last_applied_store", level="WARNING"):
                self.assertIsNone(store.load())

    def test_wrong_shape_loads_none_with_warning(self) -> None:
        cases = (
            json.dumps([1, 2, 3]),  # non-object root
            json.dumps({"profile_name": "x"}),  # missing snapshot / applied_at
            json.dumps(
                {
                    "profile_name": "x",
                    "applied_at": _ts(),
                    "failed_fields": "not-a-list",
                    "snapshot": {},
                }
            ),
            json.dumps(
                {
                    "profile_name": "x",
                    "applied_at": _ts(),
                    "failed_fields": [],
                    # codec-invalid snapshot: out-of-range step_size
                    "snapshot": {"step_size": 9999},
                }
            ),
        )
        for raw in cases:
            with self.subTest(raw=raw[:40]):
                with TemporaryDirectory() as tmp:
                    store = LastAppliedStore(base_dir=tmp)
                    store.path.write_text(raw, encoding="utf-8")
                    with self.assertLogs(
                        "zd_app.storage.last_applied_store", level="WARNING"
                    ):
                        self.assertIsNone(store.load())


class CodecShapeTests(unittest.TestCase):
    def test_record_to_dict_shape(self) -> None:
        payload = record_to_dict(_record())
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "profile_name",
                "applied_at",
                "include_device",
                "failed_fields",
                "snapshot",
            },
        )
        self.assertIsInstance(payload["failed_fields"], list)
        self.assertIsInstance(payload["snapshot"], dict)

    def test_from_dict_tolerates_missing_optional_flags(self) -> None:
        payload = record_to_dict(_record())
        del payload["include_device"]
        del payload["failed_fields"]
        parsed = record_from_dict(payload)
        self.assertFalse(parsed.include_device)
        self.assertEqual(parsed.failed_fields, ())


if __name__ == "__main__":  # pragma: no cover - manual driver
    unittest.main()
