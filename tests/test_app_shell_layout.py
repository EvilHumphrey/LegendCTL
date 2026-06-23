from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import alias_of, make_shell
from zd_app.ui import app_shell as app_shell_module
from zd_app.ui.themes import COLORS


class AppShellLayoutTests(unittest.TestCase):
    def test_main_window_has_top_bar_sidebar_content_footer(self) -> None:
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()

            for tag in ("main_window", "top_status_bar", "sidebar", "content_region", "footer_profile_bar"):
                self.assertTrue(dpg.does_item_exist(tag), tag)
        finally:
            dpg.destroy_context()

    def test_sidebar_has_5_nav_entries(self) -> None:
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()

            for screen_id in ("home", "controller", "diagnostics", "settings", "about"):
                self.assertTrue(dpg.does_item_exist(f"nav_{screen_id}"))
        finally:
            dpg.destroy_context()

    def test_active_nav_entry_uses_active_themes(self) -> None:
        """Active screen binds the active button theme + the accent strip theme.

        An earlier design split the active-state visual into two pieces (button-row tint +
        accent.primary fill on the 4px left strip); a later refinement swapped the row's
        neutral bg.raised lift for a low-alpha accent wash (NAV_ACTIVE_ROW_TINT)
        for stronger wayfinding. Either way switching screens has to re-bind
        BOTH the button tag and the strip tag, which is what this guards.
        """

        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()
            shell.switch_screen("controller")

            self.assertEqual(
                dpg.get_item_theme("nav_controller"), shell._active_nav_theme
            )
            self.assertEqual(
                dpg.get_item_theme("nav_strip_controller"),
                shell._active_nav_strip_theme,
            )
        finally:
            dpg.destroy_context()

    def test_inactive_nav_entries_use_inactive_themes(self) -> None:
        """Non-active screens bind the inactive themes for both button + strip."""

        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()
            shell.switch_screen("controller")

            for inactive in ("home", "diagnostics", "about"):
                self.assertEqual(
                    dpg.get_item_theme(f"nav_{inactive}"),
                    shell._inactive_nav_theme,
                    inactive,
                )
                self.assertEqual(
                    dpg.get_item_theme(f"nav_strip_{inactive}"),
                    shell._inactive_nav_strip_theme,
                    inactive,
                )
        finally:
            dpg.destroy_context()

    def test_each_nav_row_has_strip_alongside_button(self) -> None:
        """Every visible nav screen gets a sibling 4px strip widget."""

        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()

            for screen_id in shell._visible_nav_items():
                strip_tag = f"nav_strip_{screen_id}"
                self.assertTrue(dpg.does_item_exist(strip_tag), strip_tag)
                config = dpg.get_item_configuration(strip_tag)
                self.assertEqual(
                    config["width"], app_shell_module.NAV_ACCENT_STRIP_WIDTH
                )
                self.assertEqual(config["height"], app_shell_module.NAV_BUTTON_HEIGHT)
        finally:
            dpg.destroy_context()

    def test_sidebar_width_at_phase_1_target(self) -> None:
        """Sidebar width sits in the 180-200px-and-a-touch-wider band."""

        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()

            config = dpg.get_item_configuration("sidebar")
            self.assertEqual(config["width"], app_shell_module.SIDEBAR_WIDTH)
            # The earlier design called for ~180-200; we picked 208 for CN/EN room.
            # Guarding the band catches an accidental regression to the old
            # 200 OR an over-zealous widen past ~220.
            self.assertGreaterEqual(app_shell_module.SIDEBAR_WIDTH, 180)
            self.assertLessEqual(app_shell_module.SIDEBAR_WIDTH, 220)
        finally:
            dpg.destroy_context()

    def test_rebuild_full_ui_recreates_main_window(self) -> None:
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()

            shell.rebuild_full_ui()

            self.assertTrue(dpg.does_item_exist("main_window"))
            self.assertTrue(dpg.does_item_exist("footer_profile_bar"))
        finally:
            dpg.destroy_context()

    def test_footer_renders_at_bottom_of_main_window(self) -> None:
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()

            self.assertTrue(dpg.does_item_exist("middle_band"))
            config = dpg.get_item_configuration("footer_profile_bar")
            # An earlier revision bumped the footer bar 48→56 for legibility.
            self.assertGreaterEqual(config["height"], 56)
            self.assertEqual(alias_of(dpg.get_item_parent("footer_profile_bar")), "main_window")
        finally:
            dpg.destroy_context()

    def test_footer_profile_combo_uses_widened_constant(self) -> None:
        """Profile combo uses FOOTER_PROFILE_COMBO_WIDTH (220)."""

        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()

            config = dpg.get_item_configuration("wrapper_profile_combo")
            self.assertEqual(
                config["width"], app_shell_module.FOOTER_PROFILE_COMBO_WIDTH
            )
        finally:
            dpg.destroy_context()

    def test_footer_status_text_renders_at_primary_color(self) -> None:
        """Legibility: the live status readout uses text.primary.

        The footer's status string ("Ready.", apply/read results) was flagged as
        low-contrast at the prior secondary tier; it now renders at text.primary
        so it stays legible against the bar. Guards an accidental revert to a
        dimmer tier. (Body size is inherited from the global default body font,
        so color is the lever this test pins.)
        """

        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()

            color = dpg.get_item_configuration("footer_status_text")["color"]
            # DPG returns item colors as normalized 0-1 floats.
            color_255 = tuple(round(channel * 255) for channel in color)
            self.assertEqual(color_255, tuple(COLORS["text.primary"]))
        finally:
            dpg.destroy_context()


