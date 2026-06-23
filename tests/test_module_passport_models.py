"""Tests for :mod:`zd_app.storage.module_passport_models`.

JSON round-trip coverage, sanitiser behaviour, schema-version handling,
and read-back validation (mappings, expected types, side membership).
"""

from __future__ import annotations

import json
import unittest

from zd_app.storage.module_passport_models import (
    MAX_MODULE_ID_LEN,
    MAX_MODULE_NOTES_LEN,
    ModuleFingerprint,
    ModulePassport,
    SCHEMA_VERSION,
    SIDES,
    SIDE_LEFT,
    SIDE_RIGHT,
    STATUS_GOOD,
    STATUS_WATCH,
    STATUS_WEAR_OBSERVED,
    SUPPORTED_SCHEMA_VERSIONS,
    module_passport_from_dict,
    module_passport_to_dict,
    sanitize_module_id,
    sanitize_module_notes,
)


def _fingerprint(**overrides) -> ModuleFingerprint:
    base = dict(
        timestamp_utc="2026-05-26T18:42:11Z",
        side=SIDE_LEFT,
        duration_ms=60_000,
        samples_count=120_000,
        noise_floor_percent=1.4,
        centering_offset_x=0.2,
        centering_offset_y=-0.1,
        circularity_coverage_percent=0.97,
        outer_deadzone_min_axis=96.0,
        outer_deadzone_max_axis=99.0,
        asymmetry_score=0.04,
        bitness_observed=128,
        tremor_metric=0.18,
        linearity_score=0.05,
        overall_status=STATUS_GOOD,
        notes=None,
    )
    base.update(overrides)
    return ModuleFingerprint(**base)


def _passport(side: str = SIDE_LEFT, *, fingerprints=()) -> ModulePassport:
    return ModulePassport(
        side=side,
        module_id="STOCK_BASELINE",
        assigned_at_utc="2026-05-26T18:00:00Z",
        notes="Baseline reference module.",
        fingerprints=tuple(fingerprints),
    )


class ModuleFingerprintRoundTripTests(unittest.TestCase):
    def test_to_dict_keys_and_types(self) -> None:
        fp = _fingerprint()
        payload = fp.to_dict()
        self.assertEqual(payload["timestamp_utc"], "2026-05-26T18:42:11Z")
        self.assertEqual(payload["side"], SIDE_LEFT)
        self.assertEqual(payload["overall_status"], STATUS_GOOD)
        self.assertEqual(payload["bitness_observed"], 128)
        self.assertIsNone(payload["notes"])
        self.assertIsInstance(payload["noise_floor_percent"], float)
        self.assertIsInstance(payload["samples_count"], int)

    def test_round_trip_preserves_all_fields(self) -> None:
        fp = _fingerprint(overall_status=STATUS_WEAR_OBSERVED, notes="some note")
        again = ModuleFingerprint.from_dict(fp.to_dict())
        self.assertEqual(again, fp)
        self.assertEqual(again.notes, "some note")

    def test_from_dict_rejects_non_str_timestamp(self) -> None:
        payload = _fingerprint().to_dict()
        payload["timestamp_utc"] = None
        with self.assertRaises(ValueError):
            ModuleFingerprint.from_dict(payload)

    def test_from_dict_rejects_non_str_side(self) -> None:
        payload = _fingerprint().to_dict()
        payload["side"] = 0
        with self.assertRaises(ValueError):
            ModuleFingerprint.from_dict(payload)

    def test_from_dict_rejects_out_of_range_side(self) -> None:
        # A side that isn't a canonical SIDES value (e.g. a traversal string)
        # must be rejected — side flows into the on-disk archive filename.
        for bad_side in ("../../evil", "middle", "LEFT"):
            with self.subTest(side=bad_side):
                payload = _fingerprint().to_dict()
                payload["side"] = bad_side
                with self.assertRaises(ValueError):
                    ModuleFingerprint.from_dict(payload)

    def test_from_dict_rejects_non_str_overall_status(self) -> None:
        payload = _fingerprint().to_dict()
        payload["overall_status"] = 1
        with self.assertRaises(ValueError):
            ModuleFingerprint.from_dict(payload)

    def test_from_dict_rejects_non_str_notes(self) -> None:
        payload = _fingerprint().to_dict()
        payload["notes"] = 42
        with self.assertRaises(ValueError):
            ModuleFingerprint.from_dict(payload)

    def test_from_dict_coerces_numeric_types(self) -> None:
        payload = _fingerprint().to_dict()
        # All numeric fields arrive as strings (simulated migration drift).
        payload["duration_ms"] = "12000"
        payload["samples_count"] = "999"
        payload["noise_floor_percent"] = "2.5"
        fp = ModuleFingerprint.from_dict(payload)
        self.assertEqual(fp.duration_ms, 12000)
        self.assertEqual(fp.samples_count, 999)
        self.assertAlmostEqual(fp.noise_floor_percent, 2.5, places=3)


