"""Tests for R1 locale loading and lookup."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zd_app import i18n


class I18nTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_t_returns_value_for_known_key(self) -> None:
        self.assertEqual(i18n.t("actions.save"), "Save")

    def test_t_falls_back_to_english_for_missing_zh_key(self) -> None:
        i18n._loaded["zh-CN"] = {}
        i18n.set_locale("zh-CN")

        self.assertEqual(i18n.t("actions.save"), "Save")

    def test_t_falls_back_to_english_for_corrupt_tombstone_value(self) -> None:
        i18n._loaded["zh-CN"] = {"actions.save": "??"}
        i18n.set_locale("zh-CN")

        self.assertEqual(i18n.t("actions.save"), "Save")

    def test_language_zh_cn_label_is_real_hanzi(self) -> None:
        self.assertEqual(i18n.t("language.zh-CN"), "简体中文")

    def test_t_returns_bracketed_key_when_missing_in_both(self) -> None:
        self.assertEqual(i18n.t("does.not.exist"), "[does.not.exist]")

    def test_t_interpolates_kwargs(self) -> None:
        self.assertEqual(i18n.t("test.greeting", name="Apex"), "Hello Apex")

    def test_set_locale_unsupported_falls_back_to_en(self) -> None:
        i18n.set_locale("fr")

        self.assertEqual(i18n.get_locale(), "en")

    def test_locale_jsons_have_matching_keys(self) -> None:
        locale_dir = Path("zd_app/i18n/locales")
        en = json.loads((locale_dir / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((locale_dir / "zh-CN.json").read_text(encoding="utf-8"))

        self.assertEqual(set(en), set(zh))

    def test_locale_jsons_no_empty_values(self) -> None:
        locale_dir = Path("zd_app/i18n/locales")
        for path in (locale_dir / "en.json", locale_dir / "zh-CN.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(path=path):
                self.assertFalse([key for key, value in data.items() if not value])

    def test_apply_status_transport_profile_namespaces_in_both_locales(self) -> None:
        locale_dir = Path("zd_app/i18n/locales")
        en = json.loads((locale_dir / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((locale_dir / "zh-CN.json").read_text(encoding="utf-8"))

        for prefix in ("apply.", "status.", "transport.", "profile."):
            with self.subTest(prefix=prefix):
                en_keys = {key for key in en if key.startswith(prefix)}
                zh_keys = {key for key in zh if key.startswith(prefix)}
                self.assertEqual(en_keys, zh_keys)

    def test_polling_rate_non_commit_key_present_and_interpolates(self) -> None:
        # The 8000 Hz firmware-capability non-commit message must exist in BOTH
        # locales, carry the {kept} placeholder, and interpolate the kept rate.
        locale_dir = Path("zd_app/i18n/locales")
        en = json.loads((locale_dir / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((locale_dir / "zh-CN.json").read_text(encoding="utf-8"))
        key = "apply.polling_rate.non_commit_8000"
        self.assertIn(key, en)
        self.assertIn(key, zh)
        self.assertIn("{kept}", en[key])
        self.assertIn("{kept}", zh[key])
        # English names the firmware requirement explicitly.
        self.assertIn("1.18", en[key])
        try:
            i18n.set_locale("en")
            self.assertIn("1000Hz", i18n.t(key, kept="1000Hz"))
            i18n.set_locale("zh-CN")
            self.assertIn("2000Hz", i18n.t(key, kept="2000Hz"))
        finally:
            i18n.set_locale("en")

    def test_user_strings_do_not_expose_internal_settings_service_name(self) -> None:
        locale_dir = Path("zd_app/i18n/locales")
        for path in (locale_dir / "en.json", locale_dir / "zh-CN.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            leaked = [key for key, value in data.items() if "SettingsService" in value]
            with self.subTest(path=path):
                self.assertEqual(leaked, [])

    def test_user_strings_drop_operator_internal_127_note(self) -> None:
        locale_dir = Path("zd_app/i18n/locales")
        for path in (locale_dir / "en.json", locale_dir / "zh-CN.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            leaked = [key for key, value in data.items() if "1.2.7" in value]
            with self.subTest(path=path):
                self.assertEqual(leaked, [])

    def test_zh_back_paddle_shoulder_stick_targets_stay_latin_abbreviations(self) -> None:
        locale_dir = Path("zd_app/i18n/locales")
        zh = json.loads((locale_dir / "zh-CN.json").read_text(encoding="utf-8"))

        expected = {
            "controller.back_paddles.target.LB": "LB",
            "controller.back_paddles.target.RB": "RB",
            "controller.back_paddles.target.LS": "LS",
            "controller.back_paddles.target.RS": "RS",
        }
        for key, label in expected.items():
            with self.subTest(key=key):
                self.assertEqual(zh[key], label)
                self.assertFalse(any("\u4e00" <= char <= "\u9fff" for char in zh[key]))

    def test_zh_calibration_support_strings_do_not_fall_back_to_long_ascii(self) -> None:
        locale_dir = Path("zd_app/i18n/locales")
        zh = json.loads((locale_dir / "zh-CN.json").read_text(encoding="utf-8"))

        keys = (
            "support.calibration.summary",
            "support.calibration.bullet.stick",
            "support.calibration.bullet.trigger",
            "support.calibration.bullet.motion",
            "support.calibration.bullet.combined",
        )
        for key in keys:
            with self.subTest(key=key):
                self.assertEqual(re.findall(r"[A-Za-z]{4,}", zh[key]), [])


class I18nAmbiguousLiteralGuardTests(unittest.TestCase):
    """B6: English literals that map to multiple keys (whose translations can
    differ across locales) are a latent contributor trap — ``translate_literal``
    resolves each to the first JSON-order key. Loading must WARN so the
    ambiguity is visible, without changing the (first-key-wins) resolution that
    current call sites depend on."""

    def setUp(self) -> None:
        # Save module-global caches so this class's synthetic-locale fiddling
        # can't leak into the rest of the i18n suite.
        self._saved_loaded = dict(i18n._loaded)
        self._saved_reverse = dict(i18n._reverse_en)
        self._saved_ambiguous = dict(getattr(i18n, "_ambiguous_en", {}))

    def tearDown(self) -> None:
        i18n._loaded.clear()
        i18n._loaded.update(self._saved_loaded)
        i18n._reverse_en.clear()
        i18n._reverse_en.update(self._saved_reverse)
        if hasattr(i18n, "_ambiguous_en"):
            i18n._ambiguous_en.clear()
            i18n._ambiguous_en.update(self._saved_ambiguous)
        i18n.set_locale("en")

    def test_colliding_literal_with_differing_translation_warns(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        locale_dir = Path(tmp.name)
        (locale_dir / "en.json").write_text(
            json.dumps(
                {
                    "controller.side.left": "Left",
                    "ui.tab.left": "Left",
                    "actions.save": "Save",
                }
            ),
            encoding="utf-8",
        )
        # The two "Left" keys translate DIFFERENTLY in zh-CN — the real trap.
        (locale_dir / "zh-CN.json").write_text(
            json.dumps(
                {
                    "controller.side.left": "左",        # 左
                    "ui.tab.left": "左侧",           # 左侧
                    "actions.save": "保存",
                }
            ),
            encoding="utf-8",
        )

        i18n._loaded.clear()
        i18n._reverse_en.clear()
        with mock.patch.object(i18n, "_locale_dir", return_value=locale_dir):
            with self.assertLogs("zd_app.i18n", level="WARNING") as captured:
                i18n._load("en")

        joined = "\n".join(captured.output)
        self.assertIn("Left", joined)
        # Resolution is UNCHANGED: the first JSON-order key still wins.
        self.assertEqual(i18n._reverse_en["Left"], "controller.side.left")

    def test_same_translation_duplicate_does_not_warn(self) -> None:
        # A duplicate English literal whose siblings translate IDENTICALLY is
        # harmless (no mistranslation possible) — the narrowed guard stays quiet
        # so the warning points only at genuine ambiguities, not every duplicate.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        locale_dir = Path(tmp.name)
        (locale_dir / "en.json").write_text(
            json.dumps({"controller.side.left": "Left", "ui.tab.left": "Left"}),
            encoding="utf-8",
        )
        (locale_dir / "zh-CN.json").write_text(
            json.dumps({"controller.side.left": "左", "ui.tab.left": "左"}),
            encoding="utf-8",
        )

        i18n._loaded.clear()
        i18n._reverse_en.clear()
        with mock.patch.object(i18n, "_locale_dir", return_value=locale_dir):
            with self.assertNoLogs("zd_app.i18n", level="WARNING"):
                i18n._load("en")

    def test_unambiguous_literals_do_not_warn(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        locale_dir = Path(tmp.name)
        (locale_dir / "en.json").write_text(
            json.dumps({"actions.save": "Save", "actions.cancel": "Cancel"}),
            encoding="utf-8",
        )

        i18n._loaded.clear()
        i18n._reverse_en.clear()
        with mock.patch.object(i18n, "_locale_dir", return_value=locale_dir):
            with self.assertNoLogs("zd_app.i18n", level="WARNING"):
                i18n._load("en")


if __name__ == "__main__":
    unittest.main()
