"""Isolated REAL-render gate for the trust-front-door scroll anchor.

Runs OUTSIDE unittest discovery (no ``test_`` prefix on the filename), invoked
per-method in a subprocess by ``test_screens_diagnostics`` — the same pattern
as ``isolated_home_reference_height``. One DPG context per process: a second
create/destroy cycle in the same process hits the known teardown segfault.

Why this exists (REVISION 5 + 6): the anchor's geometry/timing was
mock-validated and hardware-falsified THREE times — the hardcoded container
identity, the rect_min coordinate math (child_window items expose NO rect_min,
so the offset silently raised and focus_item's own ensure-visible scroll
overscrolled the windowed chain by ~160px), and the attempt budget (the
rail/wide autosize chain publishes its scroll range only on the 4th-5th
rendered frame after a rebuild, so a 5-attempt budget had zero headroom and
real-hardware latency at the maximized size exhausted it — the click landed at
the top). These tests render the REAL Diagnostics screen in a REAL viewport,
drive the REAL consume + deferred-drain flow across rendered frames, and gate
both the landing geometry ([0, 40]px of the discovered container's visible
top) and the real drain-per-frame timing at the operator's actual maximized
geometry (2576x1408).
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app.i18n import set_locale
from zd_app.ui import trust_front_door
from zd_app.ui.fonts import bind_default_font, register_fonts
from zd_app.ui.screens import diagnostics


# The anchor aims the card's top MARGIN(8)px below the container's visible
# top; allow layout/padding slack up to 40px (probe-measured landings: 8px
# exactly by construction, 31-37px when estimated through rendered rects).
_TOLERANCE_TOP = 0.0
_TOLERANCE_BOTTOM = 40.0
# Real-timing gate: drains from frame 0 after the click; must comfortably
# exceed the anchor attempt budget plus the range-publish latency.
_TIMING_DRAIN_FRAMES = 40


def _first_child_window_offsets(container):
    """(screen_y, window-relative pos_y) of the container's first rect-bearing child."""

    for child in dpg.get_item_children(container, 1) or []:
        try:
            rect_y = float(dpg.get_item_rect_min(child)[1])
            pos_y = float(dpg.get_item_pos(child)[1])
        except Exception:
            continue
        return rect_y, pos_y
    return None, None