class ModulePassportRoundTripTests(unittest.TestCase):
    def test_minimal_round_trip(self) -> None:
        passport = _passport()
        again = ModulePassport.from_dict(passport.to_dict())
        self.assertEqual(again, passport)
        self.assertEqual(again.schema_version, SCHEMA_VERSION)
        self.assertEqual(again.fingerprints, ())

    def test_round_trip_with_fingerprints(self) -> None:
        fp_a = _fingerprint(timestamp_utc="2026-05-26T18:42:11Z")
        fp_b = _fingerprint(
            timestamp_utc="2026-05-26T19:00:00Z",
            overall_status=STATUS_WATCH,
        )
        passport = _passport(fingerprints=[fp_a, fp_b])
        again = ModulePassport.from_dict(passport.to_dict())
        self.assertEqual(again.fingerprints, (fp_a, fp_b))

    def test_latest_fingerprint_returns_last(self) -> None:
        fp_a = _fingerprint(timestamp_utc="2026-05-26T18:00:00Z")
        fp_b = _fingerprint(timestamp_utc="2026-05-26T19:00:00Z")
        passport = _passport(fingerprints=[fp_a, fp_b])
        self.assertEqual(passport.latest_fingerprint(), fp_b)
        self.assertIsNone(_passport().latest_fingerprint())

    def test_with_fingerprint_returns_new_passport(self) -> None:
        passport = _passport()
        fp = _fingerprint()
        new = passport.with_fingerprint(fp)
        self.assertEqual(new.fingerprints, (fp,))
        self.assertEqual(passport.fingerprints, ())  # original untouched

    def test_with_notes_returns_new_passport(self) -> None:
        passport = _passport()
        new = passport.with_notes("Replaced notes")
        self.assertEqual(new.notes, "Replaced notes")
        self.assertEqual(passport.notes, "Baseline reference module.")

    def test_from_dict_rejects_missing_side(self) -> None:
        payload = _passport().to_dict()
        payload["side"] = None
        with self.assertRaises(ValueError):
            ModulePassport.from_dict(payload)

    def test_from_dict_rejects_out_of_range_side(self) -> None:
        # A non-canonical side (traversal string / unknown side) flows into the
        # archive path, so it must be rejected at the load boundary.
        for bad_side in ("../../evil", "middle", "LEFT"):
            with self.subTest(side=bad_side):
                payload = _passport().to_dict()
                payload["side"] = bad_side
                with self.assertRaises(ValueError):
                    ModulePassport.from_dict(payload)

    def test_from_dict_rejects_missing_module_id(self) -> None:
        payload = _passport().to_dict()
        del payload["module_id"]
        with self.assertRaises(ValueError):
            ModulePassport.from_dict(payload)

    def test_from_dict_rejects_unsupported_schema_version(self) -> None:
        payload = _passport().to_dict()
        payload["schema_version"] = 999
        with self.assertRaises(ValueError):
            ModulePassport.from_dict(payload)

    def test_from_dict_treats_missing_notes_as_empty(self) -> None:
        payload = _passport().to_dict()
        payload.pop("notes")
        # Notes is optional in the persisted shape; an absent key defaults
        # to "" so a legacy file with no notes still loads cleanly.
        passport = ModulePassport.from_dict(payload)
        self.assertEqual(passport.notes, "")

    def test_from_dict_rejects_non_str_notes(self) -> None:
        payload = _passport().to_dict()
        payload["notes"] = 7
        with self.assertRaises(ValueError):
            ModulePassport.from_dict(payload)

    def test_from_dict_rejects_non_mapping_fingerprints_entry(self) -> None:
        payload = _passport(fingerprints=[_fingerprint()]).to_dict()
        payload["fingerprints"] = ["not-a-dict"]
        with self.assertRaises(ValueError):
            ModulePassport.from_dict(payload)

    def test_module_passport_to_dict_helper_matches_method(self) -> None:
        passport = _passport(fingerprints=[_fingerprint()])
        self.assertEqual(module_passport_to_dict(passport), passport.to_dict())

    def test_module_passport_from_dict_helper_matches_class_method(self) -> None:
        passport = _passport()
        self.assertEqual(
            module_passport_from_dict(passport.to_dict()),
            ModulePassport.from_dict(passport.to_dict()),
        )

    def test_json_round_trip_through_dumps_loads(self) -> None:
        passport = _passport(fingerprints=[_fingerprint(), _fingerprint(overall_status=STATUS_WATCH)])
        serialized = json.dumps(passport.to_dict(), sort_keys=True)
        deserialized = json.loads(serialized)
        again = ModulePassport.from_dict(deserialized)
        self.assertEqual(again, passport)


