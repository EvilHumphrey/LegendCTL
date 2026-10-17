"""Live-DPG regression for the four remaining inline modal builders.

Bug (inventoried 2026-07-26, fixed 2026-07-27): four user-triggerable modal
builders still carried this repo's recurring shape --
``does_item_exist(TAG)`` -> ``delete_item(TAG)`` -> inline
``with dpg.window(tag=TAG, modal=True, no_close=False)`` -- pairing a teardown
with a create in ONE render pass:

  * ``modules._open_assign_modal``   (buttons at ``modules.py`` Assign / Swap)
  * ``modules._open_notes_modal``    (button  at ``modules.py`` Edit Notes)
  * ``modules._open_export_modal``   (button  at ``modules.py`` Export row)
  * ``wear_ledger._open_note_modal`` (button  at ``wear_ledger.py`` Add Note)

Under the benched DPG modal law (2026-06-11,
``tools/diag_dpg_modal_thread_visibility.py``) the fresh modal then EXISTS but
never becomes interactive: its buttons are dead, no exception is raised and
nothing is logged. ``restore_points._on_open_delete_confirm`` was cured of the
same shape on 2026-07-25; these four were not carried across, which is the
defect shape this codebase keeps producing -- a fix landing at one call site
and not its siblings.

Reachable without exotic state: all four are built ``no_close=False``, so the
titlebar X only HIDES the window rather than deleting it. Open the modal,
dismiss it with the X, open it again -> delete+create in one pass -> a modal
with dead buttons.

What this file can and cannot prove. The visibility law itself only manifests
with a real viewport and a running render loop, which the headless suite
cannot host; nothing here observes an actual dead button. What it DOES prove
against a real DPG context is the *cure's mechanism* -- that teardown and
create no longer share a pass, which is the property the law cares about:

  * BASE (inline ``with dpg.window``): the child widgets are created
    synchronously -- they exist the instant the call returns.
  * FIX (``_defer_modal_swap``): the create is deferred; the child widgets do
    NOT exist when the call returns, the stale modal is torn down on the first
    drain pass, and the fresh widgets only appear on the second.

So each ``*_create_is_split_from_teardown`` test FAILS on base and PASSES on
the fix. Mocked tests structurally cannot catch any of this (the documented
DPG modal law), which is why ``tests/test_modules_screen.py`` and
``tests/test_wear_ledger_screen.py`` stayed green through the bug -- and, by
the ``getattr`` guard's inline fallback, stay green through the fix.

The two payload-carrying sites (Assign captures side + is_swap, Notes captures
side) additionally pin the coalescing policy. Same-key coalescing in
``_defer_modal_swap`` is drop-first, so a second request inside the pending
window is discarded; with the payload closed over, the SECOND click would open
the FIRST click's modal. Both sites therefore park the payload on the screen
state and re-read it at create time -- last request wins -- and the
``*_last_request_wins`` tests assert exactly that, including which side the
modal's Save button writes to.

Across DIFFERENT sites the seam arbitrates instead (2026-07-27): only one modal
transition owns the create slot, so Edit Notes followed by Export inside one
pending window cannot drain both teardowns together and then create two modals
in one pass. And because these creates capture screen state, each request is
scoped to the Modules / Wear Ledger screen: navigating away abandons the create
rather than opening the editor over the destination with its Save still wired
to the screen the user left.

A real-DPG context can segfault on interpreter teardown on Windows
(exit-139 != FAIL -- re-run); each test creates and destroys its own context,
mirroring ``tests/test_restore_point_delete_modal_live.py``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n
from zd_app.i18n import t
from zd_app.services.diagnostic_bundle import DiagnosticBundleService
from zd_app.services.module_passport import ModulePassportService
from zd_app.storage.module_passport_models import SIDE_LEFT, SIDE_RIGHT
from zd_app.ui.screens import modules as modules_screen
from zd_app.ui.screens import wear_ledger as wear_screen


class _ModalSeamTestBase(unittest.TestCase):
    """Shared rig: a real AppShell whose deferred-UI queue is drained by hand.

    Everything the seam touches is the real AppShell machinery; only the
    screen-rebuild side path (orthogonal to the seam) and the passport /
    bundle service directories are substituted, so no test here reads or
    writes the operator's real user-data dir.
    """

    def setUp(self) -> None:
        i18n.set_locale("en")
        self.addCleanup(i18n.set_locale, "en")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def _new_context(self) -> None:
        dpg.create_context()
        self.addCleanup(dpg.destroy_context)

    SCREEN = "modules"

    def _build_shell(self, *, screen: str | None = None):
        screen = self.SCREEN if screen is None else screen
        self.passports = ModulePassportService(base_dir=self.tmp_path / "passports")
        shell = make_shell(settings_service=mock.MagicMock())
        shell.module_passport_service = self.passports
        shell.diagnostic_bundle_service = DiagnosticBundleService(
            base_dir=self.tmp_path / "bundles",
            module_passport_service=self.passports,
        )
        shell._dpg_context_ready = True
        # Drive the deferred-UI machinery by hand, as a render loop would.
        shell._defer_ui_armed = True
        shell.rebuild_current_screen = mock.MagicMock()
        # These sites are screen-scoped: the deferred create checks that the
        # user is still on the screen that requested it.
        shell.current_screen = screen
        return shell

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
        """The fail-on-base assertion sequence, shared by all four sites."""

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
        """A re-fired click must schedule ONE transition, not two.

        Anti-vacuity: the earlier version of this helper asserted only that
        the modal and its child still existed after extra drains, which held
        even with ``key=`` removed -- both creates ran in one pass, the first
        won and the duplicate's SystemError was swallowed, so the poison
        happened invisibly. Cardinality before the first drain, plus item
        identity after, is what actually pins the coalescing.
        """

        invoke()
        invoke()
        self.assertEqual(
            shell._deferred_ui_calls.qsize(),
            1,
            "a duplicate request must schedule NO second transition",
        )

        self._drain(shell)
        self._drain(shell)
        self.assertTrue(dpg.does_item_exist(child_tag))
        landed = dpg.get_alias_id(modal_tag)

        self._drain(shell)
        self._drain(shell)
        self.assertTrue(
            dpg.does_item_exist(modal_tag),
            "a coalesced duplicate request must not tear down the modal that "
            "already landed",
        )
        self.assertTrue(dpg.does_item_exist(child_tag))
        self.assertEqual(
            dpg.get_alias_id(modal_tag),
            landed,
            "the landed modal must be the SAME item -- a new id means a "
            "second transition destroyed and rebuilt it",
        )

    def _assert_unarmed_opens_synchronously(
        self, shell, invoke, *, modal_tag: str, child_tag: str
    ) -> None:
        """The headless/mocked contract: no render loop -> the seam runs inline.

        This is what keeps tests/test_modules_screen.py and
        tests/test_wear_ledger_screen.py valid.
        """

        shell._defer_ui_armed = False

        invoke()

        self.assertTrue(dpg.does_item_exist(modal_tag))
        self.assertTrue(dpg.does_item_exist(child_tag))


# ---------------------------------------------------------------------------
# Modules — Assign / Swap modal (payload: side + is_swap)
# ---------------------------------------------------------------------------


class ModulesAssignModalSeamTests(_ModalSeamTestBase):
    def test_assign_create_is_split_from_teardown(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assert_split_from_teardown(
            shell,
            lambda: modules_screen._open_assign_modal(shell, SIDE_LEFT, is_swap=False),
            modal_tag=modules_screen.TAG_ASSIGN_MODAL,
            child_tag=modules_screen.TAG_ASSIGN_SAVE,
        )

    def test_assign_coalesced_duplicate_keeps_the_landed_modal(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assert_coalesced_duplicate_survives(
            shell,
            lambda: modules_screen._open_assign_modal(shell, SIDE_LEFT, is_swap=False),
            modal_tag=modules_screen.TAG_ASSIGN_MODAL,
            child_tag=modules_screen.TAG_ASSIGN_SAVE,
        )

    def test_assign_unarmed_shell_opens_synchronously(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assert_unarmed_opens_synchronously(
            shell,
            lambda: modules_screen._open_assign_modal(shell, SIDE_LEFT, is_swap=False),
            modal_tag=modules_screen.TAG_ASSIGN_MODAL,
            child_tag=modules_screen.TAG_ASSIGN_SAVE,
        )

    def test_assign_last_request_wins_on_different_payload(self) -> None:
        # Policy pin. Two different clicks inside the pending window: the seam
        # drops the second REQUEST, so if the payload were closed over, the
        # left/assign modal would open. It must be the right/swap one.
        self._new_context()
        shell = self._build_shell()

        modules_screen._open_assign_modal(shell, SIDE_LEFT, is_swap=False)
        modules_screen._open_assign_modal(shell, SIDE_RIGHT, is_swap=True)

        self._drain(shell)
        self._drain(shell)

        label = dpg.get_item_configuration(modules_screen.TAG_ASSIGN_MODAL)["label"]
        self.assertEqual(
            label,
            t("modules.assign.title_swap", side=t(f"modules.side.{SIDE_RIGHT}")),
        )

        # And the Save button writes to the side the LAST request named.
        dpg.set_value(modules_screen.TAG_ASSIGN_INPUT_ID, "MOD-R9")
        callback = dpg.get_item_configuration(modules_screen.TAG_ASSIGN_SAVE)["callback"]
        callback(modules_screen.TAG_ASSIGN_SAVE, None, None, None)

        right = self.passports.get(SIDE_RIGHT)
        self.assertIsNotNone(right)
        self.assertEqual(right.module_id, "MOD-R9")
        self.assertIsNone(self.passports.get(SIDE_LEFT))


# ---------------------------------------------------------------------------
# Modules — Edit Notes modal (payload: side)
# ---------------------------------------------------------------------------


class ModulesNotesModalSeamTests(_ModalSeamTestBase):
    def _assign_both_sides(self):
        left = self.passports.assign(SIDE_LEFT, "MOD-LEFT-1", "left notes")
        right = self.passports.assign(SIDE_RIGHT, "MOD-RIGHT-1", "right notes")
        self.assertIsNotNone(left)
        self.assertIsNotNone(right)
        return left, right

    def test_notes_create_is_split_from_teardown(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assign_both_sides()
        self._assert_split_from_teardown(
            shell,
            lambda: modules_screen._open_notes_modal(shell, SIDE_LEFT),
            modal_tag=modules_screen.TAG_NOTES_MODAL,
            child_tag=modules_screen.TAG_NOTES_SAVE,
        )

    def test_notes_coalesced_duplicate_keeps_the_landed_modal(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assign_both_sides()
        self._assert_coalesced_duplicate_survives(
            shell,
            lambda: modules_screen._open_notes_modal(shell, SIDE_LEFT),
            modal_tag=modules_screen.TAG_NOTES_MODAL,
            child_tag=modules_screen.TAG_NOTES_SAVE,
        )

    def test_notes_unarmed_shell_opens_synchronously(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assign_both_sides()
        self._assert_unarmed_opens_synchronously(
            shell,
            lambda: modules_screen._open_notes_modal(shell, SIDE_LEFT),
            modal_tag=modules_screen.TAG_NOTES_MODAL,
            child_tag=modules_screen.TAG_NOTES_SAVE,
        )

    def test_notes_missing_passport_fails_synchronously_without_queueing(self) -> None:
        # The service lookup stays at request time: an unassigned side must
        # still report immediately and must NOT leave a modal queued that a
        # later drain would open over nothing.
        self._new_context()
        shell = self._build_shell()

        modules_screen._open_notes_modal(shell, SIDE_LEFT)

        state = shell.modules_screen_state
        self.assertEqual(state.status_kind, "warn")
        shell.rebuild_current_screen.assert_called_once()

        self._drain(shell)
        self._drain(shell)
        self.assertFalse(dpg.does_item_exist(modules_screen.TAG_NOTES_MODAL))

    def test_notes_last_request_wins_on_different_payload(self) -> None:
        # Policy pin, and the sharper half of it: a stale payload here would
        # show the LEFT passport's notes under a title naming the right module
        # and then save them onto the right side.
        self._new_context()
        shell = self._build_shell()
        _left, right = self._assign_both_sides()

        modules_screen._open_notes_modal(shell, SIDE_LEFT)
        modules_screen._open_notes_modal(shell, SIDE_RIGHT)

        self._drain(shell)
        self._drain(shell)

        label = dpg.get_item_configuration(modules_screen.TAG_NOTES_MODAL)["label"]
        self.assertEqual(label, t("modules.notes.title", module_id=right.module_id))
        self.assertEqual(dpg.get_value(modules_screen.TAG_NOTES_INPUT), "right notes")

        dpg.set_value(modules_screen.TAG_NOTES_INPUT, "edited on the right")
        callback = dpg.get_item_configuration(modules_screen.TAG_NOTES_SAVE)["callback"]
        callback(modules_screen.TAG_NOTES_SAVE, None, None, None)

        self.assertEqual(self.passports.get(SIDE_RIGHT).notes, "edited on the right")
        self.assertEqual(self.passports.get(SIDE_LEFT).notes, "left notes")


# ---------------------------------------------------------------------------
# Modules — Export Diagnostic Bundle modal (no payload)
# ---------------------------------------------------------------------------


class ModulesExportModalSeamTests(_ModalSeamTestBase):
    def test_export_create_is_split_from_teardown(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assert_split_from_teardown(
            shell,
            lambda: modules_screen._open_export_modal(shell),
            modal_tag=modules_screen.TAG_EXPORT_MODAL,
            child_tag=modules_screen.TAG_EXPORT_GENERATE_ZIP,
        )

    def test_export_coalesced_duplicate_keeps_the_landed_modal(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assert_coalesced_duplicate_survives(
            shell,
            lambda: modules_screen._open_export_modal(shell),
            modal_tag=modules_screen.TAG_EXPORT_MODAL,
            child_tag=modules_screen.TAG_EXPORT_GENERATE_ZIP,
        )

    def test_export_unarmed_shell_opens_synchronously(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assert_unarmed_opens_synchronously(
            shell,
            lambda: modules_screen._open_export_modal(shell),
            modal_tag=modules_screen.TAG_EXPORT_MODAL,
            child_tag=modules_screen.TAG_EXPORT_GENERATE_ZIP,
        )

    def test_export_unavailable_bundle_fails_synchronously(self) -> None:
        # The bundle lookup stays at request time, so the unavailable branch
        # is still reported without a round-trip through the drain.
        self._new_context()
        shell = self._build_shell()
        shell.diagnostic_bundle_service = None

        modules_screen._open_export_modal(shell)

        self.assertEqual(shell.modules_screen_state.status_kind, "warn")
        shell.rebuild_current_screen.assert_called_once()
        self._drain(shell)
        self._drain(shell)
        self.assertFalse(dpg.does_item_exist(modules_screen.TAG_EXPORT_MODAL))


# ---------------------------------------------------------------------------
# Wear Ledger — Add Service Note modal (no payload)
# ---------------------------------------------------------------------------


class WearLedgerNoteModalSeamTests(_ModalSeamTestBase):
    SCREEN = "wear_ledger"

    def test_note_create_is_split_from_teardown(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assert_split_from_teardown(
            shell,
            lambda: wear_screen._open_note_modal(shell),
            modal_tag=wear_screen.TAG_NOTE_MODAL,
            child_tag=wear_screen.TAG_NOTE_MODAL_SAVE,
        )

    def test_note_coalesced_duplicate_keeps_the_landed_modal(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assert_coalesced_duplicate_survives(
            shell,
            lambda: wear_screen._open_note_modal(shell),
            modal_tag=wear_screen.TAG_NOTE_MODAL,
            child_tag=wear_screen.TAG_NOTE_MODAL_SAVE,
        )

    def test_note_unarmed_shell_opens_synchronously(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assert_unarmed_opens_synchronously(
            shell,
            lambda: wear_screen._open_note_modal(shell),
            modal_tag=wear_screen.TAG_NOTE_MODAL,
            child_tag=wear_screen.TAG_NOTE_MODAL_SAVE,
        )

    def test_leaving_wear_ledger_abandons_a_pending_note_modal(self) -> None:
        self._new_context()
        shell = self._build_shell(screen="wear_ledger")

        wear_screen._open_note_modal(shell)
        self._drain(shell)  # teardown pass
        shell.switch_screen("home")
        self._drain(shell)
        self._drain(shell)

        self.assertFalse(dpg.does_item_exist(wear_screen.TAG_NOTE_MODAL))


# ---------------------------------------------------------------------------
# Cross-site arbitration and screen scoping
# ---------------------------------------------------------------------------


class CrossSiteModalArbitrationTests(_ModalSeamTestBase):
    """Two DIFFERENT Modules sites inside one pending window.

    Pre-arbiter, per-key coalescing let both teardowns drain together and both
    creates run in the next pass -- two modal creates in ONE pass, the shape
    the seam exists to prevent. The policy is last-request-wins UNTIL the
    pending transition's teardown has run: before it the later request
    supersedes, after it the later request is dropped so the owner's create can
    undo its own teardown.
    """

    def _assign_left(self):
        left = self.passports.assign(SIDE_LEFT, "MOD-LEFT-1", "left notes")
        self.assertIsNotNone(left)
        return left

    def test_notes_and_export_before_the_first_drain_land_one_modal(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assign_left()

        modules_screen._open_notes_modal(shell, SIDE_LEFT)
        modules_screen._open_export_modal(shell)

        # The superseded request's closures stay in the queue but abort on a
        # stale token, so the assertion that matters is that its modal is
        # never created -- checked after EVERY pass, not just at the end.
        for _ in range(4):
            self._drain(shell)
            self.assertFalse(
                dpg.does_item_exist(modules_screen.TAG_NOTES_MODAL),
                "the superseded site must never create its modal",
            )

        self.assertTrue(dpg.does_item_exist(modules_screen.TAG_EXPORT_MODAL))
        self.assertTrue(shell._deferred_ui_calls.empty())

    def test_export_requested_between_notes_teardown_and_notes_create(self) -> None:
        # The review's second sequence. Pre-arbiter the Notes create and the
        # Export teardown shared a pass, and Export was then created while the
        # Notes modal was showing.
        #
        # CONTRACT CHANGE (2026-07-27, second round): this window used to
        # supersede and now DROPS the newcomer. Superseding after a teardown has
        # run abandons the only closure that can undo it, which strands
        # hide-based transitions (Safe Import's preview) hidden behind nothing.
        # Either resolution satisfies the law -- no pass holds two creates --
        # so the surviving request is a policy choice, and the safe one is the
        # transition whose teardown already changed the screen.
        self._new_context()
        shell = self._build_shell()
        self._assign_left()

        modules_screen._open_notes_modal(shell, SIDE_LEFT)
        self._drain(shell)  # notes teardown
        modules_screen._open_export_modal(shell)
        self._drain(shell)
        self._drain(shell)

        self.assertTrue(dpg.does_item_exist(modules_screen.TAG_NOTES_MODAL))
        self.assertFalse(dpg.does_item_exist(modules_screen.TAG_EXPORT_MODAL))

        # Anti-wedge: one click lost, not the hop. Dismiss Notes first so the
        # retry is an ordinary transition and not a hop over a standing modal.
        self.assertFalse(shell._modal_swap_pending)
        dpg.delete_item(modules_screen.TAG_NOTES_MODAL)
        modules_screen._open_export_modal(shell)
        self._drain(shell)
        self._drain(shell)
        self.assertTrue(dpg.does_item_exist(modules_screen.TAG_EXPORT_MODAL))

    def test_navigating_away_abandons_a_pending_notes_modal(self) -> None:
        # The stale-navigation finding: the create captured the module side,
        # so opening it over Home would leave a Save button writing back into
        # the screen the user left.
        self._new_context()
        shell = self._build_shell()
        self._assign_left()

        modules_screen._open_notes_modal(shell, SIDE_LEFT)
        self._drain(shell)  # teardown pass
        shell.switch_screen("home")
        self._drain(shell)
        self._drain(shell)

        self.assertFalse(dpg.does_item_exist(modules_screen.TAG_NOTES_MODAL))
        self.assertFalse(dpg.does_item_exist(modules_screen.TAG_NOTES_SAVE))
        # Anti-wedge: coming back must still open normally.
        shell.switch_screen("modules")
        modules_screen._open_notes_modal(shell, SIDE_LEFT)
        self._drain(shell)
        self._drain(shell)
        self.assertTrue(dpg.does_item_exist(modules_screen.TAG_NOTES_SAVE))

    def test_navigating_away_before_the_teardown_pass_also_abandons(self) -> None:
        self._new_context()
        shell = self._build_shell()
        self._assign_left()

        modules_screen._open_notes_modal(shell, SIDE_LEFT)
        shell.switch_screen("home")
        self._drain(shell)
        self._drain(shell)

        self.assertFalse(dpg.does_item_exist(modules_screen.TAG_NOTES_MODAL))


if __name__ == "__main__":
    unittest.main()
