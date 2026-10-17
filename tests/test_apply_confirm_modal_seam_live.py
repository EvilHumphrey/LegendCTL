"""Live-DPG regressions for the two Apply-path confirm modals on the front-door rule.

``AppShell._open_apply_device_confirm`` (Apply on a profile that carries Device
fields) and ``AppShell._open_no_restore_point_confirm`` (a write that could not
take a restore point first) were both built inline with a ``does_item_exist`` ->
``delete_item`` -> ``with dpg.window(modal=True)`` prelude. Under the benched
DPG modal law (2026-06-11, ``tools/diag_dpg_modal_thread_visibility.py``) a
modal created in a pass where another was showing exists but never becomes
interactive. Two ways in:

* the Apply confirm is ``no_close=False`` with no ``on_close``, so the titlebar
  X only HIDES it, and the next Apply deleted and created in ONE pass;
* both could be built inline in the tick a job's outcome modal held the create
  slot, so that create landed beside them (the no-restore-point confirm's own
  X runs an ``on_close`` that deletes it, so for it this is the live limb and
  the leftover-copy tests below are defensive).

The cure (2026-10-17) is the front-door rule, ``AppShell._modal_open_needs_seam``:
a fresh open builds inline, and a stale copy, a pending same-key request or a
held create slot sends the open through ``AppShell._defer_modal_swap``.

Scope, as in the precedents (``tests/test_app_shell_modal_seam_live.py``): no
viewport and no render loop, so these tests prove the mechanism -- teardown and
create land in different passes -- not that the landed modal takes a click.
Fail-on-base works because the inline shape built the fresh buttons
synchronously; on the fix they appear only on the second drain.

A real-DPG context can segfault on interpreter teardown on Windows
(exit-139 != FAIL -- re-run); each test creates and destroys its own context.
"""

from __future__ import annotations

import unittest
from unittest import mock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import empty_snapshot, make_shell
from zd_app import i18n
from zd_app.models import WrapperProfile
from zd_app.services.settings_service import PollingRate
from zd_app.ui.app_shell import (
    APPLY_DEVICE_CONFIRM_MODAL,
    APPLY_NO_RESTORE_POINT_CONTINUE_BUTTON,
    APPLY_NO_RESTORE_POINT_MODAL,
)

_DEVICE_PROFILE_ONLY = "apply_device_confirm_profile_only_button"


class _ConfirmSeamCase(unittest.TestCase):
    def setUp(self) -> None:
        i18n.set_locale("en")
        self.addCleanup(i18n.set_locale, "en")
        dpg.create_context()
        self.addCleanup(dpg.destroy_context)

    def _shell(self, *, armed: bool = True):
        shell = make_shell(settings_service=mock.MagicMock())
        shell._dpg_context_ready = True
        shell._defer_ui_armed = armed
        shell.rebuild_current_screen = mock.MagicMock()
        shell.current_screen = "controller"
        return shell

    @staticmethod
    def _drain(shell) -> None:
        shell._drain_deferred_ui_calls()

    @staticmethod
    def _stale(tag: str) -> None:
        # The titlebar-X shape: the previous instance still exists, hidden.
        with dpg.window(tag=tag, modal=True, show=False):
            dpg.add_text("stale")

    @staticmethod
    def _profile() -> WrapperProfile:
        return WrapperProfile(
            name="seam-profile",
            snapshot=empty_snapshot(polling_rate=PollingRate.HZ_8000),
        )


