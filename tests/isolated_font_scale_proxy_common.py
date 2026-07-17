"""Shared real-render helpers for the isolated font-pressure proxy matrix.

This module deliberately does not start with ``test_``.  Every matrix cell is
run in a fresh interpreter by the discovered wrapper so a child owns exactly
one Dear PyGui context and one real viewport.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from tools.diag_dpg_card_clip import (
    CLIP_THRESHOLD,
    _clip_kind,
    _format_card,
    _seed_services,
    _walk_cards,
)
from zd_app.i18n import set_locale
from zd_app.services.settings_service import ControllerButtonTarget
from zd_app.services.xinput_poll_service import XInputSnapshot
from zd_app.ui.fonts import bind_default_font, register_fonts


_SETTLE_FRAMES = 45
_ANCHOR_SETTLE_FRAMES = 10
_ANCHOR_DRAIN_FRAMES = 40
_RECT_SLACK = 3.0
_ANCESTOR_WALK_LIMIT = 64
_CHILD_WINDOW_TYPE = "mvAppItemType::mvChildWindow"
_TIER_1_SCALES = (1.25, 2.00)
_TIER_1_SCREEN_VIEWPORTS = ((1180, 760), (1480, 1040))


def _scale_token(scale: float) -> str:
    return str(int(round(scale * 100)))


@dataclass(frozen=True)
class MatrixCell:
    group: str
    font_scale: float
    locale: str
    width: int
    height: int

    @property
    def font_scale_proxy(self) -> str:
        return f"font_scale_proxy_{_scale_token(self.font_scale)}"

    @property
    def viewport(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def child_method(self) -> str:
        return f"test_{self.font_scale_proxy}_{self.locale.replace('-', '_')}_{self.viewport}"

    @property
    def wrapper_method(self) -> str:
        return f"test_{self.group}_{self.font_scale_proxy}_{self.locale.replace('-', '_')}_{self.viewport}"

    @property
    def tier(self) -> int:
        """Return the deliberately trimmed PR-gate tier for this cell."""

        if self.font_scale not in _TIER_1_SCALES:
            return 2
        if self.group == "screens" and (self.width, self.height) not in _TIER_1_SCREEN_VIEWPORTS:
            return 2
        return 1

    def describe(self, surface: str) -> str:
        return (
            f"surface={surface} locale={self.locale} "
            f"font_scale_proxy={self.font_scale_proxy} viewport={self.viewport}"
        )

    def announce(self, surface: str) -> None:
        print(f"RENDER_MATRIX_CHILD {self.describe(surface)}", flush=True)


_SCALES = (1.25, 1.50, 1.75, 2.00)
_LOCALES = ("en", "zh-CN")
_SCREEN_VIEWPORTS = ((1180, 760), (1366, 768), (1480, 1040))
_WIDE_VIEWPORT = (1920, 1040)
_MODAL_VIEWPORT = (1180, 760)

SCREEN_CELLS = tuple(
    MatrixCell("screens", scale, locale, width, height)
    for scale in _SCALES
    for locale in _LOCALES
    for width, height in _SCREEN_VIEWPORTS
)
WIDE_CELLS = tuple(
    MatrixCell("wide", scale, locale, *_WIDE_VIEWPORT)
    for scale in _SCALES
    for locale in _LOCALES
)
MODAL_CELLS = tuple(
    MatrixCell("modals", scale, locale, *_MODAL_VIEWPORT)
    for scale in _SCALES
    for locale in _LOCALES
)
ALL_CELLS = SCREEN_CELLS + WIDE_CELLS + MODAL_CELLS


class _StaticXInputService:
    """A no-thread, realistic live-input source for the rendered Live Verify UI."""

    def __init__(self) -> None:
        self._snapshot = XInputSnapshot(
            connected=True,
            dll_available=True,
            packet_number=42,
            buttons=frozenset({ControllerButtonTarget.A, ControllerButtonTarget.RB}),
            left_trigger=128,
            right_trigger=32,
            left_stick_x=24_000,
            left_stick_y=-8_000,
            right_stick_x=-16_000,
            right_stick_y=12_000,
        )
        self.selection_mode = "auto"
        self.active_slot = self._snapshot.slot
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self, **_kwargs) -> None:
        self.stopped += 1

    def get_snapshot(self) -> XInputSnapshot:
        return self._snapshot

    def select_slot(self, index: int) -> None:
        self.selection_mode = "manual"
        self.active_slot = index

    def select_auto(self) -> None:
        self.selection_mode = "auto"
        self.active_slot = None


def render_frames(count: int) -> None:
    for _ in range(count):
        dpg.render_dearpygui_frame()


@contextmanager
def boot_cell(cell: MatrixCell, *, title: str):
    """Build a seeded real shell at one font-pressure/client-space cell."""

    tempdir = tempfile.TemporaryDirectory()
    shell = make_shell(settings_service=MagicMock())
    shell._xinput_poll_service = _StaticXInputService()
    shell.settings.language = cell.locale
    shell.settings.setup_drawer_dismissed = True
    shell.settings_service.last_read_skipped_fields = 0
    set_locale(cell.locale)
    dpg.create_context()
    try:
        register_fonts()
        shell._setup_theme()
        bind_default_font(cell.locale)
        # This is deliberately a font-pressure knob only. It is set before any
        # screen widgets are built and is never presented as OS display scaling.
        dpg.set_global_font_scale(cell.font_scale)
        dpg.create_viewport(
            title=title,
            width=cell.width,
            height=cell.height,
            min_width=1180,
            min_height=760,
        )
        dpg.setup_dearpygui()
        shell._build_ui()
        dpg.show_viewport()
        render_frames(_SETTLE_FRAMES)
        shell._resize_shell_layout()
        render_frames(_SETTLE_FRAMES)
        _seed_services(shell, Path(tempdir.name))
        yield shell, Path(tempdir.name)
    finally:
        try:
            dpg.destroy_context()
        finally:
            tempdir.cleanup()


def switch_and_settle(shell, screen: str) -> None:
    shell.switch_screen(screen)
    render_frames(_SETTLE_FRAMES)


def _item_pos(item) -> tuple[float, float] | None:
    try:
        x, y = dpg.get_item_pos(item)
    except Exception:
        return None
    return float(x), float(y)


def _rendered_size(item) -> tuple[float, float] | None:
    try:
        width, height = dpg.get_item_rect_size(item)
    except Exception:
        return None
    return float(width), float(height)


def _pos_derived_offset(item, container) -> tuple[float, float] | None:
    """Return ``item``'s content-space offset inside ``container``.

    This follows the position-derived law used by the Diagnostics front-door
    anchor: child windows expose no ``rect_min``.  An item's position is
    relative to its containing window, so every intervening child-window
    position is accumulated until the requested surface.  The result remains
    scroll-independent.
    """

    offset = _item_pos(item)
    if offset is None:
        return None
    x, y = offset
    current = item
    for _ in range(_ANCESTOR_WALK_LIMIT):
        try:
            parent = dpg.get_item_parent(current)
        except Exception:
            return None
        if not parent or parent == current:
            return None
        if parent == container:
            return x, y
        try:
            is_child_window = dpg.get_item_type(parent) == _CHILD_WINDOW_TYPE
        except Exception:
            return None
        if is_child_window:
            parent_pos = _item_pos(parent)
            if parent_pos is None:
                return None
            x += parent_pos[0]
            y += parent_pos[1]
        current = parent
    return None


def _content_rect_in_surface(item, surface) -> tuple[float, float, float, float] | None:
    offset = _pos_derived_offset(item, surface)
    size = _rendered_size(item)
    if offset is None or size is None:
        return None
    left, top = offset
    width, height = size
    return left, top, left + width, top + height


def _visible_surface_rect(surface) -> tuple[float, float, float, float] | None:
    size = _rendered_size(surface)
    if size is None:
        return None
    try:
        scroll_x = float(dpg.get_x_scroll(surface))
    except Exception:
        scroll_x = 0.0
    try:
        scroll_y = float(dpg.get_y_scroll(surface))
    except Exception:
        scroll_y = 0.0
    width, height = size
    return scroll_x, scroll_y, scroll_x + width, scroll_y + height


def _has_rendered_rect(item) -> bool:
    size = _rendered_size(item)
    return size is not None and size[0] > 1.0 and size[1] > 1.0


def _intersects_vertical(item, container) -> bool:
    item_rect = _content_rect_in_surface(item, container)
    surface_rect = _visible_surface_rect(container)
    if item_rect is None or surface_rect is None:
        return False
    _item_left, item_top, _item_right, item_bottom = item_rect
    _surface_left, surface_top, _surface_right, surface_bottom = surface_rect
    return item_bottom > surface_top + _RECT_SLACK and item_top < surface_bottom - _RECT_SLACK


def _fits_horizontally(item, container) -> bool:
    item_rect = _content_rect_in_surface(item, container)
    surface_rect = _visible_surface_rect(container)
    if item_rect is None or surface_rect is None:
        return False
    item_left, _item_top, item_right, _item_bottom = item_rect
    surface_left, _surface_top, surface_right, _surface_bottom = surface_rect
    return item_left >= surface_left - _RECT_SLACK and item_right <= surface_right + _RECT_SLACK


def _nearest_scroll_surface(item, fallback):
    current = item
    seen: set[object] = set()
    while current not in seen:
        seen.add(current)
        try:
            parent = dpg.get_item_parent(current)
        except Exception:
            break
        if not parent:
            break
        try:
            if float(dpg.get_y_scroll_max(parent)) > CLIP_THRESHOLD:
                return parent
        except Exception:
            pass
        current = parent
    return fallback


def assert_item_reachable(testcase, tag: str, fallback_surface, *, case: MatrixCell, surface: str) -> None:
    prefix = case.describe(surface)
    testcase.assertTrue(dpg.does_item_exist(tag), f"{prefix}: required item missing: {tag}")
    testcase.assertTrue(_has_rendered_rect(tag), f"{prefix}: required item never rendered: {tag}")
    scroll_surface = _nearest_scroll_surface(tag, fallback_surface)
    testcase.assertTrue(
        _fits_horizontally(tag, scroll_surface),
        f"{prefix}: required item extends beyond its reachable surface horizontally: {tag}",
    )
    if _intersects_vertical(tag, scroll_surface):
        return

    try:
        current_scroll = float(dpg.get_y_scroll(scroll_surface))
        scroll_max = float(dpg.get_y_scroll_max(scroll_surface))
        item_rect = _content_rect_in_surface(tag, scroll_surface)
        surface_rect = _visible_surface_rect(scroll_surface)
        testcase.assertIsNotNone(
            item_rect,
            f"{prefix}: required item has no position-derived geometry: {tag}",
        )
        testcase.assertIsNotNone(
            surface_rect,
            f"{prefix}: reachable surface has no rendered geometry: {scroll_surface}",
        )
        _item_left, item_top, _item_right, item_bottom = item_rect
        _surface_left, surface_top, _surface_right, surface_bottom = surface_rect
        target_scroll = min(
            max(0.0, current_scroll + item_top - surface_top - 8.0),
            scroll_max,
        )
        if item_bottom < surface_top:
            target_scroll = max(0.0, current_scroll + item_bottom - surface_bottom + 8.0)
        dpg.set_y_scroll(scroll_surface, target_scroll)
        render_frames(3)
        testcase.assertTrue(
            _intersects_vertical(tag, scroll_surface),
            f"{prefix}: required item is not reachable through its intended scroll surface: {tag}",
        )
    finally:
        try:
            dpg.set_y_scroll(scroll_surface, 0.0)
            render_frames(2)
        except Exception:
            pass


def assert_no_hidden_card_overflow(testcase, root: str, *, case: MatrixCell, surface: str) -> None:
    prefix = case.describe(surface)
    testcase.assertTrue(dpg.does_item_exist(root), f"{prefix}: screen root missing: {root}")
    cards = _walk_cards(dpg, root)
    clipped = [card for card in cards if _clip_kind(card) == "real"]
    if clipped:
        details = "\n".join(_format_card(card) for card in clipped)
        testcase.fail(f"{prefix}: hidden child-card overflow detected:\n{details}")


def assert_diagnostics_anchor(testcase, shell, *, case: MatrixCell) -> None:
    """Drive the real deferred anchor and assert rendered landing geometry."""

    from zd_app.ui import trust_front_door
    from zd_app.ui.screens import diagnostics

    surface = "Diagnostics"
    case.announce(surface)
    shell._defer_ui_armed = True
    trust_front_door.open_trust_surface(shell, "trust_matrix")
    render_frames(_ANCHOR_SETTLE_FRAMES)
    card = diagnostics.TRUST_MATRIX_CARD_TAG
    container, _ = diagnostics._find_scrollable_ancestor(card)
    prefix = case.describe(surface)
    testcase.assertIsNotNone(container, f"{prefix}: no scrollable ancestor for trust target")

    for _ in range(_ANCHOR_DRAIN_FRAMES):
        dpg.render_dearpygui_frame()
        shell._drain_deferred_ui_calls()
    render_frames(5)

    scroll = float(dpg.get_y_scroll(container))
    testcase.assertGreater(scroll, 0.0, f"{prefix}: trust target never scrolled into its window")
    offset = diagnostics._pos_derived_scroll_offset(card, container)
    testcase.assertIsNotNone(offset, f"{prefix}: trust target offset was not measurable")
    relative = offset - scroll
    testcase.assertGreaterEqual(
        relative,
        0.0,
        f"{prefix}: trust target overscrolled past its window by {-relative:.1f}px",
    )
    testcase.assertLessEqual(
        relative,
        40.0,
        f"{prefix}: trust target landed {relative:.1f}px below its window top",
    )
    expected = min(
        max(0.0, offset - diagnostics._TRUST_FRONT_DOOR_SCROLL_MARGIN),
        float(dpg.get_y_scroll_max(container)),
    )
    # delta=2.0: under a fractional global font scale, scroll/target positions
    # quantize differently between DPG wheel builds (measured 2.0 px py3.11 vs
    # py3.12 at scale 1.25). The anchor contract is landing the target at the
    # container top; a 2 px landing preserves it. Researcher-gated 2026-07-12.
    testcase.assertAlmostEqual(scroll, expected, delta=2.0, msg=f"{prefix}: anchor landed at {scroll}, expected {expected}")
    assert_no_hidden_card_overflow(testcase, "diagnostics_root", case=case, surface=surface)


def prepare_live_verify(shell) -> None:
    from zd_app.ui import screens
    from zd_app.ui.screens import live_verify

    switch_and_settle(shell, "live_verify")
    live_verify._select_control(shell, ControllerButtonTarget.A)
    render_frames(8)


def assert_live_verify_surface(testcase, *, case: MatrixCell, require_wide: bool) -> None:
    from zd_app.ui.screens import live_verify

    surface = "Live Verify Inspector"
    case.announce(surface)
    root = live_verify.LIVE_VERIFY_ROOT_TAG
    assert_no_hidden_card_overflow(testcase, root, case=case, surface=surface)
    assert_item_reachable(
        testcase,
        live_verify.LIVE_VERIFY_INSPECTOR_EXPLANATION_TAG,
        root,
        case=case,
        surface=surface,
    )
    assert_item_reachable(
        testcase,
        live_verify.LIVE_VERIFY_INSPECTOR_EDIT_TAG,
        root,
        case=case,
        surface=surface,
    )
    if require_wide:
        testcase.assertTrue(
            dpg.does_item_exist("live_verify_wide_layout"),
            f"{case.describe(surface)}: 1920-wide sentinel did not enter its wide layout",
        )


def assert_restore_points_scroll_discipline(testcase, *, case: MatrixCell) -> None:
    from zd_app.ui.screens import restore_points

    surface = "Restore Points"
    case.announce(surface)
    root = restore_points.TAG_ROOT_CONTAINER
    assert_no_hidden_card_overflow(testcase, root, case=case, surface=surface)
    testcase.assertTrue(
        dpg.does_item_exist(restore_points.TAG_LIST_TABLE),
        f"{case.describe(surface)}: seeded list table did not render",
    )
    testcase.assertTrue(
        dpg.does_item_exist(restore_points.TAG_LIST_FOOTER_CAVEAT),
        f"{case.describe(surface)}: footer caveat did not render",
    )
    cards = _walk_cards(dpg, root)
    intentional_scrolls = [
        card
        for card in cards
        if _clip_kind(card) == "intentional" and card.get("y_scroll_max", 0.0) > CLIP_THRESHOLD
    ]
    root_scrolls = float(dpg.get_y_scroll_max(root)) > CLIP_THRESHOLD
    testcase.assertLessEqual(
        len(intentional_scrolls) + int(root_scrolls),
        1,
        f"{case.describe(surface)}: list screen exposed more than its one intended scroll surface",
    )
    assert_item_reachable(
        testcase,
        restore_points.TAG_LIST_FOOTER_CAVEAT,
        root,
        case=case,
        surface=surface,
    )


def assert_modal_within_client_and_reachable(
    testcase,
    modal: str,
    required_tags: tuple[str, ...],
    *,
    case: MatrixCell,
    surface: str,
) -> None:
    prefix = case.describe(surface)
    testcase.assertTrue(dpg.does_item_exist(modal), f"{prefix}: modal did not render: {modal}")
    testcase.assertTrue(_has_rendered_rect(modal), f"{prefix}: modal has no rendered rect: {modal}")
    modal_pos = _item_pos(modal)
    modal_size = _rendered_size(modal)
    testcase.assertIsNotNone(modal_pos, f"{prefix}: modal has no rendered position: {modal}")
    testcase.assertIsNotNone(modal_size, f"{prefix}: modal has no rendered size: {modal}")
    left, top = modal_pos
    width, height = modal_size
    right, bottom = left + width, top + height
    client_width = float(dpg.get_viewport_client_width())
    client_height = float(dpg.get_viewport_client_height())
    testcase.assertGreaterEqual(left, -_RECT_SLACK, f"{prefix}: modal escapes client area at left")
    testcase.assertGreaterEqual(top, -_RECT_SLACK, f"{prefix}: modal escapes client area at top")
    testcase.assertLessEqual(right, client_width + _RECT_SLACK, f"{prefix}: modal escapes client area at right")
    testcase.assertLessEqual(bottom, client_height + _RECT_SLACK, f"{prefix}: modal escapes client area at bottom")
    for tag in required_tags:
        assert_item_reachable(testcase, tag, modal, case=case, surface=surface)


def install_crash_review_fixture(shell, temp_root: Path) -> None:
    from zd_app.services import crash_reporter

    crash_reporter._reset_for_tests()
    crashes = temp_root / "crashes"
    crashes.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        (crashes / f"20260712T120{index}00Z.txt").write_text(
            "Unhandled render-path exception\n"
            "Traceback (most recent call last):\n"
            "  File \"legendctl.py\", line 421, in refresh\n"
            "RuntimeError: realistic crash preview for font-pressure proxy coverage\n" * 4,
            encoding="utf-8",
        )
    shell.settings_store = MagicMock()
    shell.settings_store.path = temp_root / "settings.json"
    shell.settings_store.save = MagicMock()
    shell._dpg_context_ready = True


def make_profile_delete_store() -> object:
    class _Store:
        def __init__(self) -> None:
            self._profiles = [SimpleNamespace(name="Font Pressure Matrix Profile")]

        def list_profiles(self):
            return list(self._profiles)

    return _Store()
