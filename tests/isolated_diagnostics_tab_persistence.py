"""Isolated REAL-render gate for Diagnostics active-tab persistence.

Runs OUTSIDE unittest discovery (no ``test_`` prefix on the filename), invoked
per-method in a subprocess by ``test_screens_diagnostics`` — the same pattern
as ``isolated_diagnostics_frontdoor_scroll``. One DPG context per process: a
second create/destroy cycle in the same process hits the known teardown
segfault.

Why this exists: the ``diagnostics_tab_bar`` callback's ``app_data`` was the
selected tab's INTEGER item id under real Dear PyGui 2.2 (2.3.1 delivers the
alias; see ``test_real_user_tab_selection_persists_across_rebuild``), but the mocked suite
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

    def test_presence_rebuild_after_focus_consumed_holds_guidance(self) -> None:
        """Does the CONFIRMED guard bypass actually flash?

        The front door sets Guidance plus a ONE-SHOT focus attribute; the
        Diagnostics builder reads that attribute to hide Status/Actions for the
        landing frame, then *consumes* it
        (``diagnostics.py`` ``_consume_trust_front_door_focus``). After
        consumption the attribute is ``None``, so any later rebuild of the same
        screen constructs Status/Actions VISIBLE and selects Guidance only by a
        same-pass ``set_value`` — exactly the ordering the hide-guard exists to
        avoid. A presence transition (controller connect/disconnect) reaches
        that rebuild via ``_request_presence_rebuild``; a right-rail resize
        threshold crossing reaches ``rebuild_current_screen`` the same way.

        Static source proves the bypass; it cannot prove what DPG *paints* for
        that frame, so this is a probe, not a regression lock on a known bug.
        Two distinct failures are in scope and both are sampled per frame:

        1. a one-frame Status flash (cosmetic, but on the trust surface), and
        2. persisted-state corruption — if DPG selects the first visible tab on
           the first rendered frame, the REAL tab_bar callback fires with
           Status's INTEGER id and ``_remember_active_tab`` rewrites
           ``diagnostics_active_tab`` to ``status``. That is the v2.6.1
           consent-gate bug class returning through a different door, and it
           outlives the frame.

        ``test_trust_surface_selection_survives_rebuild`` cannot see either: it
        drains and renders 15 frames, then checks only the settled result, so a
        one-frame transient is invisible to it. Mocks cannot see it at all — no
        real frames, no integer callback ids (documented DPG law).
        """

        dpg.create_context()
        try:
            shell = self._boot_shell()
            trust_front_door.open_trust_surface(shell, "trust_matrix")
            self._render_drain(shell, 15)

            # Preconditions: landed on Guidance, and the one-shot focus
            # attribute is SPENT — so the hide-Status/Actions guard cannot fire
            # again on the rebuild below. This is the bypassed state.
            self.assertEqual(_tab_alias(), "diag_tab_guidance")
            self.assertEqual(shell.diagnostics_active_tab, "guidance")
            self.assertIsNone(
                getattr(shell, trust_front_door.TRUST_FRONT_DOOR_FOCUS_ATTR, None),
                "front-door focus attribute was not consumed by the landing "
                "build — this probe would test the GUARDED path, not the "
                "bypassed one it exists to characterize",
            )

            tab_bar_id_before = dpg.get_alias_id("diagnostics_tab_bar")

            # The production presence transition: _request_presence_rebuild
            # defers rebuild_current_screen + rail refresh. Drain ONCE to run
            # it — the tab bar is deleted and recreated here.
            shell._request_presence_rebuild()
            shell._drain_deferred_ui_calls()

            # Anti-vacuity: _drain_deferred_ui_calls deliberately CONTAINS and
            # logs a raising deferred call, so a rebuild that blew up would
            # leave the old tab bar standing and make every assertion below
            # pass while testing nothing. A recreated item gets a fresh id.
            self.assertNotEqual(
                dpg.get_alias_id("diagnostics_tab_bar"),
                tab_bar_id_before,
                "presence rebuild did not recreate the tab bar — the deferred "
                "call likely raised and was contained by the drain, which "
                "would make the per-frame assertions below vacuous",
            )

            # Sample EVERY rendered frame, and do NOT drain between them: the
            # flash, if any, lives in the frames the settled check skips.
            observed = [("frame-0-sync", _tab_alias(), shell.diagnostics_active_tab)]
            for frame in range(1, 11):
                dpg.render_dearpygui_frame()
                observed.append(
                    (f"frame-{frame}", _tab_alias(), shell.diagnostics_active_tab)
                )

            offenders = [
                entry
                for entry in observed
                if entry[1] != "diag_tab_guidance" or entry[2] != "guidance"
            ]
            self.assertEqual(
                offenders,
                [],
                "Diagnostics left Guidance during the presence rebuild that "
                "follows a consumed front-door landing.\n"
                f"  offending frames: {offenders}\n"
                f"  full trace (frame, visible tab, persisted tab): {observed}",
            )

            # The guard hides the tabs PRECEDING the active one for the
            # construction frame, so it must also put them back — otherwise
            # "hide Status forever" would satisfy every assertion above. The
            # reveal is enqueued DURING the rebuild's own drain pass, so it
            # runs on the NEXT pass; drain again to reach it.
            self._render_drain(shell, 5)
            for tab_tag in ("diag_tab_status", "diag_tab_actions"):
                self.assertTrue(
                    dpg.is_item_shown(tab_tag),
                    f"{tab_tag} was hidden for the landing frame but never "
                    "revealed — the deferred reveal did not run, which would "
                    "leave the user with a permanently truncated tab bar",
                )
            self.assertEqual(_tab_alias(), "diag_tab_guidance")
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
                "the selection was not persisted",
            )

            # The integer shape. Dear PyGui 2.2 handed this callback the
            # selected tab's INTEGER item id (the v2.6.1 CU-smoke bug). The
            # 2.3.1 install (2026-08-07) hands it the alias string, even after
            # set_value(<int id>) (probed 2026-10-17), so no step in this module
            # reached the integer path any more: all three tests passed with
            # _diag_tab_tag_text reduced to a bare str(). Re-deliver the same
            # selection through the bar's REAL configured callback with the
            # tab's REAL integer id, so the alias resolution stays guarded
            # whichever shape the installed DPG delivers.
            callback = dpg.get_item_configuration("diagnostics_tab_bar")["callback"]
            callback("diagnostics_tab_bar", dpg.get_alias_id("diag_tab_actions"), None)
            self.assertEqual(
                shell.diagnostics_active_tab,
                "actions",
                "real integer-app_data selection was not persisted — the "
                "callback stringified DPG's integer item id and fell through "
                "to the 'status' default",
            )

            shell.rebuild_current_screen()
            self._render_drain(shell, 15)
            self.assertEqual(_tab_alias(), "diag_tab_actions")
            self.assertEqual(shell.diagnostics_active_tab, "actions")
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
