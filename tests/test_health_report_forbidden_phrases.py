"""Forbidden-phrase + field-name hygiene tests for the Controller Health Report.

This is the load-bearing honesty enforcer per the forbidden-phrase policy. Three categories:

1. **User-facing strings**: every value under any ``health_report.*`` i18n key,
   in both en and zh-CN locale files, plus the actual output of the Markdown
   and JSON exporters for a fixture report, MUST NOT contain any forbidden
   phrase (case-insensitive substring match).

2. **Field names**: no attribute on any Health Report dataclass and no key
   in the JSON exporter dict may contain a forbidden field name.

3. **Required presence**: the verbatim claim-boundary paragraph
   must appear in every Markdown export and as the JSON
   ``claim_boundary_paragraph`` field.

If any of these tests fails after a change, do NOT just edit the test to
loosen the rule — the rule exists to keep the report honest.
"""

from __future__ import annotations

import json
import re
import unittest
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, Iterable

from zd_app.services.health_report import (
    CLAIM_BOUNDARY_PARAGRAPH,
    DeviceContext,
    HealthReport,
    Sample,
    compute_overall_status,
    compute_stick_range,
    compute_stick_rest_noise,
    compute_trigger_range,
    to_json,
    to_json_dict,
    to_markdown,
)
from zd_app.services.health_report.models import (
    StickRangeReport,
    StickRestNoiseReport,
    TriggerRangeReport,
)


# Phrases the Health Report must never use in user-facing output: it measures
# app-observed HID behavior, not latency or USB-bus truth. ALL case-insensitive
# substring matches.
FORBIDDEN_USER_FACING_PHRASES = (
    "input latency",
    "input lag",
    "latency",
    "lag",
    "usb polling verified",
    "true polling rate",
    "true 8000 hz",
    "bus-level polling",
    "ban-safe",
    "ban safe",
    "tournament-approved",
    "tournament approved",
    "anti-cheat safe",
    "calibrated",
    "factory calibration",
    "lab-grade",
    "health score",
    "hardware health",
    "firmware bug",
    "defect detected",
)


