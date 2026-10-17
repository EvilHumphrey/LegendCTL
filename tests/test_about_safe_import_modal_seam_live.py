"""Live-DPG regression for the About-Licenses and Safe-Import-front-door modals.

Bug (inventoried 2026-07-27, fixed the same day): two more user-triggerable
modal builders still carried this repo's recurring shape -- an existence check
-> ``delete_item(TAG)`` -> inline ``with dpg.window(tag=TAG, modal=True)``,
pairing a teardown with a create in ONE render pass:

  * ``about._open_license_view``       (About -> "Licenses" button)
  * ``safe_import.open_file_select``   (Controller -> "Safe Import" button)

The Safe Import front door follows the design shipped in v2.6.4
(LegendCTL PR #29):
a REOPEN -- a stale front door still exists, typically hidden by
the titlebar X -- goes through the seam, while a FRESH open, with no
predecessor, builds inline. An earlier variant routed every open through the
seam; these tests were rewritten to the shipped design on 2026-10-17. The
Safe Import seam tests therefore seed the stale front door first, which is the
only state in which the seam is involved, and separate tests pin the fresh
path: synchronous when nothing is pending, arbitrated when another transition
holds the create slot.

Under the benched DPG modal law (2026-06-11,
``tools/diag_dpg_modal_thread_visibility.py``) the fresh modal then EXISTS but
never becomes interactive: its buttons are dead, no exception is raised and
nothing is logged.

Both windows omit ``no_close``. That is not a second class of window: DearPyGui
passes the keyword through with a plain ``False`` default and keeps no "unset"
sentinel, and with ``no_close`` false the titlebar X only writes ``show=false``
-- the item survives. So the repro needs no exotic state: open the modal,
dismiss it with the X, open it again -> delete+create in one pass.

The Safe Import case is the sharper one. Every DOWNSTREAM modal in that flow
(preview, apply-confirm, result) was already routed through the seam by its
caller in the shell; only the front door was left inline. A fix landing at some
call sites and not their sibling is the defect shape this codebase keeps
producing, and it is why the flow could look covered while its entry point was
not.

What this file can and cannot prove. The visibility law itself only manifests
with a real viewport and a running render loop, which the headless suite cannot
host; nothing here observes an actual dead button. What it DOES prove against a
real DPG context is the *cure's mechanism* -- that teardown and create no
longer share a pass, which is the property the law cares about:

  * BASE (inline ``with dpg.window``): the child widgets are created
    synchronously -- they exist the instant the call returns.
  * FIX (``_defer_modal_swap``): the create is deferred; the child widgets do
    NOT exist when the call returns, the stale modal is torn down on the first
    drain pass, and the fresh widgets only appear on the second.

So each ``*_create_is_split_from_teardown`` test FAILS on base and PASSES on
the fix. Mocked tests structurally cannot catch any of this (the documented DPG
modal law), which is why ``tests/test_about.py`` and
``tests/test_safe_import.py`` stayed green through the bug -- and, because the
``getattr`` guard's inline fallback keeps the synchronous contract, stay green
through the fix.

Neither site carries a per-request payload -- both take only ``shell`` -- so
both use plain drop-first coalescing rather than the parked-payload,
last-request-wins policy the Modules assign/notes modals need. The
``*_coalesced_duplicate_*`` tests pin that a re-fired click cannot tear the
landed modal back down, and ``*_leaves_downstream_modals_alone`` pins the
deliberate scope of the Safe Import teardown list.

Both are also screen-scoped. Licenses is an About button and passes
``owner_screen=`` to the seam, so ``switch_screen`` cancels its pending
transition. The Safe Import front door is a Controller button; its deferred
create checks the screen it captured at request time and returns if the user
has moved on (the shipped mechanism). Either way, a request made a frame before
the user clicks a nav item is abandoned rather than opened over the destination
screen. The ``*_navigation_*_abandons_the_create`` tests cover both
interleavings (navigate before the teardown pass, and between the two passes)
with an anti-wedge reopen, and a third test pins that staying put still opens
normally.

A real-DPG context can segfault on interpreter teardown on Windows
(exit-139 != FAIL -- re-run); each test creates and destroys its own context,
mirroring ``tests/test_restore_point_delete_modal_live.py``.
"""

