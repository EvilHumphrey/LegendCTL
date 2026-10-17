"""Isolated REAL-render gate for Controller active-tab persistence.

Runs OUTSIDE unittest discovery (no ``test_`` prefix on the filename), invoked
per-method in a subprocess by ``test_screens_controller`` — the same pattern as
``isolated_diagnostics_tab_persistence``. One DPG context per process: a second
create/destroy cycle in the same process hits the known teardown segfault.

Why this exists: Diagnostics was fixed for two REAL-render defects that the
mocked suite structurally cannot see, and Controller has the same shape but was
never probed (follow-on to the Diagnostics fix, 2026-07-24).

1. **Integer app_data.** Under real Dear PyGui 2.2 the ``tab_bar`` callback's
   ``app_data`` was the selected tab's INTEGER item id, not the alias string the
   mocked suite injects (2.3.1 delivers the alias; see the test body). ``controller._tab_tag_to_id`` does ``str(tab_tag)``,
   and ``str(<int>)`` never starts with ``tab_``, so every REAL selection fell
   through to the ``sticks`` default and silently rewrote
   ``controller_active_tab``. Diagnostics resolves the id back to its alias
   first; Controller did not.
2. **First-visible-tab flash.** Real dpg 2.2 selects the first VISIBLE tab on
   the first rendered frame even after a same-pass ``set_value``, so a rebuild
   that lands on a non-first tab paints one frame of Sticks before settling.

These tests render the REAL Controller screen in a REAL viewport and drive the
REAL integer-``app_data`` callback across rendered frames.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import empty_snapshot, make_shell
from zd_app.i18n import set_locale
from zd_app.models import AppSettings
from zd_app.ui.fonts import bind_default_font, register_fonts


def _tab_alias() -> str:
    value = dpg.get_value("controller_tab_bar")
    return dpg.get_item_alias(value) if not isinstance(value, str) else value


def _assert_tab_bar_really_drawn(case: unittest.TestCase) -> None:
    """Anti-vacuity: the tab bar these tests drive must actually be on screen.

    The settings editor that holds the tab bar is hidden until a read has
    produced a snapshot (``controller.refresh_state``: ``show=readable``). A
    hidden group is never drawn, so with no snapshot the tab_bar callback never
    fires and the first-visible-tab law is never exercised: one test here then
    fails for the wrong reason and the other passes while checking nothing.
    """

    case.assertTrue(
        dpg.get_item_configuration("controller_settings_editor")["show"],
        "controller_settings_editor is hidden (no snapshot?), so the tab bar "
        "is never drawn and every assertion below would be vacuous",
    )
    case.assertTrue(
        dpg.get_item_state("controller_tab_bar")["visible"],
        "controller_tab_bar was not drawn on the last rendered frame",
    )


class IsolatedControllerTabPersistenceTest(unittest.TestCase):
    def _boot_shell(self, viewport_width: int = 1480, viewport_height: int = 1040):
        shell = make_shell(
            settings_service=MagicMock(),
            settings=AppSettings(
                first_run_acknowledged=True, developer_panels_visible=False
            ),
        )
        shell.settings_service.last_read_skipped_fields = 0
        set_locale("en")
        register_fonts()
        shell._setup_theme()
        bind_default_font("en")
        dpg.create_viewport(
            title="controller tab persistence gate",
            width=viewport_width,
            height=viewport_height,
            min_width=1180,
            min_height=760,
        )
        dpg.setup_dearpygui()
        shell._build_ui()
        shell._dpg_context_ready = True
        # A completed read: the Controller editor (and with it the tab bar) is
        # only shown once a snapshot exists. Seeded before the first
        # switch_screen("controller") so the screen builds readable.
        shell._set_last_controller_snapshot(empty_snapshot())
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

    def test_real_user_tab_selection_persists_across_rebuild(self) -> None:
        """A real selection of a non-default tab must survive a rebuild.

        Selecting Lighting on the settled tab bar fires the REAL callback with
        that tab's INTEGER id. If ``_tab_tag_to_id`` stringifies the int instead
        of resolving it to its alias, the selection is recorded as ``sticks``
        and the next rebuild drops the user back to Sticks — the Controller
        instance of the v2.6.1 Diagnostics bug.
        """

        dpg.create_context()
        try:
            shell = self._boot_shell()
            shell.switch_screen("controller")
            self._render_drain(shell, 10)
            self.assertTrue(dpg.does_item_exist("tab_lighting"))
            _assert_tab_bar_really_drawn(self)

            dpg.set_value("controller_tab_bar", "tab_lighting")
            self._render_drain(shell, 15)
            self.assertEqual(_tab_alias(), "tab_lighting")
            self.assertEqual(
                shell.controller_active_tab,
                "lighting",
                "the selection was not persisted",
            )

            # The integer shape. Dear PyGui 2.2 handed this callback the
            # selected tab's INTEGER item id (measured 2026-07-24). The 2.3.1
            # install (2026-08-07) hands it the alias string, even after
            # set_value(<int id>) (probed 2026-10-17), so the step above no
            # longer reaches the integer path: with _tab_tag_text reduced to a
            # bare str() it still passed on the 2.3.1 install (2026-10-17).
            # Re-deliver the same selection through the bar's REAL configured
            # callback with the tab's REAL integer id, so the alias resolution
            # stays guarded whichever shape the installed DPG delivers.
            callback = dpg.get_item_configuration("controller_tab_bar")["callback"]
            callback("controller_tab_bar", dpg.get_alias_id("tab_lighting"), None)
            self.assertEqual(
                shell.controller_active_tab,
                "lighting",
                "real integer-app_data selection was not persisted — the "
                "callback stringified DPG's integer item id and fell through "
                "to the 'sticks' default",
            )

            shell.rebuild_current_screen()
            self._render_drain(shell, 15)
            _assert_tab_bar_really_drawn(self)
            self.assertEqual(
                _tab_alias(),
                "tab_lighting",
                "Controller snapped off the user's tab on rebuild",
            )
            self.assertEqual(shell.controller_active_tab, "lighting")
        finally:
            dpg.destroy_context()

    def test_rebuild_holds_non_default_tab_every_frame(self) -> None:
        """No one-frame Sticks flash when a rebuild lands on a non-first tab.

        Real dpg 2.2 selects the first VISIBLE tab on the first rendered frame
        even after a same-pass ``set_value``, so the seven-tab Controller bar
        can paint one frame of Sticks before settling. Sampling every rendered
        frame WITHOUT draining is the only way to see it: a settled-result
        check renders past the transient, and mocks produce no real frames.
        """

        dpg.create_context()
        try:
            shell = self._boot_shell()
            shell.controller_active_tab = "lighting"
            shell.switch_screen("controller")
            self._render_drain(shell, 15)
            _assert_tab_bar_really_drawn(self)
            self.assertEqual(_tab_alias(), "tab_lighting")

            tab_bar_id_before = dpg.get_alias_id("controller_tab_bar")
            shell.rebuild_current_screen()

            # Anti-vacuity: a rebuild that raised would leave the old bar
            # standing and make every assertion below pass while testing
            # nothing. A recreated item gets a fresh id.
            self.assertNotEqual(
                dpg.get_alias_id("controller_tab_bar"),
                tab_bar_id_before,
                "rebuild did not recreate the tab bar, so the per-frame "
                "assertions below would be vacuous",
            )

            observed = [
                ("frame-0-sync", _tab_alias(), shell.controller_active_tab)
            ]
            for frame in range(1, 11):
                dpg.render_dearpygui_frame()
                observed.append(
                    (f"frame-{frame}", _tab_alias(), shell.controller_active_tab)
                )
            # The rebuilt bar was really drawn during the sampled frames; a
            # hidden editor would make every frame trivially "hold" the tab.
            _assert_tab_bar_really_drawn(self)

            offenders = [
                entry
                for entry in observed
                if entry[1] != "tab_lighting" or entry[2] != "lighting"
            ]
            self.assertEqual(
                offenders,
                [],
                "Controller left the persisted tab during a rebuild.\n"
                f"  offending frames: {offenders}\n"
                f"  full trace (frame, visible tab, persisted tab): {observed}",
            )

            # The guard hides the tabs PRECEDING the active one for the
            # construction frame, so it must also put them back — otherwise
            # "hide Sticks forever" would satisfy every assertion above.
            # Lighting is the 5th tab, so FOUR tabs were hidden. Assert every
            # one of them came back — checking only the first couple would let
            # a partial reveal leave Triggers/Vibration hidden forever.
            self._render_drain(shell, 5)
            for tab_tag in (
                "tab_sticks",
                "tab_buttons",
                "tab_triggers",
                "tab_vibration",
                "tab_motion",
                "tab_profiles",
            ):
                self.assertTrue(
                    dpg.is_item_shown(tab_tag),
                    f"{tab_tag} was hidden for the landing frame but never "
                    "revealed — that would leave a permanently truncated bar",
                )
            self.assertEqual(_tab_alias(), "tab_lighting")
            self.assertEqual(shell.controller_active_tab, "lighting")
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
