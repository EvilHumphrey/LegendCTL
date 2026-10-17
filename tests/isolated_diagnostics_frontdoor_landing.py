"""Isolated REAL-render gate for the Diagnostics front-door first frame.

Runs OUTSIDE unittest discovery (no ``test_`` prefix), invoked per-method in a
subprocess by ``test_screens_diagnostics``. One DPG context per process: a
second create/destroy cycle in the same process hits the known teardown
segfault.

Why this exists: ``open_trust_surface`` requests Guidance, while Diagnostics
builds Status as the first tab and re-selects Guidance later in the same build
pass. A v2.6.2 smoke left an intermittent first-frame Status flash as a
plausible residual. Mocked tests cannot establish which tab real DPG shows on
each rendered frame. These tests open two real trust-front-door targets in a
REAL viewport and resolve ``diagnostics_tab_bar`` through its real integer item
id after every frame, starting with the pre-render frame-0 state. Guidance must
be selected throughout; even one observed Status frame is a regression.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app.i18n import set_locale
from zd_app.models import AppSettings
from zd_app.ui import trust_front_door
from zd_app.ui.fonts import bind_default_font, register_fonts


_SETTLE_FRAMES = 20


def _tab_alias() -> str:
    value = dpg.get_value("diagnostics_tab_bar")
    return dpg.get_item_alias(value) if not isinstance(value, str) else value


class IsolatedDiagnosticsFrontDoorLandingTest(unittest.TestCase):
    def _boot_shell(self, viewport_width: int = 1480, viewport_height: int = 1040):
        shell = make_shell(
            settings_service=MagicMock(),
            settings=AppSettings(
                first_run_acknowledged=False, developer_panels_visible=False
            ),
        )
        shell.settings_service.last_read_skipped_fields = 0
        set_locale("en")
        register_fonts()
        shell._setup_theme()
        bind_default_font("en")
        dpg.create_viewport(
            title="front-door landing gate",
            width=viewport_width,
            height=viewport_height,
            min_width=1180,
            min_height=760,
        )
        dpg.setup_dearpygui()
        shell._build_ui()
        shell._dpg_context_ready = True
        dpg.show_viewport()
        for _ in range(30):
            dpg.render_dearpygui_frame()
        shell._resize_shell_layout()
        for _ in range(30):
            dpg.render_dearpygui_frame()
        # Match the live loop so front-door focus work drains once per frame.
        shell._defer_ui_armed = True
        return shell

    def _assert_guidance_from_first_frame(self, target: str) -> None:
        dpg.create_context()
        try:
            shell = self._boot_shell()
            trust_front_door.open_trust_surface(shell, target)

            # Frame 0 is the synchronous post-build, pre-render selection. The
            # subsequent samples observe the tab value after every REAL frame.
            self.assertEqual(
                _tab_alias(),
                "diag_tab_guidance",
                f"{target}: Status selected at frame 0 before the first render",
            )
            for frame in range(1, _SETTLE_FRAMES + 1):
                dpg.render_dearpygui_frame()
                self.assertEqual(
                    _tab_alias(),
                    "diag_tab_guidance",
                    f"{target}: Status flashed on rendered frame {frame}",
                )
                shell._drain_deferred_ui_calls()
        finally:
            dpg.destroy_context()

    def test_trust_matrix_shows_guidance_on_every_frame(self) -> None:
        self._assert_guidance_from_first_frame("trust_matrix")

    def test_self_check_shows_guidance_on_every_frame(self) -> None:
        self._assert_guidance_from_first_frame("self_check")


if __name__ == "__main__":
    unittest.main()
