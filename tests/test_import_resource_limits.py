"""Bound classifier work without weakening normal Safe Import behavior."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zd_app.services import import_classifier as classifier
from zd_app.storage._import_guards import MAX_IMPORT_BYTES, MAX_IMPORT_JSON_DEPTH
from zd_app.ui import safe_import_model as model


def _profile() -> dict:
    return {
        "schema_version": 1,
        "name": "Resource limit control",
        "created_at": "2026-09-18",
        "last_modified_at": "2026-09-18",
        "snapshot": {},
    }


class ImportResourceLimitsTests(unittest.TestCase):
    def assert_rejected(self, raw: dict) -> None:
        result = classifier.classify_import(raw, existing_names=set())
        self.assertFalse(result.ok)
        self.assertTrue(result.resource_limited)
        self.assertIsNone(result.profile)
        self.assertEqual(result.generated_name, "")
        self.assertFalse(result.warnings)
        self.assertFalse(result.blocked_fields)
        self.assertFalse(result.unknown_fields)
        self.assertTrue(all(not fields for fields in result.categories.values()))

    def test_small_file_with_repeated_ancestry_is_rejected_before_preview(self) -> None:
        raw = _profile()
        nested = [0] * 1024
        for _ in range(4):
            nested = {"vendor" + "x" * 190: nested}
        raw["metadata"] = nested
        serialized = json.dumps(raw)
        self.assertLess(len(serialized.encode("utf-8")), MAX_IMPORT_BYTES)
        self.assert_rejected(raw)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "profile.json"
            source.write_text(serialized, encoding="utf-8")
            result = model.prepare_import(str(source), existing_names=set())
        self.assertFalse(result.ok)
        self.assertIsNone(result.profile)
        self.assertEqual(result.error_key, "safe_import.error.resource_limited")
        self.assertEqual(result.audit.source_filename, "profile.json")
        self.assertEqual(result.audit.blocked_field_names, [])

    def test_array_scalars_count_towards_node_budget(self) -> None:
        raw = _profile()
        # Root, five profile values, and the x array account for seven nodes.
        raw["x"] = [0] * (classifier.MAX_IMPORT_NODES - 7)
        self.assertTrue(classifier.classify_import(raw, existing_names=set()).ok)
        raw["x"].append(0)
        self.assert_rejected(raw)

    def test_key_limit_checked_before_codec_or_pattern_matching(self) -> None:
        raw = _profile()
        raw["x" * (classifier.MAX_IMPORT_KEY_CHARS + 1)] = 0
        with (
            patch.object(classifier.WrapperProfile, "from_dict", side_effect=AssertionError("codec ran")),
            patch.object(classifier, "_matches", side_effect=AssertionError("regex ran")),
        ):
            self.assert_rejected(raw)
            result = model.classify_import(raw, existing_names=set())
        self.assertFalse(result.ok)
        self.assertEqual(result.error_key, "safe_import.error.resource_limited")

    def test_exact_key_limit_is_accepted(self) -> None:
        raw = _profile()
        key = "x" * classifier.MAX_IMPORT_KEY_CHARS
        raw[key] = 0
        result = classifier.classify_import(raw, existing_names=set())
        self.assertTrue(result.ok)
        self.assertIn(key, result.unknown_fields)

    def test_qualified_path_limit(self) -> None:
        raw = _profile()
        nested = 0
        for _ in range(5):
            nested = {"x" * 220: nested}
        raw["vendor"] = nested
        self.assert_rejected(raw)

    def test_container_depth_matches_file_guard(self) -> None:
        raw = _profile()
        nested = []
        for _ in range(MAX_IMPORT_JSON_DEPTH - 2):
            nested = [nested]
        raw["vendor"] = nested
        self.assertTrue(classifier.classify_import(raw, existing_names=set()).ok)
        raw["vendor"] = [nested]
        self.assert_rejected(raw)

    def test_direct_call_cycles_terminate_without_partial_result(self) -> None:
        raw = _profile()
        raw["cycle"] = raw
        self.assert_rejected(raw)

    def test_many_diagnostics_are_rejected_without_partial_audit(self) -> None:
        raw = _profile()
        raw.update({"macro_" + str(i) + "x" * 190: True for i in range(400)})
        self.assertLess(len(json.dumps(raw)), MAX_IMPORT_BYTES)
        self.assert_rejected(raw)

    def test_normal_unknowns_and_blocked_fields_keep_existing_semantics(self) -> None:
        raw = _profile()
        raw["vendor"] = {"notes": ["ordinary metadata", {"turbo": True}]}
        result = classifier.classify_import(raw, existing_names=set())
        self.assertFalse(result.resource_limited)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.profile)
        self.assertEqual(result.blocked_fields, ["vendor.notes[1].turbo"])
        self.assertEqual(result.blocked_automation_count, 1)
        ui_result = model.classify_import(raw, existing_names=set())
        self.assertTrue(ui_result.ok)
        self.assertNotIn("vendor", ui_result.profile.to_dict())


if __name__ == "__main__":
    unittest.main()