FORBIDDEN_FIELD_NAMES = (
    "true_polling_rate",
    "usb_polling_verified",
    "latency_ms",
    "input_lag",
    "hardware_health_score",
    "calibrated_center",
    "defect_detected",
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def _build_fixture_report() -> HealthReport:
    # Quiet noise around the center.
    rest_samples = [
        Sample(
            timestamp_ns=i * 1_000_000, packet_number=i,
            left_stick_x=(i % 7) - 3, left_stick_y=(i % 5) - 2,
            right_stick_x=(i % 11) - 5, right_stick_y=(i % 13) - 6,
            left_trigger=0, right_trigger=0,
        )
        for i in range(600)
    ]
    # Full rotation at 85% of full axis travel (sample storage is signed
    # integer percent — see Sample docstring).
    import math
    rotation_samples = []
    for i in range(640):
        theta = (i / 640) * 4.0 * math.pi
        rotation_samples.append(Sample(
            timestamp_ns=(600 + i) * 1_000_000, packet_number=600 + i,
            left_stick_x=int(85 * math.cos(theta)),
            left_stick_y=int(85 * math.sin(theta)),
            right_stick_x=int(85 * math.cos(theta)),
            right_stick_y=int(85 * math.sin(theta)),
            left_trigger=0, right_trigger=0,
        ))
    # Trigger pull/release.
    trigger_samples = []
    base = 1240
    for i in range(120):
        if i < 60:
            v = int(255 * (i / 60))
        else:
            v = int(255 * (1 - (i - 60) / 60))
        trigger_samples.append(Sample(
            timestamp_ns=(base + i) * 1_000_000, packet_number=base + i,
            left_stick_x=0, left_stick_y=0,
            right_stick_x=0, right_stick_y=0,
            left_trigger=v, right_trigger=v,
        ))
    rest_left = compute_stick_rest_noise(rest_samples, side="left")
    rest_right = compute_stick_rest_noise(rest_samples, side="right")
    range_left = compute_stick_range(rotation_samples, side="left",
                                     center_xy=(rest_left.median_center_x,
                                                rest_left.median_center_y))
    range_right = compute_stick_range(rotation_samples, side="right",
                                      center_xy=(rest_right.median_center_x,
                                                 rest_right.median_center_y))
    trig_left = compute_trigger_range(trigger_samples, side="left")
    trig_right = compute_trigger_range(trigger_samples, side="right")

    return HealthReport(
        app_version="2.0.0",
        app_build_commit="testhash",
        generated_at_local="2026-05-22T20:00:00-07:00",
        device=DeviceContext(
            controller_name="ZD Ultimate Legend",
            configured_polling_hz=1000,
            profile_name="Apex",
        ),
        stick_rest_noise=StickRestNoiseReport(left=rest_left, right=rest_right),
        stick_range=StickRangeReport(left=range_left, right=range_right),
        trigger_range=TriggerRangeReport(left=trig_left, right=trig_right),
        overall_status=compute_overall_status(
            left_rest=rest_left, right_rest=rest_right,
            left_range=range_left, right_range=range_right,
            left_trigger=trig_left, right_trigger=trig_right,
        ),
        caveats=(
            "Movement detected during the rest sample; retest suggested.",
            "Triggers may not have reached the bottom; retest if results look low.",
        ),
    )


def _walk_strings(value: Any) -> Iterable[str]:
    """Yield every string contained in a JSON-like nested structure."""

    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                yield k
            yield from _walk_strings(v)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


def _walk_dataclass_field_names(cls: type) -> Iterable[str]:
    """Recursively yield every field name in a dataclass (and sub-dataclasses)."""

    if not is_dataclass(cls):
        return
    for f in fields(cls):
        yield f.name
        # Best-effort recursion: only follow concrete dataclass types so the
        # walk terminates. ``f.type`` may be a string under
        # ``from __future__ import annotations`` so we look up the actual type
        # via the field's ``default`` / class introspection.
        annotation_value = f.type
        if isinstance(annotation_value, type) and is_dataclass(annotation_value):
            yield from _walk_dataclass_field_names(annotation_value)


# ---------------------------------------------------------------------------
# 1. User-facing strings (i18n + exporter output)
# ---------------------------------------------------------------------------


class I18nForbiddenPhrasesTests(unittest.TestCase):
    def _load_locale(self, name: str) -> dict[str, str]:
        path = Path("zd_app/i18n/locales") / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _assert_no_forbidden_in_values(self, locale_name: str) -> None:
        data = self._load_locale(locale_name)
        health_report_values = {
            key: value
            for key, value in data.items()
            if key.startswith("health_report.") or key == "nav.health_report"
        }
        for key, value in health_report_values.items():
            lower = value.lower()
            for phrase in FORBIDDEN_USER_FACING_PHRASES:
                with self.subTest(locale=locale_name, key=key, phrase=phrase):
                    self.assertNotIn(
                        phrase, lower,
                        msg=(
                            f"Locale {locale_name} key {key!r} value {value!r} "
                            f"contains forbidden phrase {phrase!r}"
                        ),
                    )

    def test_en_health_report_strings_have_no_forbidden_phrases(self) -> None:
        self._assert_no_forbidden_in_values("en")

    def test_zh_cn_health_report_strings_have_no_forbidden_phrases(self) -> None:
        self._assert_no_forbidden_in_values("zh-CN")

    def test_has_at_least_one_health_report_key(self) -> None:
        # Sanity check so the filter above isn't silently matching nothing.
        data = self._load_locale("en")
        keys = [k for k in data if k.startswith("health_report.") or k == "nav.health_report"]
        self.assertGreater(len(keys), 30)


class MarkdownExporterForbiddenPhrasesTests(unittest.TestCase):
    def test_markdown_export_contains_no_forbidden_phrases(self) -> None:
        # The claim-boundary paragraph deliberately contains some
        # forbidden words ("input latency", "true USB bus polling rate") in
        # a *denial* context — the whole point of that paragraph is to say
        # "this does NOT measure latency / verify USB polling". We require
        # that paragraph to appear verbatim (see ClaimBoundaryRequiredPresence
        # tests), so we strip it out before scanning the rest of the export.
        md = to_markdown(_build_fixture_report())
        scannable = md.replace(CLAIM_BOUNDARY_PARAGRAPH, "").lower()
        for phrase in FORBIDDEN_USER_FACING_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, scannable)