class NavThemeBuilderTests(unittest.TestCase):
    """`_build_nav_themes` returns four theme tags (button + strip × on/off)."""

    def test_returns_four_distinct_theme_tags(self) -> None:
        dpg.create_context()
        try:
            themes = app_shell_module._build_nav_themes()
            self.assertEqual(len(themes), 4)
            # All four should be non-None and distinct.
            self.assertEqual(len(set(themes)), 4)
            for theme in themes:
                self.assertTrue(dpg.does_item_exist(theme))
        finally:
            dpg.destroy_context()

    def test_active_row_tint_is_low_alpha_accent_wash(self) -> None:
        """Wayfinding: the active row tint is a low-alpha accent wash.

        It must share the accent hue (so the row reads as accent-linked to the
        strip) but stay a subtle wash — a low, non-zero alpha — rather than the
        full accent fill the earlier design retired or the neutral bg.raised lift it
        replaced.
        """

        tint = app_shell_module.NAV_ACTIVE_ROW_TINT
        accent = COLORS["accent.primary"]
        self.assertEqual(tuple(tint[:3]), tuple(accent[:3]))
        self.assertGreater(tint[3], 0)
        self.assertLess(tint[3], 128)
        # And it must differ from the neutral lift it replaced + the inactive
        # surface, so the active row is visually distinct.
        self.assertNotEqual(tuple(tint), tuple(COLORS["bg.raised"]))
        self.assertNotEqual(tuple(tint), tuple(COLORS["bg.surface"]))


