"""Live Verify — dedicated gamepad-tester screen.

A live gamepad-tester surface: per-stick axes + a circularity sweep, live
button chips, live trigger bars, and inline FIRMWARE deadzone tuning you can
drag and watch the circularity envelope change in real time. Promoted out of a
Diagnostics section into its own nav screen so the whole surface fits one
window without the cramped, hidden nested-scroll trap the in-Diagnostics
version had.

Imports only shipping modules (circularity, settings_service via the shell, the
shell's xinput_poll_service) so the public-cut curation that strips the
developer panels leaves this screen intact — NEVER draft_to_slot / WRITE_KIND.

Scroll discipline (the whole reason for the move): the screen mounts ONE
governing container (``LIVE_VERIFY_ROOT_TAG``, ``autosize_x`` + ``autosize_y``,
matching every other screen root — diagnostics / restore_points) in
``content_region``; that single container is the ONLY scrollbar (far right) and
scrolls when the window is shorter than the content. It deliberately keeps the
legacy ``autosize_y`` (= ImGui ``size.y == 0`` → FILL the content_region) so it
spans the whole content area and owns the one scrollbar.

Every inner card uses ``_fit_card()`` (a thin wrapper over the app's ``card()``)
so it FITS its content height instead of filling. This is load-bearing on
dearpygui 2.x: the shared ``card()`` opens its child_window with the LEGACY
``autosize_y`` flag, which on dearpygui 2.x means "fill the parent's available
height", NOT "shrink to content" (the content-fit behaviour was split into the
separate ``auto_resize_y`` = ImGui ``ImGuiChildFlags_AutoResizeY``). A legacy
``autosize_y`` card stacked above other content therefore BALLOONS to fill the
screen — a real-DPG bench (``tools/diag_live_verify_card_heights.py``, Inter 14/18 +
shipped themes) measured a stick card at 625px inside a 725px region, ~40% empty
below its content, which was exactly the operator's screenshot. ``_fit_card()``
passes ``auto_resize_y=True`` (and suppresses the legacy fill flag) so each card
shrinks to its content: the stick card measures 338px, a stable height that does
NOT track the window — the same content fits to 338px at an 800px and a 1400px
viewport. A fitted card has no scroll range, so the mouse wheel passes straight
through to the single governing scroll — no ``no_scroll_with_mouse`` workaround
needed, and no card grows its OWN scrollbar (layout fit; an earlier attempt chased
that with hand-measured fixed heights that under-measured the real bound fonts
and clipped — ``auto_resize_y`` measures them exactly instead). In a horizontal
group ``auto_resize_y`` fits each card to ITS content, and the two stick cards
carry identical content (equal ``_PLOT_SIZE`` drawlists, same widgets), so they
fit to the SAME 338px height and the two reference circles match. The plot was
compacted (200 -> 170) and the redundant wrapping circularity-error line removed
(its number moved to the card header) to keep the screen compact; a window
shorter than the content falls back to the one far-right governing scroll.

All DPG mutation happens on the render thread via a self-rescheduling frame
callback; the XInput poll worker is read-only (pull). The deadzone live-write
goes through the shell (``apply_diagnostics_deadzone``) which owns the write
safety (debounce + busy gate + hydration guard + Restore-Point capture) and the
off-render-thread read-back verify — none of that lives here.
"""

from __future__ import annotations

import logging
import math

import dearpygui.dearpygui as dpg

from zd_app.i18n import t
from zd_app.services.button_binding_formatting import format_button_binding
from zd_app.services.circularity import CircularitySweep
from zd_app.services.settings_service import (
    BackPaddleBinding,
    ButtonSlot,
    ControllerButtonTarget,
    MacroSlot,
    StickDeadzones,
)
from zd_app.ui.controller_diagram_layout import (
    BACK_DIAGRAM_H,
    BACK_DIAGRAM_W,
    BACK_LABEL_SIZE,
    BACK_PADDLE_APPROX,
    BACK_PADDLE_POS,
    BACK_PADDLE_R,
)
from zd_app.ui.components import card
from zd_app.ui.typography import screen_title, section_title


logger = logging.getLogger(__name__)


# Root container tag — the screen's single governing container, mounted in
# content_region. The frame-callback chain self-terminates when this stops
# existing (nav-away tears down content_region children). Tag string kept
# verbatim from the in-Diagnostics version so the relocation is behaviour-
# identical; nothing outside this module/its test references the literal.
LIVE_VERIFY_ROOT_TAG = "diag_live_verify_root"
LIVE_VERIFY_AVAILABILITY_TAG = "diag_live_verify_availability"
LIVE_VERIFY_AVAILABILITY_DOT_TAG = "diag_live_verify_availability_dot"
DEADZONE_STATUS_TAG = "diag_live_verify_deadzone_status"
TRIGGER_LEFT_BAR_TAG = "diag_live_verify_trigger_left"
TRIGGER_RIGHT_BAR_TAG = "diag_live_verify_trigger_right"
LIVE_VERIFY_INSPECTOR_HINT_TAG = "diag_live_verify_inspector_hint"
LIVE_VERIFY_INSPECTOR_IDENTITY_TAG = "diag_live_verify_inspector_identity"
LIVE_VERIFY_INSPECTOR_LIVE_TAG = "diag_live_verify_inspector_live"
LIVE_VERIFY_INSPECTOR_LIVE_BAR_TAG = "diag_live_verify_inspector_live_bar"
LIVE_VERIFY_INSPECTOR_BINDING_TAG = "diag_live_verify_inspector_binding"
LIVE_VERIFY_INSPECTOR_BINDING_TIP_TAG = "diag_live_verify_inspector_binding_tip"
LIVE_VERIFY_INSPECTOR_REMAP_TAG = "diag_live_verify_inspector_remap"
LIVE_VERIFY_INSPECTOR_EXPLANATION_TAG = "diag_live_verify_inspector_explanation"
LIVE_VERIFY_INSPECTOR_EDIT_TAG = "diag_live_verify_inspector_edit_binding"
LIVE_VERIFY_VIEW_FRONT_BUTTON_TAG = "diag_live_verify_view_front"
LIVE_VERIFY_VIEW_BACK_BUTTON_TAG = "diag_live_verify_view_back"
LIVE_VERIFY_VIEW_TOP_BUTTON_TAG = "diag_live_verify_view_top"
LIVE_VERIFY_SHOW_BINDINGS_TAG = "diag_live_verify_show_bindings"
LIVE_VERIFY_FRONT_TITLE_TAG = "diag_live_verify_front_title"
LIVE_VERIFY_FRONT_NOTE_TAG = "diag_live_verify_front_note"
LIVE_VERIFY_BACK_TITLE_TAG = "diag_live_verify_back_title"
LIVE_VERIFY_BACK_NOTE_TAG = "diag_live_verify_back_note"
LIVE_VERIFY_TOP_TITLE_TAG = "diag_live_verify_top_title"
LIVE_VERIFY_TOP_NOTE_TAG = "diag_live_verify_top_note"
DIAGRAM_FACE_DRAWLIST_TAG = "diagram_face_drawlist"
DIAGRAM_FACE_SOURCE_NOTE_TAG = "diag_live_verify_face_source_note"
DIAGRAM_BACK_DRAWLIST_TAG = "diag_live_verify_back_drawlist"
DIAGRAM_TOP_DRAWLIST_TAG = "diag_live_verify_top_drawlist"
DIAGRAM_FACE_CLICK_HANDLER_TAG = "diag_live_verify_face_click_handler"
DIAGRAM_BACK_CLICK_HANDLER_TAG = "diag_live_verify_back_click_handler"
DIAGRAM_TOP_CLICK_HANDLER_TAG = "diag_live_verify_top_click_handler"

# Player-slot override row: which XInput user index (0-3) the live tester reads.
# The poll service owns the selection logic; these tags are just the combo the
# operator picks with and the live "Active: Player N" readout.
PLAYER_SELECT_COMBO_TAG = "diag_live_verify_player_combo"
PLAYER_ACTIVE_TAG = "diag_live_verify_player_active"

# XInput user indices the override exposes (== XUSER_MAX_COUNT). The combo lists
# Auto + Player 1..N, where Player N maps to zero-based slot N-1.
_PLAYER_SLOT_COUNT = 4

# ~60 fps: re-arm every frame for a smooth circularity sweep.
_REFRESH_FRAME_INTERVAL = 1

# Polar-plot drawlist geometry (square so a circle reads as a circle). Compacted
# 200 -> 170 (layout fit) so the stick card — and the whole screen — fits the
# default window without a scrollbar, while the circle stays clearly readable.
# The unit-circle radius leaves headroom out to the drawlist edge so the swept
# envelope can show over-travel (per-bin radius > 1.0 on square-gate corners)
# rather than clipping at the reference circle. _PLOT_RADIUS is the radius of
# the 1.0 reference; the drawable half-extent is _PLOT_SIZE/2, so over-travel
# up to (_PLOT_SIZE/2) / _PLOT_RADIUS ~= 1.44 stays visible (item C).
_PLOT_SIZE = 170
_PLOT_MARGIN = 26
_PLOT_CENTER = _PLOT_SIZE / 2.0
_PLOT_RADIUS = _PLOT_SIZE / 2.0 - _PLOT_MARGIN

# Semi-transparent accent fill alpha for the swept-envelope polygon (item C).
_ENVELOPE_FILL_ALPHA = 70

# Header avg-error % (header placement): an approximate per-character width for the
# default font, used to right-align the % readout next to the stick name in the
# card header. The number moved OUT of the obscured plot centre to the header.
_HEADER_PCT_CHAR_W = 8.5
# Widest realistic % string ("100.0%") reserved on the header's right edge so
# the number sits in a stable right-hand zone regardless of its current width.
_HEADER_PCT_RESERVE = 6 * _HEADER_PCT_CHAR_W
# Idle header avg-error readout, shown until a stick test produces samples.
# ASCII "--" (not an em-dash): the English Inter font has no U+2014 glyph and
# renders it as "?" (see test_en_live_verify_strings_avoid_unrenderable_glyphs),
# matching the established "pct=--" idle convention.
_HEADER_PCT_IDLE = "--%"

# Inline-deadzone slider width + a per-character estimate so each label can be
# centered above its slider (item G).
_DEADZONE_SLIDER_WIDTH = 300
_DEADZONE_LABEL_CHAR_W = 7.0

# Fixed WIDTH of each stick card (plus the gap) fits the content column and
# keeps the two columns aligned; with the equal _PLOT_SIZE drawlists the two
# reference circles read exactly the same size (work item 5). Height is NOT
# fixed: both cards use _fit_card() (auto_resize_y — see _fit_card for why the
# shared card()'s legacy autosize_y FILLS instead of fits on dearpygui 2.x), so
# each shrinks to its own content with no overflow / inner scrollbar, and since
# the two carry identical content they fit to the same height (measured 338px) —
# the equal-height guarantee that a hand-measured number kept getting wrong
# (under-measuring the real bound fonts and clipping). The full-width
# buttons/triggers + deadzone cards below also use _fit_card() for the same
# reason.
_STICK_CARD_W = 410
_STICK_CARD_GAP = 20

# Centering indent for the fixed-width plot inside its card (card inner width
# minus the plot, halved). Approximate window padding ~8px each side.
_STICK_CARD_INNER = _STICK_CARD_W - 16
_PLOT_INDENT = max(0.0, (_STICK_CARD_INNER - _PLOT_SIZE) / 2.0)

# Wrap width for the full-screen prose (header helper / circularity note /
# deadzone note). The content column at the default ~1100px window is ~860px;
# wrapping at 840 keeps the prose inside the visible screen.
_SCREEN_WRAP = 840

# Coverage below which the avg-error reads as provisional (keep spinning).
_COVERAGE_GATE_PCT = 90.0

# XInput-reported buttons, in chip display order. Triggers (LT/RT) are analog
# bars, not chips; the XInput mapping never reports them as buttons.
_BUTTON_CHIP_ORDER = (
    ControllerButtonTarget.A,
    ControllerButtonTarget.B,
    ControllerButtonTarget.X,
    ControllerButtonTarget.Y,
    ControllerButtonTarget.LB,
    ControllerButtonTarget.RB,
    ControllerButtonTarget.LS,
    ControllerButtonTarget.RS,
    ControllerButtonTarget.UP,
    ControllerButtonTarget.DOWN,
    ControllerButtonTarget.LEFT,
    ControllerButtonTarget.RIGHT,
    ControllerButtonTarget.BACK,
    ControllerButtonTarget.START,
)

# Code-drawn front-face controller model (Phase 1 live visualizer).
_WORKSPACE_MODEL_CARD_W = 640
_WORKSPACE_INSPECTOR_W = 400
_WORKSPACE_GAP = 18
_INSPECTOR_WRAP = 360
_WORKSPACE_VIEW_FRONT = "front"
_WORKSPACE_VIEW_BACK = "back"
_WORKSPACE_VIEW_TOP = "top"
_WORKSPACE_VIEWS = (
    _WORKSPACE_VIEW_FRONT,
    _WORKSPACE_VIEW_BACK,
    _WORKSPACE_VIEW_TOP,
)
_DIAGRAM_DRAWLIST_TAGS = {
    _WORKSPACE_VIEW_FRONT: DIAGRAM_FACE_DRAWLIST_TAG,
    _WORKSPACE_VIEW_BACK: DIAGRAM_BACK_DRAWLIST_TAG,
    _WORKSPACE_VIEW_TOP: DIAGRAM_TOP_DRAWLIST_TAG,
}
_DIAGRAM_CLICK_HANDLER_TAGS = {
    _WORKSPACE_VIEW_FRONT: DIAGRAM_FACE_CLICK_HANDLER_TAG,
    _WORKSPACE_VIEW_BACK: DIAGRAM_BACK_CLICK_HANDLER_TAG,
    _WORKSPACE_VIEW_TOP: DIAGRAM_TOP_CLICK_HANDLER_TAG,
}

_FACE_DIAGRAM_W = 560
_FACE_DIAGRAM_H = 400
_FACE_BUTTON_R = 18
_FACE_STICK_R = 40
_FACE_STICK_DOT_R = 8
_FACE_STICK_DOT_TRAVEL = 25
_FACE_LABEL_SIZE = 15
_FACE_TRIGGER_LIGHT_THRESHOLD = 0.06
_FACE_STICK_LIGHT_DEADZONE = 0.18

