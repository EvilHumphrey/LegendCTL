"""One fresh real-render child per font-pressure proxy modal-matrix cell."""

from __future__ import annotations

import unittest

import dearpygui.dearpygui as dpg

from tests.isolated_font_scale_proxy_common import (
    MODAL_CELLS,
    MatrixCell,
    assert_modal_within_client_and_reachable,
    boot_cell,
    install_crash_review_fixture,
    make_profile_delete_store,
    render_frames,
)


class IsolatedFontScaleProxyModalTest(unittest.TestCase):
    """Builds the two startup modals plus the real two-pass profile-delete swap."""

    def _run_cell(self, cell: MatrixCell) -> None:
        with boot_cell(cell, title="font-pressure modal matrix") as (shell, temp_root):
            self._assert_first_run_acknowledgment(cell, shell)
            self._assert_crash_review(cell, shell, temp_root)
            self._assert_profile_delete_swap(cell, shell)

    def _assert_first_run_acknowledgment(self, cell: MatrixCell, shell) -> None:
        surface = "first-run acknowledgment"
        cell.announce(surface)
        shell.settings.first_run_acknowledged = False
        shell._dpg_context_ready = True
        self.assertTrue(
            shell._show_first_run_acknowledgment_modal_if_needed(),
            f"{cell.describe(surface)}: modal builder unexpectedly skipped",
        )
        render_frames(12)
        assert_modal_within_client_and_reachable(
            self,
            "first_run_ack_modal",
            (
                "first_run_ack_intro_text",
                "first_run_ack_disclaimer_text",
                "first_run_ack_fact_reads_text",
                "first_run_ack_fact_writes_text",
                "first_run_ack_fact_telemetry_text",
                "first_run_ack_risk_as_is_text",
                "first_run_ack_verify_link",
                "first_run_ack_accept_button",
                "first_run_ack_decline_button",
            ),
            case=cell,
            surface=surface,
        )
        dpg.delete_item("first_run_ack_modal")
        render_frames(3)

    def _assert_crash_review(self, cell: MatrixCell, shell, temp_root) -> None:
        surface = "crash-review modal"
        cell.announce(surface)
        install_crash_review_fixture(shell, temp_root)
        shell._show_crash_review_modal_if_any()
        render_frames(12)
        assert_modal_within_client_and_reachable(
            self,
            "crash_review_modal",
            (
                "crash_review_preview",
                "crash_review_save_button",
                "crash_review_github_button",
                "crash_review_send_button",
                "crash_review_dismiss_button",
            ),
            case=cell,
            surface=surface,
        )
        dpg.delete_item("crash_review_modal")
        render_frames(3)

    def _assert_profile_delete_swap(self, cell: MatrixCell, shell) -> None:
        surface = "profile-delete modal swap"
        cell.announce(surface)
        shell.wrapper_profile_store = make_profile_delete_store()
        shell._dpg_context_ready = True
        shell._defer_ui_armed = True
        with dpg.window(tag="wrapper_profile_delete_popup", modal=True):
            dpg.add_text("stale profile-delete modal")
        render_frames(3)

        shell.confirm_delete_named_wrapper_profile("Font Pressure Matrix Profile")
        self.assertFalse(
            dpg.does_item_exist("wrapper_profile_delete_confirm_button"),
            f"{cell.describe(surface)}: confirm button was created in the stale-modal pass",
        )

        # Pass 1 is the teardown.  A real rendered frame is then required before
        # Pass 2 may create the replacement modal.
        render_frames(1)
        shell._drain_deferred_ui_calls()
        self.assertFalse(
            dpg.does_item_exist("wrapper_profile_delete_popup"),
            f"{cell.describe(surface)}: stale modal survived the teardown pass",
        )
        self.assertFalse(
            dpg.does_item_exist("wrapper_profile_delete_confirm_button"),
            f"{cell.describe(surface)}: confirm button appeared before frame separation",
        )
        render_frames(1)
        shell._drain_deferred_ui_calls()
        render_frames(8)

        assert_modal_within_client_and_reachable(
            self,
            "wrapper_profile_delete_popup",
            ("wrapper_profile_delete_confirm_button",),
            case=cell,
            surface=surface,
        )


def _install_cell_methods() -> None:
    for cell in MODAL_CELLS:
        def test_method(self, cell=cell) -> None:
            self._run_cell(cell)

        test_method.__name__ = cell.child_method
        test_method.__qualname__ = f"{IsolatedFontScaleProxyModalTest.__name__}.{cell.child_method}"
        setattr(IsolatedFontScaleProxyModalTest, cell.child_method, test_method)


_install_cell_methods()


if __name__ == "__main__":
    unittest.main()
