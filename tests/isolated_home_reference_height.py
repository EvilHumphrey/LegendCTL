from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app.i18n import set_locale
from zd_app.ui.fonts import bind_default_font, register_fonts


class IsolatedHomeReferenceHeightTest(unittest.TestCase):
    def test_home_reference_height_has_no_scrollbar_with_trust_blocks(self) -> None:
        from tools.diag_dpg_card_clip import _seed_services

        shell = make_shell(settings_service=MagicMock())
        set_locale("en")
        tmpdir = tempfile.TemporaryDirectory()
        dpg.create_context()
        try:
            register_fonts()
            shell._setup_theme()
            bind_default_font("en")
            dpg.create_viewport(
                title="home fit test",
                width=1480,
                height=1040,
                min_width=1180,
                min_height=760,
            )
            dpg.setup_dearpygui()
            shell._build_ui()
            dpg.show_viewport()
            for _ in range(60):
                dpg.render_dearpygui_frame()
            shell._resize_shell_layout()
            for _ in range(60):
                dpg.render_dearpygui_frame()

            _seed_services(shell, Path(tmpdir.name))
            # The setup drawer is an intentionally extra first-run card. This
            # legacy viewport-budget gate protects the post-dismiss steady-state
            # Home dashboard from regressing back to a permanent scrollbar.
            shell.settings.setup_drawer_dismissed = True
            shell.switch_screen("home")
            for _ in range(60):
                dpg.render_dearpygui_frame()

            root = (dpg.get_item_children("content_region", 1) or [None])[0]
            self.assertIsNotNone(root)
            self.assertLessEqual(float(dpg.get_y_scroll_max("content_region")), 0.5)
            self.assertLessEqual(float(dpg.get_y_scroll_max(root)), 0.5)
        finally:
            dpg.destroy_context()
            tmpdir.cleanup()
