"""Isolated REAL-render gate for Diagnostics active-tab persistence.

Runs OUTSIDE unittest discovery (no ``test_`` prefix on the filename), invoked
per-method in a subprocess by ``test_screens_diagnostics`` — the same pattern
as ``isolated_diagnostics_frontdoor_scroll``. One DPG context per process: a
second create/destroy cycle in the same process hits the known teardown
segfault.

Why this exists: the ``diagnostics_tab_bar`` callback's ``app_data`` is the
selected tab's INTEGER item id under real Dear PyGui, but the mocked suite
injected the ``diag_tab_*`` alias STRING. ``_diag_tab_tag_to_id`` matched only
the string form, so ``str(<int id>)`` fell through to the ``status`` default:
EVERY real tab selection silently reset ``diagnostics_active_tab`` to
``status``, the persisted tab did not survive a rebuild, and the forced
first-run consent-gate "How to verify this" link landed on the Status tab
instead of Guidance (v2.6.1 operator CU smoke). Mocked tests could not catch
this — only a REAL render loop hands the callback the integer id. These tests
render the REAL Diagnostics screen in a REAL viewport, drive the REAL
integer-``app_data`` callback across rendered frames, and gate that the
selected tab both lands AND survives the next rebuild (the device-tick rebuild
that snapped the user off Guidance).
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
from zd_app.ui.screens import diagnostics


def _tab_alias() -> str:
    value = dpg.get_value("diagnostics_tab_bar")
    return dpg.get_item_alias(value) if not isinstance(value, str) else value


class IsolatedDiagnosticsTabPersistenceTest(unittest.TestCase):
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
            title="tab persistence gate",
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
        # Live-loop cadence: the deferred-UI seam is armed and drained per frame.
        shell._defer_ui_armed = True
        return shell

    def _render_drain(self, shell, frames: int) -> None:
        for _ in range(frames):
            dpg.render_dearpygui_frame()
            shell._drain_deferred_ui_calls()

    def test_trust_surface_selection_survives_rebuild(self) -> None:
        """The exact CU-smoke bug: land on Guidance, then a rebuild snaps off.

        ``open_trust_surface`` sets ``diagnostics_active_tab='guidance'`` and
        switches to Diagnostics. The tab lands on Guidance correctly, but the
        REAL tab_bar callback fires with the integer tab id; before the fix
        that resolved to ``status`` and clobbered the persisted tab, so the
        next full rebuild (a device-state tick) re-selected Status.
        """

        dpg.create_context()
        try:
            shell = self._boot_shell()
            trust_front_door.open_trust_surface(shell, "trust_matrix")
            self._render_drain(shell, 15)

            # Initial landing is correct on Guidance...
            self.assertEqual(_tab_alias(), "diag_tab_guidance")
            # ...and the REAL integer-app_data callback must NOT have reset the
            # persisted tab to the "status" default.
            self.assertEqual(
                shell.diagnostics_active_tab,
                "guidance",
                "real tab-bar callback clobbered the persisted tab to the "
                "'status' default (integer app_data fell through to the "
                "fallback) — the v2.6.1 consent-gate landing bug",
            )

            # A device-state tick full-rebuilds Diagnostics: it re-reads
            # diagnostics_active_tab. It must STAY on Guidance.
            shell.rebuild_current_screen()
            self._render_drain(shell, 15)
            self.assertEqual(
                _tab_alias(),
                "diag_tab_guidance",
                "Diagnostics snapped off Guidance on the rebuild that follows "
                "the trust-surface navigation (the CU-smoke symptom)",
            )
            self.assertEqual(shell.diagnostics_active_tab, "guidance")
        finally:
            dpg.destroy_context()

    def test_real_user_tab_selection_persists_across_rebuild(self) -> None:
        """A real selection of a non-default tab must survive a rebuild.

        Selecting Actions via ``set_value`` on the settled tab bar fires the
        REAL callback with the tab's integer id. Before the fix that recorded
        ``status``; the next rebuild would drop the user off Actions.
        """

        dpg.create_context()
        try:
            shell = self._boot_shell()
            shell.switch_screen("diagnostics")
            self._render_drain(shell, 10)
            self.assertTrue(dpg.does_item_exist("diag_tab_actions"))

            dpg.set_value("diagnostics_tab_bar", "diag_tab_actions")
            self._render_drain(shell, 15)
            self.assertEqual(_tab_alias(), "diag_tab_actions")
            self.assertEqual(
                shell.diagnostics_active_tab,
                "actions",
                "real integer-app_data selection was not persisted",
            )

            shell.rebuild_current_screen()
            self._render_drain(shell, 15)
            self.assertEqual(_tab_alias(), "diag_tab_actions")
            self.assertEqual(shell.diagnostics_active_tab, "actions")
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
