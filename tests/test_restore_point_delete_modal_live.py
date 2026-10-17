"""Live-DPG regression for the Restore Points delete-confirm modal.

Bug (found 2026-07-24 by the asymmetry sweep, fixed 2026-07-25):
``restore_points._on_open_delete_confirm`` built its confirm modal with the
pre-fix inline shape -- ``does_item_exist`` -> ``delete_item`` -> inline
``with dpg.window(modal=True)`` -- pairing a teardown with a create in ONE
render pass. Under the benched DPG modal law (2026-06-11,
``tools/diag_dpg_modal_thread_visibility.py``) the fresh modal then EXISTS but
never becomes interactive: the Confirm button is dead, no exception, nothing
logged. The screen's own docstring said it mirrored
``AppShell.confirm_delete_named_wrapper_profile`` -- it mirrored the version
that was later cured at ``app_shell.py:4807-4811`` and never carried across.
That is this repo's recurring defect shape: a fix applied at one call site and
not its sibling.

Reachable without exotic state: the modal is built ``no_close=False``, so the
titlebar X only HIDES the window rather than deleting it. Open Delete on one
row, dismiss with the X, open Delete on another row -> delete+create in one
pass -> dead Delete button on a destructive action. That the item can outlive
its own screen is already assumed elsewhere in the codebase --
``TAG_DELETE_CONFIRM_MODAL`` is listed in
``app_shell.CLEAR_DIAGNOSTIC_LOGS_MODAL_SWAP_DELETE_TAGS``.

Like the wrapper-profile precedent this file is copied from
(``tests/test_wrapper_profile_delete_modal_live.py``), the visibility law
itself only manifests with a real viewport + render loop, which the headless
suite cannot host. What this CAN prove against a real DPG context is the
*cure's mechanism* -- teardown and create land in different drain passes, and
the rendered frame between them is what makes the fresh modal interactive:

  * BASE (inline ``with dpg.window``): the Confirm button is created
    synchronously -- it exists the instant the call returns.
  * FIX (``_defer_modal_swap``): the create is deferred; the Confirm button
    does NOT exist when the call returns, the stale modal is torn down on the
    first drain pass, and the fresh Confirm button only appears on the second.

So ``test_delete_confirm_create_is_split_from_teardown`` FAILS on base and
PASSES on the fix. Mocked tests structurally cannot catch any of this (the
documented DPG modal law), which is why the screen's existing mocked coverage
in ``tests/test_restore_points_screen.py`` stayed green through the bug.

A real-DPG context can segfault on interpreter teardown on Windows
(exit-139 != FAIL -- re-run); each test creates/destroys its own context,
mirroring the precedent file.
"""

from __future__ import annotations

import unittest
from unittest import mock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n
from zd_app.ui.app_shell import CLEAR_DIAGNOSTIC_LOGS_MODAL_SWAP_DELETE_TAGS
from zd_app.ui.screens import restore_points as screen

_MODAL = screen.TAG_DELETE_CONFIRM_MODAL
_CONFIRM_BTN = screen.TAG_DELETE_CONFIRM_CONFIRM_BUTTON
_CANCEL_BTN = screen.TAG_DELETE_CONFIRM_CANCEL_BUTTON

_RP_ID = "rp-20260725-120000"
_RP_TITLE = "Before manual write"


class _FakeRestorePointService:
    """Minimal stateful stand-in: delete actually removes; list reflects it."""

    def __init__(self, ids=(_RP_ID,), *, delete_returns=True):
        self.ids = list(ids)
        self.calls: list[tuple[str, tuple]] = []
        self._delete_returns = delete_returns

    def delete(self, rp_id: str) -> bool:
        self.calls.append(("delete", (rp_id,)))
        if not self._delete_returns:
            return False
        if rp_id in self.ids:
            self.ids.remove(rp_id)
            return True
        return False


class RestorePointDeleteModalLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n.set_locale("en")
        self.addCleanup(i18n.set_locale, "en")

    def _build_shell(self, service):
        """A real-context shell wired for the row-delete flow.

        The screen-rebuild side path is orthogonal to the modal seam, so it is
        stubbed; everything the seam touches is the real AppShell machinery.
        """

        shell = make_shell(
            settings_service=mock.MagicMock(),
            restore_point_service=service,
        )
        shell.restore_point_service = service
        shell._dpg_context_ready = True
        # Drive the deferred-UI machinery by hand, as a render loop would.
        shell._defer_ui_armed = True
        shell.rebuild_current_screen = mock.MagicMock()
        # The confirm is screen-scoped (owner_screen="restore_points"), so the
        # shell is parked where the row's More menu lives; a test shell
        # defaults to home, where every deferred create would be abandoned.
        shell.current_screen = "restore_points"
        return shell

    def _drain(self, shell) -> None:
        """One render-thread drain pass (one rendered frame)."""

        shell._drain_deferred_ui_calls()

    def test_delete_confirm_create_is_split_from_teardown(self) -> None:
        # Fail-on-base / pass-on-fix: the Confirm button must NOT be created in
        # the same pass the stale modal is torn down.
        dpg.create_context()
        try:
            service = _FakeRestorePointService()
            shell = self._build_shell(service)

            # The titlebar-X shape: a previous confirm modal is still present
            # but hidden. This is what the inline base deleted and recreated in
            # one pass.
            with dpg.window(tag=_MODAL, modal=True, show=False):
                dpg.add_text("stale delete confirm")

            screen._on_open_delete_confirm(shell, _RP_ID, _RP_TITLE)

            # Synchronously after the call: the fresh Confirm button does not
            # yet exist (deferred). BASE creates it inline -> this fails.
            self.assertFalse(
                dpg.does_item_exist(_CONFIRM_BTN),
                "delete-confirm create must be deferred, not built inline "
                "(inline create shares the teardown pass -> dead button)",
            )

            # Pass 1: the stale modal is torn down; the fresh modal/button are
            # NOT created yet -- the create is queued for the next pass.
            self._drain(shell)
            self.assertFalse(
                dpg.does_item_exist(_CONFIRM_BTN),
                "create must land a pass AFTER the teardown (the rendered "
                "frame between is what cures the modal law)",
            )

            # Pass 2: the fresh modal + its buttons are created.
            self._drain(shell)
            self.assertTrue(dpg.does_item_exist(_MODAL))
            self.assertTrue(dpg.does_item_exist(_CONFIRM_BTN))
            self.assertTrue(dpg.does_item_exist(_CANCEL_BTN))
            # Anti-vacuity: the modal that landed is the real, shown one --
            # not the stale hidden window the test seeded.
            self.assertTrue(dpg.is_item_shown(_MODAL))
            self.assertTrue(dpg.get_item_configuration(_MODAL)["modal"])
            # The service is untouched until Confirm is actually clicked.
            self.assertEqual(service.calls, [])
        finally:
            dpg.destroy_context()

    def test_confirm_click_deletes_restore_point(self) -> None:
        # End-to-end: drive the seam to completion, then invoke the real
        # Confirm callback -- the behavior the dead button blocked on base.
        dpg.create_context()
        try:
            service = _FakeRestorePointService()
            shell = self._build_shell(service)

            screen._on_open_delete_confirm(shell, _RP_ID, _RP_TITLE)
            self._drain(shell)
            self._drain(shell)
            self.assertTrue(dpg.does_item_exist(_CONFIRM_BTN))

            # Invoke the real button's callback -- the headless equivalent of
            # the click the modal law would have eaten. Four positional args:
            # the load-bearing ``lambda *args`` idiom must survive the move
            # into the deferred closure.
            callback = dpg.get_item_configuration(_CONFIRM_BTN)["callback"]
            callback(_CONFIRM_BTN, None, None, None)

            self.assertEqual(service.calls, [("delete", (_RP_ID,))])
            self.assertEqual(service.ids, [])
            state = shell.restore_points_screen_state
            self.assertEqual(state.status_kind, "info")
            self.assertIn(_RP_TITLE, state.status_text)
            shell.rebuild_current_screen.assert_called_once()
            # The confirmed-delete path dismissed the modal.
            self.assertFalse(dpg.does_item_exist(_MODAL))
        finally:
            dpg.destroy_context()

    def test_cancel_click_closes_modal_and_deletes_nothing(self) -> None:
        dpg.create_context()
        try:
            service = _FakeRestorePointService()
            shell = self._build_shell(service)

            screen._on_open_delete_confirm(shell, _RP_ID, _RP_TITLE)
            self._drain(shell)
            self._drain(shell)

            callback = dpg.get_item_configuration(_CANCEL_BTN)["callback"]
            callback(_CANCEL_BTN, None, None, None)

            self.assertFalse(dpg.does_item_exist(_MODAL))
            self.assertEqual(service.calls, [])
            self.assertEqual(service.ids, [_RP_ID])
            shell.rebuild_current_screen.assert_not_called()
        finally:
            dpg.destroy_context()

    def test_double_fired_callback_does_not_delete_and_recreate_in_one_pass(
        self,
    ) -> None:
        # The coalescing key is the guard against a re-fired row callback
        # delete+recreating the modal in a single pass -- exactly the poison
        # the seam exists to avoid.
        dpg.create_context()
        try:
            service = _FakeRestorePointService()
            shell = self._build_shell(service)

            screen._on_open_delete_confirm(shell, _RP_ID, _RP_TITLE)
            screen._on_open_delete_confirm(shell, _RP_ID, _RP_TITLE)

            self._drain(shell)
            self._drain(shell)
            self.assertTrue(dpg.does_item_exist(_CONFIRM_BTN))

            # A second swap must not still be queued behind the first; if it
            # were, this drain would tear the live modal back down.
            self._drain(shell)
            self._drain(shell)
            self.assertTrue(
                dpg.does_item_exist(_MODAL),
                "a coalesced duplicate request must not tear down the modal "
                "that already landed",
            )
            self.assertTrue(dpg.does_item_exist(_CONFIRM_BTN))
        finally:
            dpg.destroy_context()

    def test_unarmed_shell_still_opens_the_modal_synchronously(self) -> None:
        # The headless/mocked contract: with no live render loop the seam runs
        # inline, so the modal is up the instant the call returns. This is what
        # keeps tests/test_restore_points_screen.py valid.
        dpg.create_context()
        try:
            service = _FakeRestorePointService()
            shell = self._build_shell(service)
            shell._defer_ui_armed = False

            screen._on_open_delete_confirm(shell, _RP_ID, _RP_TITLE)

            self.assertTrue(dpg.does_item_exist(_MODAL))
            self.assertTrue(dpg.does_item_exist(_CONFIRM_BTN))
        finally:
            dpg.destroy_context()

    def test_delete_confirm_may_open_over_the_row_more_popup(self) -> None:
        """The second, separate way this modal could fail to appear.

        Delete is chosen from inside the row's ``More`` menu, which is a
        ``dpg.popup`` -- the only one in the app. DPG defaults
        ``no_open_over_existing_popup`` to True, which ImGui reads as
        NoOpenOverExistingPopup: the modal does not open AT ALL while a popup is
        up. Confirmed live 2026-08-01 on a build that carries the ``More``
        row action (not every build does): clicking ``Delete`` in the popup left the popup standing and no
        confirm modal appeared. A destructive action silently did nothing.

        This is NOT the same-pass modal law, and the seam does not fix it: the
        seam was already in use and the modal still never opened. The symptoms
        are identical from the outside, which is why the site sat as ``PROBE``.

        The cure is observable, so this gates it: the constructed window must
        carry the kwarg. It reads back True on base and False on the fix.
        """

        dpg.create_context()
        try:
            service = _FakeRestorePointService()
            shell = self._build_shell(service)

            screen._on_open_delete_confirm(shell, _RP_ID, _RP_TITLE)
            self._drain(shell)
            self._drain(shell)

            self.assertTrue(dpg.does_item_exist(_MODAL))
            self.assertFalse(
                dpg.get_item_configuration(_MODAL)["no_open_over_existing_popup"],
                "the delete confirm is opened from inside a dpg.popup, so it "
                "must opt out of NoOpenOverExistingPopup or it never opens",
            )
        finally:
            dpg.destroy_context()

    def _assert_navigation_abandons_the_confirm(
        self, *, navigate_after_teardown: bool
    ) -> None:
        """A pending confirm must not land over the screen the user moved to.

        The create captures this row's rp_id; landing over Home would offer to
        delete a restore point the user can no longer see. Fail-on-base: with
        no ``owner_screen`` the seam has no owner, ``switch_screen`` cannot
        cancel the transition, and the confirm lands over Home.
        """

        dpg.create_context()
        try:
            service = _FakeRestorePointService()
            shell = self._build_shell(service)

            screen._on_open_delete_confirm(shell, _RP_ID, _RP_TITLE)
            if navigate_after_teardown:
                self._drain(shell)
            shell.switch_screen("home")
            self._drain(shell)
            self._drain(shell)

            self.assertFalse(
                dpg.does_item_exist(_MODAL),
                "a pending delete confirm must not open over the screen the "
                "user navigated to",
            )
            self.assertFalse(shell._modal_swap_pending)
            self.assertEqual(service.calls, [])

            # Anti-wedge: back on Restore Points, Delete opens normally.
            shell.switch_screen("restore_points")
            screen._on_open_delete_confirm(shell, _RP_ID, _RP_TITLE)
            self._drain(shell)
            self._drain(shell)
            self.assertTrue(dpg.does_item_exist(_CONFIRM_BTN))
        finally:
            dpg.destroy_context()

    def test_navigation_between_the_passes_abandons_the_confirm(self) -> None:
        self._assert_navigation_abandons_the_confirm(navigate_after_teardown=True)

    def test_navigation_before_the_teardown_abandons_the_confirm(self) -> None:
        self._assert_navigation_abandons_the_confirm(navigate_after_teardown=False)

    def test_stale_modal_sweep_still_covers_this_modal(self) -> None:
        # The precondition for the bug: the codebase already knows this modal
        # can be left standing. If it is ever dropped from the sweep set that
        # is a deliberate decision, not an accident.
        self.assertIn(_MODAL, CLEAR_DIAGNOSTIC_LOGS_MODAL_SWAP_DELETE_TAGS)


if __name__ == "__main__":
    unittest.main()
