"""Tests for R1 locale loading and lookup."""

from __future__ import annotations

import ast
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

    def test_not_verified_profile_tooltip_exists_in_both_locales(self) -> None:
        locale_dir = Path("zd_app/i18n/locales")
        en = json.loads((locale_dir / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((locale_dir / "zh-CN.json").read_text(encoding="utf-8"))
        key = "status.config.not_verified_tooltip"

        self.assertIn(key, en)
        self.assertIn(key, zh)
        self.assertIn("official app's view can lag", en[key])
        self.assertIn("settings work normally", en[key])
        self.assertIn("Profiles tab", en[key])
        self.assertIn("协议确认", zh[key])

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

    def test_shipped_locale_load_emits_no_ambiguity_warnings(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        if hasattr(i18n, "_ambiguous_en"):
            i18n._ambiguous_en.clear()

        with self.assertNoLogs("zd_app.i18n", level="WARNING"):
            i18n._load("en")

        self.assertEqual(getattr(i18n, "_ambiguous_en", {}), {})

    def test_synthetic_new_ambiguity_still_warns(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        locale_dir = Path(tmp.name)
        (locale_dir / "en.json").write_text(
            json.dumps(
                {
                    "controller.side.preview": "Synthetic Preview",
                    "ui.tab.preview": "Synthetic Preview",
                    "actions.save": "Save",
                }
            ),
            encoding="utf-8",
        )
        # The two synthetic keys translate DIFFERENTLY in zh-CN — the real trap.
        (locale_dir / "zh-CN.json").write_text(
            json.dumps(
                {
                    "controller.side.preview": "预览",
                    "ui.tab.preview": "预演",
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
        self.assertIn("Synthetic Preview", joined)
        # Resolution is UNCHANGED: the first JSON-order key still wins.
        self.assertEqual(i18n._reverse_en["Synthetic Preview"], "controller.side.preview")

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

    def test_new_sibling_under_reviewed_literal_rearms_warning(self) -> None:
        # Fix E: the reviewed-ambiguous allowlist is key-aware. A NEW key added
        # under an allowlisted literal ("Back") with a DIFFERENT zh translation is
        # not in the reviewed key-set, so it re-arms the ambiguity warning instead
        # of shipping silently un-warned (a literal-only allowlist would suppress).
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        locale_dir = Path(tmp.name)
        (locale_dir / "en.json").write_text(
            json.dumps(
                {
                    "controller.back_paddles.target.BACK": "Back",
                    "diagnostics.live_verify.workspace.back_view": "Back",
                    "newscreen.nav.back": "Back",
                }
            ),
            encoding="utf-8",
        )
        (locale_dir / "zh-CN.json").write_text(
            json.dumps(
                {
                    "controller.back_paddles.target.BACK": "返回键",
                    "diagnostics.live_verify.workspace.back_view": "背面",
                    "newscreen.nav.back": "后退",
                }
            ),
            encoding="utf-8",
        )

        i18n._loaded.clear()
        i18n._reverse_en.clear()
        with mock.patch.object(i18n, "_locale_dir", return_value=locale_dir):
            with self.assertLogs("zd_app.i18n", level="WARNING") as captured:
                i18n._load("en")

        self.assertIn("Back", "\n".join(captured.output))
        self.assertIn("Back", i18n._ambiguous_en)

    def test_reviewed_literal_with_only_reviewed_keys_stays_silent(self) -> None:
        # Companion to the re-arm test: the EXACT reviewed key-set (a subset) is
        # the intentional, human-reviewed state and must stay quiet even though
        # the sibling zh differs.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        locale_dir = Path(tmp.name)
        (locale_dir / "en.json").write_text(
            json.dumps(
                {
                    "controller.back_paddles.target.BACK": "Back",
                    "diagnostics.live_verify.workspace.back_view": "Back",
                }
            ),
            encoding="utf-8",
        )
        (locale_dir / "zh-CN.json").write_text(
            json.dumps(
                {
                    "controller.back_paddles.target.BACK": "返回键",
                    "diagnostics.live_verify.workspace.back_view": "背面",
                }
            ),
            encoding="utf-8",
        )

        i18n._loaded.clear()
        i18n._reverse_en.clear()
        with mock.patch.object(i18n, "_locale_dir", return_value=locale_dir):
            with self.assertNoLogs("zd_app.i18n", level="WARNING"):
                i18n._load("en")

        self.assertEqual(i18n._ambiguous_en, {})

    def test_reviewed_ambiguous_literals_are_keyed_before_dpg_translation(self) -> None:
        # C3 (swept in with fix E): every reviewed-ambiguous English literal must
        # be KEYED before it reaches a localized DPG call — a raw literal there
        # resolves via translate_literal() to the first JSON-order key's
        # (possibly wrong-context) translation.
        from zd_app.ui import localized_dpg
        from zd_app.ui.screens.legacy import buttons

        i18n.set_locale("zh-CN")
        self.assertEqual(
            buttons._button_label("Back"),
            i18n.t("controller.back_paddles.target.BACK"),
        )
        self.assertEqual(
            buttons._button_label("Home"),
            i18n.t("diagnostics.live_verify.face_diagram.home"),
        )

        reviewed = set(i18n._REVIEWED_AMBIGUOUS)
        ui_root = Path(__file__).resolve().parents[1] / "zd_app" / "ui"
        offenders: list[str] = []
        for path in sorted(ui_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Attribute):
                    call_name = node.func.attr
                elif isinstance(node.func, ast.Name):
                    call_name = node.func.id
                else:
                    continue
                spec = localized_dpg._CALL_SPECS.get(call_name)
                if spec is None:
                    continue
                expressions = []
                for item in spec:
                    if isinstance(item, int) and item < len(node.args):
                        expressions.append(node.args[item])
                    elif isinstance(item, str):
                        expressions.extend(
                            kw.value for kw in node.keywords if kw.arg == item
                        )
                for expression in expressions:
                    for subnode in ast.walk(expression):
                        if (
                            isinstance(subnode, ast.Constant)
                            and isinstance(subnode.value, str)
                            and subnode.value in reviewed
                        ):
                            offenders.append(
                                f"{path}:{node.lineno}: {call_name} uses {subnode.value!r}"
                            )

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