class ContentSurfaceScrollbarTests(unittest.TestCase):
    """The content area is a SINGLE non-scrolling frame.

    Each screen root is an ``autosize_y`` child that grows to its content and
    scrolls INTERNALLY on overflow, so the frame it mounts into is the real
    scroll surface's *parent*, not a scroll surface itself. The old design
    nested a second ``autosize_y`` layer (``content_region``) inside a
    scrollable outer (``content_area``); that redundant layer measured a hair
    past the outer's inner height and surfaced a permanent ~1-2px scrollbar on
    every screen even when the content fit. The frame must therefore be a
    fixed-height (``height=-1``) ``no_scrollbar`` clip region and there must be
    no second redundant layer. Regression guard for the 2026-06-20
    content-scroll lane (real-scroll behaviour is benched in
    tools/diag_dpg_content_scrollbar.py — headless DPG cannot measure it).
    """

    def test_single_content_surface_no_redundant_outer_layer(self) -> None:
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()

            # The redundant outer scroll surface is gone...
            self.assertFalse(dpg.does_item_exist("content_area"))
            # ...leaving exactly one content surface that screens mount into.
            self.assertTrue(dpg.does_item_exist("content_region"))

            cfg = dpg.get_item_configuration("content_region")
            # Fixed-height frame, no bar of its own, NOT a second autosize layer.
            self.assertEqual(cfg["height"], -1)
            self.assertTrue(cfg["no_scrollbar"])
            self.assertFalse(cfg["autosize_y"])

            # Mounted directly under the sidebar+content row — one level, not
            # nested two child_windows deep as before.
            parent = dpg.get_item_info("content_region")["parent"]
            self.assertEqual(dpg.get_item_alias(parent), "below_status_bar")
        finally:
            dpg.destroy_context()

    def test_primary_window_reserves_its_chrome_so_it_never_scrollbars(self) -> None:
        # Root cause of the multi-round phantom bar (2026-06-21): the primary
        # window (main_window) stacks top-bar + middle-band + footer, but
        # _middle_band_height reserved only 28px for the primary window's OWN
        # chrome (inter-row spacing + top/bottom window padding), which measures
        # ~52px on the shipped theme. So the primary window overflowed by ~24px
        # and drew a persistent full-height far-right scrollbar on EVERY screen
        # even when the content fit (measured main_window y_scroll_max=24 on DPG
        # 2.3; a no_scrollbar flag does NOT stick — set_primary_window resets the
        # window's flags — so the gutter reservation IS the fix). Real ysm is
        # benched in tools/diag_dpg_main_window_scroll.py; headless DPG can't measure
        # it, so guard the reservation arithmetic instead.
        gutter = app_shell_module._MAIN_WINDOW_VERTICAL_GUTTER
        # The gutter must cover the measured 52px chrome so top+middle+footer fit.
        self.assertGreaterEqual(gutter, 52)

        shell = make_shell(settings_service=MagicMock())
        # _middle_band_height subtracts the FULL top+footer+gutter from the
        # viewport, so the three stacked rows + the reserved chrome fit exactly.
        with patch.object(shell, "_viewport_client_height", return_value=1001):
            band = shell._middle_band_height()
        expected = 1001 - (
            app_shell_module._TOP_STATUS_BAR_HEIGHT
            + app_shell_module._FOOTER_PROFILE_BAR_HEIGHT
            + gutter
        )
        self.assertEqual(band, expected)
        # Sanity: top-bar + band + footer + the reserved chrome must not exceed the
        # viewport (this is exactly what keeps the primary window from scrolling).
        rows_plus_chrome = (
            app_shell_module._TOP_STATUS_BAR_HEIGHT + band
            + app_shell_module._FOOTER_PROFILE_BAR_HEIGHT + gutter
        )
        self.assertLessEqual(rows_plus_chrome, 1001)

    def test_content_surface_survives_screen_swaps(self) -> None:
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()

            for screen in ("controller", "diagnostics", "about", "home"):
                shell.switch_screen(screen)
                self.assertTrue(dpg.does_item_exist("content_region"), screen)

                cfg = dpg.get_item_configuration("content_region")
                self.assertTrue(cfg["no_scrollbar"], screen)
                self.assertEqual(cfg["height"], -1, screen)

                # Each swap mounts exactly one screen-root child into the frame.
                kids = dpg.get_item_children("content_region", 1)
                self.assertEqual(len(kids), 1, screen)
        finally:
            dpg.destroy_context()


class NavRebuildTests(unittest.TestCase):
    def test_reclicking_active_nav_screen_skips_rebuild(self) -> None:
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()
            shell.switch_screen("controller")
            self.assertEqual(shell._canonical_screen_id(shell.current_screen), "controller")
            # The active screen must actually be built for the skip to apply.
            self.assertTrue(dpg.get_item_children("content_region", 1))

            with patch.object(
                shell, "rebuild_current_screen", wraps=shell.rebuild_current_screen
            ) as rebuild:
                # Re-clicking the already-active nav button is a no-op rebuild.
                shell._switch_screen_callback(None, None, "controller")
                self.assertEqual(rebuild.call_count, 0)

                # Navigating to a different screen still rebuilds.
                shell._switch_screen_callback(None, None, "home")
                self.assertEqual(rebuild.call_count, 1)

            self.assertEqual(shell._canonical_screen_id(shell.current_screen), "home")
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