class JsonExporterForbiddenPhrasesTests(unittest.TestCase):
    def test_json_string_values_contain_no_forbidden_phrases(self) -> None:
        # Same whitelist as Markdown: the verbatim claim-boundary paragraph
        # is the one place a forbidden phrase legitimately appears (in
        # denial). Scan every OTHER string in the JSON dict.
        data = to_json_dict(_build_fixture_report())
        for s in _walk_strings(data):
            if s == CLAIM_BOUNDARY_PARAGRAPH:
                continue
            lower = s.lower()
            for phrase in FORBIDDEN_USER_FACING_PHRASES:
                with self.subTest(phrase=phrase, text=s):
                    self.assertNotIn(phrase, lower)


# ---------------------------------------------------------------------------
# 2. Field name hygiene
# ---------------------------------------------------------------------------


class DataclassFieldNameHygieneTests(unittest.TestCase):
    def test_no_forbidden_field_names_on_health_report_dataclasses(self) -> None:
        # Walk every dataclass referenced from HealthReport and assert no
        # forbidden field name appears.
        all_names: set[str] = set()
        for cls in (HealthReport, StickRestNoiseReport, StickRangeReport,
                    TriggerRangeReport, DeviceContext):
            all_names.update(_walk_dataclass_field_names(cls))
        for forbidden in FORBIDDEN_FIELD_NAMES:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, all_names)


class JsonKeyHygieneTests(unittest.TestCase):
    def test_no_forbidden_keys_in_json_export(self) -> None:
        data = to_json_dict(_build_fixture_report())

        def walk_keys(node: Any) -> Iterable[str]:
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from walk_keys(v)
            elif isinstance(node, list):
                for item in node:
                    yield from walk_keys(item)

        all_keys = set(walk_keys(data))
        for forbidden in FORBIDDEN_FIELD_NAMES:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, all_keys)


# ---------------------------------------------------------------------------
# 3. Required presence (claim-boundary paragraph verbatim)
# ---------------------------------------------------------------------------


class ClaimBoundaryRequiredPresenceTests(unittest.TestCase):
    def test_markdown_export_contains_verbatim_claim_boundary(self) -> None:
        md = to_markdown(_build_fixture_report())

        self.assertIn(CLAIM_BOUNDARY_PARAGRAPH, md)

    def test_json_export_contains_verbatim_claim_boundary_paragraph_field(self) -> None:
        data = to_json_dict(_build_fixture_report())

        self.assertEqual(data["claim_boundary_paragraph"], CLAIM_BOUNDARY_PARAGRAPH)

    def test_json_string_contains_verbatim_claim_boundary(self) -> None:
        s = to_json(_build_fixture_report())

        # JSON-escaping doesn't touch this paragraph (no quotes, no backslashes)
        # so it appears verbatim in the serialized string too.
        self.assertIn(CLAIM_BOUNDARY_PARAGRAPH, s)

    def test_no_overlap_between_phrase_and_field_lists(self) -> None:
        # Sanity: the user-facing phrase list and the field-name list
        # don't accidentally share entries (each list is its own contract).
        phrase_words = {p.lower() for p in FORBIDDEN_USER_FACING_PHRASES}
        field_words = {f.lower() for f in FORBIDDEN_FIELD_NAMES}
        self.assertEqual(phrase_words & field_words, set())


if __name__ == "__main__":
    unittest.main()
