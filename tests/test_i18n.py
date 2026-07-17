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


def _locale_tables() -> dict[str, dict[str, str]]:
    locale_dir = Path("zd_app/i18n/locales")
    return {
        locale: json.loads((locale_dir / f"{locale}.json").read_text(encoding="utf-8"))
        for locale in i18n.SUPPORTED_LOCALES
    }


def _placeholders(value: str) -> set[str]:
    return set(re.findall(r"{([^{}]+)}", value))


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

    def test_language_ko_autonym_is_byte_identical_in_shipped_locales(self) -> None:
        expected = '"language.ko": "한국어"'.encode("utf-8")
        locale_dir = Path("zd_app/i18n/locales")

        for locale in i18n.SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                self.assertIn(expected, (locale_dir / f"{locale}.json").read_bytes())

    def test_t_returns_bracketed_key_when_missing_in_both(self) -> None:
        self.assertEqual(i18n.t("does.not.exist"), "[does.not.exist]")

    def test_t_interpolates_kwargs(self) -> None:
        self.assertEqual(i18n.t("test.greeting", name="Apex"), "Hello Apex")

    def test_set_locale_unsupported_falls_back_to_en(self) -> None:
        i18n.set_locale("fr")

        self.assertEqual(i18n.get_locale(), "en")

    def test_locale_jsons_have_matching_keys(self) -> None:
        locales = _locale_tables()
        default = locales[i18n.DEFAULT_LOCALE]

        for locale, values in locales.items():
            with self.subTest(locale=locale):
                self.assertEqual(set(default), set(values))

    def test_locale_jsons_no_empty_values(self) -> None:
        for locale, data in _locale_tables().items():
            with self.subTest(locale=locale):
                self.assertFalse([key for key, value in data.items() if not value])

    def test_locale_jsons_have_matching_placeholder_sets(self) -> None:
        locales = _locale_tables()
        default = locales[i18n.DEFAULT_LOCALE]

        for locale, values in locales.items():
            for key, default_value in default.items():
                with self.subTest(locale=locale, key=key):
                    self.assertEqual(
                        _placeholders(default_value),
                        _placeholders(values.get(key, "")),
                    )

    def test_restore_point_contract_copy_is_exact_in_both_locales(self) -> None:
        locale_dir = Path("zd_app/i18n/locales")
        en = json.loads((locale_dir / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((locale_dir / "zh-CN.json").read_text(encoding="utf-8"))

        self.assertEqual(
            {
                "apply.no_restore_point.title": "No restore point",
                "apply.no_restore_point.body": (
                    "A restore point could not be created before this change. If you "
                    "continue, there will be no automatic undo for it. Continue anyway?"
                ),
                "apply.no_restore_point.continue": "Continue without a restore point",
                "apply.result.no_restore_point_note": (
                    "Note: no restore point was created before this apply."
                ),
                "safe_import.apply.aborted_no_restore_point": (
                    "Nothing was written. A restore point could not be created, and Safe "
                    "Import writes only after one exists. Check disk space or the app data "
                    "folder, then try again."
                ),
                "restore_points.list.empty_state": (
                    "No restore points yet. LegendCTL tries to create one automatically before "
                    "higher-risk controller changes, and you can save one manually after reading "
                    "the controller. They capture app-supported settings only; they are not "
                    "factory backups."
                ),
            },
            {key: en[key] for key in (
                "apply.no_restore_point.title",
                "apply.no_restore_point.body",
                "apply.no_restore_point.continue",
                "apply.result.no_restore_point_note",
                "safe_import.apply.aborted_no_restore_point",
                "restore_points.list.empty_state",
            )},
        )
        self.assertEqual(
            {
                "apply.no_restore_point.title": "无还原点",
                "apply.no_restore_point.body": "本次更改前未能创建还原点。如果继续，此更改将没有自动撤销途径。仍要继续吗？",
                "apply.no_restore_point.continue": "不创建还原点并继续",
                "apply.result.no_restore_point_note": "注意：本次应用前未创建还原点。",
                "safe_import.apply.aborted_no_restore_point": "未写入任何内容。无法创建还原点，而安全导入只会在还原点创建成功后才写入。请检查磁盘空间或应用数据文件夹后重试。",
                "restore_points.list.empty_state": "暂无还原点。在风险较高的手柄变更前会尽量自动创建还原点，您也可以在读取手柄后手动保存一个。它们仅捕获应用支持的设置，并非出厂备份。",
            },
            {key: zh[key] for key in (
                "apply.no_restore_point.title",
                "apply.no_restore_point.body",
                "apply.no_restore_point.continue",
                "apply.result.no_restore_point_note",
                "safe_import.apply.aborted_no_restore_point",
                "restore_points.list.empty_state",
            )},
        )
        # The new cancel action intentionally reuses the existing generic key.
        self.assertNotIn("apply.no_restore_point.cancel", en)
        self.assertNotIn("apply.no_restore_point.cancel", zh)
        self.assertEqual(en["actions.cancel"], "Cancel")
        self.assertEqual(zh["actions.cancel"], "取消")

    def test_sensitivity_downgrade_copy_is_exact_in_both_locales(self) -> None:
        locale_dir = Path("zd_app/i18n/locales")
        en = json.loads((locale_dir / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((locale_dir / "zh-CN.json").read_text(encoding="utf-8"))

        self.assertEqual(
            en["apply.result.sens_8point_downgraded"],
            "Sensitivity: the 8-point curve could not be confirmed on this controller - "
            "applied the standard 3-point curve instead.",
        )
        self.assertEqual(
            en["log.apply.sens_8point_downgraded"],
            "8-point sensitivity not confirmed; applied the 3-point curve instead.",
        )
        self.assertEqual(
            zh["apply.result.sens_8point_downgraded"],
            "灵敏度：无法确认此手柄支持 8 点曲线——已改为应用标准 3 点曲线。",
        )
        self.assertEqual(
            zh["log.apply.sens_8point_downgraded"],
            "未能确认 8 点灵敏度支持；已改为应用 3 点曲线。",
        )

    def test_write_unverified_copy_is_byte_exact_in_both_locales(self) -> None:
        locale_dir = Path("zd_app/i18n/locales")
        expected = {
            "en": {
                "apply.result.write_unverified": (
                    "Some changes were written but could not be confirmed by read-back this time "
                    "— they may still have applied."
                ),
                "log.apply.write_unverified": (
                    "Write reported OK but read-back was inconclusive; reported as unverified, "
                    "not confirmed."
                ),
            },
            "zh-CN": {
                "apply.result.write_unverified": "部分更改已写入，但本次无法通过回读确认——它们可能仍已生效。",
                "log.apply.write_unverified": "写入报告为成功，但回读结果不确定；报告为未验证，而非已确认。",
            },
        }

        for locale, values in expected.items():
            with self.subTest(locale=locale):
                raw = (locale_dir / f"{locale}.json").read_bytes()
                parsed = json.loads(raw.decode("utf-8"))
                for key, value in values.items():
                    with self.subTest(key=key):
                        self.assertEqual(parsed[key], value)
                        self.assertIn(
                            f'"{key}": "{value}"'.encode("utf-8"),
                            raw,
                        )

    def test_t03_honest_write_copy_is_byte_exact_in_both_locales(self) -> None:
        locale_dir = Path("zd_app/i18n/locales")
        expected = {
            "en": {
                "apply.axis_inversion.success": "OK: {side} axis inversion written.",
                "apply.binding.success": "OK: Binding {source} -> {target} written.",
                "apply.deadzone.success": "OK: Deadzone settings written.",
                "apply.lighting.success": "OK: Lighting zone '{zone}' written.",
                "apply.polling_rate.success": "OK: Polling rate {label} written.",
                "apply.sensitivity.success": "OK: {side} sensitivity curve written.",
                "apply.sensitivity_8point.success": (
                    "OK: {side} sensitivity curve (8-pt) written."
                ),
                "apply.step_size.success": "OK: Step size {value} written.",
                "apply.trigger.success": "OK: {side} trigger settings written.",
                "apply.vibration.success": "OK: Vibration settings written.",
                "apply.profile.success": (
                    "OK: Profile '{name}' write calls completed ({n} writes)."
                ),
                "apply.profile.success_recovered": (
                    "OK: Profile '{name}' write calls completed ({n} writes, {k} recovered)."
                ),
                "apply.profile.partial": (
                    "Partial: Profile '{name}' ({m} of {n} writes; {k} failed). "
                    "See apply details."
                ),
                "results.applied_profile": (
                    "Profile {name} write calls completed ({writes} writes)."
                ),
                "apply.retry.success": (
                    "OK: Retried {n} failed settings; all write calls succeeded."
                ),
                "trust_ritual.verified_writes": (
                    "Every write reports its outcome. Restore, Safe Import, and settled "
                    "inline-deadzone changes verify by read-back; profile Apply also "
                    "read-back-checks step size and lighting zones. Write-only fields are "
                    "reported as sent, not verified."
                ),
                "diagnostics.trust.body": (
                    "Connected plus a recent read means the controller is talking to the app.\n"
                    "Read timings confirm refresh attempts.\n"
                    "Apply results are explicit: every write reports whether the write call "
                    "succeeded; Restore, Safe Import, and settled inline-deadzone changes "
                    "read the value back and compare it, and profile Apply also "
                    "read-back-checks step size and lighting zones; write-only fields "
                    "(back-paddle bindings) are reported as sent, not verified.\n"
                    "Support guidance is separate from protocol verification, so help copy "
                    "never pretends a read or write succeeded."
                ),
                # The chip word carries the honesty load: this row states a POLICY
                # and must never drift back to an evidence chip (it read "Verified
                # by read-back", green, before anything was plugged in).
                "trust_matrix.label.policy": "Verification policy",
                "trust_matrix.row.applied.why": (
                    "Restore, Safe Import and settled inline-deadzone changes read every "
                    "readable field back and compare it against what was sent. Applying a "
                    "profile read-back-checks step size and lighting zones only - its other "
                    "fields are reported as sent, not verified. A write that Windows reports "
                    "as successful is not proof the controller stored the value."
                ),
            },
            "zh-CN": {
                "apply.axis_inversion.success": "OK：已写入{side}摇杆轴反转。",
                "apply.back_paddle.success": (
                    "OK：后置侧键 {slot} -> {target} 已发送（只能写入，无法回读验证）。"
                ),
                "apply.binding.success": "OK：绑定 {source} -> {target} 已写入。",
                "apply.deadzone.success": "OK：死区设置已写入。",
                "apply.lighting.success": "OK：灯光区域“{zone}”已写入。",
                "apply.polling_rate.success": "OK：轮询率 {label} 已写入。",
                "apply.sensitivity.success": "OK：已写入{side}摇杆灵敏度曲线。",
                "apply.sensitivity_8point.success": (
                    "OK：已写入{side}摇杆灵敏度曲线（8 点）。"
                ),
                "apply.step_size.success": "OK：步长 {value} 已写入。",
                "apply.trigger.success": "OK：已写入{side}扳机设置。",
                "apply.vibration.success": "OK：振动设置已写入。",
                "apply.profile.success": "OK：配置“{name}”的写入调用已完成（{n} 项写入）。",
                "apply.profile.success_recovered": (
                    "OK：配置“{name}”的写入调用已完成（{n} 项写入，{k} 项已恢复）。"
                ),
                "apply.profile.partial": (
                    "部分完成：配置“{name}”（{n} 项写入中的 {m} 项；{k} 项失败）。请查看应用详情。"
                ),
                "results.applied_profile": "配置 {name} 的写入调用已完成（{writes} 项写入）。",
                "apply.retry.success": "OK：已重试 {n} 项失败设置；全部写入调用成功。",
                "trust_ritual.verified_writes": (
                    "每次写入都会报告其结果。还原、安全导入和内联死区的最终写入会通过回读验证；"
                    "应用配置时还会回读确认步长和各灯光区域。只写字段只报告为已发送，不会标记为已验证。"
                ),
                "diagnostics.trust.body": (
                    "已连接并且最近完成读取，表示手柄正在与应用通信。\n"
                    "读取时间戳确认刷新尝试。\n"
                    "应用结果是明确的：每次写入都会报告写入调用是否成功；还原、安全导入和内联死区的最终"
                    "写入会回读数值并进行比对，应用配置时还会回读确认步长和各灯光区域；只写字段（背键绑定）"
                    "报告为已发送，而非已验证。\n"
                    "支持指引与协议验证相互独立，因此帮助文案绝不会假装读取或写入已成功。"
                ),
                "trust_matrix.label.policy": "验证策略",
                "trust_matrix.row.applied.why": (
                    "还原、安全导入和内联死区的最终写入会回读每个可读字段，并与发送的值进行比对。"
                    "应用配置时只回读确认步长和各灯光区域——其余字段只报告为已发送，不做验证。"
                    "Windows 报告写入成功，并不能证明手柄已存储该值。"
                ),
            },
        }

        for locale, values in expected.items():
            with self.subTest(locale=locale):
                raw = (locale_dir / f"{locale}.json").read_bytes()
                parsed = json.loads(raw.decode("utf-8"))
                for key, value in values.items():
                    with self.subTest(key=key):
                        self.assertEqual(parsed[key], value)
                        self.assertIn(
                            f'"{key}": {json.dumps(value, ensure_ascii=False)}'.encode("utf-8"),
                            raw,
                        )

    def test_about_disclaimer_copy_is_byte_exact_in_both_locales(self) -> None:
        # about.disclaimer states the honest write-verification scope (Track A
        # honesty cluster). Pinned so the wording cannot silently re-broaden back
        # to the old universal-verification overclaim. This wording is the
        # CONSERVATIVE, v2.6.1-cut-true variant: profile Apply is described as
        # read-back-checking step size and lighting only. When the Track B cut
        # lands (Apply reads back every readable field), this pin is updated
        # DELIBERATELY in the same reconciliation pass as trust_ritual.verified_writes
        # and diagnostics.trust.body. The zh-CN value is an AI draft pending a
        # Hermes venue-native review before the cut.
        locale_dir = Path("zd_app/i18n/locales")
        expected = {
            "en": {
                "about.disclaimer": (
                    "This app is local-device tooling. Every controller write reports its "
                    "outcome; the Restore, Safe Import and inline-deadzone flows read the "
                    "value back to confirm it, and a profile Apply does the same for step "
                    "size and lighting — write-only fields are reported as sent. It doesn't "
                    "claim a write succeeded that it couldn't confirm."
                ),
            },
            "zh-CN": {
                "about.disclaimer": (
                    "此应用是本地设备工具。每次向手柄写入都会报告其结果；还原、安全导入和内联死区流程"
                    "会通过回读数值进行确认，应用配置时则仅对步长和灯光执行相同的回读确认——只写字段"
                    "则报告为“已发送”。对于无法确认的写入，它不会声称其已成功。"
                ),
            },
        }

        for locale, values in expected.items():
            with self.subTest(locale=locale):
                raw = (locale_dir / f"{locale}.json").read_bytes()
                parsed = json.loads(raw.decode("utf-8"))
                for key, value in values.items():
                    with self.subTest(key=key):
                        self.assertEqual(parsed[key], value)
                        self.assertIn(
                            f'"{key}": {json.dumps(value, ensure_ascii=False)}'.encode("utf-8"),
                            raw,
                        )

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

    def test_reviewed_ambiguous_allowlist_matches_current_english_literals(self) -> None:
        en = _locale_tables()["en"]
        for literal, keys in i18n._REVIEWED_AMBIGUOUS.items():
            for key in keys:
                with self.subTest(literal=literal, key=key):
                    self.assertIn(key, en)
                    self.assertEqual(en[key], literal)

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

    def test_third_locale_ambiguity_rearms_warning(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        locale_dir = Path(tmp.name)
        (locale_dir / "en.json").write_text(
            json.dumps(
                {
                    "controller.side.preview": "Synthetic Preview",
                    "ui.tab.preview": "Synthetic Preview",
                }
            ),
            encoding="utf-8",
        )
        # zh-CN is harmless here; the third sibling is context-different.
        (locale_dir / "zh-CN.json").write_text(
            json.dumps(
                {
                    "controller.side.preview": "预览",
                    "ui.tab.preview": "预览",
                }
            ),
            encoding="utf-8",
        )
        (locale_dir / "ko.json").write_text(
            json.dumps(
                {
                    "controller.side.preview": "미리 보기",
                    "ui.tab.preview": "미리보기",
                }
            ),
            encoding="utf-8",
        )

        i18n._loaded.clear()
        i18n._reverse_en.clear()
        with mock.patch.object(i18n, "SUPPORTED_LOCALES", ("en", "zh-CN", "ko")), mock.patch.object(
            i18n, "_locale_dir", return_value=locale_dir
        ):
            with self.assertLogs("zd_app.i18n", level="WARNING") as captured:
                i18n._load("en")

        self.assertIn("Synthetic Preview", "\n".join(captured.output))
        self.assertIn("Synthetic Preview", i18n._ambiguous_en)

    def test_reviewed_literal_covers_all_sibling_locales(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        locale_dir = Path(tmp.name)
        keys = {
            "controller.back_paddles.target.BACK": "Back",
            "diagnostics.live_verify.workspace.back_view": "Back",
        }
        (locale_dir / "en.json").write_text(json.dumps(keys), encoding="utf-8")
        (locale_dir / "zh-CN.json").write_text(
            json.dumps(
                {
                    "controller.back_paddles.target.BACK": "返回键",
                    "diagnostics.live_verify.workspace.back_view": "背面",
                }
            ),
            encoding="utf-8",
        )
        (locale_dir / "ko.json").write_text(
            json.dumps(
                {
                    "controller.back_paddles.target.BACK": "뒤로",
                    "diagnostics.live_verify.workspace.back_view": "뒤쪽",
                }
            ),
            encoding="utf-8",
        )

        i18n._loaded.clear()
        i18n._reverse_en.clear()
        with mock.patch.object(i18n, "SUPPORTED_LOCALES", ("en", "zh-CN", "ko")), mock.patch.object(
            i18n, "_locale_dir", return_value=locale_dir
        ):
            with self.assertNoLogs("zd_app.i18n", level="WARNING"):
                i18n._load("en")

        self.assertEqual(i18n._ambiguous_en, {})

    def test_missing_future_locale_file_is_skipped_silently(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        locale_dir = Path(tmp.name)
        (locale_dir / "en.json").write_text(
            json.dumps(
                {
                    "controller.side.left": "Synthetic Left",
                    "ui.tab.left": "Synthetic Left",
                }
            ),
            encoding="utf-8",
        )
        (locale_dir / "zh-CN.json").write_text(
            json.dumps(
                {
                    "controller.side.left": "左",
                    "ui.tab.left": "左",
                }
            ),
            encoding="utf-8",
        )

        i18n._loaded.clear()
        i18n._reverse_en.clear()
        with mock.patch.object(i18n, "SUPPORTED_LOCALES", ("en", "zh-CN", "ko")), mock.patch.object(
            i18n, "_locale_dir", return_value=locale_dir
        ):
            with self.assertNoLogs("zd_app.i18n", level="WARNING"):
                i18n._load("en")

        self.assertEqual(i18n._ambiguous_en, {})

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
