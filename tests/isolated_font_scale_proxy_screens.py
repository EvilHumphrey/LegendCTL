"""One fresh real-render child per font-pressure proxy screen-matrix cell."""

from __future__ import annotations

import unittest

import dearpygui.dearpygui as dpg

from tests.isolated_font_scale_proxy_common import (
    SCREEN_CELLS,
    MatrixCell,
    assert_diagnostics_anchor,
    assert_item_reachable,
    assert_live_verify_surface,
    assert_no_hidden_card_overflow,
    assert_restore_points_scroll_discipline,
    boot_cell,
    prepare_live_verify,
    render_frames,
    switch_and_settle,
)


class IsolatedFontScaleProxyScreenTest(unittest.TestCase):
    """Visits the five high-yield screens in one real shell/context."""

    def _run_cell(self, cell: MatrixCell) -> None:
        with boot_cell(cell, title="font-pressure screen matrix") as (shell, _temp_root):
            self._assert_home(cell, shell)
            assert_diagnostics_anchor(self, shell, case=cell)
            self._assert_live_verify(cell, shell)
            self._assert_controller(cell, shell)
            self._assert_restore_points(cell, shell)

    def _assert_home(self, cell: MatrixCell, shell) -> None:
        surface = "Home"
        cell.announce(surface)
        switch_and_settle(shell, "home")
        root = "home_root"
        assert_no_hidden_card_overflow(self, root, case=cell, surface=surface)
        for tag in (
            "home_status_firmware", "home_status_battery", "home_profile_active",
            "home_profile_pending", "home_profile_draft",
            "home_trust_front_door_self_check", "home_trust_front_door_compat_report",
            "home_trust_front_door_evidence_card", "home_trust_front_door_trust_matrix",
        ):
            assert_item_reachable(self, tag, root, case=cell, surface=surface)
        assert_item_reachable(
            self,
            "home_orientation_live_verify",
            root,
            case=cell,
            surface=surface,
        )
        assert_item_reachable(
            self,
            "home_recent_events",
            root,
            case=cell,
            surface=surface,
        )

    def _assert_live_verify(self, cell: MatrixCell, shell) -> None:
        cell.announce("Live Verify")
        prepare_live_verify(shell)
        assert_live_verify_surface(self, case=cell, require_wide=False)
        if cell.locale == "en" and cell.font_scale == 1.25 and cell.width == 1480:
            # Resize the existing workspace without navigating or rebuilding:
            # both the narrow stack and the normal two-pane layout must work.
            for width, horizontal in ((1180, False), (1480, True)):
                dpg.set_viewport_width(width)
                render_frames(60)
                self.assertEqual(
                    dpg.get_item_configuration("live_verify_controller_workspace")["horizontal"],
                    horizontal,
                )
                resized = MatrixCell(cell.group, cell.font_scale, cell.locale, width, cell.height)
                assert_live_verify_surface(self, case=resized, require_wide=False)
            for initial, resized_width in ((1650, 1400), (1400, 1650)):
                dpg.set_viewport_width(initial)
                render_frames(45)
                switch_and_settle(shell, "home")
                prepare_live_verify(shell)
                model = dpg.get_alias_id("live_verify_workspace_model_card")
                dpg.set_viewport_width(resized_width)
                render_frames(60)
                self.assertEqual(dpg.get_alias_id("live_verify_workspace_model_card"), model)
                resized = MatrixCell(cell.group, cell.font_scale, cell.locale, resized_width, cell.height)
                assert_live_verify_surface(self, case=resized, require_wide=False)
            dpg.set_viewport_width(cell.width)
            render_frames(45)

    def _assert_controller(self, cell: MatrixCell, shell) -> None:
        from zd_app.ui.screens import controller

        surface = "Controller"
        cell.announce(surface)
        # The Buttons tab is the fixed-diagram/localized control surface; build
        # it as the active tab rather than treating an unshown tab as coverage.
        shell.controller_active_tab = "buttons"
        switch_and_settle(shell, "controller")
        root = "controller_root"
        assert_no_hidden_card_overflow(self, root, case=cell, surface=surface)
        for tag in (
            "binding_source_combo",
            "binding_target_combo",
            "diagram_back_drawlist",
        ):
            assert_item_reachable(self, tag, root, case=cell, surface=surface)
        self.assertTrue(
            dpg.does_item_exist("diagram_back_drawlist"),
            f"{cell.describe(surface)}: fixed controller diagram did not render",
        )
        self.assertGreater(
            float(dpg.get_item_rect_size("diagram_back_drawlist")[0]),
            1.0,
            f"{cell.describe(surface)}: fixed controller diagram has no rendered width",
        )

    def _assert_restore_points(self, cell: MatrixCell, shell) -> None:
        cell.announce("Restore Points")
        switch_and_settle(shell, "restore_points")
        assert_restore_points_scroll_discipline(self, case=cell)


def _install_cell_methods() -> None:
    for cell in SCREEN_CELLS:
        def test_method(self, cell=cell) -> None:
            self._run_cell(cell)

        test_method.__name__ = cell.child_method
        test_method.__qualname__ = f"{IsolatedFontScaleProxyScreenTest.__name__}.{cell.child_method}"
        setattr(IsolatedFontScaleProxyScreenTest, cell.child_method, test_method)


_install_cell_methods()


if __name__ == "__main__":
    unittest.main()
