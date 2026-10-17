"""Live-DPG regressions for two AppShell modal seams, plus one non-seam.

Two of this file's three subjects are seam migrations (2026-07-27) of the
repo's recurring defect shape -- ``does_item_exist`` -> ``delete_item`` ->
inline ``with dpg.window(modal=True)``, a teardown paired with a create in ONE
render pass. Under the benched DPG modal law (2026-06-11,
``tools/diag_dpg_modal_thread_visibility.py``) the fresh modal then EXISTS but
never becomes interactive: its buttons are dead, no exception, nothing logged.
One rendered frame between teardown and create cures it, which is what
``AppShell._defer_modal_swap`` inserts.

  * ``_open_save_as_modal`` -- built ``no_close=False``, so the titlebar X only
    HIDES it. Save As -> X -> Save As is delete+create in one pass.
  * ``open_support_guide`` -- no ``on_close`` and no explicit ``no_close``, so
    the default X also leaves the item standing. Guide -> X -> another guide is
    the same shape. Both tags were already listed in
    ``CLEAR_DIAGNOSTIC_LOGS_MODAL_SWAP_DELETE_TAGS``: the codebase already
    assumed these two modals can outlive their dismissal.

Support Guide differs from Save As in one way that matters: it carries a
payload. Three Diagnostics buttons open three different guides
(the support-guide buttons in ``screens/diagnostics.py``), and same-key coalescing is DROP-FIRST,
so keying alone would display the OLDER guide if a second button were clicked
inside the pending window. The site therefore stores the requested topic and
resolves it in the deferred create -- ``test_second_topic_replaces_the_first_
pending_topic`` is what pins that policy. Save As has no per-request payload,
so plain drop-first is right there.

Across DIFFERENT sites the seam arbitrates rather than coalesces (2026-07-27):
only one modal transition owns the create slot, so a Save As request and a
support-guide request inside the same pending window cannot drain their
teardowns together and then create two modals in one pass. Which request wins
depends on where in the window it arrives: before the owner's teardown the later
request supersedes (``test_cross_site_requests_land_exactly_one_modal``), after
it the later request is dropped so the owner's create can undo its own teardown
(``test_cross_site_request_between_teardown_and_create_is_dropped``). The
mechanism itself is pinned in tests/test_app_shell_ui_deferral.py.

The third subject is a deliberate NON-migration. The Diagnostics bundle
preview (``app_shell.export_rich_diagnostic_bundle``) invokes
``diagnostic_bundle_preview.open_preview_modal`` inline and was once listed as
a fourth defect; it is not one. That helper always deletes an existing preview
before construction, and its titlebar ``on_close`` always routes through
``_cancel()``, which deletes the preview whether or not ``on_cancel`` was
supplied. Unlike the Modules caller it has no predecessor modal to transition
out of, so no delete+create pair ever shares a pass. The lifecycle tests below
are the regression that keeps that reasoning true; the Modules caller's own
``on_cancel`` is pinned in ``tests/test_modules_screen.py``.

Scope honesty, same as the precedent this file is modeled on
(``tests/test_restore_point_delete_modal_live.py``): the visibility law itself
only manifests with a real viewport + render loop, which the headless suite
cannot host. These tests drain WITHOUT rendering, so they prove the cure's
*mechanism* -- teardown and create land in different passes -- not that the
landed modal accepts a click. Fail-on-base works because the inline shape
creates the fresh widgets synchronously:

  * BASE: the Save As name input exists the instant the call returns.
  * FIX: it does not; the stale modal is torn down on drain 1 and the fresh
    input appears only on drain 2.

Mocked tests structurally cannot catch any of this, which is why the existing
mocked coverage stayed green through the inline shape.

A real-DPG context can segfault on interpreter teardown on Windows
(exit-139 != FAIL -- re-run); each test creates/destroys its own context.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import empty_snapshot, make_shell
from zd_app import i18n
from zd_app.services.diagnostic_bundle import (
    DiagnosticBundlePreviewItem,
    DiagnosticBundlePreviewManifest,
    DiagnosticBundleService,
)
from zd_app.ui import diagnostic_bundle_preview
from zd_app.ui import support_reference
from zd_app.ui.app_shell import (
    CLEAR_DIAGNOSTIC_LOGS_MODAL_SWAP_DELETE_TAGS,
    SAVE_AS_INCLUDE_DEVICE_CHECKBOX,
)

_SAVE_AS_MODAL = "wrapper_profile_save_as_modal"
_SAVE_AS_NAME_INPUT = "wrapper_profile_name_input"
_SUPPORT_MODAL = "support_guide_modal"
_PREVIEW_MODAL = diagnostic_bundle_preview.PREVIEW_MODAL_TAG

_TOPIC_A = "calibration"
_TOPIC_B = "firmware"


def _guide_title(topic: str) -> str:
    return support_reference.localized_title(support_reference.get_guide(topic))


class _SeamTestCase(unittest.TestCase):
    """Shared armed/unarmed shell plumbing for the two seam subjects."""

    def setUp(self) -> None:
        i18n.set_locale("en")
        self.addCleanup(i18n.set_locale, "en")

    def _build_shell(self, *, armed: bool = True, screen: str = "diagnostics"):
        shell = make_shell(settings_service=mock.MagicMock())
        shell._dpg_context_ready = True
        # Drive the deferred-UI machinery by hand, as a render loop would.
        shell._defer_ui_armed = armed
        shell.rebuild_current_screen = mock.MagicMock()
        # The support guide is screen-scoped (it is opened from Diagnostics),
        # so its deferred create checks where the user is now.
        shell.current_screen = screen
        return shell

    def _drain(self, shell) -> None:
        """One render-thread drain pass (one rendered frame)."""

        shell._drain_deferred_ui_calls()


class SaveAsModalSeamLiveTests(_SeamTestCase):
    def test_save_as_create_is_split_from_teardown(self) -> None:
        # Fail-on-base / pass-on-fix: the Save As form must NOT be built in the
        # same pass the stale modal is torn down.
        dpg.create_context()
        try:
            shell = self._build_shell()

            # The titlebar-X shape: a previous Save As modal is still present
            # but hidden. This is what the inline base deleted and recreated in
            # one pass.
            with dpg.window(tag=_SAVE_AS_MODAL, modal=True, show=False):
                dpg.add_text("stale save as")

            shell._open_save_as_modal()

            # Synchronously after the call the fresh form does not yet exist
            # (deferred). BASE builds it inline -> this fails.
            self.assertFalse(
                dpg.does_item_exist(_SAVE_AS_NAME_INPUT),
                "Save As create must be deferred, not built inline (inline "
                "create shares the teardown pass -> dead modal)",
            )

            # Pass 1: the stale modal is torn down; the fresh one is NOT built
            # yet -- the create is queued for the next pass.
            self._drain(shell)
            self.assertFalse(
                dpg.does_item_exist(_SAVE_AS_NAME_INPUT),
                "create must land a pass AFTER the teardown (the rendered "
                "frame between is what cures the modal law)",
            )

            # Pass 2: the fresh modal + its widgets are created.
            self._drain(shell)
            self.assertTrue(dpg.does_item_exist(_SAVE_AS_MODAL))
            self.assertTrue(dpg.does_item_exist(_SAVE_AS_NAME_INPUT))
            self.assertTrue(dpg.does_item_exist(SAVE_AS_INCLUDE_DEVICE_CHECKBOX))
            # Anti-vacuity: the modal that landed is the real, shown one --
            # not the stale hidden window the test seeded.
            self.assertTrue(dpg.is_item_shown(_SAVE_AS_MODAL))
            self.assertTrue(dpg.get_item_configuration(_SAVE_AS_MODAL)["modal"])
        finally:
            dpg.destroy_context()

    def test_nothing_is_on_screen_while_the_save_as_swap_is_only_pending(
        self,
    ) -> None:
        # Replaces the former ``_save_as_modal_open`` flag-timing test: that
        # flag is gone (it was cleared only after a successful save, so Cancel
        # and the titlebar X both left it lying true), and the real item state
        # is now the only "is a Save As modal up?" answer. Same fail-on-base
        # property -- base builds the window synchronously, so the modal
        # already exists the instant the request returns.
        dpg.create_context()
        try:
            shell = self._build_shell()
            self.assertFalse(dpg.does_item_exist(_SAVE_AS_MODAL))

            shell._open_save_as_modal()
            self.assertFalse(
                dpg.does_item_exist(_SAVE_AS_MODAL),
                "no modal may exist while the swap is only pending",
            )

            self._drain(shell)  # teardown pass, still nothing on screen
            self.assertFalse(dpg.does_item_exist(_SAVE_AS_MODAL))

            self._drain(shell)  # create pass
            self.assertTrue(dpg.does_item_exist(_SAVE_AS_MODAL))
            self.assertTrue(dpg.does_item_exist(_SAVE_AS_NAME_INPUT))
        finally:
            dpg.destroy_context()

    def _save_ready_shell(self):
        shell = self._build_shell()
        shell.last_controller_snapshot = empty_snapshot()
        shell._refresh_footer_profile_combo = mock.MagicMock()
        shell.refresh_shell = mock.MagicMock()
        return shell

    def test_cancel_then_reopen_then_save_runs_the_whole_cycle(self) -> None:
        # The behaviour the removed flag was standing in for, end to end and
        # through the dismissal that used to leave it stale: Cancel deleted
        # the window WITHOUT clearing the flag, so it stayed true with no
        # modal on screen.
        dpg.create_context()
        try:
            shell = self._save_ready_shell()

            shell._open_save_as_modal()
            self._drain(shell)
            self._drain(shell)
            self.assertTrue(dpg.does_item_exist(_SAVE_AS_NAME_INPUT))

            # Cancel: the button's own callback, not a hand-rolled delete.
            group = dpg.get_item_children(_SAVE_AS_MODAL, 1)[-1]
            cancel_button = dpg.get_item_children(group, 1)[-1]
            dpg.get_item_configuration(cancel_button)["callback"]()
            self.assertFalse(dpg.does_item_exist(_SAVE_AS_MODAL))

            # Reopen after the cancel: the seam must still land a fresh modal.
            shell._open_save_as_modal()
            self._drain(shell)
            self._drain(shell)
            self.assertTrue(dpg.does_item_exist(_SAVE_AS_NAME_INPUT))

            # A successful save dismisses it -- previously gated on the flag.
            shell.save_current_as_named_wrapper_profile("profile-a")
            self.assertFalse(dpg.does_item_exist(_SAVE_AS_MODAL))
        finally:
            dpg.destroy_context()

    def test_a_titlebar_hidden_modal_is_still_dismissed_by_a_save(self) -> None:
        # The stale-state case the old flag got wrong the other way: the
        # titlebar X only HIDES the window, so an X-dismissed Save As modal is
        # still a live item. On a fresh shell the flag was false, so the save
        # left the hidden window standing for the next open to delete and
        # recreate in one pass. Real item state has no such blind spot.
        dpg.create_context()
        try:
            shell = self._save_ready_shell()

            with dpg.window(tag=_SAVE_AS_MODAL, modal=True, show=False):
                dpg.add_input_text(tag=_SAVE_AS_NAME_INPUT)

            shell.save_current_as_named_wrapper_profile("profile-b")

            self.assertFalse(dpg.does_item_exist(_SAVE_AS_MODAL))
        finally:
            dpg.destroy_context()

    def test_coalesced_duplicate_schedules_exactly_one_transition(self) -> None:
        # Anti-vacuity (the previous version of this test passed with ``key=``
        # removed: both creates ran, the first won and the duplicate's
        # SystemError was swallowed, so the final existence assertions still
        # held even though the poison had occurred). Assert the CARDINALITY of
        # what was scheduled, before anything drains.
        dpg.create_context()
        try:
            shell = self._build_shell()

            shell._open_save_as_modal()
            shell._open_save_as_modal()
            self.assertEqual(
                shell._deferred_ui_calls.qsize(),
                1,
                "a duplicate request must schedule NO second transition",
            )

            self._drain(shell)
            self._drain(shell)
            self.assertTrue(dpg.does_item_exist(_SAVE_AS_NAME_INPUT))
            landed = dpg.get_alias_id(_SAVE_AS_MODAL)

            # Nothing is queued behind the create, so further drains cannot
            # tear the modal down and rebuild it.
            self._drain(shell)
            self._drain(shell)
            self.assertTrue(
                dpg.does_item_exist(_SAVE_AS_MODAL),
                "a coalesced duplicate request must not tear down the modal "
                "that already landed",
            )
            self.assertEqual(
                dpg.get_alias_id(_SAVE_AS_MODAL),
                landed,
                "the landed modal must be the SAME item -- a new id means it "
                "was destroyed and rebuilt by a second transition",
            )
        finally:
            dpg.destroy_context()

    def test_cross_site_requests_land_exactly_one_modal(self) -> None:
        # The HIGH finding: two DIFFERENT sites inside one pending window.
        # Pre-arbiter both teardowns drained together and both creates ran in
        # the next pass -- two modals in one pass, the forbidden shape. Now
        # the later request supersedes and the earlier one never creates.
        dpg.create_context()
        try:
            shell = self._build_shell()

            shell._open_save_as_modal()
            shell.open_support_guide(_TOPIC_A)

            # The superseded request's closures stay queued but abort on a
            # stale token, so check its modal is absent after EVERY pass.
            for _ in range(4):
                self._drain(shell)
                self.assertFalse(
                    dpg.does_item_exist(_SAVE_AS_MODAL),
                    "the superseded request must never create its modal",
                )

            self.assertTrue(dpg.does_item_exist(_SUPPORT_MODAL))
            self.assertTrue(shell._deferred_ui_calls.empty())
        finally:
            dpg.destroy_context()

    def test_cross_site_request_between_teardown_and_create_is_dropped(
        self,
    ) -> None:
        # The review's second sequence: the second site is requested after the
        # first site's teardown pass but before its create pass. Pre-arbiter
        # the first create and the second teardown shared a pass and the
        # second modal was then created while the first was showing.
        #
        # CONTRACT CHANGE (2026-07-27, second round): this window used to
        # SUPERSEDE, and superseding here abandons the closure that undoes the
        # teardown that already ran -- which strands hide-based transitions
        # (Safe Import's preview) hidden with nothing in front of them. The
        # newcomer is now dropped and the owner's create lands. Either way no
        # pass holds two creates, which is what the law requires; the choice is
        # about which of the two requests survives.
        dpg.create_context()
        try:
            shell = self._build_shell()

            shell._open_save_as_modal()
            self._drain(shell)  # Save As teardown
            shell.open_support_guide(_TOPIC_A)
            self._drain(shell)
            self._drain(shell)

            self.assertTrue(dpg.does_item_exist(_SAVE_AS_MODAL))
            self.assertFalse(dpg.does_item_exist(_SUPPORT_MODAL))

            # Anti-wedge: the dropped request costs one click, not the hop.
            # Dismiss Save As first, as a user would, so the retry is an
            # ordinary transition rather than a hop over a standing modal.
            self.assertFalse(shell._modal_swap_pending)
            dpg.delete_item(_SAVE_AS_MODAL)
            shell.open_support_guide(_TOPIC_A)
            self._drain(shell)
            self._drain(shell)
            self.assertTrue(dpg.does_item_exist(_SUPPORT_MODAL))
        finally:
            dpg.destroy_context()

    def test_unarmed_shell_still_opens_save_as_synchronously(self) -> None:
        # The headless/mocked contract: with no live render loop the seam runs
        # inline, so the modal is up the instant the call returns. This is what
        # keeps tests/test_footer_profile_switcher.py valid.
        dpg.create_context()
        try:
            shell = self._build_shell(armed=False)

            shell._open_save_as_modal()

            self.assertTrue(dpg.does_item_exist(_SAVE_AS_MODAL))
            self.assertTrue(dpg.does_item_exist(_SAVE_AS_NAME_INPUT))
        finally:
            dpg.destroy_context()

    def test_stale_modal_sweep_still_covers_save_as(self) -> None:
        # The precondition for the bug: the codebase already knows this modal
        # can be left standing. If it is ever dropped from the sweep set that
        # is a deliberate decision, not an accident.
        self.assertIn(_SAVE_AS_MODAL, CLEAR_DIAGNOSTIC_LOGS_MODAL_SWAP_DELETE_TAGS)


class SupportGuideModalSeamLiveTests(_SeamTestCase):
    def test_support_guide_create_is_split_from_teardown(self) -> None:
        # Fail-on-base / pass-on-fix, read through the window label: the stale
        # seeded label must survive until the create pass replaces it.
        dpg.create_context()
        try:
            shell = self._build_shell()
            stale_label = "stale support guide"
            with dpg.window(
                tag=_SUPPORT_MODAL, label=stale_label, modal=True, show=False
            ):
                dpg.add_text("stale guide body")

            shell.open_support_guide(_TOPIC_A)

            # BASE replaces the window inline -> the label is already the
            # guide's the instant the call returns.
            self.assertEqual(
                dpg.get_item_configuration(_SUPPORT_MODAL)["label"],
                stale_label,
                "support-guide create must be deferred, not built inline "
                "(inline create shares the teardown pass -> dead modal)",
            )

            # Pass 1: teardown only. The tag is gone and not yet rebuilt.
            self._drain(shell)
            self.assertFalse(
                dpg.does_item_exist(_SUPPORT_MODAL),
                "create must land a pass AFTER the teardown (the rendered "
                "frame between is what cures the modal law)",
            )

            # Pass 2: the fresh guide window is created.
            self._drain(shell)
            self.assertTrue(dpg.does_item_exist(_SUPPORT_MODAL))
            self.assertEqual(
                dpg.get_item_configuration(_SUPPORT_MODAL)["label"],
                _guide_title(_TOPIC_A),
            )
            # Anti-vacuity: shown, modal, and actually populated.
            self.assertTrue(dpg.is_item_shown(_SUPPORT_MODAL))
            self.assertTrue(dpg.get_item_configuration(_SUPPORT_MODAL)["modal"])
            self.assertTrue(dpg.get_item_children(_SUPPORT_MODAL, 1))
        finally:
            dpg.destroy_context()

    def test_second_topic_replaces_the_first_pending_topic(self) -> None:
        # Payload policy: LAST REQUEST WINS. Two different guide buttons inside
        # the pending window must not land the OLDER guide, which is what plain
        # drop-first coalescing would do. Per-topic keys are not the fix --
        # independent keys let two opens share a pass.
        dpg.create_context()
        try:
            shell = self._build_shell()
            self.assertNotEqual(
                _guide_title(_TOPIC_A),
                _guide_title(_TOPIC_B),
                "the two topics must render distinguishable titles or this "
                "test proves nothing",
            )

            shell.open_support_guide(_TOPIC_A)
            shell.open_support_guide(_TOPIC_B)  # before any drain

            self._drain(shell)
            self._drain(shell)

            self.assertTrue(dpg.does_item_exist(_SUPPORT_MODAL))
            self.assertEqual(
                dpg.get_item_configuration(_SUPPORT_MODAL)["label"],
                _guide_title(_TOPIC_B),
                "the guide that lands must be the one requested LAST",
            )

            # Exactly one open ran: no second swap is queued behind the first,
            # so these drains must not tear the landed guide down.
            self._drain(shell)
            self._drain(shell)
            self.assertTrue(dpg.does_item_exist(_SUPPORT_MODAL))
            self.assertEqual(
                dpg.get_item_configuration(_SUPPORT_MODAL)["label"],
                _guide_title(_TOPIC_B),
            )
        finally:
            dpg.destroy_context()

    def test_unknown_topic_raises_at_request_time(self) -> None:
        # The topic is resolved at request time as validation, so a wiring bug
        # raises in the caller's own stack instead of being swallowed by the
        # drain's exception containment (which would silently open nothing).
        # A rejected request must also leave the stored topic untouched.
        dpg.create_context()
        try:
            shell = self._build_shell()
            shell.open_support_guide(_TOPIC_A)

            with self.assertRaises(KeyError):
                shell.open_support_guide("no_such_guide")

            self.assertEqual(shell._support_guide_topic, _TOPIC_A)
            self._drain(shell)
            self._drain(shell)
            self.assertEqual(
                dpg.get_item_configuration(_SUPPORT_MODAL)["label"],
                _guide_title(_TOPIC_A),
            )
        finally:
            dpg.destroy_context()

    def test_unarmed_shell_still_opens_the_guide_synchronously(self) -> None:
        dpg.create_context()
        try:
            shell = self._build_shell(armed=False)

            shell.open_support_guide(_TOPIC_B)

            self.assertTrue(dpg.does_item_exist(_SUPPORT_MODAL))
            self.assertEqual(
                dpg.get_item_configuration(_SUPPORT_MODAL)["label"],
                _guide_title(_TOPIC_B),
            )
        finally:
            dpg.destroy_context()

    def test_leaving_diagnostics_abandons_a_pending_guide(self) -> None:
        # The guide's three entry buttons live on Diagnostics. A guide
        # requested a frame before the user navigates away must not land on
        # top of the destination screen.
        dpg.create_context()
        try:
            shell = self._build_shell()
            shell.open_support_guide(_TOPIC_A)

            self._drain(shell)  # teardown pass
            shell.switch_screen("home")
            self._drain(shell)
            self._drain(shell)

            self.assertFalse(dpg.does_item_exist(_SUPPORT_MODAL))
            # The slot is free again: coming back opens normally.
            shell.switch_screen("diagnostics")
            shell.open_support_guide(_TOPIC_A)
            self._drain(shell)
            self._drain(shell)
            self.assertTrue(dpg.does_item_exist(_SUPPORT_MODAL))
        finally:
            dpg.destroy_context()

    def test_stale_modal_sweep_still_covers_the_support_guide(self) -> None:
        self.assertIn(_SUPPORT_MODAL, CLEAR_DIAGNOSTIC_LOGS_MODAL_SWAP_DELETE_TAGS)


def _sample_manifest() -> DiagnosticBundlePreviewManifest:
    return DiagnosticBundlePreviewManifest(
        items=(
            DiagnosticBundlePreviewItem(
                key="report",
                count=1,
                member_count=1,
                member_paths=("report.md",),
            ),
        ),
        excluded_items=(
            DiagnosticBundlePreviewItem(
                key="raw_app_logs",
                count=0,
                member_count=0,
            ),
        ),
        member_paths=("report.md",),
    )


class DiagnosticBundlePreviewLifecycleTests(unittest.TestCase):
    """Why the Diagnostics preview is NOT on the seam, kept honest.

    The preview's titlebar close deletes the window on its own, in both
    invocation forms, so Diagnostics never leaves a stale item behind for a
    later open to delete+create in one pass. If that ever stops being true the
    non-migration decision has to be revisited, and these fail.
    """

    def setUp(self) -> None:
        i18n.set_locale("en")
        self.addCleanup(i18n.set_locale, "en")

    def _shell(self):
        return SimpleNamespace(
            COLORS={
                "accent": (10, 10, 10, 255),
                "muted": (20, 20, 20, 255),
                "text": (30, 30, 30, 255),
            }
        )

    def _open_and_capture_on_close(self, **kwargs):
        diagnostic_bundle_preview.open_preview_modal(
            self._shell(), _sample_manifest(), **kwargs
        )
        self.assertTrue(dpg.does_item_exist(_PREVIEW_MODAL))
        on_close = dpg.get_item_configuration(_PREVIEW_MODAL)["on_close"]
        self.assertTrue(callable(on_close))
        return on_close

    def test_titlebar_close_deletes_the_preview_without_on_cancel(self) -> None:
        # The Diagnostics invocation form: no on_cancel. _cancel() still
        # deletes, which is why this caller needs no seam.
        dpg.create_context()
        try:
            exported: list[str] = []
            on_close = self._open_and_capture_on_close(
                on_export=lambda: exported.append("export"),
            )

            on_close(_PREVIEW_MODAL, None, None)

            self.assertFalse(dpg.does_item_exist(_PREVIEW_MODAL))
            self.assertEqual(exported, [])
        finally:
            dpg.destroy_context()

    def test_titlebar_close_deletes_the_preview_and_runs_on_cancel(self) -> None:
        # The Modules invocation form: on_cancel is a post-cleanup side effect
        # (a screen rebuild), never the thing that does the deleting.
        dpg.create_context()
        try:
            exported: list[str] = []
            cancelled: list[str] = []
            on_close = self._open_and_capture_on_close(
                on_export=lambda: exported.append("export"),
                on_cancel=lambda: cancelled.append("rebuild"),
            )

            on_close(_PREVIEW_MODAL, None, None)

            self.assertFalse(dpg.does_item_exist(_PREVIEW_MODAL))
            self.assertEqual(cancelled, ["rebuild"])
            self.assertEqual(exported, [])

            # Idempotent: a second close cannot re-fire the side effect.
            on_close(_PREVIEW_MODAL, None, None)
            self.assertEqual(cancelled, ["rebuild"])
        finally:
            dpg.destroy_context()

    def test_diagnostics_caller_passes_no_on_cancel(self) -> None:
        # Pins the asymmetry the two tests above rely on: only the Modules
        # caller supplies the rebuild side effect (pinned from that side in
        # tests/test_modules_screen.py). Diagnostics opens the preview from an
        # ordinary screen button with no predecessor modal.
        shell = make_shell(settings_service=mock.MagicMock())
        bundle = mock.MagicMock(spec=DiagnosticBundleService)
        bundle.preview_bundle_manifest.return_value = _sample_manifest()
        shell.diagnostic_bundle_service = bundle

        with mock.patch.object(
            diagnostic_bundle_preview, "open_preview_modal"
        ) as open_preview:
            shell.export_rich_diagnostic_bundle()

        open_preview.assert_called_once()
        self.assertNotIn("on_cancel", open_preview.call_args.kwargs)
        self.assertTrue(callable(open_preview.call_args.kwargs["on_export"]))


if __name__ == "__main__":
    unittest.main()