_FACE_BUTTON_POS = {
    ControllerButtonTarget.Y: (443, 150),
    ControllerButtonTarget.X: (402, 190),
    ControllerButtonTarget.B: (484, 190),
    ControllerButtonTarget.A: (443, 230),
    ControllerButtonTarget.LB: (128, 84),
    ControllerButtonTarget.RB: (432, 84),
    ControllerButtonTarget.BACK: (244, 204),
    ControllerButtonTarget.START: (316, 204),
    ControllerButtonTarget.LS: (176, 166),
    ControllerButtonTarget.RS: (376, 276),
    ControllerButtonTarget.UP: (140, 242),
    ControllerButtonTarget.DOWN: (140, 306),
    ControllerButtonTarget.LEFT: (108, 274),
    ControllerButtonTarget.RIGHT: (172, 274),
}

_FACE_BUMPER_RECTS = {
    ControllerButtonTarget.LB: ((72, 68), (184, 100)),
    ControllerButtonTarget.RB: ((376, 68), (488, 100)),
}

_FACE_TRIGGER_RECTS = {
    ControllerButtonTarget.LT: ((88, 24), (208, 54)),
    ControllerButtonTarget.RT: ((352, 24), (472, 54)),
}

_FACE_STICK_CENTERS = {
    "left": _FACE_BUTTON_POS[ControllerButtonTarget.LS],
    "right": _FACE_BUTTON_POS[ControllerButtonTarget.RS],
}
_FACE_DPAD_TARGETS = frozenset(
    {
        ControllerButtonTarget.UP,
        ControllerButtonTarget.DOWN,
        ControllerButtonTarget.LEFT,
        ControllerButtonTarget.RIGHT,
    }
)

_FACE_LABEL_POS_OVERRIDES = {
    ControllerButtonTarget.UP: (140, 208),
    ControllerButtonTarget.DOWN: (140, 338),
    ControllerButtonTarget.LEFT: (66, 274),
    ControllerButtonTarget.RIGHT: (218, 274),
    ControllerButtonTarget.BACK: (236, 236),
    ControllerButtonTarget.START: (324, 236),
}

_FACE_SOURCE_LABELS = {
    "M3": (218, 128),
    "M4": (340, 322),
}

_FACE_SELECTABLE_TARGETS = _BUTTON_CHIP_ORDER + (
    ControllerButtonTarget.LT,
    ControllerButtonTarget.RT,
)

_NON_LIVE_SELECTABLES = tuple(MacroSlot)
_BUTTON_SLOT_LABELS = {
    ButtonSlot.UP: "Up",
    ButtonSlot.RIGHT: "Right",
    ButtonSlot.DOWN: "Down",
    ButtonSlot.LEFT: "Left",
    ButtonSlot.A: "A",
    ButtonSlot.B: "B",
    ButtonSlot.X: "X",
    ButtonSlot.Y: "Y",
    ButtonSlot.LB: "LB",
    ButtonSlot.RB: "RB",
    ButtonSlot.LT: "LT",
    ButtonSlot.RT: "RT",
    ButtonSlot.BACK: "Back",
    ButtonSlot.START: "Start",
    ButtonSlot.LS: "LS",
    ButtonSlot.RS: "RS",
}

_FRONT_BINDING_BADGE_TARGETS = _FACE_SELECTABLE_TARGETS
_BINDING_BADGE_SIZE = 13
_BINDING_BADGE_ARROW = "\u2192"

_BACK_DIAGRAM_W = _FACE_DIAGRAM_W
_BACK_DIAGRAM_H = _FACE_DIAGRAM_H
_BACK_VIEW_SCALE = 1.45
_BACK_VIEW_OFFSET = (
    (_BACK_DIAGRAM_W - BACK_DIAGRAM_W * _BACK_VIEW_SCALE) / 2.0,
    (_BACK_DIAGRAM_H - BACK_DIAGRAM_H * _BACK_VIEW_SCALE) / 2.0 + 8,
)
_BACK_PADDLE_SLOTS = tuple(BACK_PADDLE_POS)
_BACK_TOP_CLAW_SLOTS = tuple(
    slot for slot in _BACK_PADDLE_SLOTS if slot in BACK_PADDLE_APPROX
)
_BACK_UPPER_PADDLE_SLOTS = (MacroSlot.RM, MacroSlot.LM)
_BACK_LOWER_PADDLE_SLOTS = (MacroSlot.M2, MacroSlot.M1)
_BACK_BADGE_SIZE = 12

_TOP_DIAGRAM_W = _FACE_DIAGRAM_W
_TOP_DIAGRAM_H = _FACE_DIAGRAM_H
_TOP_CONTROL_RECTS = {
    "L2": ((78, 94), (218, 136)),
    "L1": ((94, 152), (244, 194)),
    "LK": ((192, 216), (252, 254)),
    "R2": ((342, 94), (482, 136)),
    "R1": ((316, 152), (466, 194)),
    "RK": ((308, 216), (368, 254)),
}
_TOP_LIVE_TARGETS = {
    "L1": ControllerButtonTarget.LB,
    "L2": ControllerButtonTarget.LT,
    "R1": ControllerButtonTarget.RB,
    "R2": ControllerButtonTarget.RT,
}
_TOP_SOURCE_SLOTS = {
    "LK": MacroSlot.LK,
    "RK": MacroSlot.RK,
}
_TOP_CONTROLS = ("L1", "L2", "LK", "R1", "R2", "RK")
_TOP_BADGE_SIZE = 12
_CRYSTAL_BLUE = (46, 155, 255, 220)


class _LiveVerifyState:
    """Per-build state for the live-verify screen.

    Holds the two circularity accumulators and per-stick active flags. The
    frame callback and the button callbacks capture the same instance; the
    shell also stores it (``shell._live_verify_state``) so a rebuild can
    supersede an older chain by identity.
    """

    def __init__(self) -> None:
        self.left_sweep = CircularitySweep()
        self.right_sweep = CircularitySweep()
        self.left_active = False
        self.right_active = False
        # True once the frame callback has seen the screen root at least once.
        # Until then a missing root means "not committed yet" (keep re-arming),
        # not "torn down" (stop the worker + end the chain) — item I.
        self.root_seen = False
        self.selected_control = None
        self.active_view = _WORKSPACE_VIEW_FRONT
        self.show_binding_badges = True
        self.front_badge_text: dict[ControllerButtonTarget, str] = {}
        self.back_badge_text: dict[MacroSlot, str] = {}
        self.top_badge_text: dict[str, str] = {}

    def sweep_for(self, side: str) -> CircularitySweep:
        return self.left_sweep if side == "left" else self.right_sweep


def _envelope_tag(side: str) -> str:
    return f"diag_live_verify_{side}_envelope"


def _cursor_tag(side: str) -> str:
    return f"diag_live_verify_{side}_cursor"


def _radial_tag(side: str) -> str:
    return f"diag_live_verify_{side}_radial"


def _header_error_tag(side: str) -> str:
    # Avg-error % in the stick card HEADER row (header placement), right-aligned next
    # to the stick name. Replaces the old centred-in-plot number, which the
    # green shaded sweep obscured.
    return f"diag_live_verify_{side}_header_error"


def _header_pct_spacer(label: str) -> float:
    """Spacer width that pushes the avg-error % to the card header's right zone.

    The label sits at the header's left; a spacer of (inner width − estimated
    label width − reserved %-zone) drops the % into a stable right-hand zone.
    Exact glyph metrics aren't available pre-render; over-estimating the label
    width only nudges the % leftward (never clips), and a few px of drift is
    invisible — so a per-character estimate is enough. Clamped to a small
    minimum so an unexpectedly long label can't produce a negative spacer.
    """

    label_w = len(label) * _HEADER_PCT_CHAR_W
    return max(8.0, _STICK_CARD_INNER - label_w - _HEADER_PCT_RESERVE)


def _axes_tag(side: str) -> str:
    return f"diag_live_verify_{side}_axes"


def _coverage_tag(side: str) -> str:
    return f"diag_live_verify_{side}_coverage"


def _test_button_tag(side: str) -> str:
    return f"diag_live_verify_{side}_test_button"


def _stick_block_tag(side: str) -> str:
    return f"diag_live_verify_{side}_stick_block"


def _button_chip_tag(target: ControllerButtonTarget) -> str:
    return f"diag_live_verify_btn_{target.name}"


def _face_hotspot_tag(target: ControllerButtonTarget) -> str:
    return f"diagram_face_{target.name}"


def _face_label_tag(target: ControllerButtonTarget) -> str:
    return f"diagram_face_label_{target.name}"


def _face_binding_badge_tag(target: ControllerButtonTarget) -> str:
    return f"diagram_face_binding_{target.name}"


def _face_stick_dot_tag(side: str) -> str:
    return f"diagram_face_{side}_stick_dot"


def _back_hotspot_tag(slot: MacroSlot) -> str:
    return f"diag_live_verify_back_paddle_{slot.name}"


def _back_label_tag(slot: MacroSlot) -> str:
    return f"diag_live_verify_back_label_{slot.name}"


def _back_binding_badge_tag(slot: MacroSlot) -> str:
    return f"diag_live_verify_back_binding_{slot.name}"


def _top_hotspot_tag(label: str) -> str:
    return f"diag_live_verify_top_{label}"


def _top_label_tag(label: str) -> str:
    return f"diag_live_verify_top_label_{label}"


def _top_binding_badge_tag(label: str) -> str:
    return f"diag_live_verify_top_binding_{label}"


def _deadzone_slider_tag(side: str, kind: str) -> str:
    return f"diag_live_verify_deadzone_{side}_{kind}_slider"


def _deadzone_label_tag(side: str, kind: str) -> str:
    # Mirrors the in-cell derivation in _labeled_deadzone_slider so tests can
    # look the (above-slider) label up by (side, kind) — item G.
    return _deadzone_slider_tag(side, kind).removesuffix("_slider") + "_label"


def _fit_card(*, width: int = -1, tag=0):
    """Open a ``card()`` that FITS its content height on dearpygui 2.x.

    Root cause of the stick-card balloon: the shared ``card()`` helper opens its
    child_window with the LEGACY ``autosize_y`` flag. On dearpygui 2.x that flag
    means "fill the parent's available height" (ImGui ``size.y == 0`` semantics),
    NOT "shrink to fit content" — the content-fit flag was split out into the
    separate ``auto_resize_y`` (ImGui ``ImGuiChildFlags_AutoResizeY``). So every
    legacy-``autosize_y`` card silently fills its remaining vertical space; a card
    stacked above other content balloons to fill the screen. A real-DPG bench
    (``tools/diag_live_verify_card_heights.py``, Inter 14/18 + shipped themes) measured a
    stick card at 625px inside a 725px region — ~40% empty below its content —
    which is exactly the operator's screenshot.

    The cure is the DPG-2.x flag: pass ``auto_resize_y=True`` and suppress the
    legacy fill flag ``card()`` would otherwise default-on (``autosize_y=False``),
    so the card shrinks to its content. Verified on real DPG: in a horizontal
    group each card fits to ITS OWN content, so the two identical-content stick
    cards fit to the SAME height and the reference circles stay equal. Scoped to
    this screen (rather than changed inside ``card()``) so the public cut's other
    screens keep their current card flow unchanged.
    """

    return card(width=width, tag=tag, auto_resize_y=True, autosize_y=False)


def build(shell, parent: str) -> None:
    """Render the dedicated Live Verify screen into ``parent``.

    Mounts ONE governing container (``LIVE_VERIFY_ROOT_TAG``); everything inside
    is laid out per the approved mockup with no wheel-grabbing sub-region (see
    module docstring). Starts the XInput poll worker (idempotent) and arms the
    self-rescheduling frame callback that drives ~60 fps updates on the render
    thread and self-terminates (stopping the worker) when the screen is torn
    down on nav-away.
    """

    state = _LiveVerifyState()
    # Store on the shell so a rebuild can supersede an older frame-callback
    # chain by identity (the tick guards on ``shell._live_verify_state is
    # state``).
    shell._live_verify_state = state
    _ensure_poll_started(shell)

    # The screen's single governing container — the ONLY scrollbar on the
    # screen (far right). autosize_x + autosize_y so it sizes to its content and
    # the content_region's own scroll governs a short window without a redundant
    # outer bar peeking alongside it; this matches every other screen root
    # (diagnostics / restore_points), which is what removes the duplicate outer
    # scroll the operator saw. NO no_scrollbar here — this IS the governing
    # scroll.
    with dpg.child_window(
        parent=parent,
        tag=LIVE_VERIFY_ROOT_TAG,
        autosize_x=True,
        autosize_y=True,
        border=False,
    ):
        _build_header(shell)
        dpg.add_spacer(height=6)
        _build_controller_workspace(shell)
        dpg.add_spacer(height=8)
        _build_demoted_live_cards(shell, state)

    _schedule_live_verify_refresh(shell, state)


def _build_header(shell) -> None:
    """Title + live status pill (dot + availability text) + one-line helper."""

    with dpg.group(horizontal=True):
        screen_title(t("diagnostics.live_verify.title"))
        dpg.add_spacer(width=16)
        # Status "pill": a colored dot + the availability text, matching the
        # top-status-bar idiom. Kept as plain text items (no bordered
        # child_window) so the header introduces no scroll sub-region; the dot
        # colour + text both update live in _set_availability.
        dpg.add_text(
            "●",
            tag=LIVE_VERIFY_AVAILABILITY_DOT_TAG,
            color=shell.COLORS["muted"],
        )
        dpg.add_text(
            t("diagnostics.live_verify.availability.checking"),
            tag=LIVE_VERIFY_AVAILABILITY_TAG,
            color=shell.COLORS["muted"],
        )
    dpg.add_text(
        t("diagnostics.live_verify.intro"),
        color=shell.COLORS["muted"],
        wrap=_SCREEN_WRAP,
    )
    _build_player_selector(shell)


def _build_player_selector(shell) -> None:
    """Controller-slot override row: which XInput pad the live tester reads.

    A small Auto / Player 1-4 combo plus a live "Active: Player N" readout. All
    slot logic lives in the poll service (``XInputPollService`` scans 0-3 and
    sticks to the first connected pad); this row only reflects the active slot
    and forwards the operator's override. The combo's content matches the
    current locale, so it inherits the bound default font's CJK glyphs — no
    per-item font bind, unlike the cross-locale language picker in Preferences.
    """

    service = getattr(shell, "xinput_poll_service", None)
    dpg.add_spacer(height=6)
    with dpg.group(horizontal=True):
        dpg.add_text(
            t("diagnostics.live_verify.player.control_label"),
            color=shell.COLORS["muted"],
        )
        dpg.add_combo(
            items=_player_combo_items(),
            default_value=_player_combo_default(service),
            width=130,
            tag=PLAYER_SELECT_COMBO_TAG,
            callback=lambda _s, value: _on_player_select(shell, value),
        )
        dpg.add_spacer(width=12)
        dpg.add_text(
            _player_active_text(_current_snapshot(service)),
            tag=PLAYER_ACTIVE_TAG,
            color=shell.COLORS["muted"],
        )


