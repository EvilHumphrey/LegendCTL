"""Live Verify — dedicated gamepad-tester screen.

A live gamepad-tester surface: per-stick axes + a circularity sweep, live
button chips, live trigger bars, and inline FIRMWARE deadzone tuning you can
drag and watch the circularity envelope change in real time. Promoted out of a
Diagnostics section into its own nav screen so the whole surface fits one
window without the cramped, hidden nested-scroll trap the in-Diagnostics
version had.

Reads controller input only (circularity, settings_service via the shell, the
shell's xinput_poll_service); apart from the inline firmware-deadzone sliders it
never writes to the controller.

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

import dearpygui.dearpygui as dpg

from zd_app.i18n import t
from zd_app.services.circularity import CircularitySweep
from zd_app.services.settings_service import ControllerButtonTarget, StickDeadzones
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
        with dpg.group(horizontal=True):
            _build_stick_block(shell, state, "left")
            dpg.add_spacer(width=_STICK_CARD_GAP)
            _build_stick_block(shell, state, "right")
        dpg.add_text(
            t("diagnostics.live_verify.circularity_help"),
            color=shell.COLORS["muted"],
            wrap=_SCREEN_WRAP,
        )
        # Tightened spacers (layout fit) so the whole screen fits the default
        # window with little or no governing scroll.
        dpg.add_spacer(height=8)
        _build_buttons_triggers_card(shell)
        dpg.add_spacer(height=8)
        _build_inline_deadzone_card(shell)

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


def _device_write_supported(shell) -> bool:
    """True when the connected controller is an allowlisted ZD Ultimate Legend."""

    state = getattr(getattr(shell, "device_service", None), "state", None)
    return bool(getattr(state, "write_supported", True))


def _build_inline_deadzone_card(shell) -> None:
    """Inline firmware-deadzone tuning card.

    On an allowlisted ZD these sliders write the REAL controller
    ``StickDeadzones`` (debounced + read-back-verified via the shell). On any
    other XInput controller the live tester still runs, but this write surface is
    read-only and never issues a firmware deadzone read or write.

    _fit_card() (auto_resize_y) fits its content so it never overflows / grows its
    own scrollbar (layout fit); a fitted card has no scroll range, so any wheel
    passes through to the screen's single governing scroll. (The shared card()'s
    legacy autosize_y would FILL instead on dearpygui 2.x — see _fit_card.)
    """

    write_supported = _device_write_supported(shell)
    current = _read_current_deadzones(shell) if write_supported else None
    hydrated = current is not None
    shell._diag_deadzone_hydrated = hydrated
    defaults = current if current is not None else StickDeadzones(0, 0, 0, 0)
    if write_supported:
        status_key = (
            "diagnostics.live_verify.deadzone.status.idle"
            if hydrated
            else "diagnostics.live_verify.deadzone.status.unavailable"
        )
        note_key = "diagnostics.live_verify.deadzone.note"
        shell._diag_deadzone_status_key = "idle" if hydrated else "unavailable"
    else:
        status_key = "diagnostics.live_verify.deadzone.status.unverified_device"
        note_key = "diagnostics.live_verify.deadzone.note_unverified"
        shell._diag_deadzone_status_key = "unverified_device"

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
        return
    if not snap.connected:
        _set_availability(shell, "no_controller")
        return
    _set_availability(shell, "live")
    lx, ly = snap.left_stick_normalized
    rx, ry = snap.right_stick_normalized
    _refresh_stick(shell, state, "left", lx, ly)
    _refresh_stick(shell, state, "right", rx, ry)
    _refresh_buttons(shell, snap)
    _refresh_triggers(shell, snap)
    _refresh_deadzone_status(shell)


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
