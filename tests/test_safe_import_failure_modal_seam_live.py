"""Live-DPG regression for the Safe Import Save + Apply failure modal.

Bug (registry ``DEFECT`` entry for ``AppShell._show_apply_failure_modal``,
fixed 2026-08-01): ``_finish_safe_import_apply`` -- the ``on_done`` tail of
the jobbed Save + Apply flow -- committed the result and opened its modal
inline, in one pass. Its docstring justified that: on_done lands frames after
the job-start teardown, so the opener is a pure create.

That reasoning holds for the SUCCESS opener and not for the failure one. The
job-start teardown in ``safe_import_apply`` sweeps ``CONFIRM_MODAL``,
``PREVIEW_MODAL`` and ``RESULT_MODAL`` -- and its comment reasons explicitly
about a stale result modal stacking two windows -- but it never names
``apply_failure_modal``. That tag is built ``no_close=False``, so the titlebar
X only HIDES it and ``does_item_exist`` stays true. A user who dismissed an
earlier failure report with the X therefore leaves an instance standing into
this pass, where ``_show_apply_failure_modal``'s own does_item_exist ->
delete_item -> ``with dpg.window(modal=True)`` pairs a teardown with a create.

Under the benched modal law (2026-06-11,
``tools/diag_dpg_modal_thread_visibility.py``) the fresh window then exists and
never becomes visible. The user's controller write failed and the dialog saying
so -- with its per-field breakdown and Retry button -- cannot be seen. Nothing
raises and nothing is logged.

This is the repo's recurring shape: the same modal's other seam-riding caller,
``_open_profile_apply_readback_details_modal``, was cured at
``app_shell.py:4790-4794`` with the same tag in ``delete_tags``, and the Safe
Import sibling never got it.

Like the precedents this file is modelled on
(``tests/test_restore_point_delete_modal_live.py``,
``tests/test_wrapper_profile_delete_modal_live.py``), the visibility law itself
only manifests with a real viewport + render loop the headless suite cannot
host. What a real DPG context CAN prove is the cure's mechanism -- teardown and
create land in different drain passes:

  * BASE (inline): the stale modal is deleted the instant the call returns.
  * FIX (``_defer_modal_swap``): the stale modal survives the call, is torn
    down on the first drain pass, and the fresh one is built on the second.

So ``test_failure_modal_create_is_split_from_teardown`` FAILS on base and
PASSES on the fix. Mocked tests structurally cannot catch this, which is why
the existing Safe Import coverage stayed green through the bug.

A real-DPG context can segfault on interpreter teardown on Windows
(exit-139 != FAIL -- re-run); each test creates and destroys its own context.
"""

from __future__ import annotations

import unittest
from unittest import mock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n
from zd_app.services.settings_apply_coordinator import ApplyFailure, ApplyResult
from zd_app.ui.screens import safe_import

_FAILURE_MODAL = "apply_failure_modal"
# A child of the STALE modal only. The fresh modal never builds this tag, so
# its lifetime is an identity signal across a delete+recreate of the shared
# window tag.
_STALE_MARKER = "stale_failure_report_marker"

_PROFILE_NAME = "Imported profile"


class SafeImportFailureModalSeamLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n.set_locale("en")
        self.addCleanup(i18n.set_locale, "en")

    def _build_shell(self):
        """A real-context shell wired for the Save + Apply completion tail.

        Everything the seam touches is the real AppShell machinery; the
        orthogonal bookkeeping side paths (status banner, footer combo, screen
        rebuild) are stubbed so the test isolates the modal transition.
        """

        shell = make_shell(settings_service=mock.MagicMock())
        shell._dpg_context_ready = True
        # Drive the deferred-UI machinery by hand, as a render loop would.
        shell._defer_ui_armed = True
        shell._record_settings_apply_result = mock.MagicMock()
        shell._refresh_footer_profile_combo = mock.MagicMock()
        shell._sync_profile_apply_readback_details_action = mock.MagicMock()
        shell.rebuild_current_screen = mock.MagicMock()
        shell._last_apply_result = ApplyResult(
            total_attempted=2,
            succeeded=1,
            failed=[ApplyFailure("deadzones", "HID write rejected", True)],
        )
        return shell

    def _drain(self, shell) -> None:
        """One render-thread drain pass (one rendered frame)."""

        shell._drain_deferred_ui_calls()

    def _make_stale_hidden_failure_modal(self) -> None:
        """The titlebar-X shape: an earlier failure report, hidden, not deleted."""

        with dpg.window(tag=_FAILURE_MODAL, modal=True, show=False):
            dpg.add_text("earlier failure report", tag=_STALE_MARKER)

    def test_failure_modal_create_is_split_from_teardown(self) -> None:
        dpg.create_context()
        try:
            shell = self._build_shell()
            self._make_stale_hidden_failure_modal()

            shell._finish_safe_import_apply(
                _PROFILE_NAME, apply_to_controller=True
            )

            # Synchronously after the call the stale modal is untouched: the
            # teardown is queued, not run. BASE deletes it inline -> fails here.
            self.assertTrue(
                dpg.does_item_exist(_STALE_MARKER),
                "failure-modal teardown must be deferred, not run in the "
                "completion pass (inline teardown shares the create pass -> "
                "an unviewable failure report)",
            )

            # The commit half is NOT deferred: the profile is already on disk,
            # so its bookkeeping must have run synchronously above.
            shell._record_settings_apply_result.assert_called_once()

            # Pass 1: the stale modal is torn down and nothing is created yet.
            self._drain(shell)
            self.assertFalse(
                dpg.does_item_exist(_STALE_MARKER),
                "first drain pass must tear the stale failure modal down",
            )
            self.assertFalse(
                dpg.does_item_exist(_FAILURE_MODAL),
                "the fresh failure modal must not be created in the teardown "
                "pass -- the rendered frame between is the whole cure",
            )

            # Pass 2: the fresh failure modal is built, into an empty slot.
            self._drain(shell)
            self.assertTrue(
                dpg.does_item_exist(_FAILURE_MODAL),
                "second drain pass must build the fresh failure modal",
            )
        finally:
            dpg.destroy_context()

    def test_delete_tags_name_every_tag_either_opener_builds(self) -> None:
        """The seam contract: a hop names every tag its ``open_fn`` constructs.

        Missing one means the opener's own defensive ``_delete_if_exists`` runs
        INSIDE the create pass and rebuilds the very pair the seam breaks. This
        tail has two openers -- failure and success -- so both tags must be
        named regardless of which branch a given completion takes.
        """

        dpg.create_context()
        try:
            shell = self._build_shell()
            with mock.patch.object(shell, "_defer_modal_swap") as swap:
                shell._finish_safe_import_apply(
                    _PROFILE_NAME, apply_to_controller=True
                )

            swap.assert_called_once()
            delete_tags = set(swap.call_args.kwargs["delete_tags"])
            self.assertIn(_FAILURE_MODAL, delete_tags)
            self.assertIn(safe_import.RESULT_MODAL, delete_tags)
        finally:
            dpg.destroy_context()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