def _player_combo_items() -> list[str]:
    """Override-combo labels: ``Auto`` then ``Player 1..N`` (localized)."""

    items = [t("diagnostics.live_verify.player.auto")]
    items.extend(
        t("diagnostics.live_verify.player.slot", n=n)
        for n in range(1, _PLAYER_SLOT_COUNT + 1)
    )
    return items


def _player_combo_default(service) -> str:
    """Initial combo value reflecting the service's current selection.

    ``Player N`` only when the service is explicitly pinned (MANUAL) to a real
    slot; AUTO (and any stub service lacking the selection API) shows ``Auto``.
    """

    mode = getattr(service, "selection_mode", "auto") if service is not None else "auto"
    slot = getattr(service, "active_slot", None) if service is not None else None
    if mode == "manual" and isinstance(slot, int) and 0 <= slot < _PLAYER_SLOT_COUNT:
        return t("diagnostics.live_verify.player.slot", n=slot + 1)
    return t("diagnostics.live_verify.player.auto")


def _player_active_text(snap) -> str:
    """The "Active: Player N" readout for the current snapshot.

    Reads the slot OFF the snapshot so it is atomic with the connection state:
    a connected pad shows its 1-based player number; anything else (no pad, or a
    manually-pinned-but-disconnected slot) reads "No controller".
    """

    slot = getattr(snap, "slot", None) if snap is not None else None
    connected = bool(getattr(snap, "connected", False)) if snap is not None else False
    if connected and isinstance(slot, int):
        label = t("diagnostics.live_verify.player.slot", n=slot + 1)
    else:
        label = t("diagnostics.live_verify.player.none")
    return t("diagnostics.live_verify.player.active", label=label)


def _current_snapshot(service):
    """Best-effort latest snapshot for the build-time readout (None on any stub
    service without a usable ``get_snapshot``)."""

    if service is None:
        return None
    try:
        return service.get_snapshot()
    except Exception:  # noqa: BLE001 — the readout is cosmetic; never block build
        return None


def _on_player_select(shell, value) -> None:
    """Forward an override-combo pick to the poll service (Auto / Player N)."""

    service = getattr(shell, "xinput_poll_service", None)
    if service is None:
        return
    if value == t("diagnostics.live_verify.player.auto"):
        _call_selection(service, "select_auto")
        return
    for n in range(1, _PLAYER_SLOT_COUNT + 1):
        if value == t("diagnostics.live_verify.player.slot", n=n):
            _call_selection(service, "select_slot", n - 1)
            return
    # Unknown label (locale swap mid-open, stray value): default to auto.
    _call_selection(service, "select_auto")


def _call_selection(service, name: str, *args) -> None:
    fn = getattr(service, name, None)
    if not callable(fn):
        return
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 — a UI click must never crash the screen
        logger.exception("Live-verify: %s%r failed", name, args)


def _build_controller_workspace(shell) -> None:
    """Two-pane controller workspace: enlarged front model + inspector."""

    with dpg.group(horizontal=True):
        with _fit_card(width=_WORKSPACE_MODEL_CARD_W):
            section_title(t("diagnostics.live_verify.workspace.title"))
            _build_workspace_controls(shell)
            dpg.add_spacer(height=4)
            _render_face_diagram(shell, show=True)
            _render_back_diagram(shell, show=False)
            _render_top_diagram(shell, show=False)
            refresh_binding_overlays(shell)
            dpg.add_spacer(height=6)
            _build_control_selectors(shell)
        dpg.add_spacer(width=_WORKSPACE_GAP)
        with _fit_card(width=_WORKSPACE_INSPECTOR_W):
            _build_inspector(shell)


def _build_workspace_controls(shell) -> None:
    with dpg.group(horizontal=True):
        dpg.add_button(
            label=t("diagnostics.live_verify.workspace.front_view"),
            tag=LIVE_VERIFY_VIEW_FRONT_BUTTON_TAG,
            width=90,
            callback=lambda: _set_workspace_view(shell, _WORKSPACE_VIEW_FRONT),
        )
        dpg.add_button(
            label=t("diagnostics.live_verify.workspace.back_view"),
            tag=LIVE_VERIFY_VIEW_BACK_BUTTON_TAG,
            width=90,
            callback=lambda: _set_workspace_view(shell, _WORKSPACE_VIEW_BACK),
        )
        dpg.add_button(
            label=t("diagnostics.live_verify.workspace.top_view"),
            tag=LIVE_VERIFY_VIEW_TOP_BUTTON_TAG,
            width=90,
            callback=lambda: _set_workspace_view(shell, _WORKSPACE_VIEW_TOP),
        )
        dpg.add_spacer(width=18)
        dpg.add_checkbox(
            label=t("diagnostics.live_verify.workspace.show_bindings"),
            default_value=True,
            tag=LIVE_VERIFY_SHOW_BINDINGS_TAG,
            callback=lambda _sender, value: _set_binding_badges_visible(
                shell, bool(value)
            ),
        )
    _refresh_workspace_view_buttons(shell)


def _set_workspace_view(shell, view: str) -> None:
    state = getattr(shell, "_live_verify_state", None)
    if state is None or view not in _WORKSPACE_VIEWS:
        return
    state.active_view = view
    front = view == _WORKSPACE_VIEW_FRONT
    back = view == _WORKSPACE_VIEW_BACK
    top = view == _WORKSPACE_VIEW_TOP
    for tag, show in (
        (LIVE_VERIFY_FRONT_TITLE_TAG, front),
        (DIAGRAM_FACE_DRAWLIST_TAG, front),
        (LIVE_VERIFY_FRONT_NOTE_TAG, front),
        (LIVE_VERIFY_BACK_TITLE_TAG, back),
        (DIAGRAM_BACK_DRAWLIST_TAG, back),
        (LIVE_VERIFY_BACK_NOTE_TAG, back),
        (LIVE_VERIFY_TOP_TITLE_TAG, top),
        (DIAGRAM_TOP_DRAWLIST_TAG, top),
        (LIVE_VERIFY_TOP_NOTE_TAG, top),
    ):
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, show=show)
    _refresh_workspace_view_buttons(shell)
    _apply_binding_badge_visibility(shell)


def _refresh_workspace_view_buttons(shell) -> None:
    state = getattr(shell, "_live_verify_state", None)
    active = getattr(state, "active_view", _WORKSPACE_VIEW_FRONT)
    for tag, view in (
        (LIVE_VERIFY_VIEW_FRONT_BUTTON_TAG, _WORKSPACE_VIEW_FRONT),
        (LIVE_VERIFY_VIEW_BACK_BUTTON_TAG, _WORKSPACE_VIEW_BACK),
        (LIVE_VERIFY_VIEW_TOP_BUTTON_TAG, _WORKSPACE_VIEW_TOP),
    ):
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, enabled=active != view)


def _build_demoted_live_cards(shell, state: "_LiveVerifyState") -> None:
    """Keep the legacy numeric/live-write surfaces one disclosure below."""

    with dpg.collapsing_header(
        label=t("diagnostics.live_verify.binding_guide.title"),
        default_open=False,
    ):
        _build_on_device_binding_guide(shell)
    with dpg.collapsing_header(
        label=t("diagnostics.live_verify.advanced.sticks"),
        default_open=False,
    ):
        with dpg.group(horizontal=True):
            _build_stick_block(shell, state, "left")
            dpg.add_spacer(width=_STICK_CARD_GAP)
            _build_stick_block(shell, state, "right")
        dpg.add_text(
            t("diagnostics.live_verify.circularity_help"),
            color=shell.COLORS["muted"],
            wrap=_SCREEN_WRAP,
        )
    with dpg.collapsing_header(
        label=t("diagnostics.live_verify.advanced.buttons"),
        default_open=False,
    ):
        _build_buttons_triggers_card(shell)
    with dpg.collapsing_header(
        label=t("diagnostics.live_verify.advanced.deadzone"),
        default_open=False,
    ):
        _build_inline_deadzone_card(shell)


def _build_on_device_binding_guide(shell) -> None:
    """Manual on-controller binding workflow; display-only, no app action."""

    with _fit_card():
        dpg.add_text(
            t("diagnostics.live_verify.binding_guide.framing"),
            color=shell.COLORS["warn"],
            wrap=_SCREEN_WRAP,
        )
        dpg.add_spacer(height=4)
        for key in (
            "diagnostics.live_verify.binding_guide.assign",
            "diagnostics.live_verify.binding_guide.clear",
            "diagnostics.live_verify.binding_guide.profiles",
        ):
            dpg.add_text(
                t(key),
                color=shell.COLORS["muted"],
                wrap=_SCREEN_WRAP,
            )
            dpg.add_spacer(height=3)


def _build_control_selectors(shell) -> None:
    dpg.add_text(
        t("diagnostics.live_verify.workspace.select_label"),
        color=shell.COLORS["text"],
    )
    for row in _chunks(_FACE_SELECTABLE_TARGETS, 8):
        with dpg.group(horizontal=True):
            for target in row:
                dpg.add_button(
                    label=_control_label(target),
                    tag=_control_selectable_tag(target),
                    width=68,
                    callback=lambda _sender, _app_data, user_data: _select_control(
                        shell, user_data
                    ),
                    user_data=target,
                )
    dpg.add_spacer(height=4)
    dpg.add_text(
        t("diagnostics.live_verify.workspace.paddles_label"),
        color=shell.COLORS["muted"],
    )
    for row in _chunks(_NON_LIVE_SELECTABLES, 8):
        with dpg.group(horizontal=True):
            for slot in row:
                dpg.add_button(
                    label=slot.name,
                    tag=_control_selectable_tag(slot),
                    width=68,
                    callback=lambda _sender, _app_data, user_data: _select_control(
                        shell, user_data
                    ),
                    user_data=slot,
                )


def _build_inspector(shell) -> None:
    section_title(t("diagnostics.live_verify.inspector.title"))
    dpg.add_text(
        t("diagnostics.live_verify.inspector.select_hint"),
        tag=LIVE_VERIFY_INSPECTOR_HINT_TAG,
        color=shell.COLORS["muted"],
        wrap=_INSPECTOR_WRAP,
    )
    dpg.add_spacer(height=6)
    dpg.add_text(
        t("diagnostics.live_verify.inspector.identity"),
        color=shell.COLORS["muted"],
    )
    dpg.add_text(
        "",
        tag=LIVE_VERIFY_INSPECTOR_IDENTITY_TAG,
        color=shell.COLORS["text"],
        wrap=_INSPECTOR_WRAP,
    )
    dpg.add_spacer(height=6)
    dpg.add_text(
        t("diagnostics.live_verify.inspector.live"),
        color=shell.COLORS["muted"],
    )
    dpg.add_text(
        "",
        tag=LIVE_VERIFY_INSPECTOR_LIVE_TAG,
        color=shell.COLORS["muted"],
        wrap=_INSPECTOR_WRAP,
    )
    dpg.add_progress_bar(
        default_value=0.0,
        width=260,
        overlay="0",
        tag=LIVE_VERIFY_INSPECTOR_LIVE_BAR_TAG,
        show=False,
    )
    dpg.add_spacer(height=6)
    dpg.add_text(
        t("diagnostics.live_verify.inspector.binding"),
        color=shell.COLORS["muted"],
    )
    binding_item = dpg.add_text(
        "",
        tag=LIVE_VERIFY_INSPECTOR_BINDING_TAG,
        color=shell.COLORS["muted"],
        wrap=_INSPECTOR_WRAP,
    )
    with dpg.tooltip(binding_item, tag=LIVE_VERIFY_INSPECTOR_BINDING_TIP_TAG):
        dpg.add_text(t("controller.buttons.current.unknown_tooltip"), wrap=320)
    dpg.add_text(
        "",
        color=shell.COLORS["accent"],
        tag=LIVE_VERIFY_INSPECTOR_REMAP_TAG,
    )
    dpg.add_spacer(height=6)
    dpg.add_text(
        t("diagnostics.live_verify.inspector.explanation"),
        color=shell.COLORS["muted"],
    )
    dpg.add_text(
        t("diagnostics.live_verify.face_diagram.note"),
        tag=LIVE_VERIFY_INSPECTOR_EXPLANATION_TAG,
        color=shell.COLORS["muted"],
        wrap=_INSPECTOR_WRAP,
    )
    dpg.add_spacer(height=8)
    dpg.add_button(
        label=t("diagnostics.live_verify.inspector.edit_binding"),
        tag=LIVE_VERIFY_INSPECTOR_EDIT_TAG,
        width=160,
        show=False,
        enabled=False,
        callback=lambda: _edit_selected_binding(shell),
    )
    _refresh_inspector_static(shell)


def _chunks(items, size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _control_key(control) -> str:
    return control.name if hasattr(control, "name") else str(control)


def _control_selectable_tag(control) -> str:
    return f"diag_live_verify_select_{_control_key(control)}"


def _control_label(control) -> str:
    return _control_key(control)


def _control_identity(control) -> str:
    if isinstance(control, MacroSlot):
        return t("diagnostics.live_verify.inspector.paddle_identity", slot=control.name)
    return _control_label(control)


def _select_control(shell, control) -> None:
    """Select a control from the list, repainting inspector + face highlight."""

    state = getattr(shell, "_live_verify_state", None)
    if state is None:
        return
    state.selected_control = control
    for item in _FACE_SELECTABLE_TARGETS + _NON_LIVE_SELECTABLES:
        tag = _control_selectable_tag(item)
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, enabled=item != control)

    snap = _current_snapshot(getattr(shell, "xinput_poll_service", None))
    if snap is not None:
        _refresh_live_face_highlights(shell, snap)
        _refresh_live_top_highlights(shell, snap)
    _refresh_selection_highlights(shell, reset_others=True)
    _refresh_inspector_static(shell)
    if snap is not None:
        _refresh_inspector_live(shell, snap)


def _refresh_selection_highlights(shell, *, reset_others: bool = False) -> None:
    """Repaint visible model-selection accents from the shared selection state."""

    _refresh_face_selection_highlight(shell, reset_others=reset_others)
    _refresh_back_selection_highlight(shell, reset_others=reset_others)
    _refresh_top_selection_highlight(shell, reset_others=reset_others)


