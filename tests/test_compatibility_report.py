"""Tests for the share-safe compatibility report packet."""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path

from zd_app import i18n
from zd_app.models import DeviceState
from zd_app.services import trust_self_check
from zd_app.services.compatibility_report import (
    ISSUE_TEMPLATE_FIELD_IDS,
    CompatibilityChecklistItem,
    build_compatibility_report,
)


_LOCALE_DIR = Path("zd_app/i18n/locales")
_NOW = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


class CompatibilityReportRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_redacts_device_tail_and_local_paths(self) -> None:
        state = DeviceState(
            product_name="ZD Ultimate Legend",
            stable_identifier=r"HID\VID_413D&PID_2104&MI_02\ABC123DEF456",
            connection_state="connected",
            data_freshness="write_success",
            last_read_time="2026-06-30T12:00:00+00:00",
            last_apply_time="2026-06-30T12:01:00+00:00",
            xinput_slot=0,
        )

        report = build_compatibility_report(
            device_state=state,
            variant=r"C:\Users\Avery Stone\Variant|X",
            firmware=r"v1.24 `[demo]`",
            last_read_duration_ms=3.25,
            last_write_duration_ms=4.5,
            recent_events=[
                (
                    r"Read path C:\Users\Avery Stone\AppData\Roaming\ZDUltimateLegend\settings.json "
                    r"from HID\VID_413D&PID_2104&MI_02\ABC123DEF456"
                )
            ],
            diagnostic_bundle_path=(
                r"C:\Users\Avery Stone\AppData\Roaming"
                r"\ZDUltimateLegend\diagnostic_bundles\bundle.zip"
            ),
            now=_NOW,
        )

        combined = report.to_markdown() + report.to_issue_body()
        self.assertNotIn("Avery Stone", combined)
        self.assertNotIn("ABC123DEF456", combined)
        self.assertIn(r"HID\VID_413D&PID_2104", combined)
        self.assertIn(r"Variant\|X", combined)
        self.assertIn(r"v1.24 \`\[demo\]\`", combined)
        self.assertIn("bundle.zip", combined)

    def test_required_fields_and_checklist_states_render(self) -> None:
        report = build_compatibility_report(
            device_state=DeviceState(stable_identifier=r"USB\VID_413D&PID_2104\TAIL"),
            checklist=(
                CompatibilityChecklistItem("Read", "read_ok", "Observed."),
                CompatibilityChecklistItem("Write", "write_ok", "Observed."),
                CompatibilityChecklistItem("Paddles", "sent_not_verified", "Sent."),
                CompatibilityChecklistItem("Lighting", "failed", "Failed."),
                CompatibilityChecklistItem("Live Verify", "not_tried", "No run."),
            ),
            windows_version="Windows 11 test build",
            now=_NOW,
        )

        text = report.to_markdown()
        for expected in (
            "LegendCTL version",
            "Build commit",
            "Windows 11 test build",
            r"USB\VID_413D&PID_2104",
            "read OK",
            "write OK",
            "sent-not-verified",
            "failed",
            "not tried",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)

    def test_read_only_live_verify_report_is_valid_and_limited(self) -> None:
        state = DeviceState(
            device_class="generic_xinput",
            connection_state="connected",
            stable_identifier="xinput-slot-1",
            xinput_slot=1,
        )
        report = build_compatibility_report(device_state=state, now=_NOW)
        body = report.to_issue_body()

        self.assertIn("Limited report", body)
        self.assertIn("Live Verify", body)
        self.assertIn("read OK", body)
        self.assertIn("No settings write was recorded", body)

    def test_no_controller_does_not_claim_live_verify_available(self) -> None:
        report = build_compatibility_report(device_state=DeviceState(), now=_NOW)
        body = report.to_issue_body()

        self.assertIn("The app did not detect my controller", body)
        self.assertIn("Not observed - no controller detected.", body)

    def test_forbidden_overclaim_phrases_are_absent(self) -> None:
        report = build_compatibility_report(
            device_state=DeviceState(
                stable_identifier=r"HID\VID_413D&PID_2104\TAIL",
                xinput_slot=0,
            ),
            now=_NOW,
        )
        lowered = (report.to_markdown() + report.to_issue_body()).lower()

        for phrase in (
            "certified",
            "approved",
            "tournament-safe",
            "vendor-verified",
            "guaranteed",
        ):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, lowered)

    def test_issue_body_field_set_aligns_with_template(self) -> None:
        template = Path(".github/ISSUE_TEMPLATE/compatibility_report.yml").read_text(
            encoding="utf-8"
        )
        ids = tuple(re.findall(r"^\s+id:\s*([A-Za-z0-9_-]+)\s*$", template, re.M))

        self.assertEqual(ids, ISSUE_TEMPLATE_FIELD_IDS)

        body = build_compatibility_report(now=_NOW).to_issue_body()
        for label in (
            "LegendCTL / app version",
            "Windows version",
            "Controller VID/PID + variant + firmware",
            "Overall result",
            "What did you test, and what happened?",
            "Did Live Verify (the live stick / circularity view) work?",
            "Log file / Diagnostic Bundle",
            "Anything else?",
        ):
            with self.subTest(label=label):
                self.assertIn(f"### {label}", body)

    def test_static_no_network_import_scan_stays_clean(self) -> None:
        self.assertEqual(trust_self_check.scan_network_imports(Path("zd_app")), ())


class CompatibilityReportI18nTests(unittest.TestCase):
    def test_en_zh_cn_have_matching_compat_report_keys(self) -> None:
        en = json.loads((_LOCALE_DIR / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((_LOCALE_DIR / "zh-CN.json").read_text(encoding="utf-8"))

        en_keys = {key for key in en if key.startswith("compat_report.")}
        zh_keys = {key for key in zh if key.startswith("compat_report.")}

        self.assertEqual(en_keys, zh_keys)
        self.assertGreater(len(en_keys), 70)

    def test_compat_report_locale_placeholders_match(self) -> None:
        en = json.loads((_LOCALE_DIR / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((_LOCALE_DIR / "zh-CN.json").read_text(encoding="utf-8"))

        for key in (k for k in en if k.startswith("compat_report.")):
            en_placeholders = set(_placeholders(en[key]))
            zh_placeholders = set(_placeholders(zh[key]))
            with self.subTest(key=key):
                self.assertEqual(en_placeholders, zh_placeholders)


def _placeholders(value: str) -> list[str]:
    parts = []
    cursor = 0
    while True:
        start = value.find("{", cursor)
        if start == -1:
            return parts
        end = value.find("}", start)
        if end == -1:
            return parts
        parts.append(value[start + 1:end])
        cursor = end + 1


if __name__ == "__main__":
    unittest.main()