class DeviceConfirmFrontDoorTests(_ConfirmSeamCase):
    def _open(self, shell) -> None:
        profile = self._profile()
        shell._open_apply_device_confirm(profile.name, profile)

    def test_fresh_open_builds_inline(self) -> None:
        shell = self._shell()
        self._open(shell)
        self.assertTrue(dpg.does_item_exist(_DEVICE_PROFILE_ONLY))
        self.assertTrue(shell._deferred_ui_calls.empty())

    def test_reopen_after_the_x_splits_teardown_from_create(self) -> None:
        # Fail-on-base: the inline prelude deleted the stale modal and built
        # the fresh buttons in the same call.
        shell = self._shell()
        self._stale(APPLY_DEVICE_CONFIRM_MODAL)

        self._open(shell)
        self.assertFalse(
            dpg.does_item_exist(_DEVICE_PROFILE_ONLY),
            "the reopen built its buttons in the pass that deletes the stale modal",
        )

        self._drain(shell)  # teardown pass
        self.assertFalse(dpg.does_item_exist(APPLY_DEVICE_CONFIRM_MODAL))
        self.assertFalse(dpg.does_item_exist(_DEVICE_PROFILE_ONLY))

        self._drain(shell)  # create pass, a frame later
        self.assertTrue(dpg.does_item_exist(_DEVICE_PROFILE_ONLY))
        self.assertTrue(dpg.get_item_configuration(APPLY_DEVICE_CONFIRM_MODAL)["show"])

    def test_open_while_another_transition_holds_the_slot_is_arbitrated(self) -> None:
        # The R2-5 race: a job's outcome modal requested in the tick that
        # re-enabled Apply. Before its teardown the click supersedes it, so
        # exactly one create runs, and it is the confirm.
        shell = self._shell()
        other = []
        shell._defer_modal_swap(lambda: other.append("outcome"), key="outcome")

        self._open(shell)
        self.assertFalse(dpg.does_item_exist(_DEVICE_PROFILE_ONLY))
        self._drain(shell)
        self._drain(shell)

        self.assertEqual(other, [])
        self.assertTrue(dpg.does_item_exist(_DEVICE_PROFILE_ONLY))

    def test_open_after_the_holder_tore_down_is_dropped_and_reported(self) -> None:
        shell = self._shell()
        shell._update_apply_status = mock.MagicMock()
        other = []
        shell._defer_modal_swap(lambda: other.append("outcome"), key="outcome")
        self._drain(shell)  # the holder's teardown ran

        self._open(shell)
        self._drain(shell)

        self.assertEqual(other, ["outcome"])
        self.assertFalse(dpg.does_item_exist(APPLY_DEVICE_CONFIRM_MODAL))
        shell._update_apply_status.assert_called_once_with(
            i18n.t("shell.modal_request_dropped"), None
        )

    def test_a_seamed_create_does_not_open_for_a_changed_controller(self) -> None:
        # The body would show the new unit's values under a confirm bound to
        # the old one. Its buttons would refuse; the create says why instead.
        shell = self._shell()
        shell._discard_stale_controller_operation = mock.MagicMock()
        self._stale(APPLY_DEVICE_CONFIRM_MODAL)
        clicked_key = shell._controller_presence_key()
        clicked_generation = shell._controller_presence_generation

        self._open(shell)
        shell._controller_presence_generation += 1
        self._drain(shell)
        self._drain(shell)

        self.assertFalse(dpg.does_item_exist(_DEVICE_PROFILE_ONLY))
        # The same discard every other stale-presence refusal takes (it
        # records apply.device_changed and observes the new presence).
        shell._discard_stale_controller_operation.assert_called_once_with(
            clicked_key, clicked_generation
        )

    def test_presence_is_bound_at_the_click_not_at_create(self) -> None:
        # A seamed open lands two passes later. Its buttons must carry the
        # controller presence the user clicked against, so a controller
        # change in between is refused rather than applied to the new unit.
        shell = self._shell()
        self._stale(APPLY_DEVICE_CONFIRM_MODAL)
        clicked_key = shell._controller_presence_key()
        clicked_generation = shell._controller_presence_generation

        self._open(shell)
        shell._controller_presence_generation = clicked_generation + 1
        # Let the create through (the bail is pinned by the test above) to see
        # what the landed buttons carry.
        shell._controller_presence_token_is_current = lambda *_args: True
        self._drain(shell)
        self._drain(shell)

        resolved = mock.MagicMock()
        shell._apply_wrapper_profile_resolved = resolved
        dpg.get_item_callback(_DEVICE_PROFILE_ONLY)()
        kwargs = resolved.call_args.kwargs
        self.assertEqual(kwargs["_presence_key"], clicked_key)
        self.assertEqual(kwargs["_presence_generation"], clicked_generation)


