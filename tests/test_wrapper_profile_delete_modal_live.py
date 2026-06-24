"""Live-DPG regression for the wrapper-profile delete-confirm modal.

Bug (2026-06-23): ``confirm_delete_named_wrapper_profile`` built the
delete-confirm popup with a plain inline ``with dpg.window(...)``, bypassing
the ``_defer_modal_swap`` seam. Under the benched DPG modal law
(2026-06-11, ``tools/diag_dpg_modal_thread_visibility.py``) a modal created in
the same render pass another modal was showing / torn down EXISTS but never
becomes interactive -- no exception, nothing logged. After a Save + Apply the
Confirm button was dead and the delete callback never ran, so a profile could
not be deleted.

The visibility law itself only manifests with a real viewport + render loop
(see the diag tool), which the headless suite cannot host. What this test CAN
do against a real DPG context is prove the *cure's mechanism*: the fix routes
the create through ``_defer_modal_swap``, which splits teardown and create
across two drain passes (the rendered frame between them is what makes the
fresh modal interactive). With deferral armed:

  * BASE (inline ``with dpg.window``): the Confirm button is created
    synchronously inside the call -- it exists the instant the call returns.
  * FIX (``_defer_modal_swap``): the create is deferred; the Confirm button
    does NOT exist when the call returns, the prior popup is torn down on the
    first drain pass, and the fresh Confirm button only appears on the second.

So ``test_delete_confirm_create_is_split_from_teardown`` FAILS on base (the
button exists too early) and PASSES on the fix. The companion test then drives
the real Confirm callback and asserts the profile is removed from the store and
the footer combo -- the end-to-end wiring the dead button blocked.

A real-DPG context can segfault on interpreter teardown on Windows
(exit-139 != FAIL -- re-run); each test creates/destroys its own context,
mirroring tests/test_crash_review_modal.py.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n

_POPUP = "wrapper_profile_delete_popup"
_CONFIRM_BTN = "wrapper_profile_delete_confirm_button"
_COMBO = "wrapper_profile_combo"


class _FakeWrapperStore:
    """Minimal stateful stand-in: delete actually removes; list reflects it."""

    def __init__(self, names):
        self._names = list(names)

    def list_profiles(self):
        return [SimpleNamespace(name=n) for n in self._names]

    def delete(self, name):
        if name in self._names:
            self._names.remove(name)
            return True
        return False


class WrapperProfileDeleteModalLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n.set_locale("en")
        self.addCleanup(i18n.set_locale, "en")

    def _build_shell(self, names):
        """A real-context shell wired for the delete flow.

        The combo + a host window are real DPG items; the store is a stateful
        fake; the screen-rebuild / status-record side paths (orthogonal to the
        modal bug) are stubbed so the test stays focused on the seam + wiring.
        """

        shell = make_shell(settings_service=mock.MagicMock())
        shell.wrapper_profile_store = _FakeWrapperStore(names)
        shell._dpg_context_ready = True
        shell._footer_profile_combo_ready = True
        # Drive the deferred-UI machinery by hand, as a render loop would.
        shell._defer_ui_armed = True
        # Side paths the dead button never reached; not what this guards.
        shell.rebuild_current_screen = mock.MagicMock()
        shell._record_settings_apply_result = mock.MagicMock()
        shell.refresh_shell = mock.MagicMock()

        with dpg.window(tag="live_host_window"):
            dpg.add_combo(tag=_COMBO, items=list(names), default_value=names[0])
        return shell

    def _drain(self, shell) -> None:
        """One render-thread drain pass (one rendered frame)."""

        shell._drain_deferred_ui_calls()

    def test_delete_confirm_create_is_split_from_teardown(self) -> None:
        # Fail-on-base / pass-on-fix: the Confirm button must NOT be created in
        # the same pass the request is made (and the prior popup is torn down).
        dpg.create_context()
        try:
            shell = self._build_shell(["Apex", "Boreas"])

            # A prior delete popup is already up -- the "Save + Apply then open
            # delete" shape. The seam must tear it down a frame BEFORE the fresh
            # create; the inline bug would delete + recreate in one pass.
            with dpg.window(tag=_POPUP, modal=True):
                dpg.add_text("stale popup")

            shell.confirm_delete_named_wrapper_profile("Apex")

            # Synchronously after the call: the fresh Confirm button does not yet
            # exist (deferred). BASE creates it inline -> this assertion fails.
            self.assertFalse(
                dpg.does_item_exist(_CONFIRM_BTN),
                "delete-confirm create must be deferred, not built inline "
                "(inline create shares the teardown pass -> dead button)",
            )

            # Pass 1: the stale popup is torn down; the fresh popup/button are
            # NOT created yet -- the create is queued for the next pass.
            self._drain(shell)
            self.assertFalse(
                dpg.does_item_exist(_CONFIRM_BTN),
                "create must land a pass AFTER the teardown (the rendered frame "
                "between is what cures the modal law)",
            )

            # Pass 2: the fresh popup + Confirm button are created.
            self._drain(shell)
            self.assertTrue(dpg.does_item_exist(_POPUP))
            self.assertTrue(dpg.does_item_exist(_CONFIRM_BTN))
        finally:
            dpg.destroy_context()

    def test_confirm_click_deletes_profile_from_store_and_combo(self) -> None:
        # End-to-end: drive the seam to completion, then invoke the real Confirm
        # callback (the "click") and assert the delete actually happens -- the
        # behavior the dead button blocked on base.
        dpg.create_context()
        try:
            shell = self._build_shell(["Apex", "Boreas"])

            shell.confirm_delete_named_wrapper_profile("Apex")
            # Drain both passes (teardown, then create) so the real button lands.
            self._drain(shell)
            self._drain(shell)
            self.assertTrue(dpg.does_item_exist(_CONFIRM_BTN))

            # Invoke the real button's callback with its real user_data -- the
            # headless equivalent of the click the modal law would have eaten.
            config = dpg.get_item_configuration(_CONFIRM_BTN)
            callback = config["callback"]
            user_data = dpg.get_item_user_data(_CONFIRM_BTN)
            self.assertEqual(user_data, "Apex")
            callback(_CONFIRM_BTN, None, user_data)

            # Profile gone from the store and from the live footer combo.
            remaining = [p.name for p in shell.wrapper_profile_store.list_profiles()]
            self.assertEqual(remaining, ["Boreas"])
            self.assertEqual(dpg.get_item_configuration(_COMBO)["items"], ["Boreas"])
            # The popup was dismissed by the confirmed-delete path.
            self.assertFalse(dpg.does_item_exist(_POPUP))
        finally:
            dpg.destroy_context()

    def test_no_selection_records_failure_and_opens_no_popup(self) -> None:
        # Empty selection short-circuits before any modal work (unchanged
        # behavior, guarded here against the seam refactor).
        dpg.create_context()
        try:
            shell = self._build_shell(["Apex"])

            shell.confirm_delete_named_wrapper_profile("")
            self._drain(shell)
            self._drain(shell)

            self.assertFalse(dpg.does_item_exist(_CONFIRM_BTN))
            shell._record_settings_apply_result.assert_called_once()
            self.assertIs(shell._record_settings_apply_result.call_args.args[0], False)
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
