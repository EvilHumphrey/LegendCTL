"""Live-DPG regressions for two modal sites cured 2026-08-01.

**1. Safe Import Browse — the front door does not come back.**
Benched 2026-08-01: a
``dpg.file_dialog(modal=True)`` opened while ``FILE_MODAL`` is showing sets that
window's ``visible`` to False permanently. It is hidden, not deleted, and
closing the dialog never restores it. ``open_browse`` never touched
``FILE_MODAL``, so DPG did that, not the app. Live symptom: Safe Import ->
``Browse...`` -> Cancel left the user on the bare Profiles tab with the import
flow gone. Both dialog exits now re-show the front door through the seam.

**2. The first-run consent gate built into an occupied pass.**
Registry ``DEFECT`` entry for ``_show_first_run_acknowledgment_modal_if_needed``:
``_consent_pending_write_allowed_or_refuse`` re-offers the gate from write entry
points only reachable while another modal is PROVABLY up -- the Safe Import
confirm modal owns the button reaching ``safe_import_apply``. The benched modal
law covers a predecessor merely left up, so the gate was built into that pass
and never rendered. It now rides the seam, hiding the standing modals rather
than deleting them so a half-finished import survives.

Both cures are mechanism-testable against a real context even though the
visibility law itself needs a viewport: teardown and create land in DIFFERENT
drain passes, so the thing that should not exist yet does not exist yet. Each
test FAILS on base and PASSES on the fix.

A real-DPG context can segfault on interpreter teardown on Windows
(exit-139 != FAIL -- re-run); each test creates and destroys its own context.
"""

from __future__ import annotations

import contextlib
import unittest
from unittest import mock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n
from zd_app.ui.screens import safe_import

_GATE = "first_run_ack_modal"


class _BrowseRestoreLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n.set_locale("en")
        self.addCleanup(i18n.set_locale, "en")

    def _shell(self):
        shell = make_shell(settings_service=mock.MagicMock())
        shell._dpg_context_ready = True
        shell._defer_ui_armed = True
        return shell

    def test_cancelling_browse_restores_the_front_door(self) -> None:
        dpg.create_context()
        try:
            shell = self._shell()
            with dpg.window(tag=safe_import.FILE_MODAL, modal=True):
                dpg.add_text("front door")

            captured: dict = {}
            real_file_dialog = dpg.file_dialog

            @contextlib.contextmanager
            def _spy(**kwargs):
                # Delegate to the real constructor -- add_file_extension only
                # accepts an mvFileDialog parent, so a stand-in window cannot
                # host it. Yield the real dialog id: open_browse binds it
                # (``as dialog``) and both exits check that exact item, so a
                # spy that yields None makes every exit return early.
                captured.update(kwargs)
                with real_file_dialog(**kwargs) as dialog:
                    yield dialog

            with mock.patch.object(dpg, "file_dialog", _spy):
                safe_import.open_browse(shell)

            # Both exits must be wired. BASE passes no cancel_callback at all,
            # so there is nothing to put the front door back -> fails here.
            self.assertIn("cancel_callback", captured)
            self.assertTrue(callable(captured["cancel_callback"]))

            # The front door is hidden, as DPG leaves it after opening a dialog.
            dpg.configure_item(safe_import.FILE_MODAL, show=False)

            captured["cancel_callback"]()

            # Deferred: not restored in the callback's own pass.
            self.assertFalse(
                dpg.get_item_configuration(safe_import.FILE_MODAL)["show"],
                "the re-show must not land in the pass the dialog tore down in",
            )

            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

            self.assertTrue(
                dpg.get_item_configuration(safe_import.FILE_MODAL)["show"],
                "cancelling Browse must put the Safe Import front door back",
            )
        finally:
            dpg.destroy_context()


class _ConsentReofferLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n.set_locale("en")
        self.addCleanup(i18n.set_locale, "en")

    def _shell(self):
        shell = make_shell(settings_service=mock.MagicMock())
        shell._dpg_context_ready = True
        shell._defer_ui_armed = True
        shell._record_settings_apply_result = mock.MagicMock()
        shell.settings.first_run_acknowledged = False
        shell._consent_pending_verify = True
        return shell

    def test_gate_is_not_built_into_the_standing_modals_pass(self) -> None:
        dpg.create_context()
        try:
            shell = self._shell()
            with dpg.window(tag=safe_import.CONFIRM_MODAL, modal=True):
                dpg.add_text("confirm")

            allowed = shell._consent_pending_write_allowed_or_refuse()
            self.assertFalse(allowed, "a pending-consent write must be refused")

            # BASE builds the gate inline, in the pass the confirm modal is
            # still up -> this fails there.
            self.assertFalse(
                dpg.does_item_exist(_GATE),
                "the consent gate must not be created while a modal is standing",
            )

            shell._drain_deferred_ui_calls()
            self.assertFalse(
                dpg.get_item_configuration(safe_import.CONFIRM_MODAL)["show"],
                "the standing modal must be HIDDEN on the teardown pass",
            )
            self.assertTrue(
                dpg.does_item_exist(safe_import.CONFIRM_MODAL),
                "hidden, not deleted -- the user's import must survive",
            )
            self.assertFalse(dpg.does_item_exist(_GATE))

            shell._drain_deferred_ui_calls()
            self.assertTrue(
                dpg.does_item_exist(_GATE),
                "the gate is built on the pass after the teardown",
            )
        finally:
            dpg.destroy_context()

    def test_accepting_brings_the_hidden_import_back(self) -> None:
        dpg.create_context()
        try:
            shell = self._shell()
            with dpg.window(tag=safe_import.CONFIRM_MODAL, modal=True):
                dpg.add_text("confirm")

            shell._consent_pending_write_allowed_or_refuse()
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()
            self.assertTrue(dpg.does_item_exist(_GATE))

            # Without this the test is VACUOUS: on base nothing is ever hidden,
            # so "is it shown afterwards" is trivially true and the test passes
            # against the bug. Proven by rebinding to the pre-fix body.
            self.assertFalse(
                dpg.get_item_configuration(safe_import.CONFIRM_MODAL)["show"],
                "precondition: the re-offer must have hidden the import modal",
            )
            self.assertEqual(
                shell._consent_reoffer_hidden_tags,
                (safe_import.CONFIRM_MODAL,),
            )

            dpg.get_item_callback("first_run_ack_accept_button")()

            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

            self.assertTrue(
                dpg.get_item_configuration(safe_import.CONFIRM_MODAL)["show"],
                "accepting consent must restore the import the gate interrupted",
            )
            self.assertEqual(shell._consent_reoffer_hidden_tags, ())
        finally:
            dpg.destroy_context()

    def test_verify_link_clears_rather_than_stranding(self) -> None:
        dpg.create_context()
        try:
            shell = self._shell()
            shell.switch_screen = mock.MagicMock()
            with dpg.window(tag=safe_import.CONFIRM_MODAL, modal=True):
                dpg.add_text("confirm")

            shell._consent_pending_write_allowed_or_refuse()
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

            with mock.patch(
                "zd_app.ui.app_shell.trust_front_door.open_trust_surface"
            ):
                dpg.get_item_callback("first_run_ack_verify_link")()

            # Navigated away: a hidden modal left behind has no route back.
            self.assertFalse(
                dpg.does_item_exist(safe_import.CONFIRM_MODAL),
                "the verify path must clear the hidden modal, not strand it",
            )
            self.assertEqual(shell._consent_reoffer_hidden_tags, ())
        finally:
            dpg.destroy_context()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
