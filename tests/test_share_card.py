"""Tests for the shareable evidence card renderer."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from zd_app import i18n
from zd_app.models import DeviceState
from zd_app.services import trust_self_check
from zd_app.services.compatibility_report import (
    CompatibilityChecklistItem,
    build_compatibility_report,
)
from zd_app.services.diagnostic_bundle import DiagnosticBundleService
from zd_app.services.share_card import (
    FORBIDDEN_OVERCLAIM_PHRASES,
    build_share_card,
)
from zd_app.services.wear_ledger import WearLedgerService
from zd_app.services.wear_ledger.models import (
    MODULE_CHARACTERIZED,
    READINESS_CHECK,
    SESSION_START,
)


_LOCALE_DIR = Path("zd_app/i18n/locales")
_NOW = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


class ShareCardRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_html_is_self_contained_without_script_or_remote_refs(self) -> None:
        card = build_share_card(now=_NOW)
        html = card.to_html()
        lowered = html.lower()

        self.assertNotRegex(lowered, r"https?://")
        self.assertNotIn("<script", lowered)
        self.assertNotRegex(lowered, r"\s(?:src|href)\s*=")
        self.assertNotIn("@import", lowered)
        self.assertNotIn("url(", lowered)

    def test_redacts_home_paths_and_instance_tails_in_html_and_markdown(self) -> None:
        state = DeviceState(
            product_name=r"ZD Ultimate Legend at C:\Users\Avery Stone\Desktop",
            stable_identifier=r"HID\VID_413D&PID_2104&MI_02\ABC123DEF456",
            connection_state="connected",
            last_read_time="2026-06-30T12:00:00+00:00",
        )
        card = build_share_card(
            device_state=state,
            variant=r"C:\Users\Avery Stone\Controller Variant",
            firmware="v1.24",
            recent_events=[
                (
                    r"Read C:\Users\Avery Stone\AppData\Roaming"
                    r"\ZDUltimateLegend\settings.json from "
                    r"HID\VID_413D&PID_2104&MI_02\ABC123DEF456"
                )
            ],
            diagnostic_bundle_path=(
                r"C:\Users\Avery Stone\AppData\Roaming"
                r"\ZDUltimateLegend\diagnostic_bundles\bundle.zip"
            ),
            now=_NOW,
        )

        for rendered in (card.to_html(), card.to_markdown()):
            with self.subTest(renderer=rendered[:20]):
                self.assertNotIn("Avery Stone", rendered)
                self.assertNotIn("ABC123DEF456", rendered)
                self.assertNotIn(r"C:\Users", rendered)

    def test_html_escapes_interpolated_metacharacters(self) -> None:
        compatibility = build_compatibility_report(
            device_state=DeviceState(
                product_name='ZD <Legend> & "Pad"',
                stable_identifier=r"HID\VID_413D&PID_2104&TAIL",
            ),
            variant='Variant <A & "B">',
            firmware='v1.24 <beta> & "quoted"',
            checklist=(
                CompatibilityChecklistItem(
                    'Read <settings> & "now"',
                    "read_ok",
                    'Observed <value> & "ok".',
                ),
            ),
            now=_NOW,
        )
        card = build_share_card(compatibility_report=compatibility, now=_NOW)
        html = card.to_html()

        self.assertIn("ZD &lt;Legend&gt; &amp; &quot;Pad&quot;", html)
        self.assertIn("Variant &lt;A &amp; &quot;B&quot;&gt;", html)
        self.assertIn("Read &lt;settings&gt; &amp; &quot;now&quot;", html)
        self.assertNotIn('ZD <Legend> & "Pad"', html)
        self.assertNotIn('Observed <value> & "ok".', html)

    def test_required_sections_and_bundle_posture_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            health_dir = root / "health_reports"
            health_dir.mkdir()
            md_path = health_dir / "zd_health_report_2026-06-30_120000.md"
            md_path.write_text("# Health Report\n", encoding="utf-8")
            md_path.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "overall_status": "normal",
                        "captured_at_utc": "2026-06-30T12:00:00Z",
                        "sample_count": 240,
                        "device": {"controller_name": "ZD Ultimate Legend"},
                    }
                ),
                encoding="utf-8",
            )
            ledger = WearLedgerService(base_dir=root / "ledger", utc_now=lambda: _NOW)
            ledger.append(SESSION_START, summary="session")
            ledger.append(
                READINESS_CHECK,
                summary="Readiness check: good",
                details={"verdict": "good"},
            )
            ledger.append(
                MODULE_CHARACTERIZED,
                summary="Module characterized: STOCK -> good",
                details={"overall_status": "good"},
            )
            bundle = DiagnosticBundleService(
                base_dir=root / "bundles",
                health_report_dir=health_dir,
                wear_ledger=ledger,
                utc_now=lambda: _NOW,
            )

            markdown = build_share_card(
                diagnostic_bundle_service=bundle,
                now=_NOW,
            ).to_markdown()

        for expected in (
            "## Trust posture",
            "## Device and configuration summary",
            "### Tested settings",
            "## Latest Health / Readiness / Module / Wear signals",
            "## Diagnostic Bundle posture",
            "### Deliberately not included",
            "## What this evidence can and cannot prove",
            "zd_health_report_2026-06-30_120000.md",
            "Readiness-check verdict counts",
            "Module evidence summary",
            "Raw event-timing log",
            "Raw app/crash logs",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, markdown)

    def test_forbidden_overclaim_phrases_are_absent(self) -> None:
        card = build_share_card(now=_NOW)
        lowered = (card.to_html() + card.to_markdown()).lower()

        for phrase in FORBIDDEN_OVERCLAIM_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, lowered)

    def test_static_no_network_import_scan_stays_clean(self) -> None:
        self.assertEqual(trust_self_check.scan_network_imports(Path("zd_app")), ())


class ShareCardI18nTests(unittest.TestCase):
    def test_en_zh_cn_have_matching_share_card_keys(self) -> None:
        en = json.loads((_LOCALE_DIR / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((_LOCALE_DIR / "zh-CN.json").read_text(encoding="utf-8"))

        en_keys = {
            key
            for key in en
            if key.startswith("share_card.")
            or key in {
                "log.diagnostics.share_card_saved",
                "log.diagnostics.share_card_failed",
            }
        }
        zh_keys = {
            key
            for key in zh
            if key.startswith("share_card.")
            or key in {
                "log.diagnostics.share_card_saved",
                "log.diagnostics.share_card_failed",
            }
        }

        self.assertEqual(en_keys, zh_keys)
        self.assertGreater(len(en_keys), 40)

    def test_share_card_locale_placeholders_match(self) -> None:
        en = json.loads((_LOCALE_DIR / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((_LOCALE_DIR / "zh-CN.json").read_text(encoding="utf-8"))

        keys = [
            key
            for key in en
            if key.startswith("share_card.")
            or key in {
                "log.diagnostics.share_card_saved",
                "log.diagnostics.share_card_failed",
            }
        ]
        for key in keys:
            with self.subTest(key=key):
                self.assertEqual(
                    set(_placeholders(en[key])),
                    set(_placeholders(zh[key])),
                )


def _placeholders(value: str) -> list[str]:
    return re.findall(r"{([^{}]+)}", value)


if __name__ == "__main__":
    unittest.main()