def _refresh_face_selection_highlight(shell, *, reset_others: bool = False) -> None:
    """Recolor/thicken the selected front-face hotspot in place."""

    state = getattr(shell, "_live_verify_state", None)
    selected = getattr(state, "selected_control", None)
    for target in _FACE_SELECTABLE_TARGETS:
        tag = _face_hotspot_tag(target)
        if not dpg.does_item_exist(tag):
            continue
        if selected == target:
            dpg.configure_item(
                tag,
                color=shell.COLORS["accent_hover"],
                thickness=4.0,
            )
        elif reset_others:
            dpg.configure_item(tag, thickness=2.0)


def _refresh_back_selection_highlight(shell, *, reset_others: bool = False) -> None:
    state = getattr(shell, "_live_verify_state", None)
    selected = getattr(state, "selected_control", None)
    for slot in _BACK_PADDLE_SLOTS:
        tag = _back_hotspot_tag(slot)
        if not dpg.does_item_exist(tag):
            continue
        if selected == slot:
            dpg.configure_item(
                tag,
                color=shell.COLORS["accent_hover"],
                thickness=4.0,
            )
        elif reset_others:
            dpg.configure_item(
                tag,
                color=shell.COLORS["muted"],
                thickness=2.0,
            )


def _refresh_top_selection_highlight(shell, *, reset_others: bool = False) -> None:
    state = getattr(shell, "_live_verify_state", None)
    selected = getattr(state, "selected_control", None)
    for label in _TOP_CONTROLS:
        tag = _top_hotspot_tag(label)
        if not dpg.does_item_exist(tag):
            continue
        if selected == _top_control_selection(label):
            dpg.configure_item(
                tag,
                color=shell.COLORS["accent_hover"],
                thickness=4.0,
            )
        elif reset_others:
            if label in _TOP_SOURCE_SLOTS:
                dpg.configure_item(
                    tag,
                    color=shell.COLORS["warn"],
                    thickness=2.0,
                )
            else:
                dpg.configure_item(tag, thickness=2.0)


def _refresh_inspector_static(shell) -> None:
    """Repaint identity, cached binding, and explanation for the selection."""

    if not dpg.does_item_exist(LIVE_VERIFY_INSPECTOR_IDENTITY_TAG):
        return
    state = getattr(shell, "_live_verify_state", None)
    selected = getattr(state, "selected_control", None)
    _set_inspector_tip(False)
    if selected is None:
        _show_inspector_hint(True)
        _set_edit_binding_action(False)
        _lv_set(LIVE_VERIFY_INSPECTOR_IDENTITY_TAG, "")
        _lv_set(LIVE_VERIFY_INSPECTOR_LIVE_TAG, "")
        _lv_set(LIVE_VERIFY_INSPECTOR_BINDING_TAG, "")
        _lv_set(LIVE_VERIFY_INSPECTOR_REMAP_TAG, "")
        _lv_set(
            LIVE_VERIFY_INSPECTOR_EXPLANATION_TAG,
            t("diagnostics.live_verify.face_diagram.note"),
        )
        _set_inspector_live_bar(False)
        return

    _show_inspector_hint(False)
    _set_edit_binding_action(_is_bindable_control(selected))
    _lv_set(LIVE_VERIFY_INSPECTOR_IDENTITY_TAG, _control_identity(selected))
    _lv_set(
        LIVE_VERIFY_INSPECTOR_EXPLANATION_TAG,
        t(
            "diagnostics.live_verify.inspector.explanation_paddle"
            if isinstance(selected, MacroSlot)
            else "diagnostics.live_verify.inspector.explanation_output"
        ),
    )
    _refresh_inspector_binding(shell)


def _show_inspector_hint(show: bool) -> None:
    if dpg.does_item_exist(LIVE_VERIFY_INSPECTOR_HINT_TAG):
        dpg.configure_item(LIVE_VERIFY_INSPECTOR_HINT_TAG, show=show)


def _set_inspector_tip(show: bool) -> None:
    if dpg.does_item_exist(LIVE_VERIFY_INSPECTOR_BINDING_TIP_TAG):
        dpg.configure_item(LIVE_VERIFY_INSPECTOR_BINDING_TIP_TAG, show=show)


def _set_edit_binding_action(available: bool) -> None:
    if dpg.does_item_exist(LIVE_VERIFY_INSPECTOR_EDIT_TAG):
        dpg.configure_item(
            LIVE_VERIFY_INSPECTOR_EDIT_TAG,
            show=available,
            enabled=available,
        )


def _is_bindable_control(control) -> bool:
    return (
        isinstance(control, MacroSlot)
        or _button_slot_for_control(control) is not None
    )


def _button_slot_for_control(control) -> ButtonSlot | None:
    if not isinstance(control, ControllerButtonTarget):
        return None
    return ButtonSlot.__members__.get(control.name)


def _edit_selected_binding(shell) -> None:
    state = getattr(shell, "_live_verify_state", None)
    selected = getattr(state, "selected_control", None)
    if isinstance(selected, MacroSlot):
        _navigate_to_controller_buttons(shell)
        tag = f"back_paddle_combo_{selected.name}"
        if dpg.does_item_exist(tag) and hasattr(dpg, "focus_item"):
            dpg.focus_item(tag)
        return

    slot = _button_slot_for_control(selected)
    if slot is None:
        return
    _navigate_to_controller_buttons(shell)
    label = _button_slot_label(slot)
    if dpg.does_item_exist("binding_source_combo"):
        dpg.set_value("binding_source_combo", label)
    callback = getattr(shell, "on_binding_source_changed", None)
    if callable(callback):
        callback(label)


def _navigate_to_controller_buttons(shell) -> None:
    shell.controller_active_tab = "buttons"
    switch_screen = getattr(shell, "switch_screen", None)
    if callable(switch_screen):
        switch_screen("controller")
    else:
        shell.current_screen = "controller"


def _button_slot_label(slot: ButtonSlot) -> str:
    return _BUTTON_SLOT_LABELS[slot]


def _set_inspector_live_bar(
    show: bool, value: float = 0.0, overlay: str = "0"
) -> None:
    if not dpg.does_item_exist(LIVE_VERIFY_INSPECTOR_LIVE_BAR_TAG):
        return
    dpg.configure_item(LIVE_VERIFY_INSPECTOR_LIVE_BAR_TAG, show=show, overlay=overlay)
    dpg.set_value(LIVE_VERIFY_INSPECTOR_LIVE_BAR_TAG, max(0.0, min(1.0, value)))


def _refresh_inspector_binding(shell) -> None:
    if not dpg.does_item_exist(LIVE_VERIFY_INSPECTOR_BINDING_TAG):
        return
    state = getattr(shell, "_live_verify_state", None)
    selected = getattr(state, "selected_control", None)
    if selected is None:
        return
    snapshot = getattr(shell, "last_controller_snapshot", None)
    _set_inspector_tip(False)
    _lv_set(LIVE_VERIFY_INSPECTOR_REMAP_TAG, "")

    if isinstance(selected, MacroSlot):
        bindings = snapshot.back_paddle_bindings or {} if snapshot is not None else {}
        binding = bindings.get(selected)
        text, color_role = _format_paddle_binding(selected, binding)
        _lv_set(LIVE_VERIFY_INSPECTOR_BINDING_TAG, text)
        if dpg.does_item_exist(LIVE_VERIFY_INSPECTOR_BINDING_TAG):
            dpg.configure_item(
                LIVE_VERIFY_INSPECTOR_BINDING_TAG,
                color=shell.COLORS[color_role],
            )
        return

    slot = _button_slot_for_control(selected)
    if slot is None:
        _lv_set(LIVE_VERIFY_INSPECTOR_BINDING_TAG, "")
        return
    bindings = snapshot.button_bindings or {} if snapshot is not None else {}
    display = format_button_binding(slot, bindings.get(slot))
    _lv_set(LIVE_VERIFY_INSPECTOR_BINDING_TAG, f"{slot.name} -> {display.text}")
    if dpg.does_item_exist(LIVE_VERIFY_INSPECTOR_BINDING_TAG):
        dpg.configure_item(
            LIVE_VERIFY_INSPECTOR_BINDING_TAG,
            color=shell.COLORS[display.color_role],
        )
    _set_inspector_tip(display.show_unknown_tooltip)
    _lv_set(LIVE_VERIFY_INSPECTOR_REMAP_TAG, display.remapped_tag)


def _format_paddle_binding(
    slot: MacroSlot, binding: BackPaddleBinding | None
) -> tuple[str, str]:
    if binding is None:
        return (
            f"{slot.name} -> {t('controller.back_paddles.not_set_here')}",
            "muted",
        )
    if binding.target is None:
        return f"{slot.name} -> {t('controller.back_paddles.unbound')}", "accent"
    return f"{slot.name} -> {_control_label(binding.target)}", "accent"


def _refresh_inspector_live(shell, snap) -> None:
    if not dpg.does_item_exist(LIVE_VERIFY_INSPECTOR_LIVE_TAG):
        return
    state = getattr(shell, "_live_verify_state", None)
    selected = getattr(state, "selected_control", None)
    if selected is None:
        return
    if isinstance(selected, MacroSlot):
        _set_inspector_live_text(
            shell,
            t("diagnostics.live_verify.inspector.not_readable_live"),
            "muted",
        )
        _set_inspector_live_bar(False)
        return
    if not getattr(snap, "dll_available", False) or not getattr(snap, "connected", False):
        _set_inspector_live_text(
            shell,
            t("diagnostics.live_verify.inspector.no_live_controller"),
            "muted",
        )
        _set_inspector_live_bar(False)
        return
    if selected is ControllerButtonTarget.LT or selected is ControllerButtonTarget.RT:
        value = snap.left_trigger if selected is ControllerButtonTarget.LT else snap.right_trigger
        intensity = max(0.0, min(1.0, value / 255.0))
        lit = intensity > _FACE_TRIGGER_LIGHT_THRESHOLD
        _set_inspector_live_text(
            shell,
            t("diagnostics.live_verify.inspector.trigger_value", value=int(value)),
            "good" if lit else "muted",
        )
        _set_inspector_live_bar(True, intensity, str(int(value)))
        return
    if selected is ControllerButtonTarget.LS or selected is ControllerButtonTarget.RS:
        x, y = (
            snap.left_stick_normalized
            if selected is ControllerButtonTarget.LS
            else snap.right_stick_normalized
        )
        mag = math.hypot(x, y)
        _set_inspector_live_text(
            shell,
            t(
                "diagnostics.live_verify.inspector.stick_value",
                x=f"{x:+.5f}",
                y=f"{y:+.5f}",
                mag=f"{mag:.5f}",
            ),
            "good" if mag > _FACE_STICK_LIGHT_DEADZONE else "muted",
        )
        _set_inspector_live_bar(False)
        return
    pressed = selected in snap.buttons
    _set_inspector_live_text(
        shell,
        t(
            "diagnostics.live_verify.inspector.pressed"
            if pressed
            else "diagnostics.live_verify.inspector.released"
        ),
        "good" if pressed else "muted",
    )
    _set_inspector_live_bar(False)


def _set_inspector_live_text(shell, text: str, color_role: str) -> None:
    _lv_set(LIVE_VERIFY_INSPECTOR_LIVE_TAG, text)
    if dpg.does_item_exist(LIVE_VERIFY_INSPECTOR_LIVE_TAG):
        dpg.configure_item(
            LIVE_VERIFY_INSPECTOR_LIVE_TAG,
            color=shell.COLORS[color_role],
        )


def _build_stick_block(shell, state: "_LiveVerifyState", side: str) -> None:
    """One stick card: header(label + avg-error %) + centered polar plot +
    5-dec axes + Start/Stop + coverage.

    Fixed width (equal columns + equal plots) but _fit_card()-fitted height (see
    _fit_card: auto_resize_y, because the shared card()'s legacy autosize_y FILLS
    on dearpygui 2.x): the card shrinks to its content so it never overflows /
    grows an inner scrollbar, and since both stick cards hold identical content
    they fit to the same height (measured 338px). A fitted card has no scroll
    range, so the wheel passes through to the screen's single governing scroll —
    no no_scroll_with_mouse needed and no no_scrollbar (the wheel-trap this whole
    move fixes). The avg-error % rides the header row (header placement), not the
    obscured plot centre.
    """

    with _fit_card(tag=_stick_block_tag(side), width=_STICK_CARD_W):
        # Header row (header placement): the Controller-screen stick label (Left Stick
        # / Right Stick — reused so we add no duplicate English literal) on the
        # left, the avg-error % right-aligned next to it. The % carries the
        # gated-while-spinning behaviour as COLOUR (muted while the sweep is
        # provisional, accent "good" once coverage is firm) and reads "--%" until
        # a test has run — it replaces the centred-in-plot number the shaded
        # sweep obscured.
        with dpg.group(horizontal=True):
            label = t(f"controller.sticks.{side}")
            dpg.add_text(label, color=shell.COLORS["muted"])
            dpg.add_spacer(width=_header_pct_spacer(label))
            dpg.add_text(
                _HEADER_PCT_IDLE,
                tag=_header_error_tag(side),
                color=shell.COLORS["muted"],
            )
        with dpg.group(horizontal=True):
            if _PLOT_INDENT:
                dpg.add_spacer(width=_PLOT_INDENT)
            with dpg.drawlist(width=_PLOT_SIZE, height=_PLOT_SIZE):
                c, r = _PLOT_CENTER, _PLOT_RADIUS
                accent = shell.COLORS["accent"]
                # Swept envelope FIRST (bottom layer), filled + outlined, so the
                # reference circle + crosshair stay visible on top of the shaded
                # fill. Not clipped to the unit circle: a per-bin radius can
                # exceed 1.0 (over-travel) and the fill shows it (item C).
                # Starts collapsed at the centre until a stick test sweeps it.
                dpg.draw_polygon(
                    _to_screen_points(state.sweep_for(side).envelope_points()),
                    color=accent,
                    fill=(accent[0], accent[1], accent[2], _ENVELOPE_FILL_ALPHA),
                    thickness=2,
                    tag=_envelope_tag(side),
                )
                # Reference unit circle + crosshair axes (static), above fill.
                dpg.draw_circle((c, c), r, color=shell.COLORS["muted"], thickness=1)
                dpg.draw_line((c - r, c), (c + r, c), color=shell.COLORS["muted"], thickness=1)
                dpg.draw_line((c, c - r), (c, c + r), color=shell.COLORS["muted"], thickness=1)
                # Centre->cursor radial line (item B): direction + magnitude,
                # drawn under the dot, updated each frame via p2=...
                dpg.draw_line(
                    (c, c), (c, c),
                    color=accent,
                    thickness=1,
                    tag=_radial_tag(side),
                )
                # (The avg-error % used to be drawn here, centred in the plot;
                # the shaded sweep obscured it, so it was relocated to the
                # card header — see _build_stick_block's header row.)
                # Live cursor dot (top layer).
                dpg.draw_circle(
                    (c, c), 4,
                    color=shell.COLORS["good"],
                    fill=shell.COLORS["good"],
                    tag=_cursor_tag(side),
                )
        dpg.add_text(
            t("diagnostics.live_verify.stick.axes", x="+0.00000", y="+0.00000"),
            tag=_axes_tag(side),
        )
        dpg.add_button(
            label=t("diagnostics.live_verify.run_stick_test"),
            tag=_test_button_tag(side),
            width=200,
            callback=lambda: _toggle_stick_test(shell, side),
        )
        # Coverage readout (kept — the explicit "keep spinning" cue: it rises as
        # the user sweeps, and the header % stays muted/provisional until it
        # crosses the gate). The old "Circularity error: X%" line was removed
        # because its number now lives in the header and its provisional
        # gating is the header colour, so the line was a redundant duplicate —
        # and a wrapping line in a fixed-height card was the source of the
        # operator's per-card scrollbars (N1).
        dpg.add_text(
            t("diagnostics.live_verify.coverage", pct="0"),
            tag=_coverage_tag(side),
            color=shell.COLORS["muted"],
            wrap=_STICK_CARD_INNER,
        )