class SanitizeModuleIdTests(unittest.TestCase):
    def test_empty_returns_empty(self) -> None:
        self.assertEqual(sanitize_module_id(""), "")
        self.assertEqual(sanitize_module_id("   "), "")

    def test_trims_and_keeps_simple(self) -> None:
        self.assertEqual(sanitize_module_id("  STOCK_LEFT  "), "STOCK_LEFT")

    def test_strips_control_chars(self) -> None:
        self.assertEqual(sanitize_module_id("STOCK\x00LEFT"), "STOCKLEFT")

    def test_collapses_internal_whitespace_to_single_space(self) -> None:
        self.assertEqual(
            sanitize_module_id("K  silver\tmod\nv1"),
            "K silver mod v1",
        )

    def test_caps_at_max_id_len(self) -> None:
        long = "a" * (MAX_MODULE_ID_LEN + 50)
        result = sanitize_module_id(long)
        self.assertEqual(len(result), MAX_MODULE_ID_LEN)


class SanitizeModuleNotesTests(unittest.TestCase):
    def test_empty_returns_empty(self) -> None:
        self.assertEqual(sanitize_module_notes(""), "")

    def test_strips_carriage_returns(self) -> None:
        self.assertEqual(sanitize_module_notes("line1\r\nline2"), "line1\nline2")

    def test_collapses_multiple_blank_lines(self) -> None:
        result = sanitize_module_notes("a\n\n\n\nb")
        self.assertEqual(result, "a\n\nb")

    def test_strips_control_chars(self) -> None:
        self.assertEqual(sanitize_module_notes("a\x00b"), "ab")

    def test_caps_at_max_notes_len(self) -> None:
        long = "x" * (MAX_MODULE_NOTES_LEN + 100)
        self.assertEqual(len(sanitize_module_notes(long)), MAX_MODULE_NOTES_LEN)


class ConstantsTests(unittest.TestCase):
    def test_sides_tuple_matches_known(self) -> None:
        self.assertEqual(set(SIDES), {SIDE_LEFT, SIDE_RIGHT})

    def test_schema_version_in_supported_set(self) -> None:
        self.assertIn(SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS)

    def test_status_labels_are_distinct(self) -> None:
        self.assertEqual(
            len({STATUS_GOOD, STATUS_WATCH, STATUS_WEAR_OBSERVED}),
            3,
        )


if __name__ == "__main__":
    unittest.main()
