"""One fresh real-render child per wide font-pressure proxy sentinel."""

from __future__ import annotations

import unittest

from tests.isolated_font_scale_proxy_common import (
    WIDE_CELLS,
    MatrixCell,
    assert_diagnostics_anchor,
    assert_live_verify_surface,
    boot_cell,
    prepare_live_verify,
)


class IsolatedFontScaleProxyWideTest(unittest.TestCase):
    """Exercises the rail layout's Diagnostics anchor and Live Verify Inspector."""

    def _run_cell(self, cell: MatrixCell) -> None:
        with boot_cell(cell, title="font-pressure wide sentinel") as (shell, _temp_root):
            assert_diagnostics_anchor(self, shell, case=cell)
            cell.announce("Live Verify")
            prepare_live_verify(shell)
            assert_live_verify_surface(self, case=cell, require_wide=True)


def _install_cell_methods() -> None:
    for cell in WIDE_CELLS:
        def test_method(self, cell=cell) -> None:
            self._run_cell(cell)

        test_method.__name__ = cell.child_method
        test_method.__qualname__ = f"{IsolatedFontScaleProxyWideTest.__name__}.{cell.child_method}"
        setattr(IsolatedFontScaleProxyWideTest, cell.child_method, test_method)


_install_cell_methods()


if __name__ == "__main__":
    unittest.main()
