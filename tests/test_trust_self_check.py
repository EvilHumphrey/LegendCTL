"""Tests for the Diagnostics Trust Self-Check evidence artifact."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from zd_app import i18n
from zd_app.services import trust_self_check


_LOCALE_DIR = Path("zd_app/i18n/locales")
_NOW = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


class TrustSelfCheckEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_claim_wording_and_boundary_are_present(self) -> None:
        result = trust_self_check.build_trust_self_check(now=_NOW)
        text = result.to_text()

        self.assertIn(
            "No network: this build imports no networking modules.",
            text,
        )
        self.assertIn(
            "Observed for THIS process THIS session - not a system-wide audit.",
            text,
        )

    def test_static_no_network_scan_is_clean_and_webbrowser_is_separate(self) -> None:
        result = trust_self_check.build_trust_self_check(now=_NOW)

        self.assertEqual(result.network_import_findings, ())
        self.assertTrue(
            any(handoff.call == "webbrowser.open" for handoff in result.browser_handoffs),
            "About screen browser handoff should be named separately.",
        )
        markdown = result.to_markdown()
        self.assertIn("Static scan of zd_app found 0 imports", markdown)
        self.assertIn("webbrowser.open", markdown)
        self.assertIn("not LegendCTL telemetry", markdown)

    def test_path_output_uses_env_placeholders_without_raw_home_path(self) -> None:
        env = {
            "APPDATA": r"C:\Users\Avery Stone\AppData\Roaming",
            "LOCALAPPDATA": r"C:\Users\Avery Stone\AppData\Local",
        }
        with patch.dict(os.environ, env, clear=False):
            result = trust_self_check.build_trust_self_check(
                executable_path=(
                    r"C:\Users\Avery Stone\AppData\Local\Programs"
                    r"\LegendCTL\ZD Ultimate Legend.exe"
                ),
                user_data_dir=(
                    r"C:\Users\Avery Stone\AppData\Roaming\ZDUltimateLegend"
                ),
                frozen=True,
                now=_NOW,
            )

        combined = result.to_text() + result.to_markdown()
        self.assertNotIn("Avery Stone", combined)
        self.assertIn(r"%APPDATA%\ZDUltimateLegend", combined)
        self.assertIn(
            r"%LOCALAPPDATA%\Programs\LegendCTL\ZD Ultimate Legend.exe",
            combined,
        )

    def test_forbidden_overclaim_phrases_are_absent(self) -> None:
        result = trust_self_check.build_trust_self_check(now=_NOW)
        lowered = result.to_text().lower() + result.to_markdown().lower()

        for phrase in (
            "guaranteed",
            "malware",
            "anti-cheat",
            "system clean",
            "pii-free",
            "safe for every game",
        ):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, lowered)

    def test_markdown_renderer_escapes_interpolated_metacharacters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp) / "zd_app"
            package_root.mkdir()
            (package_root / "__init__.py").write_text("", encoding="utf-8")
            with patch.object(
                trust_self_check.app_version,
                "__version__",
                "2|[demo](x)`",
            ), patch.object(
                trust_self_check.app_version,
                "__build_commit__",
                "abc|[sha](x)",
            ):
                result = trust_self_check.build_trust_self_check(
                    package_root=package_root,
                    executable_path=r"C:\Users\Avery Stone\Legend|CTL.exe",
                    user_data_dir=r"C:\Users\Avery Stone\AppData\Roaming\ZDUltimateLegend",
                    now=_NOW,
                )

        markdown = result.to_markdown()
        self.assertIn(r"2\|\[demo\]\(x\)\`", markdown)
        self.assertIn(r"abc\|\[sha\]\(x\)", markdown)
        self.assertIn(r"Legend\|CTL.exe", markdown)
        self.assertNotIn("Avery Stone", markdown)
        self.assertNotIn("2|[demo](x)`", markdown)


class TrustSelfCheckI18nTests(unittest.TestCase):
    def test_en_zh_cn_have_matching_trust_self_check_keys(self) -> None:
        en = json.loads((_LOCALE_DIR / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((_LOCALE_DIR / "zh-CN.json").read_text(encoding="utf-8"))

        en_keys = {key for key in en if key.startswith("trust_self_check.")}
        zh_keys = {key for key in zh if key.startswith("trust_self_check.")}

        self.assertEqual(en_keys, zh_keys)
        self.assertGreater(len(en_keys), 20)

    def test_trust_self_check_locale_placeholders_match(self) -> None:
        en = json.loads((_LOCALE_DIR / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((_LOCALE_DIR / "zh-CN.json").read_text(encoding="utf-8"))

        for key in (k for k in en if k.startswith("trust_self_check.")):
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
