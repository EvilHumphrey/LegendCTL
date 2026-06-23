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


_MODAL = "first_run_ack_modal"
_ACCEPT = "first_run_ack_accept_button"
_DECLINE = "first_run_ack_decline_button"
_DISCLAIMER = "first_run_ack_disclaimer_text"
_RISK_TAGS = (
    "first_run_ack_risk_writes_text",
    "first_run_ack_risk_hardware_text",
    "first_run_ack_risk_reversible_text",
    "first_run_ack_risk_as_is_text",
)

_LOCALES_DIR = Path("zd_app/i18n/locales")
_FIRST_RUN_KEYS = (
    "first_run.title",
    "first_run.intro",
    "first_run.risk.writes",
    "first_run.risk.hardware",
    "first_run.risk.reversible",
    "first_run.risk.as_is",
    "first_run.accept",
    "first_run.decline",
)


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


class FirstRunModalContentTests(unittest.TestCase):
    """The modal renders the reused ZD disclaimer + the risk language."""

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

    def test_modal_renders_all_risk_lines(self) -> None:
        dpg.create_context()
        try:
            shell = make_shell(settings=AppSettings(first_run_acknowledged=False))
            shell._dpg_context_ready = True
            shell._show_first_run_acknowledgment_modal_if_needed()
            for tag in _RISK_TAGS:
                self.assertTrue(dpg.does_item_exist(tag), f"missing risk widget {tag}")
                self.assertTrue(dpg.get_value(tag), f"empty risk widget {tag}")
        finally:
            dpg.destroy_context()

    def test_risk_lines_cover_required_points(self) -> None:
        dpg.create_context()
        try:
            shell = make_shell(settings=AppSettings(first_run_acknowledged=False))
            shell._dpg_context_ready = True
            shell._show_first_run_acknowledgment_modal_if_needed()
            writes = dpg.get_value("first_run_ack_risk_writes_text")
            reversible = dpg.get_value("first_run_ack_risk_reversible_text")
            as_is = dpg.get_value("first_run_ack_risk_as_is_text").lower()
            # writes over USB HID; reversible via vendor restore + Restore Points;
            # provided "as is" without warranty at your own risk.
            self.assertIn("USB HID", writes)
            self.assertIn("Restore", reversible)
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