def _build_buttons_triggers_card(shell) -> None:
    """Combined "Live Buttons & Triggers" card: chip row + L2/R2 analog bars.

    _fit_card() (auto_resize_y) fits its content so it never overflows / grows its
    own scrollbar (layout fit); a fitted card has no scroll range, so any wheel
    passes through to the screen's single governing scroll. (The shared card()'s
    legacy autosize_y would FILL instead on dearpygui 2.x — see _fit_card.)
    """

    with _fit_card():
        section_title(t("diagnostics.live_verify.buttons_triggers_title"))
        dpg.add_spacer(height=4)
        _build_button_chip_row(shell)
        dpg.add_spacer(height=8)
        _build_trigger_bars(shell)


def _build_button_chip_row(shell) -> None:
    dpg.add_text(t("diagnostics.live_verify.buttons.title"), color=shell.COLORS["muted"])
    with dpg.group(horizontal=True):
        for target in _BUTTON_CHIP_ORDER:
            # Universal controller abbreviations (A / LB / LS / ...) stay as
            # latin labels in both locales, matching the back-paddle target
            # convention; only the colour changes on press.
            dpg.add_text(
                target.name,
                tag=_button_chip_tag(target),
                color=shell.COLORS["muted"],
            )


def _build_trigger_bars(shell) -> None:
    dpg.add_text(t("diagnostics.live_verify.triggers.title"), color=shell.COLORS["muted"])
    with dpg.group(horizontal=True):
        dpg.add_text(t("diagnostics.live_verify.triggers.left"), color=shell.COLORS["muted"])
        dpg.add_progress_bar(default_value=0.0, width=240, overlay="0", tag=TRIGGER_LEFT_BAR_TAG)
    with dpg.group(horizontal=True):
        dpg.add_text(t("diagnostics.live_verify.triggers.right"), color=shell.COLORS["muted"])
        dpg.add_progress_bar(default_value=0.0, width=240, overlay="0", tag=TRIGGER_RIGHT_BAR_TAG)


def _render_face_diagram(shell, *, show: bool = True) -> None:
    """Code-drawn FRONT-face live model for XInput output state.

    The model deliberately lights XInput outputs only: physical paddle/source
    identity is not visible through XInput, so the always-on note below states
    the honest boundary. The live refresh pre-creates every draw item here and
    only recolors / moves items in place.
    """

    dpg.add_text(
        t("diagnostics.live_verify.face_diagram.title"),
        tag=LIVE_VERIFY_FRONT_TITLE_TAG,
        color=shell.COLORS["muted"],
        show=show,
    )
    with dpg.drawlist(
        width=_FACE_DIAGRAM_W,
        height=_FACE_DIAGRAM_H,
        tag=DIAGRAM_FACE_DRAWLIST_TAG,
        show=show,
    ):
        muted = shell.COLORS["muted"]
        text = shell.COLORS["text"]
        source = shell.COLORS["warn"]
        _draw_face_shell(shell)

        for target, (p1, p2) in _FACE_TRIGGER_RECTS.items():
            dpg.draw_rectangle(
                p1,
                p2,
                color=muted,
                fill=_with_alpha(muted, 28),
                thickness=2,
                rounding=6,
                tag=_face_hotspot_tag(target),
            )
            _draw_centered_label(
                target.name,
                ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2),
                text,
            )

        for target, (p1, p2) in _FACE_BUMPER_RECTS.items():
            dpg.draw_rectangle(
                p1,
                p2,
                color=muted,
                fill=_with_alpha(muted, 22),
                thickness=2,
                rounding=9,
                tag=_face_hotspot_tag(target),
            )
            _draw_centered_label(
                target.name,
                ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2),
                text,
                tag=_face_label_tag(target),
            )

        dpg.draw_circle((280, 188), 12, color=muted, thickness=1)
        _draw_centered_label(
            t("diagnostics.live_verify.face_diagram.home"),
            (280, 168),
            text,
            size=13,
        )
        for label, (cx, cy) in _FACE_SOURCE_LABELS.items():
            dpg.draw_rectangle(
                (cx - 20, cy - 11),
                (cx + 20, cy + 11),
                color=source,
                fill=_with_alpha(source, 20),
                thickness=1,
                rounding=5,
            )
            _draw_centered_label(label, (cx, cy), source, size=13)
        dpg.draw_text(
            (166, 360),
            t("diagnostics.live_verify.face_diagram.source_note"),
            color=muted,
            size=12,
            tag=DIAGRAM_FACE_SOURCE_NOTE_TAG,
        )

        for target, (cx, cy) in _FACE_BUTTON_POS.items():
            if target in _FACE_BUMPER_RECTS:
                continue
            radius = (
                _FACE_STICK_R
                if target in (ControllerButtonTarget.LS, ControllerButtonTarget.RS)
                else _FACE_BUTTON_R
            )
            if target in _FACE_DPAD_TARGETS:
                _draw_dpad_facet(target, (cx, cy), muted)
            elif target in (ControllerButtonTarget.LS, ControllerButtonTarget.RS):
                _draw_stick_collar((cx, cy), muted, _face_hotspot_tag(target))
            else:
                dpg.draw_circle(
                    (cx, cy),
                    radius,
                    color=muted,
                    fill=_with_alpha(muted, 34),
                    thickness=2.0,
                    tag=_face_hotspot_tag(target),
                )
            _draw_centered_label(
                target.name,
                _FACE_LABEL_POS_OVERRIDES.get(target, (cx, cy - radius - 13)),
                text,
                tag=_face_label_tag(target),
            )

        for side, (cx, cy) in _FACE_STICK_CENTERS.items():
            dpg.draw_circle(
                (cx, cy),
                _FACE_STICK_DOT_R,
                color=muted,
                fill=muted,
                tag=_face_stick_dot_tag(side),
            )

        for target in _FRONT_BINDING_BADGE_TARGETS:
            dpg.draw_text(
                _front_binding_badge_pos(target),
                "",
                color=shell.COLORS["accent"],
                size=_BINDING_BADGE_SIZE,
                show=False,
                tag=_face_binding_badge_tag(target),
            )

    _bind_diagram_click_handler(shell, _WORKSPACE_VIEW_FRONT)

    dpg.add_text(
        t("diagnostics.live_verify.face_diagram.note"),
        tag=LIVE_VERIFY_FRONT_NOTE_TAG,
        color=shell.COLORS["muted"],
        wrap=_FACE_DIAGRAM_W,
        show=show,
    )


def _render_back_diagram(shell, *, show: bool = False) -> None:
    """Code-drawn BACK view: static paddle slots + cached binding badges only."""

    dpg.add_text(
        t("diagnostics.live_verify.back_diagram.title"),
        tag=LIVE_VERIFY_BACK_TITLE_TAG,
        color=shell.COLORS["muted"],
        show=show,
    )
    with dpg.drawlist(
        width=_BACK_DIAGRAM_W,
        height=_BACK_DIAGRAM_H,
        tag=DIAGRAM_BACK_DRAWLIST_TAG,
        show=show,
    ):
        muted = shell.COLORS["muted"]
        text = shell.COLORS["text"]
        _draw_back_shell(shell)

        label_size = int(BACK_LABEL_SIZE * _BACK_VIEW_SCALE)
        for slot in _BACK_PADDLE_SLOTS:
            cx, cy = _back_paddle_center(slot)
            label = f"{slot.name}*" if slot in BACK_PADDLE_APPROX else slot.name
            if slot in BACK_PADDLE_APPROX:
                _draw_smooth_polygon(
                    _back_view_points(_back_claw_points(slot)),
                    color=muted,
                    fill=_with_alpha(muted, 28),
                    thickness=2.0,
                    tag=_back_hotspot_tag(slot),
                )
                dpg.draw_line(
                    _back_view_point(
                        (
                            BACK_PADDLE_POS[slot][0] - 18,
                            BACK_PADDLE_POS[slot][1] + 12,
                        )
                    ),
                    _back_view_point(
                        (
                            BACK_PADDLE_POS[slot][0] + 18,
                            BACK_PADDLE_POS[slot][1] + 12,
                        )
                    ),
                    color=_with_alpha(muted, 120),
                    thickness=1.0,
                )
            else:
                p1, p2 = _back_paddle_bounds(slot)
                _draw_rounded_polygon(
                    _back_view_point(p1),
                    _back_view_point(p2),
                    10 * _BACK_VIEW_SCALE,
                    color=muted,
                    fill=_with_alpha(muted, 26),
                    thickness=2.0,
                    tag=_back_hotspot_tag(slot),
                )
            dpg.draw_text(
                (
                    cx - len(label) * label_size * 0.25,
                    cy - label_size * 0.5,
                ),
                label,
                color=text,
                size=label_size,
                tag=_back_label_tag(slot),
            )
            dpg.draw_text(
                _back_binding_badge_pos(slot),
                "",
                color=muted,
                size=_BACK_BADGE_SIZE,
                show=False,
                tag=_back_binding_badge_tag(slot),
            )

    _bind_diagram_click_handler(shell, _WORKSPACE_VIEW_BACK)

    dpg.add_text(
        t("diagnostics.live_verify.back_diagram.note"),
        tag=LIVE_VERIFY_BACK_NOTE_TAG,
        color=shell.COLORS["muted"],
        wrap=_BACK_DIAGRAM_W,
        show=show,
    )


def _render_top_diagram(shell, *, show: bool = False) -> None:
    """Code-drawn TOP view: live shoulders/triggers + source-only claws."""

    dpg.add_text(
        t("diagnostics.live_verify.top_diagram.title"),
        tag=LIVE_VERIFY_TOP_TITLE_TAG,
        color=shell.COLORS["muted"],
        show=show,
    )
    with dpg.drawlist(
        width=_TOP_DIAGRAM_W,
        height=_TOP_DIAGRAM_H,
        tag=DIAGRAM_TOP_DRAWLIST_TAG,
        show=show,
    ):
        muted = shell.COLORS["muted"]
        text = shell.COLORS["text"]
        source = shell.COLORS["warn"]
        _draw_top_shell(shell)
        for label in _TOP_CONTROLS:
            p1, p2 = _top_control_bounds(label)
            source_only = label in _TOP_SOURCE_SLOTS
            color = source if source_only else muted
            _draw_rounded_polygon(
                p1,
                p2,
                10,
                color=color,
                fill=_with_alpha(color, 24 if source_only else 30),
                thickness=2,
                tag=_top_hotspot_tag(label),
            )
            if source_only:
                dpg.draw_line(
                    (p1[0] + 10, p2[1] - 8),
                    (p2[0] - 10, p2[1] - 8),
                    color=source,
                    thickness=1,
                )
            _draw_centered_label(
                label,
                ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2),
                text,
                tag=_top_label_tag(label),
            )
            dpg.draw_text(
                _top_binding_badge_pos(label),
                "",
                color=shell.COLORS["accent"],
                size=_TOP_BADGE_SIZE,
                show=False,
                tag=_top_binding_badge_tag(label),
            )

    _bind_diagram_click_handler(shell, _WORKSPACE_VIEW_TOP)

    dpg.add_text(
        t("diagnostics.live_verify.top_diagram.note"),
        tag=LIVE_VERIFY_TOP_NOTE_TAG,
        color=shell.COLORS["muted"],
        wrap=_TOP_DIAGRAM_W,
        show=show,
    )


def _draw_face_shell(shell) -> None:
    glass_edge = (232, 244, 255, 166)
    glass_fill = (244, 249, 255, 32)
    inner_fill = (244, 249, 255, 18)
    panel_shadow = _with_alpha(shell.COLORS["panel_alt"], 70)
    accent = _CRYSTAL_BLUE
    body = [
        (78, 92),
        (138, 56),
        (222, 46),
        (280, 62),
        (338, 46),
        (422, 56),
        (482, 92),
        (522, 186),
        (505, 306),
        (458, 376),
        (408, 370),
        (361, 304),
        (315, 312),
        (280, 288),
        (245, 312),
        (199, 304),
        (152, 370),
        (102, 376),
        (55, 306),
        (38, 186),
    ]
    inner = [
        (92, 106),
        (152, 74),
        (223, 64),
        (280, 80),
        (337, 64),
        (408, 74),
        (468, 106),
        (496, 190),
        (480, 294),
        (446, 346),
        (412, 342),
        (374, 286),
        (318, 294),
        (280, 270),
        (242, 294),
        (186, 286),
        (148, 342),
        (114, 346),
        (80, 294),
        (64, 190),
    ]
    _draw_smooth_polygon(body, color=panel_shadow, fill=panel_shadow, thickness=1)
    _draw_smooth_polygon(body, color=glass_edge, fill=glass_fill, thickness=2.2)
    _draw_smooth_polygon(inner, color=(255, 255, 255, 56), fill=inner_fill, thickness=1.0)
    _draw_smooth_polygon(
        [(78, 66), (96, 34), (207, 34), (220, 60), (190, 112), (72, 110)],
        color=glass_edge,
        fill=(244, 249, 255, 22),
        thickness=1.6,
    )
    _draw_smooth_polygon(
        [(482, 66), (464, 34), (353, 34), (340, 60), (370, 112), (488, 110)],
        color=glass_edge,
        fill=(244, 249, 255, 22),
        thickness=1.6,
    )
    _draw_smooth_polygon(
        [(205, 64), (280, 82), (355, 64), (370, 110), (190, 110)],
        color=(232, 244, 255, 120),
        fill=(244, 249, 255, 18),
        thickness=1.4,
    )
    _draw_segmented_line(
        [(166, 338), (188, 296), (204, 242), (218, 190), (238, 146)],
        accent,
        2.4,
    )
    _draw_segmented_line(
        [(394, 338), (372, 296), (356, 242), (342, 190), (322, 146)],
        accent,
        2.4,
    )
    _draw_segmented_line(
        [(236, 126), (258, 112), (280, 108), (302, 112), (324, 126)],
        accent,
        2.0,
    )


