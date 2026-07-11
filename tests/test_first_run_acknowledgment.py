"""Tests for the first-run acknowledgment (clickwrap) gate.

Exercises ``AppShell._show_first_run_acknowledgment_modal_if_needed`` — the
one-time legal accept the user must give before using the app — against a
real DPG context, plus i18n parity / forbidden-phrase guards for the new
``first_run.*`` keys.

The gate logic is exercised WITHOUT a live render loop: the method only
builds the modal window and returns, and the accept / decline callbacks are
plain closures invoked directly (mirroring tests/test_crash_review_modal.py).
``dpg.stop_dearpygui`` is patched in the decline/close tests — calling it
without a live viewport segfaults, and the production decline path only runs
while the render loop is up.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n
from zd_app.i18n import t
from zd_app.models import AppSettings
from zd_app.ui import trust_front_door


_MODAL = "first_run_ack_modal"
_ACCEPT = "first_run_ack_accept_button"
_DECLINE = "first_run_ack_decline_button"
_VERIFY = "first_run_ack_verify_link"
_INTRO = "first_run_ack_intro_text"
_DISCLAIMER = "first_run_ack_disclaimer_text"
_FACT_TAGS = (
    "first_run_ack_fact_reads_text",
    "first_run_ack_fact_writes_text",
    "first_run_ack_fact_telemetry_text",
)
_AS_IS = "first_run_ack_risk_as_is_text"
_OLD_RISK_TAGS = (
    "first_run_ack_risk_writes_text",
    "first_run_ack_risk_hardware_text",
    "first_run_ack_risk_reversible_text",
)

_FIRST_RUN_KEYS = (
    "first_run.title",
    "first_run.intro",
    "first_run.fact.reads",
    "first_run.fact.writes",
    "first_run.pending_write_blocked",
    "first_run.fact.telemetry",
    "first_run.risk.as_is",
    "first_run.verify_link",
    "first_run.accept",
    "first_run.decline",
)

_LOCALES_DIR = Path("zd_app/i18n/locales")
_REVISED_COPY = {
    "en": {
        "first_run.intro": "LegendCTL is an unofficial, community-made tool for the ZD Ultimate Legend controller.",
        "first_run.fact.reads": "Reads controller state locally on this PC.",
        "first_run.fact.writes": "Writes controller settings only when you act — using Apply, or live controls it labels as writing immediately.",
        "first_run.pending_write_blocked": "Accept the first-run notice before changing controller settings. Reading and verifying stay open.",
        "first_run.fact.telemetry": "No telemetry. Source code and diagnostics are available.",
        "first_run.verify_link": "How to verify this",
    },
    "zh-CN": {
        "first_run.intro": "LegendCTL 是一款非官方的、由社区制作的 ZD Ultimate Legend 手柄工具。",
        "first_run.fact.reads": "仅在本机读取手柄状态。",
        "first_run.fact.writes": "仅在你操作时才写入手柄设置——通过“应用”，或标注为即时写入的实时控件。",
        "first_run.pending_write_blocked": "更改手柄设置前请先接受首次运行须知。读取与验证功能不受影响。",
        "first_run.fact.telemetry": "不收集任何遥测数据；源代码与诊断功能均可查验。",
        "first_run.verify_link": "如何验证这些说法",
    },
}


class FirstRunGateLogicTests(unittest.TestCase):
    """Gating + persistence — the parts that must hold without a render loop."""

    def setUp(self) -> None:
        i18n.set_locale("en")

    def tearDown(self) -> None:
        i18n.set_locale("en")

    def test_skipped_when_no_dpg_context_without_touching_dpg(self) -> None:
        # Headless / sync path: not-acknowledged but no context up. Must return
        # False and never call into DPG (no live context exists here).
        shell = make_shell(settings=AppSettings(first_run_acknowledged=False))
        shell._dpg_context_ready = False
        self.assertFalse(shell._show_first_run_acknowledgment_modal_if_needed())

    def test_gate_shows_when_not_acknowledged(self) -> None:
        dpg.create_context()
        try:
            shell = make_shell(settings=AppSettings(first_run_acknowledged=False))
            shell._dpg_context_ready = True
            shown = shell._show_first_run_acknowledgment_modal_if_needed()
            self.assertTrue(shown)
            self.assertTrue(dpg.does_item_exist(_MODAL))
        finally:
            dpg.destroy_context()

    def test_gate_skipped_when_already_acknowledged(self) -> None:
        dpg.create_context()
        try:
            shell = make_shell(settings=AppSettings(first_run_acknowledged=True))
            shell._dpg_context_ready = True
            shown = shell._show_first_run_acknowledgment_modal_if_needed()
            self.assertFalse(shown)
            self.assertFalse(dpg.does_item_exist(_MODAL))
        finally:
            dpg.destroy_context()

    def test_accept_sets_and_persists_flag_and_does_not_exit(self) -> None:
        dpg.create_context()
        try:
            shell = make_shell(settings=AppSettings(first_run_acknowledged=False))
            shell._dpg_context_ready = True
            shell._show_first_run_acknowledgment_modal_if_needed()

            with mock.patch("dearpygui.dearpygui.stop_dearpygui") as stop:
                dpg.get_item_configuration(_ACCEPT)["callback"]()

            self.assertTrue(shell.settings.first_run_acknowledged)
            self.assertFalse(shell._consent_pending_verify)
            shell.settings_store.save.assert_called_once_with(shell.settings)
            self.assertFalse(dpg.does_item_exist(_MODAL))
            # Accepting proceeds into the app — it must NOT stop the loop.
            stop.assert_not_called()
        finally:
            dpg.destroy_context()

    def test_decline_leaves_flag_unset_and_requests_exit(self) -> None:
        dpg.create_context()
        try:
            shell = make_shell(settings=AppSettings(first_run_acknowledged=False))
            shell._dpg_context_ready = True
            shell._show_first_run_acknowledgment_modal_if_needed()

            with mock.patch("dearpygui.dearpygui.stop_dearpygui") as stop:
                dpg.get_item_configuration(_DECLINE)["callback"]()

            self.assertFalse(shell.settings.first_run_acknowledged)
            self.assertFalse(shell._consent_pending_verify)
            shell.settings_store.save.assert_not_called()
            self.assertFalse(dpg.does_item_exist(_MODAL))
            stop.assert_called_once()
        finally:
            dpg.destroy_context()

    def test_x_close_behaves_like_decline(self) -> None:
        dpg.create_context()
        try:
            shell = make_shell(settings=AppSettings(first_run_acknowledged=False))
            shell._dpg_context_ready = True
            shell._show_first_run_acknowledgment_modal_if_needed()

            on_close = dpg.get_item_configuration(_MODAL)["on_close"]
            self.assertTrue(callable(on_close), "on_close handler must be set on the gate modal")
            with mock.patch("dearpygui.dearpygui.stop_dearpygui") as stop:
                on_close()

            self.assertFalse(shell.settings.first_run_acknowledged)
            self.assertFalse(shell._consent_pending_verify)
            shell.settings_store.save.assert_not_called()
            self.assertFalse(dpg.does_item_exist(_MODAL))
            stop.assert_called_once()
        finally:
            dpg.destroy_context()

    def test_gate_does_not_reshow_after_acceptance_persisted(self) -> None:
        # Simulate the real lifecycle: accept once (persists the flag), then a
        # later launch with the persisted settings must not re-show the gate.
        dpg.create_context()
        try:
            settings = AppSettings(first_run_acknowledged=False)
            shell = make_shell(settings=settings)
            shell._dpg_context_ready = True
            shell._show_first_run_acknowledgment_modal_if_needed()
            with mock.patch("dearpygui.dearpygui.stop_dearpygui"):
                dpg.get_item_configuration(_ACCEPT)["callback"]()
            self.assertTrue(settings.first_run_acknowledged)

            # Next launch — same (now-acknowledged) settings object.
            shell2 = make_shell(settings=settings)
            shell2._dpg_context_ready = True
            self.assertFalse(shell2._show_first_run_acknowledgment_modal_if_needed())
            self.assertFalse(dpg.does_item_exist(_MODAL))
        finally:
            dpg.destroy_context()

    def test_verify_opens_trust_matrix_without_acknowledging_or_exiting(self) -> None:
        dpg.create_context()
        try:
            shell = make_shell(settings=AppSettings(first_run_acknowledged=False))
            shell._dpg_context_ready = True
            shell.switch_screen = mock.MagicMock()
            shell._show_first_run_acknowledgment_modal_if_needed()

            with mock.patch.object(shell, "_request_app_exit") as request_exit:
                dpg.get_item_configuration(_VERIFY)["callback"]()

            self.assertFalse(shell.settings.first_run_acknowledged)
            self.assertTrue(shell._consent_pending_verify)
            shell.settings_store.save.assert_not_called()
            self.assertFalse(dpg.does_item_exist(_MODAL))
            request_exit.assert_not_called()
            self.assertEqual(shell.diagnostics_active_tab, "guidance")
            self.assertEqual(
                getattr(shell, trust_front_door.TRUST_FRONT_DOOR_FOCUS_ATTR),
                "trust_matrix",
            )
            shell.switch_screen.assert_called_once_with("diagnostics")
        finally:
            dpg.destroy_context()

    def test_gate_rearms_after_verify(self) -> None:
        dpg.create_context()
        try:
            shell = make_shell(settings=AppSettings(first_run_acknowledged=False))
            shell._dpg_context_ready = True
            shell.switch_screen = mock.MagicMock()
            self.assertTrue(shell._show_first_run_acknowledgment_modal_if_needed())
            dpg.get_item_configuration(_VERIFY)["callback"]()

            self.assertFalse(shell.settings.first_run_acknowledged)
            self.assertTrue(shell._show_first_run_acknowledgment_modal_if_needed())
            self.assertTrue(dpg.does_item_exist(_MODAL))
        finally:
            dpg.destroy_context()

    def test_verify_blocks_live_write_until_acceptance(self) -> None:
        dpg.create_context()
        try:
            shell = make_shell(
                settings_service=mock.MagicMock(),
                settings=AppSettings(first_run_acknowledged=False),
            )
            shell._dpg_context_ready = True
            shell.switch_screen = mock.MagicMock()
            shell._hid_available_or_refuse = mock.MagicMock(return_value=True)
            shell._step_size_hydrated = True
            shell._manual_device_write_gate = mock.MagicMock(return_value=(True, False))
            shell._do_write_step_size = mock.MagicMock()
            shell._show_first_run_acknowledgment_modal_if_needed()

            dpg.get_item_configuration(_VERIFY)["callback"]()
            self.assertTrue(shell._consent_pending_verify)
            self.assertFalse(dpg.does_item_exist(_MODAL))

            self.assertIsNone(shell.apply_step_size(150))
            shell._do_write_step_size.assert_not_called()
            shell.device_service.record_apply_result.assert_called_once_with(
                False,
                t("first_run.pending_write_blocked"),
            )
            self.assertTrue(dpg.does_item_exist(_MODAL))

            dpg.get_item_configuration(_ACCEPT)["callback"]()
            self.assertFalse(shell._consent_pending_verify)
            shell.apply_step_size(150)
            shell._do_write_step_size.assert_called_once_with(
                150,
                no_restore_point=False,
            )
        finally:
            dpg.destroy_context()


class FirstRunModalContentTests(unittest.TestCase):
    """The modal renders the concise factual disclosure + verification link."""

    def setUp(self) -> None:
        i18n.set_locale("en")

    def tearDown(self) -> None:
        i18n.set_locale("en")

    def test_modal_renders_reused_zd_disclaimer_verbatim(self) -> None:
        dpg.create_context()
        try:
            shell = make_shell(settings=AppSettings(first_run_acknowledged=False))
            shell._dpg_context_ready = True
            shell._show_first_run_acknowledgment_modal_if_needed()
            self.assertTrue(dpg.does_item_exist(_DISCLAIMER))
            # Single source of truth: the gate reuses about.zd_disclaimer.
            self.assertEqual(dpg.get_value(_DISCLAIMER), t("about.zd_disclaimer"))
        finally:
            dpg.destroy_context()

    def test_modal_renders_factual_body_and_removes_retired_risk_lines(self) -> None:
        dpg.create_context()
        try:
            shell = make_shell(settings=AppSettings(first_run_acknowledged=False))
            shell._dpg_context_ready = True
            shell._show_first_run_acknowledgment_modal_if_needed()
            self.assertTrue(dpg.does_item_exist(_INTRO))
            self.assertEqual(dpg.get_value(_INTRO), t("first_run.intro"))
            for tag, key in zip(
                _FACT_TAGS,
                (
                    "first_run.fact.reads",
                    "first_run.fact.writes",
                    "first_run.fact.telemetry",
                ),
                strict=True,
            ):
                self.assertTrue(dpg.does_item_exist(tag), f"missing fact widget {tag}")
                self.assertEqual(dpg.get_value(tag), t(key))
            self.assertTrue(dpg.does_item_exist(_AS_IS))
            self.assertEqual(dpg.get_value(_AS_IS), t("first_run.risk.as_is"))
            self.assertTrue(dpg.does_item_exist(_VERIFY))
            self.assertEqual(
                dpg.get_item_configuration(_VERIFY)["label"],
                t("first_run.verify_link"),
            )
            for tag in _OLD_RISK_TAGS:
                self.assertFalse(dpg.does_item_exist(tag), f"retired risk widget {tag}")
        finally:
            dpg.destroy_context()

    def test_factual_lines_and_as_is_line_have_expected_content(self) -> None:
        dpg.create_context()
        try:
            shell = make_shell(settings=AppSettings(first_run_acknowledged=False))
            shell._dpg_context_ready = True
            shell._show_first_run_acknowledgment_modal_if_needed()
            self.assertIn("locally", dpg.get_value("first_run_ack_fact_reads_text"))
            self.assertIn("only when", dpg.get_value("first_run_ack_fact_writes_text"))
            self.assertIn("No telemetry", dpg.get_value("first_run_ack_fact_telemetry_text"))
            as_is = dpg.get_value(_AS_IS).lower()
            self.assertIn("as is", as_is)
            self.assertIn("warranty", as_is)
            self.assertIn("risk", as_is)
        finally:
            dpg.destroy_context()

    def test_accept_and_decline_buttons_present(self) -> None:
        dpg.create_context()
        try:
            shell = make_shell(settings=AppSettings(first_run_acknowledged=False))
            shell._dpg_context_ready = True
            shell._show_first_run_acknowledgment_modal_if_needed()
            self.assertTrue(dpg.does_item_exist(_ACCEPT))
            self.assertTrue(dpg.does_item_exist(_DECLINE))
            self.assertEqual(
                dpg.get_item_configuration(_ACCEPT)["label"], t("first_run.accept")
            )
            self.assertEqual(
                dpg.get_item_configuration(_DECLINE)["label"], t("first_run.decline")
            )
        finally:
            dpg.destroy_context()


# ---------------------------------------------------------------------------
# Locale parity + forbidden-phrase guard for the new first_run.* keys
# ---------------------------------------------------------------------------


def _load_locale(name: str) -> dict[str, str]:
    return json.loads((_LOCALES_DIR / f"{name}.json").read_text(encoding="utf-8"))


class FirstRunI18nKeysTests(unittest.TestCase):
    def test_en_has_all_keys(self) -> None:
        data = _load_locale("en")
        self.assertEqual([k for k in _FIRST_RUN_KEYS if k not in data], [])

    def test_zh_cn_has_all_keys(self) -> None:
        data = _load_locale("zh-CN")
        self.assertEqual([k for k in _FIRST_RUN_KEYS if k not in data], [])

    def test_locales_have_identical_first_run_keyset(self) -> None:
        en = {k for k in _load_locale("en") if k.startswith("first_run.")}
        zh = {k for k in _load_locale("zh-CN") if k.startswith("first_run.")}
        self.assertEqual(en, zh)
        self.assertEqual(en, set(_FIRST_RUN_KEYS))

    def test_first_run_values_non_empty(self) -> None:
        for name in ("en", "zh-CN"):
            data = _load_locale(name)
            for key in _FIRST_RUN_KEYS:
                with self.subTest(locale=name, key=key):
                    self.assertTrue(data[key])

    def test_zh_cn_strings_are_actually_translated(self) -> None:
        en = _load_locale("en")
        zh = _load_locale("zh-CN")
        for key in _FIRST_RUN_KEYS:
            with self.subTest(key=key):
                self.assertNotEqual(en[key], zh[key])

    def test_revised_copy_matches_the_researcher_authored_strings(self) -> None:
        for locale_name, expected in _REVISED_COPY.items():
            data = _load_locale(locale_name)
            with self.subTest(locale=locale_name):
                for key, value in expected.items():
                    with self.subTest(key=key):
                        self.assertEqual(data[key], value)


# Forbidden-token blocklist — same tokens guarded for trust_ritual / restore
# points. The first-run gate makes legal promises to the user, so it follows
# the same vocabulary discipline. The risk copy is a denial ("at your own
# risk", "without warranty") and deliberately avoids the never-words, so no
# whitelist is needed here.
_FORBIDDEN_TOKENS = (
    "factory_backup",
    "factory_restore",
    "factory_image",
    "full_backup",
    "complete_backup",
    "clone",
    "calibration_backup",
    "firmware_backup",
    "guaranteed_rollback",
    "factory",
    "backup",
)


class FirstRunForbiddenPhrasesTests(unittest.TestCase):
    def _assert_no_forbidden(self, locale_name: str) -> None:
        data = _load_locale(locale_name)
        for key, value in data.items():
            if not key.startswith("first_run."):
                continue
            lowered = value.lower()
            for token in _FORBIDDEN_TOKENS:
                with self.subTest(locale=locale_name, key=key, token=token):
                    self.assertNotIn(token, lowered)

    def test_en_first_run_strings_have_no_forbidden_tokens(self) -> None:
        self._assert_no_forbidden("en")

    def test_zh_cn_first_run_strings_have_no_forbidden_tokens(self) -> None:
        self._assert_no_forbidden("zh-CN")


if __name__ == "__main__":
    unittest.main()
