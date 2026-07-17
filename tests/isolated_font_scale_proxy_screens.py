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