def _draw_top_shell(shell) -> None:
    glass_edge = (232, 244, 255, 160)
    glass_fill = (244, 249, 255, 30)
    accent = _CRYSTAL_BLUE
    _draw_smooth_polygon(
        [
            (62, 112),
            (92, 78),
            (226, 68),
            (280, 86),
            (334, 68),
            (468, 78),
            (498, 112),
            (510, 272),
            (462, 312),
            (336, 298),
            (280, 314),
            (224, 298),
            (98, 312),
            (50, 272),
        ],
        color=glass_edge,
        fill=glass_fill,
        thickness=2,
    )
    _draw_smooth_polygon(
        [
            (112, 82),
            (216, 78),
            (280, 94),
            (344, 78),
            (448, 82),
            (470, 150),
            (452, 258),
            (352, 278),
            (280, 294),
            (208, 278),
            (108, 258),
            (90, 150),
        ],
        color=(255, 255, 255, 52),
        fill=(244, 249, 255, 14),
        thickness=1,
    )
    _draw_segmented_line([(154, 272), (196, 286), (244, 286)], accent, 2.2)
    _draw_segmented_line([(406, 272), (364, 286), (316, 286)], accent, 2.2)
    _draw_segmented_line([(236, 96), (258, 104), (280, 106), (302, 104), (324, 96)], accent, 2.0)


def _draw_back_shell(shell) -> None:
    glass_edge = (232, 244, 255, 160)
    glass_fill = (244, 249, 255, 30)
    inner_fill = (244, 249, 255, 15)
    panel_shadow = _with_alpha(shell.COLORS["panel_alt"], 70)
    muted = shell.COLORS["muted"]
    accent = _CRYSTAL_BLUE
    body = [
        (34, 54),
        (62, 22),
        (108, 10),
        (150, 24),
        (192, 10),
        (238, 22),
        (266, 54),
        (286, 112),
        (278, 162),
        (244, 210),
        (218, 206),
        (196, 170),
        (174, 164),
        (150, 182),
        (126, 164),
        (104, 170),
        (82, 206),
        (56, 210),
        (22, 162),
        (14, 112),
    ]
    inner = [
        (48, 62),
        (72, 34),
        (110, 24),
        (150, 38),
        (190, 24),
        (228, 34),
        (252, 62),
        (270, 112),
        (262, 154),
        (236, 190),
        (214, 188),
        (194, 154),
        (170, 150),
        (150, 166),
        (130, 150),
        (106, 154),
        (86, 188),
        (64, 190),
        (38, 154),
        (30, 112),
    ]
    _draw_smooth_polygon(
        _back_view_points(body),
        color=panel_shadow,
        fill=panel_shadow,
        thickness=1,
    )
    _draw_smooth_polygon(
        _back_view_points(body),
        color=glass_edge,
        fill=glass_fill,
        thickness=2.0,
    )
    _draw_smooth_polygon(
        _back_view_points(inner),
        color=(255, 255, 255, 50),
        fill=inner_fill,
        thickness=1.0,
    )
    _draw_smooth_polygon(
        _back_view_points(
            [
                (64, 24),
                (108, 8),
                (150, 22),
                (192, 8),
                (236, 24),
                (222, 48),
                (78, 48),
            ]
        ),
        color=(232, 244, 255, 120),
        fill=(244, 249, 255, 16),
        thickness=1.2,
    )
    _draw_segmented_line(
        _back_view_points([(70, 178), (92, 150), (108, 118), (124, 86)]),
        accent,
        2.1,
    )
    _draw_segmented_line(
        _back_view_points([(230, 178), (208, 150), (192, 118), (176, 86)]),
        accent,
        2.1,
    )
    _draw_segmented_line(
        _back_view_points([(114, 54), (136, 44), (150, 42), (164, 44), (186, 54)]),
        accent,
        1.8,
    )
    dpg.draw_circle(
        _back_view_point((150, 42)),
        4.8 * _BACK_VIEW_SCALE,
        color=muted,
        fill=_with_alpha(muted, 36),
        thickness=1.0,
    )
    for p1, p2 in (((46, 70), (78, 77)), ((222, 70), (254, 77))):
        dpg.draw_rectangle(
            _back_view_point(p1),
            _back_view_point(p2),
            color=_with_alpha(muted, 130),
            fill=_with_alpha(muted, 18),
            thickness=1.0,
            rounding=2 * _BACK_VIEW_SCALE,
        )


def _back_view_points(
    points: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    return [_back_view_point(point) for point in points]


def _back_claw_points(slot: MacroSlot) -> list[tuple[float, float]]:
    cx, cy = BACK_PADDLE_POS[slot]
    if slot is MacroSlot.RK:
        return [
            (cx - 29, cy - 3),
            (cx - 20, cy - 15),
            (cx + 20, cy - 14),
            (cx + 30, cy),
            (cx + 17, cy + 15),
            (cx - 21, cy + 11),
        ]
    return [
        (cx - 30, cy),
        (cx - 20, cy - 14),
        (cx + 20, cy - 15),
        (cx + 29, cy - 3),
        (cx + 21, cy + 11),
        (cx - 17, cy + 15),
    ]


def _back_paddle_bounds(
    slot: MacroSlot,
) -> tuple[tuple[float, float], tuple[float, float]]:
    cx, cy = BACK_PADDLE_POS[slot]
    width, height = (42, 26) if slot in _BACK_UPPER_PADDLE_SLOTS else (38, 30)
    return ((cx - width / 2, cy - height / 2), (cx + width / 2, cy + height / 2))


def _draw_segmented_line(
    points: list[tuple[float, float]],
    color,
    thickness: float,
) -> None:
    smooth_points = _smooth_open_points(points)
    for p1, p2 in zip(smooth_points, smooth_points[1:]):
        dpg.draw_line(p1, p2, color=_with_alpha(color, 42), thickness=thickness + 3)
        dpg.draw_line(p1, p2, color=color, thickness=thickness)


def _draw_smooth_polygon(
    points: list[tuple[float, float]],
    *,
    color,
    fill,
    thickness: float,
    tag=0,
) -> None:
    dpg.draw_polygon(
        _smooth_closed_points(points),
        color=color,
        fill=fill,
        thickness=thickness,
        tag=tag,
    )


def _draw_rounded_polygon(
    p1: tuple[float, float],
    p2: tuple[float, float],
    radius: float,
    *,
    color,
    fill,
    thickness: float,
    tag=0,
) -> None:
    dpg.draw_polygon(
        _rounded_rect_points(p1, p2, radius),
        color=color,
        fill=fill,
        thickness=thickness,
        tag=tag,
    )


def _smooth_closed_points(
    points: list[tuple[float, float]],
    *,
    samples: int = 8,
) -> list[tuple[float, float]]:
    if len(points) < 3:
        return points
    smoothed: list[tuple[float, float]] = []
    count = len(points)
    for index, point in enumerate(points):
        p0 = points[(index - 1) % count]
        p1 = point
        p2 = points[(index + 1) % count]
        p3 = points[(index + 2) % count]
        for step in range(samples):
            smoothed.append(_catmull_rom_point(p0, p1, p2, p3, step / samples))
    return smoothed


def _smooth_open_points(
    points: list[tuple[float, float]],
    *,
    samples: int = 8,
) -> list[tuple[float, float]]:
    if len(points) < 3:
        return points
    smoothed: list[tuple[float, float]] = []
    last = len(points) - 1
    for index in range(last):
        p0 = points[index - 1] if index > 0 else points[index]
        p1 = points[index]
        p2 = points[index + 1]
        p3 = points[index + 2] if index + 2 <= last else points[index + 1]
        for step in range(samples):
            smoothed.append(_catmull_rom_point(p0, p1, p2, p3, step / samples))
    smoothed.append(points[-1])
    return smoothed


def _catmull_rom_point(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    t_value: float,
) -> tuple[float, float]:
    t2 = t_value * t_value
    t3 = t2 * t_value
    return (
        0.5
        * (
            (2 * p1[0])
            + (-p0[0] + p2[0]) * t_value
            + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
            + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3
        ),
        0.5
        * (
            (2 * p1[1])
            + (-p0[1] + p2[1]) * t_value
            + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
            + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3
        ),
    )


def _rounded_rect_points(
    p1: tuple[float, float],
    p2: tuple[float, float],
    radius: float,
    *,
    segments: int = 6,
) -> list[tuple[float, float]]:
    x1, y1 = p1
    x2, y2 = p2
    left, right = sorted((x1, x2))
    top, bottom = sorted((y1, y2))
    radius = max(0.0, min(radius, (right - left) / 2, (bottom - top) / 2))
    corners = (
        ((left + radius, top + radius), 180, 270),
        ((right - radius, top + radius), 270, 360),
        ((right - radius, bottom - radius), 0, 90),
        ((left + radius, bottom - radius), 90, 180),
    )
    points: list[tuple[float, float]] = []
    for (cx, cy), start, end in corners:
        for step in range(segments + 1):
            angle = math.radians(start + (end - start) * step / segments)
            points.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
    return points


def _draw_dpad_facet(
    target: ControllerButtonTarget,
    center: tuple[float, float],
    color,
) -> None:
    cx, cy = center
    if target is ControllerButtonTarget.UP:
        points = [(cx, cy - 24), (cx + 27, cy + 2), (cx + 10, cy + 22), (cx - 10, cy + 22), (cx - 27, cy + 2)]
    elif target is ControllerButtonTarget.DOWN:
        points = [(cx, cy + 24), (cx + 27, cy - 2), (cx + 10, cy - 22), (cx - 10, cy - 22), (cx - 27, cy - 2)]
    elif target is ControllerButtonTarget.LEFT:
        points = [(cx - 24, cy), (cx + 2, cy - 27), (cx + 22, cy - 10), (cx + 22, cy + 10), (cx + 2, cy + 27)]
    else:
        points = [(cx + 24, cy), (cx - 2, cy - 27), (cx - 22, cy - 10), (cx - 22, cy + 10), (cx - 2, cy + 27)]
    dpg.draw_polygon(
        points,
        color=color,
        fill=_with_alpha(color, 34),
        thickness=2,
        tag=_face_hotspot_tag(target),
    )
    dpg.draw_line(points[0], points[2], color=_with_alpha((255, 255, 255, 180), 90), thickness=1)


def _draw_stick_collar(
    center: tuple[float, float],
    color,
    tag: str,
) -> None:
    dpg.draw_polygon(
        _octagon_points(center, _FACE_STICK_R),
        color=color,
        fill=(244, 249, 255, 18),
        thickness=2.2,
        tag=tag,
    )
    dpg.draw_polygon(
        _octagon_points(center, _FACE_STICK_R - 11),
        color=_with_alpha((255, 255, 255, 180), 90),
        fill=(0, 0, 0, 0),
        thickness=1.1,
    )
    dpg.draw_circle(center, _FACE_STICK_R - 18, color=color, fill=_with_alpha(color, 26), thickness=1.4)


def _octagon_points(
    center: tuple[float, float],
    radius: float,
) -> list[tuple[float, float]]:
    cx, cy = center
    return [
        (
            cx + math.cos(math.radians(22.5 + index * 45)) * radius,
            cy + math.sin(math.radians(22.5 + index * 45)) * radius,
        )
        for index in range(8)
    ]


def _face_hotspot_bounds(
    target: ControllerButtonTarget,
) -> tuple[tuple[float, float], tuple[float, float]]:
    if target in _FACE_TRIGGER_RECTS:
        return _FACE_TRIGGER_RECTS[target]
    if target in _FACE_BUMPER_RECTS:
        return _FACE_BUMPER_RECTS[target]
    cx, cy = _FACE_BUTTON_POS[target]
    radius = (
        _FACE_STICK_R
        if target in (ControllerButtonTarget.LS, ControllerButtonTarget.RS)
        else _FACE_BUTTON_R
    )
    return ((cx - radius, cy - radius), (cx + radius, cy + radius))


def _front_binding_badge_pos(target: ControllerButtonTarget) -> tuple[float, float]:
    p1, p2 = _face_hotspot_bounds(target)
    return (p2[0] + 8, p1[1] - 2)


def _top_control_bounds(label: str) -> tuple[tuple[float, float], tuple[float, float]]:
    return _TOP_CONTROL_RECTS[label]


def _top_control_center(label: str) -> tuple[float, float]:
    p1, p2 = _top_control_bounds(label)
    return ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)


def _top_binding_badge_pos(label: str) -> tuple[float, float]:
    p1, p2 = _top_control_bounds(label)
    return (p2[0] + 8, p1[1] - 1)


def _top_is_source_only(label: str) -> bool:
    return label in _TOP_SOURCE_SLOTS


def _top_control_selection(label: str):
    if label in _TOP_SOURCE_SLOTS:
        return _TOP_SOURCE_SLOTS[label]
    return _TOP_LIVE_TARGETS[label]


def _bind_diagram_click_handler(shell, view: str) -> None:
    drawlist_tag = _DIAGRAM_DRAWLIST_TAGS[view]
    handler_tag = _DIAGRAM_CLICK_HANDLER_TAGS[view]
    if dpg.does_item_exist(handler_tag):
        dpg.delete_item(handler_tag)
    with dpg.item_handler_registry(tag=handler_tag):
        dpg.add_item_clicked_handler(
            callback=_on_diagram_clicked,
            user_data=(shell, view),
        )
    dpg.bind_item_handler_registry(drawlist_tag, handler_tag)


def _on_diagram_clicked(_sender=None, _app_data=None, user_data=None) -> None:
    if not user_data:
        return
    shell, view = user_data
    _handle_diagram_click(shell, view)


def _handle_diagram_click(shell, view: str) -> None:
    state = getattr(shell, "_live_verify_state", None)
    if state is None or getattr(state, "active_view", None) != view:
        return
    drawlist_tag = _DIAGRAM_DRAWLIST_TAGS.get(view)
    if not drawlist_tag:
        return
    point = _diagram_local_mouse_pos(drawlist_tag)
    if point is None:
        return
    control = _diagram_control_at_point(view, point)
    if control is None:
        return
    _select_control(shell, control)