from __future__ import annotations

import unittest
from unittest import mock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n
from zd_app.ui.themes import LEGACY_COLOR_ALIASES
from zd_app.ui.screens import about as about_screen
from zd_app.ui.screens import safe_import as safe_import_screen


class _NoSeamShell:
    """A screen-module shell with no ``_defer_modal_swap`` at all.

    Stands in for the lightweight fakes the mocked suite hands screens, which
    is the branch the ``getattr`` guard exists for. It carries only the palette
    the two builders read.
    """

    COLORS = LEGACY_COLOR_ALIASES


_ABOUT_MODAL = about_screen.LICENSE_MODAL
_ABOUT_CLOSE_BTN = "about_license_close_btn"

_FILE_MODAL = safe_import_screen.FILE_MODAL
_FILE_PATH_INPUT = safe_import_screen.PATH_INPUT


class _ModalSeamTestBase(unittest.TestCase):
    """Shared rig: a real AppShell whose deferred-UI queue is drained by hand.

    Everything the seam touches is the real AppShell machinery; only the
    screen-rebuild side path (orthogonal to the seam) is substituted.
    """

    #: Canonical screen id the site under test is scoped to. Both sites are
    #: screen-scoped (Licenses through ``owner_screen=``, the Safe Import front
    #: door through its deferred create's own screen check), so an armed shell
    #: parked on the default ``home`` would have every deferred create
    #: correctly abandoned as stale navigation. Subclasses set this to the
    #: screen the entry button actually lives on.
    OWNER_SCREEN = "home"

    def setUp(self) -> None:
        i18n.set_locale("en")
        self.addCleanup(i18n.set_locale, "en")

    def _new_context(self) -> None:
        dpg.create_context()
        self.addCleanup(dpg.destroy_context)

    def _build_shell(self):
        shell = make_shell(settings_service=mock.MagicMock())
        shell._dpg_context_ready = True
        # Drive the deferred-UI machinery by hand, as a render loop would.
        shell._defer_ui_armed = True
        shell.rebuild_current_screen = mock.MagicMock()
        shell.current_screen = self.OWNER_SCREEN
        return shell

    def _assert_navigation_abandons_the_create(
        self, shell, invoke, *, modal_tag: str, navigate_after_teardown: bool
    ) -> None:
        """A screen-scoped create must not open over the destination screen.

        Both interleavings: the user navigates before the teardown pass, and
        between the teardown and create passes. Fail-on-base: with no screen
        scoping (no ``owner_screen`` for Licenses; no screen check in the Safe
        Import front door's deferred create) the modal lands on top of Home.
        """

        invoke()
        if navigate_after_teardown:
            self._drain(shell)
        shell.switch_screen("home")
        self._drain(shell)
        self._drain(shell)

        self.assertFalse(
            dpg.does_item_exist(modal_tag),
            f"{modal_tag}: a pending screen-scoped modal must not open over "
            "the screen the user navigated to",
        )
        self.assertFalse(shell._modal_swap_pending)

        # Anti-wedge: coming back must still open normally.
        shell.switch_screen(self.OWNER_SCREEN)
        invoke()
        self._drain(shell)
        self._drain(shell)
        self.assertTrue(dpg.does_item_exist(modal_tag))

    def _drain(self, shell) -> None:
        """One render-thread drain pass (one rendered frame)."""

        shell._drain_deferred_ui_calls()

    def _seed_stale_hidden_modal(self, tag: str) -> None:
        """The titlebar-X shape: the previous modal is still present, hidden.

        This is precisely what the inline base deleted and recreated in one
        pass.
        """

        with dpg.window(tag=tag, modal=True, show=False):
            dpg.add_text("stale modal")

    def _assert_split_from_teardown(
        self, shell, invoke, *, modal_tag: str, child_tag: str
    ) -> None:
        """The fail-on-base assertion sequence, shared by both sites."""

        self._seed_stale_hidden_modal(modal_tag)

        invoke()

        # Synchronously after the call the fresh child does not yet exist
        # (deferred). BASE builds it inline -> this fails.
        self.assertFalse(
            dpg.does_item_exist(child_tag),
            f"{child_tag}: the modal create must be deferred, not built inline "
            "(an inline create shares the teardown pass -> dead buttons)",
        )

        # Pass 1: the stale modal is torn down; the create is queued behind it.
        self._drain(shell)
        self.assertFalse(
            dpg.does_item_exist(child_tag),
            f"{child_tag}: the create must land a pass AFTER the teardown (the "
            "rendered frame between is what cures the modal law)",
        )

        # Pass 2: the fresh modal and its children are created.
        self._drain(shell)
        self.assertTrue(dpg.does_item_exist(modal_tag))
        self.assertTrue(dpg.does_item_exist(child_tag))
        # Anti-vacuity: what landed is the real, shown modal -- not the stale
        # hidden window the test seeded.
        self.assertTrue(dpg.is_item_shown(modal_tag))
        self.assertTrue(dpg.get_item_configuration(modal_tag)["modal"])

    def _assert_coalesced_duplicate_survives(
        self, shell, invoke, *, modal_tag: str, child_tag: str
    ) -> None:
        """A re-fired click must not tear the landed modal back down.

        The coalescing key is the guard: without it the second request's
        teardown would run a pass after the first request's create.
        """

        invoke()
        invoke()

        self._drain(shell)
        self._drain(shell)
        self.assertTrue(dpg.does_item_exist(child_tag))

        self._drain(shell)
        self._drain(shell)
        self.assertTrue(
            dpg.does_item_exist(modal_tag),
            "a coalesced duplicate request must not tear down the modal that "
            "already landed",
        )
        self.assertTrue(dpg.does_item_exist(child_tag))

    def _assert_unarmed_opens_synchronously(
        self, shell, invoke, *, modal_tag: str, child_tag: str
    ) -> None:
        """The headless/mocked contract: no render loop -> the seam runs inline.

        This is what keeps tests/test_about.py and tests/test_safe_import.py
        valid.
        """

        shell._defer_ui_armed = False

        invoke()

        self.assertTrue(dpg.does_item_exist(modal_tag))
        self.assertTrue(dpg.does_item_exist(child_tag))