class IsolatedDiagnosticsFrontDoorScrollTest(unittest.TestCase):
    def _boot_shell(self, viewport_width: int, viewport_height: int):
        shell = make_shell(settings_service=MagicMock())
        shell.settings_service.last_read_skipped_fields = 0
        set_locale("en")
        register_fonts()
        shell._setup_theme()
        bind_default_font("en")
        dpg.create_viewport(
            title="front-door scroll gate",
            width=viewport_width,
            height=viewport_height,
            min_width=1180,
            min_height=760,
        )
        dpg.setup_dearpygui()
        shell._build_ui()
        dpg.show_viewport()
        for _ in range(30):
            dpg.render_dearpygui_frame()
        shell._resize_shell_layout()
        for _ in range(30):
            dpg.render_dearpygui_frame()
        return shell

    def _run_anchor_and_measure(
        self, viewport_width: int, viewport_height: int = 1040
    ) -> None:
        """Geometry gate: settled layout, then assert the landing position."""

        dpg.create_context()
        try:
            shell = self._boot_shell(viewport_width, viewport_height)

            # Emulate the live render loop's armed deferred-UI seam: the
            # anchor attempts must run one drain pass (one rendered frame)
            # apart, exactly as in production.
            shell._defer_ui_armed = True
            trust_front_door.open_trust_surface(shell, "trust_matrix")

            card = diagnostics.TRUST_MATRIX_CARD_TAG
            # Let the rebuild render before any drain runs (the armed seam
            # holds the queued anchor until we drain, so scroll stays 0), then
            # measure the container's stable visible top at scroll 0.
            for _ in range(10):
                dpg.render_dearpygui_frame()
            container, _ = diagnostics._find_scrollable_ancestor(card)
            self.assertIsNotNone(
                container,
                "no scrollable ancestor discovered for the matrix card",
            )
            self.assertEqual(float(dpg.get_y_scroll(container)), 0.0)
            ref_rect_y, ref_pos_y = _first_child_window_offsets(container)
            self.assertIsNotNone(
                ref_rect_y, "no rect-bearing child to reference the container top"
            )
            container_top = ref_rect_y - ref_pos_y

            # Drive the anchor attempts through rendered frames + drains.
            for _ in range(12):
                dpg.render_dearpygui_frame()
                shell._drain_deferred_ui_calls()
            for _ in range(5):
                dpg.render_dearpygui_frame()

            scroll = float(dpg.get_y_scroll(container))
            self.assertGreater(
                scroll,
                0.0,
                "anchor never scrolled the discovered container",
            )
            # Card top through rendered geometry: the intro TEXT item exposes
            # rect_min; its pos is relative to its containing window (the
            # card), so card_top = intro rect - intro pos.
            intro = diagnostics.TRUST_MATRIX_INTRO_TAG
            card_top = float(dpg.get_item_rect_min(intro)[1]) - float(
                dpg.get_item_pos(intro)[1]
            )
            relative = card_top - container_top
            self.assertGreaterEqual(
                relative,
                _TOLERANCE_TOP,
                f"matrix card OVERSCROLLED past the container top by {-relative:.1f}px "
                f"(scroll={scroll}, container_top={container_top}, card_top={card_top})",
            )
            self.assertLessEqual(
                relative,
                _TOLERANCE_BOTTOM,
                f"matrix card landed {relative:.1f}px below the container top "
                f"(scroll={scroll}, container_top={container_top}, card_top={card_top})",
            )
        finally:
            dpg.destroy_context()

    def _run_real_timing_and_assert_landed(
        self, viewport_width: int, viewport_height: int
    ) -> None:
        """Timing gate: REAL app cadence — drain every frame from the click.

        This is the timing shape that failed hardware P3 (round-2 verify,
        maximized): the rail autosize chain publishes its scroll range only
        on the 4th-5th rendered frame after the rebuild (probe-measured), so
        an anchor attempt budget without headroom exhausts before the range
        exists and the click lands at the top (probe: cap 4 -> final scroll
        0.0). Asserts the anchor actually landed: final scroll > 0 and equal
        to the card's pos-derived offset minus the margin (the pos API is
        scroll-independent and was probe-validated against rendered rects).
        """

        dpg.create_context()
        try:
            shell = self._boot_shell(viewport_width, viewport_height)
            shell._defer_ui_armed = True
            trust_front_door.open_trust_surface(shell, "trust_matrix")
            card = diagnostics.TRUST_MATRIX_CARD_TAG
            # Drain from the very first frame after the click-rebuild — no
            # pre-rendered settling. The attempt budget must absorb the
            # range-publish latency on its own.
            for _ in range(_TIMING_DRAIN_FRAMES):
                dpg.render_dearpygui_frame()
                shell._drain_deferred_ui_calls()
            for _ in range(5):
                dpg.render_dearpygui_frame()

            container, _ = diagnostics._find_scrollable_ancestor(card)
            self.assertIsNotNone(
                container,
                "no scrollable ancestor discovered for the matrix card",
            )
            scroll = float(dpg.get_y_scroll(container))
            self.assertGreater(
                scroll,
                0.0,
                "anchor never scrolled under real drain-per-frame timing "
                "(attempt budget exhausted before the layout published its "
                "scroll range — the hardware P3 failure class)",
            )
            offset = diagnostics._pos_derived_scroll_offset(card, container)
            self.assertIsNotNone(
                offset, "pos-derived offset unmeasurable post-render"
            )
            expected = min(
                max(0.0, offset - diagnostics._TRUST_FRONT_DOOR_SCROLL_MARGIN),
                float(dpg.get_y_scroll_max(container)),
            )
            self.assertAlmostEqual(
                scroll,
                expected,
                delta=1.0,
                msg=f"anchor landed at {scroll}, expected {expected}",
            )
        finally:
            dpg.destroy_context()

    def test_windowed_anchor_lands_matrix_card_at_container_top(self) -> None:
        # 1480 < the 1600 wide threshold: the windowed chain (the shape the
        # 2026-07-06 hardware verify caught overscrolling by ~130px).
        self._run_anchor_and_measure(1480)

    def test_rail_anchor_lands_matrix_card_at_container_top(self) -> None:
        # 1920 >= threshold: the rail/wide chain.
        self._run_anchor_and_measure(1920)

    def test_rail_maximized_anchor_lands_matrix_card_at_container_top(self) -> None:
        # The operator's REAL maximized geometry (2576x1408 physical, from the
        # round-2 verify captures) — the shape hardware P3 failed at.
        self._run_anchor_and_measure(2576, 1408)

    def test_rail_real_timing_anchor_scrolls(self) -> None:
        self._run_real_timing_and_assert_landed(1920, 1040)

    def test_rail_maximized_real_timing_anchor_scrolls(self) -> None:
        # The hardware-P3 configuration AND cadence: maximized geometry with
        # drains from frame 0. Gates the attempt budget against the
        # range-publish latency.
        self._run_real_timing_and_assert_landed(2576, 1408)


if __name__ == "__main__":
    unittest.main()