def _diagram_local_mouse_pos(drawlist_tag: str) -> tuple[float, float] | None:
    try:
        mouse = dpg.get_mouse_pos(local=True)
        origin = dpg.get_item_rect_min(drawlist_tag)
    except Exception:  # noqa: BLE001 - a click should never crash the screen
        return None
    if not mouse or not origin:
        return None
    return (float(mouse[0]) - float(origin[0]), float(mouse[1]) - float(origin[1]))


def _diagram_control_at_point(view: str, point: tuple[float, float]):
    if view == _WORKSPACE_VIEW_FRONT:
        return _front_control_at_point(point)
    if view == _WORKSPACE_VIEW_BACK:
        return _back_control_at_point(point)
    if view == _WORKSPACE_VIEW_TOP:
        return _top_control_at_point(point)
    return None


def _front_control_at_point(point: tuple[float, float]):
    for target in _FACE_SELECTABLE_TARGETS:
        if _point_in_bounds(point, _face_hotspot_bounds(target)):
            return target
    return None


def _back_control_at_point(point: tuple[float, float]):
    for slot in _BACK_PADDLE_SLOTS:
        if slot in _BACK_TOP_CLAW_SLOTS:
            bounds = _bounds_for_points(_back_view_points(_back_claw_points(slot)))
        else:
            p1, p2 = _back_paddle_bounds(slot)
            bounds = (_back_view_point(p1), _back_view_point(p2))
        if _point_in_bounds(point, bounds):
            return slot
    return None


def _top_control_at_point(point: tuple[float, float]):
    for label in _TOP_CONTROLS:
        if _point_in_bounds(point, _top_control_bounds(label)):
            return _top_control_selection(label)
    return None


def _point_in_bounds(
    point: tuple[float, float],
    bounds: tuple[tuple[float, float], tuple[float, float]],
) -> bool:
    x, y = point
    (x1, y1), (x2, y2) = bounds
    left, right = sorted((x1, x2))
    top, bottom = sorted((y1, y2))
    return left <= x <= right and top <= y <= bottom


def _bounds_for_points(
    points: list[tuple[float, float]],
) -> tuple[tuple[float, float], tuple[float, float]]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return ((min(xs), min(ys)), (max(xs), max(ys)))


def _back_view_point(point: tuple[float, float]) -> tuple[float, float]:
    return (
        _BACK_VIEW_OFFSET[0] + point[0] * _BACK_VIEW_SCALE,
        _BACK_VIEW_OFFSET[1] + point[1] * _BACK_VIEW_SCALE,
    )


def _back_paddle_center(slot: MacroSlot) -> tuple[float, float]:
    return _back_view_point(BACK_PADDLE_POS[slot])


def _back_binding_badge_pos(slot: MacroSlot) -> tuple[float, float]:
    cx, cy = _back_paddle_center(slot)
    radius = BACK_PADDLE_R * _BACK_VIEW_SCALE
    return (cx + radius + 10, cy - _BACK_BADGE_SIZE * 0.5)


def _draw_centered_label(
    label: str,
    center: tuple[float, float],
    color,
    *,
    size: int = _FACE_LABEL_SIZE,
    tag=0,
) -> None:
    x, y = center
    dpg.draw_text(
        (x - len(label) * size * 0.24, y - size * 0.5),
        label,
        color=color,
        size=size,
        tag=tag,
    )


def _with_alpha(color, alpha: int):
    return (color[0], color[1], color[2], alpha)


def _device_write_supported(shell) -> bool:
    """True when the connected controller is an allowlisted ZD Ultimate Legend.

    The firmware-deadzone card is the one WRITE surface on this otherwise
    read-only screen, so it gates on the same capability the shell's write paths
    do. Reads ``device_service.state.write_supported`` and defaults True for stub
    shells that lack the flag, so headless build-smoke tests are unaffected.
    """

    state = getattr(getattr(shell, "device_service", None), "state", None)
    return bool(getattr(state, "write_supported", True))


def _build_inline_deadzone_card(shell) -> None:
    """Inline firmware-deadzone tuning card. On an allowlisted ZD these sliders
    write the REAL controller ``StickDeadzones`` (debounced + read-back-verified
    via the shell), which is what makes the circularity envelope move. Hydrated
    from a live ``get_deadzones()`` read; callbacks are inert until that read
    succeeds so a read-miss can never clobber the controller with a default.

    On a non-ZD / unverified controller the card is read-only: we never even
    issue the firmware read, the sliders stay disabled, and the status + note say
    so plainly. The live tester above (sticks / circularity / buttons / triggers)
    still runs for any XInput pad — only this write surface is gated.

    _fit_card() (auto_resize_y) fits its content so it never overflows / grows its
    own scrollbar (layout fit); a fitted card has no scroll range, so any wheel
    passes through to the screen's single governing scroll. (The shared card()'s
    legacy autosize_y would FILL instead on dearpygui 2.x — see _fit_card.)
    """

    write_supported = _device_write_supported(shell)
    # Skip the firmware read entirely on a non-allowlisted controller — never
    # attempt a ZD HID read on hardware whose protocol is unverified.
    current = _read_current_deadzones(shell) if write_supported else None
    hydrated = current is not None
    shell._diag_deadzone_hydrated = hydrated
    defaults = current if current is not None else StickDeadzones(0, 0, 0, 0)

    if not write_supported:
        status_key = "diagnostics.live_verify.deadzone.status.unverified_device"
        note_key = "diagnostics.live_verify.deadzone.note_unverified"
    else:
        status_key = (
            "diagnostics.live_verify.deadzone.status.idle"
            if hydrated
            else "diagnostics.live_verify.deadzone.status.unavailable"
        )
        note_key = "diagnostics.live_verify.deadzone.note"
    # Pin the live-tick status so the per-frame _refresh_deadzone_status keeps
    # showing the right line (it reads this key every tick); on a non-ZD pad the
    # write path is refused before it can change the key, so it stays "read-only".
    shell._diag_deadzone_status_key = (
        "unverified_device" if not write_supported else ("idle" if hydrated else "unavailable")
    )

    with _fit_card():
        section_title(t("diagnostics.live_verify.deadzone.title"))
        dpg.add_text(
            t(note_key),
            color=shell.COLORS["warn"],
            wrap=_SCREEN_WRAP,
        )
        dpg.add_text(
            t(status_key),
            tag=DEADZONE_STATUS_TAG,
            color=shell.COLORS["muted"] if hydrated else shell.COLORS["warn"],
        )
        dpg.add_spacer(height=6)
        # 2x2 grid of label-over-slider cells (item G): two columns (Left /
        # Right), each with an Inner and an Outer slider, the label centred
        # above its slider and the live value riding inside the slider grab.
        with dpg.group(horizontal=True):
            with dpg.group():
                _labeled_deadzone_slider(
                    shell,
                    label=t("diagnostics.live_verify.deadzone.left_center"),
                    default_value=int(defaults.left_center),
                    tag=_deadzone_slider_tag("left", "center"),
                    enabled=hydrated,
                )
                _labeled_deadzone_slider(
                    shell,
                    label=t("diagnostics.live_verify.deadzone.left_outer"),
                    default_value=int(defaults.left_outer),
                    tag=_deadzone_slider_tag("left", "outer"),
                    enabled=hydrated,
                )
            dpg.add_spacer(width=40)
            with dpg.group():
                _labeled_deadzone_slider(
                    shell,
                    label=t("diagnostics.live_verify.deadzone.right_center"),
                    default_value=int(defaults.right_center),
                    tag=_deadzone_slider_tag("right", "center"),
                    enabled=hydrated,
                )
                _labeled_deadzone_slider(
                    shell,
                    label=t("diagnostics.live_verify.deadzone.right_outer"),
                    default_value=int(defaults.right_outer),
                    tag=_deadzone_slider_tag("right", "outer"),
                    enabled=hydrated,
                )


def _deadzone_label_indent(label: str) -> float:
    """Approximate left indent that centres ``label`` over its slider (item G).

    The slider sits at the group's left edge; shifting the label right by half
    the slack between the estimated label width and the slider width centres
    it. Exact glyph metrics aren't available pre-render and a few px of drift
    is invisible, so a per-character estimate is enough.
    """

    width = len(label) * _DEADZONE_LABEL_CHAR_W
    return max(0.0, (_DEADZONE_SLIDER_WIDTH - width) / 2.0)


def _labeled_deadzone_slider(
    shell, *, label: str, default_value: int, tag: str, enabled: bool
) -> None:
    """One inline-deadzone cell: a centred label above its slider (item G).

    The slider carries no inline label (a right-side label re-creates the
    cramped layout the polish pass is removing); the live value shows inside
    the slider grab. A trailing spacer separates the stacked cells.
    """

    dpg.add_text(
        label,
        tag=tag.removesuffix("_slider") + "_label",
        indent=_deadzone_label_indent(label),
        color=shell.COLORS["muted"],
    )
    dpg.add_slider_int(
        label="",
        default_value=default_value,
        min_value=0,
        max_value=100,
        width=_DEADZONE_SLIDER_WIDTH,
        tag=tag,
        enabled=enabled,
        callback=lambda: _on_deadzone_change(shell),
    )
    dpg.add_spacer(height=8)


# ---------------------------------------------------------------------------
# Live-verify callbacks + frame-callback drive
# ---------------------------------------------------------------------------


def _ensure_poll_started(shell) -> None:
    try:
        shell.xinput_poll_service.start()
    except Exception:  # noqa: BLE001 — never let a service hiccup block the build
        logger.exception("Live-verify: poll service start failed")


def _toggle_stick_test(shell, side: str) -> None:
    state = getattr(shell, "_live_verify_state", None)
    if state is None:
        return
    active = not getattr(state, f"{side}_active")
    setattr(state, f"{side}_active", active)
    if active:
        state.sweep_for(side).reset()
    tag = _test_button_tag(side)
    if dpg.does_item_exist(tag):
        dpg.configure_item(
            tag,
            label=t("diagnostics.live_verify.stop_stick_test")
            if active
            else t("diagnostics.live_verify.run_stick_test"),
        )


def _read_current_deadzones(shell):
    """Best-effort live read of the firmware deadzones for hydration.

    Returns a ``StickDeadzones`` on a clean read, else ``None`` (no service,
    HID busy, read error, or a non-``StickDeadzones`` stub in tests) so the
    sliders stay disabled and the write callbacks stay inert.
    """

    service = getattr(shell, "settings_service", None)
    if service is None:
        return None
    if getattr(shell, "hid_busy", False):
        return None
    try:
        current = service.get_deadzones()
    except Exception:  # noqa: BLE001 — hydration is best-effort
        logger.exception("Live-verify: deadzone hydration read raised")
        return None
    return current if isinstance(current, StickDeadzones) else None


def _collect_deadzone_sliders():
    tags = (
        _deadzone_slider_tag("left", "center"),
        _deadzone_slider_tag("right", "center"),
        _deadzone_slider_tag("left", "outer"),
        _deadzone_slider_tag("right", "outer"),
    )
    if not all(dpg.does_item_exist(tag) for tag in tags):
        return None
    try:
        return StickDeadzones(
            left_center=int(dpg.get_value(tags[0])),
            right_center=int(dpg.get_value(tags[1])),
            left_outer=int(dpg.get_value(tags[2])),
            right_outer=int(dpg.get_value(tags[3])),
        )
    except Exception:  # noqa: BLE001 — a stray callback must not crash the UI
        logger.exception("Live-verify: deadzone slider collect raised")
        return None


def _on_deadzone_change(shell) -> None:
    deadzones = _collect_deadzone_sliders()
    if deadzones is None:
        return
    shell.apply_diagnostics_deadzone(deadzones)


def _schedule_live_verify_refresh(shell, state: "_LiveVerifyState") -> None:
    """Register a self-rescheduling frame callback that drives the screen.

    Mirrors the Health Report screen's frame-callback chain. Each tick:
    supersede-by-identity guard (a rebuild started a newer chain, this one
    dies) -> root lifecycle guard -> refresh -> re-arm. Runs on the render
    thread, so all DPG mutation is safe.

    The root guard distinguishes two "root missing" cases (item I robustness):
    the root not being queryable on an EARLY tick (before its widgets commit /
    frame-count timing) must NOT be mistaken for a nav-away teardown — doing so
    would stop the worker and end the chain, leaving the screen frozen until a
    rebuild that nothing triggers. So the chain keeps re-arming until the root
    has been seen at least once (``state.root_seen``); only after the screen has
    actually gone live does a missing root mean a genuine teardown (stop the
    worker, end the chain). This makes the chain provably self-sustaining
    across a build -> teardown -> rebuild cycle.
    """

    try:
        next_frame = dpg.get_frame_count() + _REFRESH_FRAME_INTERVAL
    except SystemError:
        return

    def _tick(*_args) -> None:
        if getattr(shell, "_live_verify_state", None) is not state:
            # A rebuild superseded this chain; die without stopping the worker
            # (the newer chain owns it).
            return
        if dpg.does_item_exist(LIVE_VERIFY_ROOT_TAG):
            state.root_seen = True
            try:
                _refresh_live_verify(shell, state)
            except Exception:  # noqa: BLE001 — never crash the frame loop
                logger.exception("Live-verify refresh raised")
            _schedule_live_verify_refresh(shell, state)
            return
        if state.root_seen:
            # Genuine teardown (the screen went live, the root is now gone):
            # stop the worker and end the chain.
            try:
                shell.xinput_poll_service.stop()
            except Exception:  # noqa: BLE001
                logger.exception("Live-verify: poll service stop failed")
            return
        # Root not committed yet (an early tick before the widgets are
        # queryable): keep the chain alive and try again next frame rather than
        # mistaking a not-ready state for a teardown.
        _schedule_live_verify_refresh(shell, state)

    try:
        dpg.set_frame_callback(next_frame, _tick)
    except SystemError:
        return