# ---------------------------------------------------------------------------
# About -- bundled-license modal (no payload)
# ---------------------------------------------------------------------------


class AboutLicenseModalSeamTests(_ModalSeamTestBase):
    OWNER_SCREEN = "about"

    def test_license_create_is_split_from_teardown(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assert_split_from_teardown(
            shell,
            lambda: about_screen._open_license_view(shell),
            modal_tag=_ABOUT_MODAL,
            child_tag=_ABOUT_CLOSE_BTN,
        )

    def test_license_coalesced_duplicate_keeps_the_landed_modal(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assert_coalesced_duplicate_survives(
            shell,
            lambda: about_screen._open_license_view(shell),
            modal_tag=_ABOUT_MODAL,
            child_tag=_ABOUT_CLOSE_BTN,
        )

    def test_license_unarmed_shell_opens_synchronously(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assert_unarmed_opens_synchronously(
            shell,
            lambda: about_screen._open_license_view(shell),
            modal_tag=_ABOUT_MODAL,
            child_tag=_ABOUT_CLOSE_BTN,
        )

    def test_license_shell_without_seam_still_opens_inline(self) -> None:
        # The other half of the getattr guard: a lightweight fake shell that
        # has no _defer_modal_swap at all must still get a modal, built the
        # pre-seam way. This is the branch the mocked suite exercises.
        self._new_context()
        shell = _NoSeamShell()
        self.assertFalse(hasattr(shell, "_defer_modal_swap"))

        about_screen._open_license_view(shell)

        self.assertTrue(dpg.does_item_exist(_ABOUT_MODAL))
        self.assertTrue(dpg.does_item_exist(_ABOUT_CLOSE_BTN))

    def test_license_close_button_works_after_the_seam(self) -> None:
        # End-to-end: drive the seam to completion, then invoke the real Close
        # callback -- the behavior the dead button blocked on base. Also pins
        # that moving the callback into the deferred closure kept the tag it
        # deletes correct.
        self._new_context()
        shell = self._build_shell()

        about_screen._open_license_view(shell)
        self._drain(shell)
        self._drain(shell)
        self.assertTrue(dpg.does_item_exist(_ABOUT_CLOSE_BTN))

        callback = dpg.get_item_configuration(_ABOUT_CLOSE_BTN)["callback"]
        callback()

        self.assertFalse(dpg.does_item_exist(_ABOUT_MODAL))

    def test_license_sections_all_land_after_the_swap(self) -> None:
        # Anti-vacuity for the deferred build: the whole modal body moved into
        # the closure, so assert the per-dependency sections are all there and
        # not just the window shell.
        self._new_context()
        shell = self._build_shell()

        about_screen._open_license_view(shell)
        self._drain(shell)
        self._drain(shell)

        for slug, _spdx, _header_key in about_screen.LICENSE_DEPS:
            section_tag = f"about_license_section_{slug.replace('-', '_')}"
            self.assertTrue(
                dpg.does_item_exist(section_tag),
                f"expected modal section tag {section_tag} to exist",
            )

    def test_license_navigation_between_the_passes_abandons_the_create(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assert_navigation_abandons_the_create(
            shell,
            lambda: about_screen._open_license_view(shell),
            modal_tag=_ABOUT_MODAL,
            navigate_after_teardown=True,
        )

    def test_license_navigation_before_the_teardown_abandons_the_create(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assert_navigation_abandons_the_create(
            shell,
            lambda: about_screen._open_license_view(shell),
            modal_tag=_ABOUT_MODAL,
            navigate_after_teardown=False,
        )

    def test_license_opens_normally_while_the_user_stays_on_about(self) -> None:
        # Anti-vacuity for the two navigation tests: the owner check must not
        # simply refuse every create.
        self._new_context()
        shell = self._build_shell()

        about_screen._open_license_view(shell)
        self._drain(shell)
        self._drain(shell)

        self.assertTrue(dpg.does_item_exist(_ABOUT_MODAL))


# ---------------------------------------------------------------------------
# Safe Import -- file-select front door (no payload)
# ---------------------------------------------------------------------------


class SafeImportFileSelectSeamTests(_ModalSeamTestBase):
    """The front door: a reopen rides the seam, a fresh open builds inline.

    Every test that exercises the seam seeds the stale front door first,
    because that is what makes an open a reopen. Without the seed these tests
    would only see the fresh inline path.
    """

    OWNER_SCREEN = "controller"

    def test_file_select_create_is_split_from_teardown(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assert_split_from_teardown(
            shell,
            lambda: safe_import_screen.open_file_select(shell),
            modal_tag=_FILE_MODAL,
            child_tag=_FILE_PATH_INPUT,
        )

    def test_file_select_coalesced_duplicate_keeps_the_landed_modal(self) -> None:
        self._new_context()
        shell = self._build_shell()
        # A reopen, so both requests reach the seam and the second is the
        # coalesced duplicate (a fresh first request would build inline).
        self._seed_stale_hidden_modal(_FILE_MODAL)
        self._assert_coalesced_duplicate_survives(
            shell,
            lambda: safe_import_screen.open_file_select(shell),
            modal_tag=_FILE_MODAL,
            child_tag=_FILE_PATH_INPUT,
        )

    def test_file_select_unarmed_shell_opens_synchronously(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assert_unarmed_opens_synchronously(
            shell,
            lambda: safe_import_screen.open_file_select(shell),
            modal_tag=_FILE_MODAL,
            child_tag=_FILE_PATH_INPUT,
        )

    def test_file_select_via_shell_entry_point_is_also_split(self) -> None:
        # The only production caller is AppShell.open_safe_import, which calls
        # through with no seam of its own. Pin that the hop is reached from the
        # real entry point and not only from a direct screen-level call.
        self._new_context()
        shell = self._build_shell()
        self._seed_stale_hidden_modal(_FILE_MODAL)

        shell.open_safe_import()

        self.assertFalse(dpg.does_item_exist(_FILE_PATH_INPUT))
        self._drain(shell)
        self._drain(shell)
        self.assertTrue(dpg.does_item_exist(_FILE_MODAL))
        self.assertTrue(dpg.does_item_exist(_FILE_PATH_INPUT))
        self.assertTrue(dpg.is_item_shown(_FILE_MODAL))

    def test_file_select_leaves_downstream_modals_alone(self) -> None:
        # Scope pin for delete_tags. The front door tears down its OWN tag and
        # nothing else: each downstream modal is already deleted by the hop
        # that recreates it, and the file dialog is a different widget family
        # (mvFileDialog) whose behaviour under the modal law has not been
        # benched -- sweeping it from here would be an unverified change.
        # close_modals() remains the whole-flow teardown.
        self._new_context()
        shell = self._build_shell()

        survivors = (
            safe_import_screen.PREVIEW_MODAL,
            safe_import_screen.CONFIRM_MODAL,
            safe_import_screen.RESULT_MODAL,
            safe_import_screen.FILE_DIALOG,
        )
        for tag in survivors:
            self._seed_stale_hidden_modal(tag)
        # A reopen, so the scope pinned here is the seam's delete_tags.
        self._seed_stale_hidden_modal(_FILE_MODAL)

        safe_import_screen.open_file_select(shell)
        self._drain(shell)
        self._drain(shell)

        self.assertTrue(dpg.does_item_exist(_FILE_MODAL))
        for tag in survivors:
            self.assertTrue(
                dpg.does_item_exist(tag),
                f"{tag}: the front door must not sweep tags it never swept",
            )

    def test_file_select_browse_and_scan_callbacks_survive_the_swap(self) -> None:
        # The button row moved into the deferred closure; assert the three
        # buttons still land wired. Scan is the one that matters most -- it is
        # the hop into the already-seamed preview.
        self._new_context()
        shell = self._build_shell()
        shell.safe_import_scan_path = mock.MagicMock()
        self._seed_stale_hidden_modal(_FILE_MODAL)

        safe_import_screen.open_file_select(shell)
        self.assertFalse(
            dpg.does_item_exist(_FILE_PATH_INPUT),
            "precondition: this must be the deferred reopen, not the fresh "
            "inline build",
        )
        self._drain(shell)
        self._drain(shell)

        callbacks = [
            dpg.get_item_configuration(item)["callback"]
            for item in _descendants(_FILE_MODAL)
            if dpg.get_item_type(item) == "mvAppItemType::mvButton"
        ]
        self.assertEqual(len(callbacks), 3)
        self.assertTrue(all(callable(cb) for cb in callbacks))

        # The Scan button is the middle one in the row; invoking every button
        # would open a file dialog and delete the modal, so drive only Scan by
        # locating it through the label.
        scan_label = i18n.t("safe_import.file_select.scan")
        scan = [
            item
            for item in _descendants(_FILE_MODAL)
            if dpg.get_item_type(item) == "mvAppItemType::mvButton"
            and dpg.get_item_label(item) == scan_label
        ]
        self.assertEqual(len(scan), 1)
        dpg.get_item_configuration(scan[0])["callback"]()
        shell.safe_import_scan_path.assert_called_once()

    def test_file_select_shell_without_seam_still_opens_inline(self) -> None:
        # The getattr guard's other branch, as for About.
        self._new_context()
        shell = _NoSeamShell()
        self.assertFalse(hasattr(shell, "_defer_modal_swap"))

        safe_import_screen.open_file_select(shell)

        self.assertTrue(dpg.does_item_exist(_FILE_MODAL))
        self.assertTrue(dpg.does_item_exist(_FILE_PATH_INPUT))

    def test_file_select_navigation_between_the_passes_abandons_the_create(
        self,
    ) -> None:
        # A reopen: the stale front door is what routes it through the seam.
        # The guard is the deferred create's own screen check (fail-on-base:
        # without it the front door lands over Home).
        self._new_context()
        shell = self._build_shell()
        self._seed_stale_hidden_modal(_FILE_MODAL)
        self._assert_navigation_abandons_the_create(
            shell,
            lambda: safe_import_screen.open_file_select(shell),
            modal_tag=_FILE_MODAL,
            navigate_after_teardown=True,
        )

    def test_file_select_navigation_before_the_teardown_abandons_the_create(
        self,
    ) -> None:
        self._new_context()
        shell = self._build_shell()
        self._seed_stale_hidden_modal(_FILE_MODAL)
        self._assert_navigation_abandons_the_create(
            shell,
            lambda: safe_import_screen.open_file_select(shell),
            modal_tag=_FILE_MODAL,
            navigate_after_teardown=False,
        )

    def test_file_select_opens_normally_while_the_user_stays_on_controller(
        self,
    ) -> None:
        # Anti-vacuity for the two navigation tests: the same reopen, with no
        # navigation, must land.
        self._new_context()
        shell = self._build_shell()
        self._seed_stale_hidden_modal(_FILE_MODAL)

        safe_import_screen.open_file_select(shell)
        self._drain(shell)
        self._drain(shell)

        self.assertTrue(dpg.does_item_exist(_FILE_MODAL))
        self.assertTrue(dpg.is_item_shown(_FILE_MODAL))

    # -- the fresh path (the v2.6.4 contract) --------------------------------

    def test_file_select_fresh_open_builds_inline(self) -> None:
        # Nothing precedes a fresh open, so there is no teardown to separate
        # from the create and it builds synchronously, even on an armed shell.
        # Law-safe because its only entry button sits inside the Controller
        # editor, which cannot be clicked while any modal stands (or while
        # busy). The picker-return tests rely on this synchronous build.
        self._new_context()
        shell = self._build_shell()

        safe_import_screen.open_file_select(shell)

        self.assertTrue(dpg.does_item_exist(_FILE_MODAL))
        self.assertTrue(dpg.does_item_exist(_FILE_PATH_INPUT))
        self.assertTrue(shell._deferred_ui_calls.empty())
        self.assertFalse(shell._modal_swap_pending)

    def test_file_select_fresh_open_is_dropped_while_another_transition_is_mid_teardown(
        self,
    ) -> None:
        # The one way a fresh open could bypass the arbiter: another transition
        # holds the create slot and has already run its teardown, so its create
        # lands on the next pass. Built inline in that gap, the front door
        # would be standing when that create runs, and the other modal would
        # never render. In production the holder is typically a threaded HID
        # job's outcome modal, drained in the same tick that re-enables the
        # Import button; Save As stands in for it here.
        # Fail-on-base: without the slot check the front door is built inline.
        self._new_context()
        shell = self._build_shell()

        shell._open_save_as_modal()
        self._drain(shell)  # its teardown pass; its create is queued
        self.assertTrue(shell._modal_swap_pending)

        safe_import_screen.open_file_select(shell)

        self.assertFalse(
            dpg.does_item_exist(_FILE_MODAL),
            "a fresh open must not be built inline while another transition "
            "holds the create slot",
        )
        self._drain(shell)  # the holder's create pass
        self.assertTrue(dpg.does_item_exist("wrapper_profile_save_as_modal"))
        self._drain(shell)
        self._drain(shell)
        self.assertFalse(
            dpg.does_item_exist(_FILE_MODAL),
            "past the holder's teardown the request is dropped, as at every "
            "other seamed site",
        )
        self.assertFalse(shell._modal_swap_pending)

    def test_file_select_fresh_open_supersedes_a_transition_before_its_teardown(
        self,
    ) -> None:
        # Before the holder's teardown has run nothing has changed on screen,
        # so the arbiter's last-request-wins applies: the front door replaces
        # the pending request instead of landing beside it.
        self._new_context()
        shell = self._build_shell()

        shell._open_save_as_modal()
        safe_import_screen.open_file_select(shell)
        self.assertFalse(dpg.does_item_exist(_FILE_MODAL))

        self._drain(shell)
        self._drain(shell)

        self.assertTrue(dpg.does_item_exist(_FILE_MODAL))
        self.assertTrue(dpg.does_item_exist(_FILE_PATH_INPUT))
        self.assertFalse(dpg.does_item_exist("wrapper_profile_save_as_modal"))


def _descendants(root: str) -> list[int]:
    """Every widget under ``root``, all slots, depth-first."""

    found: list[int] = []
    stack = [root]
    while stack:
        item = stack.pop()
        for slot in range(4):
            for child in dpg.get_item_children(item, slot) or []:
                found.append(child)
                stack.append(child)
    return found


if __name__ == "__main__":
    unittest.main()