class NoRestorePointConfirmFrontDoorTests(_ConfirmSeamCase):
    def _open(self, shell, on_continue=None) -> None:
        shell._open_no_restore_point_confirm(on_continue=on_continue or (lambda: None))

    def test_fresh_open_builds_inline(self) -> None:
        shell = self._shell()
        self._open(shell)
        self.assertTrue(dpg.does_item_exist(APPLY_NO_RESTORE_POINT_CONTINUE_BUTTON))
        self.assertTrue(shell._deferred_ui_calls.empty())

    def test_a_leftover_copy_splits_teardown_from_create(self) -> None:
        # Defensive: this modal's on_close deletes it, so the X leaves nothing
        # behind; a leftover copy would come only from a future change.
        shell = self._shell()
        self._stale(APPLY_NO_RESTORE_POINT_MODAL)

        self._open(shell)
        self.assertFalse(
            dpg.does_item_exist(APPLY_NO_RESTORE_POINT_CONTINUE_BUTTON),
            "the open built its buttons in the pass that deletes the leftover modal",
        )

        self._drain(shell)
        self.assertFalse(dpg.does_item_exist(APPLY_NO_RESTORE_POINT_MODAL))
        self._drain(shell)
        self.assertTrue(dpg.does_item_exist(APPLY_NO_RESTORE_POINT_CONTINUE_BUTTON))

    def test_the_x_deletes_this_modal_so_nothing_stale_survives(self) -> None:
        # Pins the property the paragraph above leans on: the X's on_close
        # deletes the modal rather than leaving it hidden.
        shell = self._shell()
        cancelled = []
        shell._open_no_restore_point_confirm(
            on_continue=lambda: None, on_cancel=lambda: cancelled.append(True)
        )
        on_close = dpg.get_item_configuration(APPLY_NO_RESTORE_POINT_MODAL)["on_close"]
        on_close()
        self.assertFalse(dpg.does_item_exist(APPLY_NO_RESTORE_POINT_MODAL))
        self.assertEqual(cancelled, [True])

    def test_continue_still_reaches_the_write_after_a_seamed_open(self) -> None:
        # Anti-regression for the deferral itself: the on_continue the caller
        # passed must be the one the landed modal's Continue runs.
        shell = self._shell()
        self._stale(APPLY_NO_RESTORE_POINT_MODAL)
        continued = []

        self._open(shell, on_continue=lambda: continued.append(True))
        self._drain(shell)
        self._drain(shell)
        dpg.get_item_callback(APPLY_NO_RESTORE_POINT_CONTINUE_BUTTON)()

        self.assertEqual(continued, [True])
        self.assertFalse(dpg.does_item_exist(APPLY_NO_RESTORE_POINT_MODAL))

    def test_open_while_another_transition_holds_the_slot_is_arbitrated(self) -> None:
        shell = self._shell()
        other = []
        shell._defer_modal_swap(lambda: other.append("outcome"), key="outcome")

        self._open(shell)
        self.assertFalse(dpg.does_item_exist(APPLY_NO_RESTORE_POINT_CONTINUE_BUTTON))
        self._drain(shell)
        self._drain(shell)

        self.assertEqual(other, [])
        self.assertTrue(dpg.does_item_exist(APPLY_NO_RESTORE_POINT_CONTINUE_BUTTON))

    def test_unarmed_open_with_a_stale_copy_still_builds_synchronously(self) -> None:
        # The synchronous contract headless paths rely on: unarmed, the seam
        # runs teardown and create inline, so the modal is there on return.
        shell = self._shell(armed=False)
        self._stale(APPLY_NO_RESTORE_POINT_MODAL)

        self._open(shell)

        self.assertTrue(dpg.does_item_exist(APPLY_NO_RESTORE_POINT_CONTINUE_BUTTON))


if __name__ == "__main__":
    unittest.main()