def _refresh_live_verify(shell, state: "_LiveVerifyState") -> None:
    snap = shell.xinput_poll_service.get_snapshot()
    # Keep the active-slot readout honest in EVERY state (live / no-pad /
    # unavailable) so it tracks auto-selection and reflects a lost pad.
    _lv_set(PLAYER_ACTIVE_TAG, _player_active_text(snap))
    if not snap.dll_available:
        _set_availability(shell, "unavailable")
        _refresh_inspector_live(shell, snap)
        return
    if not snap.connected:
        _set_availability(shell, "no_controller")
        _refresh_inspector_live(shell, snap)
        return
    _set_availability(shell, "live")
    lx, ly = snap.left_stick_normalized
    rx, ry = snap.right_stick_normalized
    _refresh_stick(shell, state, "left", lx, ly)
    _refresh_stick(shell, state, "right", rx, ry)
    _refresh_buttons(shell, snap)
    _refresh_live_face_highlights(shell, snap)
    _refresh_live_top_highlights(shell, snap)
    _refresh_selection_highlights(shell)
    _refresh_inspector_live(shell, snap)
    _refresh_triggers(shell, snap)
    _refresh_deadzone_status(shell)


def refresh_inspector_binding(shell) -> None:
    """Refresh Live Verify cached binding surfaces, if mounted."""

    _refresh_inspector_binding(shell)
    refresh_binding_overlays(shell)


def refresh_binding_overlays(shell) -> None:
    """Refresh static on-model binding badges from the cached snapshot."""

    state = getattr(shell, "_live_verify_state", None)
    if state is None:
        return
    snapshot = getattr(shell, "last_controller_snapshot", None)
    button_bindings = snapshot.button_bindings or {} if snapshot is not None else {}
    back_paddle_bindings = (
        snapshot.back_paddle_bindings or {} if snapshot is not None else {}
    )

    for target in _FRONT_BINDING_BADGE_TARGETS:
        slot = _button_slot_for_control(target)
        text = _front_binding_badge_text(
            slot, button_bindings.get(slot) if slot else None
        )
        state.front_badge_text[target] = text
        tag = _face_binding_badge_tag(target)
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, text)
            dpg.configure_item(tag, color=shell.COLORS["accent"])

    for slot in _BACK_PADDLE_SLOTS:
        binding = back_paddle_bindings.get(slot)
        text, color_role = _back_binding_badge_text(binding)
        state.back_badge_text[slot] = text
        tag = _back_binding_badge_tag(slot)
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, text)
            dpg.configure_item(tag, color=shell.COLORS[color_role])

    for label in _TOP_CONTROLS:
        if label in _TOP_LIVE_TARGETS:
            target = _TOP_LIVE_TARGETS[label]
            slot = _button_slot_for_control(target)
            text = _front_binding_badge_text(
                slot, button_bindings.get(slot) if slot else None
            )
            color_role = "accent"
        else:
            slot = _TOP_SOURCE_SLOTS[label]
            binding = back_paddle_bindings.get(slot)
            text, color_role = _back_binding_badge_text(binding)
        state.top_badge_text[label] = text
        tag = _top_binding_badge_tag(label)
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, text)
            dpg.configure_item(tag, color=shell.COLORS[color_role])

    _apply_binding_badge_visibility(shell)


def _front_binding_badge_text(
    slot: ButtonSlot | None,
    mapping,
) -> str:
    if slot is None:
        return ""
    display = format_button_binding(slot, mapping)
    if not display.remapped_tag:
        return ""
    return f"{_BINDING_BADGE_ARROW}{display.text}"


def _back_binding_badge_text(binding: BackPaddleBinding | None) -> tuple[str, str]:
    if binding is None:
        return t("controller.back_paddles.not_set_here"), "muted"
    if binding.target is None:
        return f"{_BINDING_BADGE_ARROW}{t('controller.back_paddles.unbound')}", "accent"
    return f"{_BINDING_BADGE_ARROW}{_control_label(binding.target)}", "accent"


def _set_binding_badges_visible(shell, show: bool) -> None:
    state = getattr(shell, "_live_verify_state", None)
    if state is None:
        return
    state.show_binding_badges = show
    _apply_binding_badge_visibility(shell)


def _apply_binding_badge_visibility(shell) -> None:
    state = getattr(shell, "_live_verify_state", None)
    if state is None:
        return
    show_badges = bool(getattr(state, "show_binding_badges", True))
    front_active = (
        getattr(state, "active_view", _WORKSPACE_VIEW_FRONT)
        == _WORKSPACE_VIEW_FRONT
    )
    back_active = (
        getattr(state, "active_view", _WORKSPACE_VIEW_FRONT)
        == _WORKSPACE_VIEW_BACK
    )
    top_active = (
        getattr(state, "active_view", _WORKSPACE_VIEW_FRONT)
        == _WORKSPACE_VIEW_TOP
    )
    for target in _FRONT_BINDING_BADGE_TARGETS:
        tag = _face_binding_badge_tag(target)
        if dpg.does_item_exist(tag):
            dpg.configure_item(
                tag,
                show=show_badges
                and front_active
                and bool(state.front_badge_text.get(target)),
            )
    for slot in _BACK_PADDLE_SLOTS:
        tag = _back_binding_badge_tag(slot)
        if dpg.does_item_exist(tag):
            dpg.configure_item(
                tag,
                show=show_badges
                and back_active
                and bool(state.back_badge_text.get(slot)),
            )
    for label in _TOP_CONTROLS:
        tag = _top_binding_badge_tag(label)
        if dpg.does_item_exist(tag):
            dpg.configure_item(
                tag,
                show=show_badges
                and top_active
                and bool(state.top_badge_text.get(label)),
            )


# Availability state -> (i18n key, COLORS key for the text + dot).
_AVAILABILITY_DISPLAY = {
    "unavailable": ("diagnostics.live_verify.availability.unavailable", "warn"),
    "no_controller": ("diagnostics.live_verify.availability.no_controller", "warn"),
    "live": ("diagnostics.live_verify.availability.live", "good"),
}


def _set_availability(shell, key: str) -> None:
    """Update the header status pill text + dot colour for ``key``."""

    text_key, color_key = _AVAILABILITY_DISPLAY[key]
    _lv_set(LIVE_VERIFY_AVAILABILITY_TAG, t(text_key))
    color = shell.COLORS.get(color_key, shell.COLORS["muted"])
    for tag in (LIVE_VERIFY_AVAILABILITY_TAG, LIVE_VERIFY_AVAILABILITY_DOT_TAG):
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, color=color)


def _refresh_stick(shell, state: "_LiveVerifyState", side: str, x: float, y: float) -> None:
    c, r = _PLOT_CENTER, _PLOT_RADIUS
    # 5 decimals so resting stick-drift is visible (item A), matching the
    # gamepad-tester reference's signed style.
    _lv_set(_axes_tag(side), t("diagnostics.live_verify.stick.axes", x=f"{x:+.5f}", y=f"{y:+.5f}"))
    cursor_point = [c + x * r, c - y * r]
    cursor_tag = _cursor_tag(side)
    if dpg.does_item_exist(cursor_tag):
        dpg.configure_item(cursor_tag, center=cursor_point)
    # Centre->cursor radial line (item B): tracks the stick every frame, even
    # when no test is running, so direction + magnitude read at a glance.
    radial_tag = _radial_tag(side)
    if dpg.does_item_exist(radial_tag):
        dpg.configure_item(radial_tag, p1=[c, c], p2=cursor_point)
    if not getattr(state, f"{side}_active"):
        return
    sweep = state.sweep_for(side)
    sweep.add_sample(x, y)
    env_tag = _envelope_tag(side)
    if dpg.does_item_exist(env_tag):
        dpg.configure_item(env_tag, points=_to_screen_points(sweep.envelope_points()))
    _lv_set(_coverage_tag(side), t("diagnostics.live_verify.coverage", pct=f"{sweep.coverage_pct:.0f}"))
    # Header avg-error %, updated live every tick while active (header placement),
    # not only on Stop.
    _update_header_error(shell, side, sweep)


def _refresh_buttons(shell, snap) -> None:
    for target in _BUTTON_CHIP_ORDER:
        tag = _button_chip_tag(target)
        if dpg.does_item_exist(tag):
            pressed = target in snap.buttons
            dpg.configure_item(
                tag,
                color=shell.COLORS["good"] if pressed else shell.COLORS["muted"],
            )


def _refresh_live_face_highlights(shell, snap) -> None:
    for target in _BUTTON_CHIP_ORDER:
        tag = _face_hotspot_tag(target)
        if dpg.does_item_exist(tag):
            pressed = target in snap.buttons
            color = shell.COLORS["good"] if pressed else shell.COLORS["muted"]
            dpg.configure_item(tag, color=color)

    for value, target in (
        (snap.left_trigger, ControllerButtonTarget.LT),
        (snap.right_trigger, ControllerButtonTarget.RT),
    ):
        tag = _face_hotspot_tag(target)
        if dpg.does_item_exist(tag):
            intensity = max(0.0, min(1.0, value / 255.0))
            lit = intensity > _FACE_TRIGGER_LIGHT_THRESHOLD
            color = shell.COLORS["good"] if lit else shell.COLORS["muted"]
            if lit:
                scaled = (intensity - _FACE_TRIGGER_LIGHT_THRESHOLD) / (
                    1.0 - _FACE_TRIGGER_LIGHT_THRESHOLD
                )
                fill_alpha = int(28 + 190 * max(0.0, min(1.0, scaled)))
            else:
                fill_alpha = 28
            dpg.configure_item(
                tag,
                color=color,
                fill=_with_alpha(color, fill_alpha),
            )

    for side, (x, y) in (
        ("left", snap.left_stick_normalized),
        ("right", snap.right_stick_normalized),
    ):
        tag = _face_stick_dot_tag(side)
        if dpg.does_item_exist(tag):
            cx, cy = _FACE_STICK_CENTERS[side]
            mag = math.hypot(x, y)
            lit = mag > _FACE_STICK_LIGHT_DEADZONE
            color = shell.COLORS["good"] if lit else shell.COLORS["muted"]
            center = (
                [
                    cx + x * _FACE_STICK_DOT_TRAVEL,
                    cy - y * _FACE_STICK_DOT_TRAVEL,
                ]
                if lit
                else [cx, cy]
            )
            dpg.configure_item(
                tag,
                color=color,
                fill=color,
                center=center,
            )


def _refresh_live_top_highlights(shell, snap) -> None:
    for label, target in _TOP_LIVE_TARGETS.items():
        tag = _top_hotspot_tag(label)
        if not dpg.does_item_exist(tag):
            continue
        if target is ControllerButtonTarget.LT:
            value = snap.left_trigger
            intensity = max(0.0, min(1.0, value / 255.0))
            lit = intensity > _FACE_TRIGGER_LIGHT_THRESHOLD
        elif target is ControllerButtonTarget.RT:
            value = snap.right_trigger
            intensity = max(0.0, min(1.0, value / 255.0))
            lit = intensity > _FACE_TRIGGER_LIGHT_THRESHOLD
        else:
            intensity = 1.0 if target in snap.buttons else 0.0
            lit = target in snap.buttons
        color = shell.COLORS["good"] if lit else shell.COLORS["muted"]
        fill_alpha = 32
        if lit:
            fill_alpha = 72 if intensity <= 0.0 else int(52 + 128 * intensity)
        dpg.configure_item(tag, color=color, fill=_with_alpha(color, fill_alpha))


def _refresh_triggers(shell, snap) -> None:
    for value, bar_tag in (
        (snap.left_trigger, TRIGGER_LEFT_BAR_TAG),
        (snap.right_trigger, TRIGGER_RIGHT_BAR_TAG),
    ):
        if dpg.does_item_exist(bar_tag):
            dpg.set_value(bar_tag, max(0.0, min(1.0, value / 255.0)))
            dpg.configure_item(bar_tag, overlay=str(int(value)))


def _refresh_deadzone_status(shell) -> None:
    if not dpg.does_item_exist(DEADZONE_STATUS_TAG):
        return
    key = getattr(shell, "_diag_deadzone_status_key", "idle")
    text, color = _deadzone_status_display(shell, key)
    dpg.set_value(DEADZONE_STATUS_TAG, text)
    dpg.configure_item(DEADZONE_STATUS_TAG, color=color)


_DEADZONE_STATUS_DISPLAY = {
    "idle": ("diagnostics.live_verify.deadzone.status.idle", "muted"),
    "sending": ("diagnostics.live_verify.deadzone.status.sending", "warn"),
    "verifying": ("diagnostics.live_verify.deadzone.status.verifying", "warn"),
    "verified": ("diagnostics.live_verify.deadzone.status.verified", "good"),
    "mismatch": ("diagnostics.live_verify.deadzone.status.mismatch", "warn"),
    # The write landed; the confirm read just couldn't be answered in time.
    # Advisory (warn), never an error (bad) — the value IS on the controller.
    "sent_unverified": ("diagnostics.live_verify.deadzone.status.sent_unverified", "warn"),
    "failed": ("diagnostics.live_verify.deadzone.status.failed", "bad"),
    "unavailable": ("diagnostics.live_verify.deadzone.status.unavailable", "warn"),
    # Non-ZD / unverified controller: the deadzone write surface is read-only.
    # Pinned at build and never overwritten (the write path is refused before it
    # can set another key), so the live tick keeps showing the honest line.
    "unverified_device": (
        "diagnostics.live_verify.deadzone.status.unverified_device",
        "warn",
    ),
}


def _deadzone_status_display(shell, key: str):
    key_name, color_key = _DEADZONE_STATUS_DISPLAY.get(
        key, _DEADZONE_STATUS_DISPLAY["idle"]
    )
    return t(key_name), shell.COLORS.get(color_key, shell.COLORS["muted"])


def _update_header_error(shell, side: str, sweep: CircularitySweep) -> None:
    """Update the stick card's header avg-error % (header placement), live.

    Reads "--%" until the sweep has samples; once sampling, shows the avg error
    muted while the sweep is provisional (coverage below the gate) and in the
    accent ``good`` colour once coverage is firm — the gated-while-spinning
    behaviour the old centred-in-plot number carried, now in the header row
    where the shaded sweep can't obscure it.
    """

    tag = _header_error_tag(side)
    if not dpg.does_item_exist(tag):
        return
    if sweep.sample_count <= 0:
        _lv_set(tag, _HEADER_PCT_IDLE)
        dpg.configure_item(tag, color=shell.COLORS["muted"])
        return
    firm = sweep.coverage_pct >= _COVERAGE_GATE_PCT
    _lv_set(tag, f"{sweep.avg_error_pct:.1f}%")
    dpg.configure_item(tag, color=shell.COLORS["good"] if firm else shell.COLORS["muted"])


def _to_screen_points(unit_points):
    """Map unit-space envelope vertices (y up) to drawlist screen coords."""

    c, r = _PLOT_CENTER, _PLOT_RADIUS
    return [[c + x * r, c - y * r] for (x, y) in unit_points]


def _lv_set(tag: str, value) -> None:
    if dpg.does_item_exist(tag):
        dpg.set_value(tag, value)


__all__ = ["build"]
