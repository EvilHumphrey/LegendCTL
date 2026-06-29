"""Tabbed R2 controller settings screen."""

from __future__ import annotations

import dearpygui.dearpygui as dpg

from zd_app.i18n import get_locale, t
from zd_app.ui import safe_import_badges
from zd_app.ui.fonts import font_for
from zd_app.ui.themes import SPACE_MD, SPACE_LG, SPACE_XL
from zd_app.ui.components import Column, action_button, table, table_empty_state
from zd_app.ui.typography import helper_text, screen_title, section_title
from zd_app.services.settings_service import (
    ButtonSlot,
    ControllerButtonTarget,
    MacroSlot,
    MotionMappingMode,
    MotionMappingTarget,
    STEP_SIZE_VALUE_DEFAULT,
    STEP_SIZE_VALUE_MAX,
    STEP_SIZE_VALUE_MIN,
)


POLLING_RATE_ITEMS = ["250Hz", "500Hz", "1000Hz", "2000Hz", "4000Hz", "8000Hz"]
# Safe placeholder for the polling-rate combo BEFORE a real device read hydrates
# it (and the fallback whenever the device's current rate is unknown). 1000 Hz is
# supported on every firmware revision; 8000 Hz requires controller fw v1.18+, so
# it must never be the value we silently nudge an as-yet-unread device toward.
# Once a read lands, _hydrate_polling_rate replaces this with the device's actual
# rate (see app_shell.AppShell._hydrate_polling_rate).
POLLING_RATE_DEFAULT_LABEL = "1000Hz"
VIBRATION_MODE_ITEMS = ["Native Trigger Vibration", "Stereo Resonance", "Trigger Vibration"]
TRIGGER_MODE_ITEMS = ["Short", "Long"]
BUTTON_SLOT_ITEMS = [
    "Up", "Right", "Down", "Left",
    "A", "B", "X", "Y",
    "LB", "RB", "LT", "RT",
    "Back", "Start", "LS", "RS",
]
BUTTON_TARGET_ITEMS = [
    "LS", "RS",
    "Up", "Right", "Down", "Left",
    "A", "B", "X", "Y",
    "LB", "RB", "LT", "RT",
    "Back", "Start",
]
BACK_PADDLE_TARGET_ORDER = (
    ControllerButtonTarget.A,
    ControllerButtonTarget.B,
    ControllerButtonTarget.X,
    ControllerButtonTarget.Y,
    ControllerButtonTarget.LB,
    ControllerButtonTarget.RB,
    ControllerButtonTarget.LT,
    ControllerButtonTarget.RT,
    ControllerButtonTarget.BACK,
    ControllerButtonTarget.START,
    ControllerButtonTarget.LS,
    ControllerButtonTarget.RS,
    ControllerButtonTarget.UP,
    ControllerButtonTarget.DOWN,
    ControllerButtonTarget.LEFT,
    ControllerButtonTarget.RIGHT,
)
LIGHTING_ZONE_ITEMS = ["Home", "Left", "Right"]
LIGHTING_MODE_ITEMS = ["Off", "Always On", "Breath", "Fade", "Flow"]
SENSITIVITY_PRESETS = ["Default", "Instant", "Balanced", "Delayed", "High Performance"]
# 8-point editor preset button order. The curve data (name → 8 anchors) lives in
# ``app_shell.SENSITIVITY_PRESETS_8POINT``; these are just the button order +
# i18n-label stems (``controller.sticks.preset_8point.<name>``).
SENSITIVITY_PRESETS_8POINT = ["Linear", "Aggressive", "Smooth", "Balanced"]
CONTROLLER_TAB_IDS = ("vibration", "triggers", "sticks", "buttons", "lighting", "motion", "profiles")

# Quarter-point ticks for the 0-100 sensitivity plot axes. Labelled at 0/25/50/
# 75/100 so the gridlines read as readable quarters of the input/output range
# (DPG's auto-ticks land on 0/20/40/.../100, which crowd the grid). Pairs are
# (label, value) per dpg.set_axis_ticks.
_PLOT_AXIS_TICKS = (("0", 0.0), ("25", 25.0), ("50", 50.0), ("75", 75.0), ("100", 100.0))

# Pad the view a few % past 0-100 so the (0,0)/(100,100) endpoint dots aren't
# clipped; ticks stay 0/25/50/75/100 (gridline labels are unaffected).
_PLOT_AXIS_VIEW_PAD = 4.0

# Drag-handle affordance (UI Phase: Sticks precision pass). DPG 2.2 fixes the
# drag-point grab radius at 4.0 px (not settable via the constructor or
# configure_item), so two levers make the anchors read as grabbable: a heavier
# point ring (``thickness``) on each drag point, and a larger scatter MARKER
# under it via a plot-bound theme (see ``_sensitivity_handle_theme``). The
# anchors stay accent-coloured — green is the app's reserved "draggable/active"
# colour — so the size bump reinforces an already-correct colour cue.
_DRAG_HANDLE_THICKNESS = 3.0
_DRAG_HANDLE_MARKER_SIZE = 6.0


# ---------------------------------------------------------------------------
# Controller back-view diagram (Buttons tab). A small, code-drawn map of the
# six BACK-side macro slots so a user can SEE which physical paddle is "M1" vs
# "M2", etc. — the slot names alone aren't intuitive. Pure UI: NO device I/O,
# no snapshot dependency. The highlight reflects the back-paddle ROW the user
# is touching, NEVER a device read (LegendCTL can't read the device's real
# paddle state — cf. the section's cannot_read note + honest-abstention work).
# Drawlist precedent: about.py (radar mark) + live_verify.py (circularity plot,
# the tag + configure_item live-recolor model this reuses).
# ---------------------------------------------------------------------------

# Compact canvas; the back view is wider-than-tall. Pixel literals — no hi-dpi
# scaling exists anywhere in this codebase, so match that (known limitation).
_DIAGRAM_W = 300
_DIAGRAM_H = 220
_PADDLE_R = 14  # hotspot radius
_DIAGRAM_LABEL_SIZE = 13  # small enough to sit inside a _PADDLE_R hotspot

# Back-view physical positions, MIRRORED — drawn as you see the controller when
# you flip it over to look at its back (matches the official ZD manual's
# back-view BUTTON LAYOUT): diagram image-LEFT = player's RIGHT. (x, y) in
# drawlist-local px, origin top-left. CONFIRMED 2026-06-27 from official ZD
# sources (manual + website images); see _spec_controller_diagram_2026-06-27 §6.
#   Player's LEFT grip  = LM (upper paddle), M1 (lower rear button), LK (top claw)
#   Player's RIGHT grip = RM (upper paddle), M2 (lower rear button), RK (top claw)
# The verified, counter-intuitive split is M1 = LEFT-grip lower and M2 =
# RIGHT-grip lower — that is CORRECT and intentional; do not "fix" it.
# Only these six MacroSlots are on the back; M3/M4 are FRONT (beside the sticks)
# and are deliberately NOT drawn here (a short note points to them instead).
_BACK_PADDLE_POS: dict[MacroSlot, tuple[int, int]] = {
    MacroSlot.RK: (95, 38),    # right top-edge claw   (image-left)
    MacroSlot.LK: (205, 38),   # left  top-edge claw   (image-right)
    MacroSlot.RM: (122, 102),  # right upper paddle    (central-left)
    MacroSlot.LM: (178, 102),  # left  upper paddle    (central-right)
    MacroSlot.M2: (120, 144),  # right lower rear button (below RM)
    MacroSlot.M1: (180, 144),  # left  lower rear button (below LM)
}

# Slots whose DRAWN position is the most approximate, so they carry a visible
# "*" marker + the approx caption (honest §7). The two top-edge CLAWS sit on a
# different physical plane (the top edge, by LB/RB) than the flat rear face, so
# projecting them onto a flat back-view canvas is the one genuinely schematic
# placement; the four rear-face slots (LM/RM/M1/M2) map directly. Empty would
# simply hide the approx caption (the honest framing note stays unconditional).
_BACK_PADDLE_APPROX: frozenset[MacroSlot] = frozenset({MacroSlot.LK, MacroSlot.RK})


def _on_drag_point_edit(shell, side: str, anchor: int, sender) -> None:
    """Funnel a draggable curve-point move into the shared 8-point edit path.

    A plot drag point reports a single ``(x, y)``; we route each axis through
    ``shell._on_sensitivity_8point_edit`` — the same funnel the coarse slider and
    the exact-entry input use. That funnel clamps 0-100, runs the monotonic
    auto-assist, mirrors the value into the slider + input twins, and repaints the
    plot, which in turn repositions every drag point from the now-canonical
    sliders (see ``app_shell._set_sensitivity_8point_plot_points``). So a drag,
    a typed value, and a slider all converge on one source of truth.

    Reading ``get_value(sender)`` (rather than the callback's ``app_data``) keeps
    this robust to DPG's drag-point app_data shape and to the test harness, which
    sets the point's value before dispatching the callback.

    After the edit lands we refresh the side's point-value callout (#3) to the
    anchor's now-canonical value so the exact (input, output) of the point being
    dragged is surfaced live.
    """

    if sender is None:
        return
    value = dpg.get_value(sender)
    if not value:
        return
    x = value[0]
    y = value[1] if len(value) > 1 else value[0]
    shell._on_sensitivity_8point_edit(side, anchor, "x", x)
    shell._on_sensitivity_8point_edit(side, anchor, "y", y)
    _update_drag_readout(shell, side, anchor)


def _format_drag_readout(anchor: int, x, y) -> str:
    """Point-value callout string for an anchor edit — e.g. ``A3: Input 35%,
    Output 42%``. Kept tiny + side-effect-free so the format is unit-testable
    independently of a DPG context."""

    return t(
        "controller.sticks.plot_8point.readout",
        anchor=anchor,
        input=int(x),
        output=int(y),
    )


def _update_drag_readout(shell, side: str, anchor: int) -> None:
    """Refresh a side's point-value callout to the just-edited anchor's canonical
    (post-sync) value.

    Reads the canonical sliders rather than the raw drag position so the callout
    reflects the monotonic auto-assist (a drag that pushes neighbours shows the
    value the curve actually settled on). No-ops when the callout or the sliders
    aren't on screen (3-point editor / no DPG context)."""

    readout_tag = f"sensitivity_{side}_drag_readout_8point"
    x_tag = f"sensitivity_{side}_a{anchor}x_slider_8point"
    y_tag = f"sensitivity_{side}_a{anchor}y_slider_8point"
    if not dpg.does_item_exist(readout_tag):
        return
    if not (dpg.does_item_exist(x_tag) and dpg.does_item_exist(y_tag)):
        return
    dpg.set_value(
        readout_tag,
        _format_drag_readout(anchor, dpg.get_value(x_tag), dpg.get_value(y_tag)),
    )


def _on_reset_sensitivity_8point_linear(shell, side: str) -> None:
    """Reset affordance #1: snap a side's 8-point curve to the linear identity
    (y=x).

    Routes through the EXISTING preset apply path — identical to clicking the
    bundled "Linear" preset, just surfaced as a clearly-labelled reset adjacent to
    the plot. Applies immediately, matching the preset buttons (the screen's
    established "preset = apply now" behaviour). No new service path."""

    callback = (
        shell.apply_left_sensitivity_preset_8point
        if side == "left"
        else shell.apply_right_sensitivity_preset_8point
    )
    callback("Linear")


def _on_reset_sensitivity_8point_defaults(shell, side: str) -> None:
    """Reset affordance #2: revert a side's 8-point curve to the controller's
    last-read values.

    Distinct from the linear reset: this discards any unapplied editor edits and
    puts the curve back to what the device reported at connect (the snapshot),
    then applies it — a "put it back the way it was" action. Loads the anchors via
    the same shell helper the hydrate + preset paths use, then dispatches the
    normal apply-curve callback; no new service path. Falls back to the linear
    reset when no snapshot curve is available for that side (defensive — the
    8-point editor only renders when that side's snapshot field is present)."""

    snapshot = getattr(shell, "last_controller_snapshot", None)
    anchors = (
        getattr(snapshot, f"sensitivity_{side}_8point", None)
        if snapshot is not None
        else None
    )
    if not anchors:
        _on_reset_sensitivity_8point_linear(shell, side)
        return
    shell._set_sensitivity_8point_anchor_widgets(side, anchors)
    apply_callback = (
        shell.apply_left_sensitivity_curve_8point
        if side == "left"
        else shell.apply_right_sensitivity_curve_8point
    )
    apply_callback()


def _sensitivity_handle_theme(shell):
    """Lazily build (once) and return the plot theme that enlarges the scatter
    anchor markers so the curve points read as grabbable handles.

    Sets only the marker size (``mvPlotStyleVar_MarkerSize``) and is bound to the
    PLOT, so it cascades to the scatter series for size while leaving the accent
    fill/outline that app_shell binds to the series itself untouched (DPG merges
    theme style-vars per-var across the parent chain). DPG 2.2 fixes the
    drag-point grab radius, so the markers underneath are the lever for a bigger
    visible handle. Cached on the shell (mirrors how ``_sensitivity_plot_themes``
    is cached there) and rebuilt when the cached item is stale — DPG contexts are
    created/destroyed per test, which invalidates the id."""

    cached = getattr(shell, "_sensitivity_handle_theme_id", None)
    if cached is not None and dpg.does_item_exist(cached):
        return cached
    with dpg.theme() as theme_id:
        with dpg.theme_component(dpg.mvScatterSeries):
            dpg.add_theme_style(
                dpg.mvPlotStyleVar_MarkerSize,
                _DRAG_HANDLE_MARKER_SIZE,
                category=dpg.mvThemeCat_Plots,
            )
    shell._sensitivity_handle_theme_id = theme_id
    return theme_id


def build(shell, parent: str) -> None:
    with dpg.child_window(parent=parent, autosize_x=True, autosize_y=True, border=False):
        render(shell)


def render(shell) -> None:
    if shell.settings_service is None:
        dpg.add_text(t("controller.no_service"), color=shell.COLORS["warn"], wrap=600)
        dpg.add_text("", tag="settings_v2_status_text", color=shell.COLORS["muted"], wrap=600)
        return

    screen_title(t("controller.title"))
    helper_text(t("controller.subtitle"), wrap=720)
    _render_unverified_device_banner(shell)
    dpg.add_spacer(height=8)
    dpg.add_text("", tag="settings_v2_status_text", color=shell.COLORS["muted"], wrap=720)
    dpg.add_spacer(height=8)

    with dpg.tab_bar(tag="controller_tab_bar", callback=lambda _s, selected_tab, _u: _remember_active_tab(shell, selected_tab)):
        with dpg.tab(label=t("controller.tab.vibration"), tag="tab_vibration"):
            _render_vibration_tab(shell)
        with dpg.tab(label=t("controller.tab.triggers"), tag="tab_triggers"):
            _render_triggers_tab(shell)
        with dpg.tab(label=t("controller.tab.sticks"), tag="tab_sticks"):
            _render_sticks_tab(shell)
        with dpg.tab(label=t("controller.tab.buttons"), tag="tab_buttons"):
            _render_buttons_tab(shell)
        with dpg.tab(label=t("controller.tab.lighting"), tag="tab_lighting"):
            _render_lighting_tab(shell)
        with dpg.tab(label=t("controller.tab.motion"), tag="tab_motion"):
            _render_motion_tab(shell)
        with dpg.tab(label=t("controller.tab.profiles"), tag="tab_profiles"):
            _render_profiles_tab(shell)
    active_tab_tag = _tab_id_to_tag(getattr(shell, "controller_active_tab", "vibration"))
    if dpg.does_item_exist(active_tab_tag):
        dpg.set_value("controller_tab_bar", active_tab_tag)


def _render_unverified_device_banner(shell) -> None:
    """Warn banner shown when the connected controller isn't an allowlisted ZD
    Ultimate Legend.

    Every setting on this screen is a HID write gated to the verified ZD, so on
    any other XInput pad they are unverified and refuse to write. The banner says
    so plainly and points the user at the model-agnostic Live Verify tester.
    Built unconditionally with a stable tag (``show`` toggled) so device-state
    changes can flip its visibility, and so tests can assert it. Defaults to
    hidden for stub shells without a device state.
    """

    state = getattr(getattr(shell, "device_service", None), "state", None)
    write_supported = bool(getattr(state, "write_supported", True))
    dpg.add_text(
        t("controller.unverified_device.banner"),
        tag="controller_unverified_device_banner",
        color=shell.COLORS["warn"],
        wrap=720,
        show=not write_supported,
    )


def _render_loading_hint(shell) -> None:
    if shell.last_controller_snapshot is not None:
        return
    # Label + skeleton rectangle. Fit to content (DPG-2.x auto_resize_y, with
    # the legacy fill flag suppressed) instead of a hand-measured fixed height:
    # the prior 80 still clipped the skeleton by 6px at the shipped fonts
    # (tools/diag_dpg_card_clip.py). Fit can't clip and needs no magic number.
    with dpg.child_window(border=True, auto_resize_y=True, autosize_y=False):
        dpg.add_text(t("controller.loading"), color=shell.COLORS["muted"])
        _draw_skeleton()


def _render_vibration_tab(shell) -> None:
    _render_loading_hint(shell)
    dpg.add_combo(
        items=POLLING_RATE_ITEMS,
        default_value=POLLING_RATE_DEFAULT_LABEL,
        label=t("controller.polling_rate.label"),
        width=220,
        tag="usb_polling_rate_combo",
        enabled=False,
        callback=lambda _s, value: shell.apply_polling_rate(value),
    )
    dpg.add_text(
        t("controller.polling_rate.unread"),
        tag="usb_polling_rate_unread_hint",
        color=shell.COLORS["warn"],
        wrap=520,
        show=False,
    )
    dpg.add_text(t("controller.polling_rate.helper"), color=shell.COLORS["muted"], wrap=520)
    dpg.add_spacer(height=16)
    section_title(t("controller.vibration.title"))
    dpg.add_text(t("controller.vibration.helper"), color=shell.COLORS["muted"])
    for tag, label_key in (
        ("vibration_lg_slider", "controller.vibration.left_grip"),
        ("vibration_rg_slider", "controller.vibration.right_grip"),
        ("vibration_lm_slider", "controller.vibration.left_trigger"),
        ("vibration_rm_slider", "controller.vibration.right_trigger"),
    ):
        dpg.add_slider_int(label=t(label_key), default_value=15, min_value=0, max_value=100, width=320, tag=tag)
    dpg.add_combo(
        items=VIBRATION_MODE_ITEMS,
        default_value="Native Trigger Vibration",
        label=t("controller.vibration.mode"),
        width=320,
        tag="vibration_mode_combo",
    )
    dpg.add_button(
        label=t("controller.vibration.apply"),
        width=220,
        callback=lambda: shell.apply_vibration_settings(),
    )
    dpg.add_text(t("controller.vibration.apply_note"), color=shell.COLORS["muted"], wrap=600)


def _render_triggers_tab(shell) -> None:
    _render_loading_hint(shell)
    section_title(t("controller.triggers.title"))
    dpg.add_text(t("controller.triggers.helper"), color=shell.COLORS["muted"])
    _render_trigger_side(shell, "left")
    dpg.add_spacer(height=12)
    _render_trigger_side(shell, "right")
    dpg.add_text(t("controller.triggers.note"), color=shell.COLORS["muted"], wrap=600)


def _render_trigger_side(shell, side: str) -> None:
    label_prefix = f"controller.triggers.{side}"
    tag_prefix = f"trigger_{side}"
    dpg.add_text(t(f"{label_prefix}.title"), color=shell.COLORS["muted"])
    dpg.add_slider_int(label=t(f"{label_prefix}.min"), default_value=0, min_value=0, max_value=100, width=320, tag=f"{tag_prefix}_min_slider")
    dpg.add_slider_int(label=t(f"{label_prefix}.max"), default_value=100, min_value=0, max_value=100, width=320, tag=f"{tag_prefix}_max_slider")
    dpg.add_combo(items=TRIGGER_MODE_ITEMS, default_value="Short", label=t(f"{label_prefix}.mode"), width=320, tag=f"{tag_prefix}_mode_combo")
    if side == "left":
        callback = shell.apply_left_trigger_settings
        apply_key = "controller.triggers.left.apply"
    else:
        callback = shell.apply_right_trigger_settings
        apply_key = "controller.triggers.right.apply"
    dpg.add_button(label=t(apply_key), width=220, callback=lambda: callback())


def _render_sticks_tab(shell) -> None:
    _render_loading_hint(shell)
    dpg.add_slider_int(
        label=t("controller.sticks.step_size.label"),
        default_value=STEP_SIZE_VALUE_DEFAULT,
        min_value=STEP_SIZE_VALUE_MIN,
        max_value=STEP_SIZE_VALUE_MAX,
        width=320,
        tag="step_size_slider",
        enabled=False,
        callback=lambda _s, value: shell.apply_step_size(value),
    )
    dpg.add_text(
        t("controller.sticks.step_size.unread"),
        tag="step_size_unread_hint",
        color=shell.COLORS["warn"],
        wrap=520,
        show=False,
    )
    dpg.add_text(t("controller.sticks.step_size.helper"), color=shell.COLORS["muted"], wrap=520)
    # Fix B (2026-06-24): dismissible nudge to persist a manually-changed
    # step_size into the active profile ("73 is one click from sticking").
    # Built hidden every screen rebuild; the shell re-syncs visibility + the
    # button label from its pending-save state via _render_step_size_save_nudge,
    # which only shows it after a live write diverges from the active profile's
    # stored step_size. Tags mirror app_shell.STEP_SIZE_SAVE_NUDGE_* (kept as
    # literals here to avoid a screen->app_shell import cycle).
    with dpg.group(tag="step_size_save_nudge_group", show=False):
        with dpg.group(horizontal=True):
            dpg.add_button(
                label=t("controller.sticks.step_size.save_to_profile", value=0, name=""),
                tag="step_size_save_nudge_button",
                callback=lambda: shell.save_step_size_to_active_profile(),
            )
            dpg.add_button(
                label=t("controller.sticks.step_size.save_to_profile_dismiss"),
                tag="step_size_save_nudge_dismiss",
                callback=lambda: shell.dismiss_step_size_save_nudge(),
            )
        dpg.add_text(
            t("controller.sticks.step_size.save_to_profile_hint"),
            color=shell.COLORS["muted"],
            wrap=520,
        )
    shell._render_step_size_save_nudge()
    dpg.add_spacer(height=16)
    section_title(t("controller.sticks.deadzones"))
    for tag, label_key in (
        ("deadzone_left_center_slider", "controller.sticks.left_center"),
        ("deadzone_right_center_slider", "controller.sticks.right_center"),
        ("deadzone_left_outer_slider", "controller.sticks.left_outer"),
        ("deadzone_right_outer_slider", "controller.sticks.right_outer"),
    ):
        dpg.add_slider_int(label=t(label_key), default_value=0, min_value=0, max_value=100, width=320, tag=tag)
    dpg.add_button(label=t("controller.sticks.apply_deadzones"), width=220, callback=lambda: shell.apply_deadzone_settings())
    dpg.add_text(t("controller.sticks.deadzones_note"), color=shell.COLORS["muted"], wrap=640)
    dpg.add_spacer(height=SPACE_LG)
    # Sensitivity curves are the screen's centerpiece: a clearly-titled section
    # with the left and right editors laid out side by side (two columns) so both
    # curves are visible and editable at once, filling the horizontal space the
    # old stacked layout left empty. Each side's editor — the 8-point curve graph
    # (with draggable points) on capable devices, or the legacy 3-point editor —
    # builds into its own column group.
    section_title(t("controller.sticks.sensitivity_curves"))
    helper_text(t("controller.sticks.drag_hint"), wrap=900)
    dpg.add_spacer(height=SPACE_MD)
    with dpg.group(horizontal=True):
        with dpg.group():
            _render_sensitivity_side(shell, "left")
        dpg.add_spacer(width=SPACE_XL)
        with dpg.group():
            _render_sensitivity_side(shell, "right")
    # Swap the explanatory note to the 8-point wording when either side rendered
    # the 8-point editor (the 3-point note's "Anchor 1..3" language is wrong for a
    # capable device). In practice the cat-0x86 probe is device-wide, so both
    # sides flip together; the OR is just defensive against a mixed snapshot.
    snapshot = shell.last_controller_snapshot
    sensitivity_is_8point = snapshot is not None and (
        getattr(snapshot, "sensitivity_left_8point", None) is not None
        or getattr(snapshot, "sensitivity_right_8point", None) is not None
    )
    note_key = (
        "controller.sticks.sensitivity_8point_note"
        if sensitivity_is_8point
        else "controller.sticks.sensitivity_note"
    )
    dpg.add_text(t(note_key), color=shell.COLORS["muted"], wrap=680)
    dpg.add_spacer(height=SPACE_LG)
    _render_axis_inversion(shell)


def _render_sensitivity_side(shell, side: str) -> None:
    # Capability is gated by snapshot presence (set spec, choice #2): the 8-point
    # (cat 0x86) fields are populated by SettingsService.get_all_settings only on
    # devices that pass the probe. When present, show ONLY the 8-point editor
    # (choice #1: replace, don't supplement — matches the apply dispatch, which
    # writes 0x86 not 0x06 on capable devices). Legacy devices fall through to the
    # unchanged 3-point editor below.
    snapshot = shell.last_controller_snapshot
    if snapshot is not None and getattr(snapshot, f"sensitivity_{side}_8point", None) is not None:
        _render_sensitivity_side_8point(shell, side)
        return
    title_key = f"controller.sticks.{side}"
    tag_prefix = f"sensitivity_{side}_"
    # Column header for the side-by-side layout (was a muted text line; promoted
    # to an h2 so each side reads as its own clearly-labelled column).
    section_title(t(title_key))
    defaults = [(1, "x", 0), (1, "y", 0), (2, "x", 50), (2, "y", 50), (3, "x", 100), (3, "y", 100)]
    for anchor, axis, default in defaults:
        dpg.add_slider_int(
            label=t(f"controller.sticks.anchor_{anchor}_{axis}", side=t(title_key)),
            default_value=default,
            min_value=0,
            max_value=100,
            width=320,
            tag=f"{tag_prefix}a{anchor}{axis}_slider",
        )
    dpg.add_text(t("controller.sticks.presets"), color=shell.COLORS["muted"])
    with dpg.group(horizontal=True):
        for preset in SENSITIVITY_PRESETS:
            label = t(f"controller.sticks.preset.{preset.lower().replace(' ', '_')}")
            callback = shell.apply_left_sensitivity_preset if side == "left" else shell.apply_right_sensitivity_preset
            dpg.add_button(label=label, width=92, callback=lambda *args, p=preset, cb=callback: cb(p))
    callback = shell.apply_left_sensitivity_curve if side == "left" else shell.apply_right_sensitivity_curve
    apply_key = f"controller.sticks.{side}_apply_sensitivity"
    dpg.add_button(label=t(apply_key), width=230, callback=lambda: callback())


def _render_sensitivity_side_8point(shell, side: str) -> None:
    # 8-point (cat 0x86) variant. The curve graph is the centerpiece: a live
    # input→output plot whose 8 anchors are DIRECTLY DRAGGABLE (one DPG drag point
    # per anchor), backed by an "Exact values" panel of 16 input+slider twins
    # (a1x/a1y..a8x/a8y), a preset row, and the 8-pt apply button. Tags carry the
    # ``_8point`` suffix so they never collide with the 3-point tags — both widget
    # sets may briefly co-exist if the user reconnects between a legacy and a
    # capable device before a rebuild. No client-side monotonic clamp at apply:
    # the service layer's _validate_sensitivity_anchors_8point enforces each X/Y >=
    # the previous anchor's and raises; the apply path surfaces that as a failed
    # log entry (the bundled presets are pre-validated monotonic, so they never
    # trip it). The drag / slider / input editors all funnel through the same
    # shell._on_sensitivity_8point_edit, which runs the monotonic auto-assist so a
    # user can't *build* an invalid curve.
    title_key = f"controller.sticks.{side}"
    tag_prefix = f"sensitivity_{side}_"
    # Column header (promoted from a muted text line to an h2) so each side reads
    # as its own labelled column in the side-by-side layout.
    section_title(t(title_key))

    # Live curve plot. X = stick input (0-100), Y = mapped output (0-100); a faint
    # static diagonal marks y=x so the user sees how far the curve bends away from
    # linear (mirrors the vendor app's sensitivity graph). The line + scatter
    # series are repainted from the live slider values on every edit by
    # shell._refresh_sensitivity_8point_plot (which owns the slider→series read).
    # Series themes are bound when available; the bind is skipped (default colors)
    # under render paths that never ran _setup_theme.
    plot_themes = getattr(shell, "_sensitivity_plot_themes", None)
    with dpg.plot(
        # The hero of the screen: enlarged so the curve reads as clearly as the
        # vendor app's editor and two sit side by side within the content area.
        tag=f"{tag_prefix}plot_8point",
        width=420,
        height=360,
        no_mouse_pos=True,
    ):
        # Quarter-point ticks + directional axis labels make the curve legible:
        # gridlines fall at 0/25/50/75/100 and the labels show which way input
        # (→) and output (↑) grow.
        dpg.add_plot_axis(
            dpg.mvXAxis,
            label=t("controller.sticks.plot_8point.input"),
            tag=f"{tag_prefix}plot_xaxis_8point",
        )
        dpg.set_axis_limits(f"{tag_prefix}plot_xaxis_8point", -_PLOT_AXIS_VIEW_PAD, 100 + _PLOT_AXIS_VIEW_PAD)
        dpg.set_axis_ticks(f"{tag_prefix}plot_xaxis_8point", _PLOT_AXIS_TICKS)
        y_axis = dpg.add_plot_axis(
            dpg.mvYAxis,
            label=t("controller.sticks.plot_8point.output"),
            tag=f"{tag_prefix}plot_yaxis_8point",
        )
        dpg.set_axis_limits(f"{tag_prefix}plot_yaxis_8point", -_PLOT_AXIS_VIEW_PAD, 100 + _PLOT_AXIS_VIEW_PAD)
        dpg.set_axis_ticks(f"{tag_prefix}plot_yaxis_8point", _PLOT_AXIS_TICKS)
        # Static y=x reference (drawn first so it sits behind the live curve).
        diagonal = dpg.add_line_series(
            [0, 100], [0, 100],
            tag=f"{tag_prefix}plot_diagonal_8point",
            parent=y_axis,
        )
        # Live curve: a line through the 8 anchors plus scatter markers on them.
        curve = dpg.add_line_series(
            [0] * 8, [0] * 8,
            tag=f"{tag_prefix}plot_series_8point",
            parent=y_axis,
        )
        scatter = dpg.add_scatter_series(
            [0] * 8, [0] * 8,
            tag=f"{tag_prefix}plot_scatter_8point",
            parent=y_axis,
        )
        if plot_themes is not None:
            dpg.bind_item_theme(diagonal, plot_themes["diagonal"])
            dpg.bind_item_theme(curve, plot_themes["curve"])
            dpg.bind_item_theme(scatter, plot_themes["scatter"])

        # Draggable anchor handles — the primary editing affordance. One drag
        # point per anchor sits on the scatter marker; dragging it moves that
        # anchor in both axes at once. The move funnels through
        # _on_drag_point_edit → shell._on_sensitivity_8point_edit (the same path
        # the slider + input use), so the slider, input, series, and every drag
        # point stay in two-way sync (the shell repaints all of them from the
        # canonical sliders). Accent color + a heavier ring (thickness) mark them
        # as grabbable, reinforced by the enlarged scatter markers underneath
        # (bound below); ImPlot supplies the on-hover highlight/cursor natively.
        # clamped keeps them inside 0-100; no_cursor drops the crosshair so 8
        # points don't clutter the grid; the slider-form lambda (3
        # POSITIONAL_OR_KEYWORD + keyword-only closure) satisfies the DPG dispatch
        # contract (test_dpg_callback_signature_audit) — _s is the drag point's
        # own tag, which we read for (x, y).
        drag_color = shell.COLORS["accent"]
        for anchor in range(1, 9):
            dpg.add_drag_point(
                default_value=(0.0, 0.0),
                color=drag_color,
                thickness=_DRAG_HANDLE_THICKNESS,
                clamped=True,
                no_cursor=True,
                show_label=False,
                tag=f"{tag_prefix}drag_a{anchor}_8point",
                callback=lambda _s=None, _a=None, _u=None, *, sh=shell, sd=side, ai=anchor: _on_drag_point_edit(sh, sd, ai, _s),
            )

    # Enlarge the scatter anchor markers so the handles read as grabbable. Bound
    # to the plot (not the series) so the marker SIZE cascades to the scatter
    # while app_shell's accent fill/outline stays bound to the series itself.
    dpg.bind_item_theme(f"{tag_prefix}plot_8point", _sensitivity_handle_theme(shell))

    # Point-value callout (#3). A single muted readout line under the plot that
    # surfaces the exact (input, output) of the anchor being edited — updated live
    # from _on_drag_point_edit (and any slider/input edit that routes a drag).
    # One line, not 8 per-point labels, keeps the grid uncluttered. Seeded with a
    # hint until the first interaction.
    dpg.add_text(
        t("controller.sticks.plot_8point.readout_hint"),
        tag=f"{tag_prefix}drag_readout_8point",
        color=shell.COLORS["muted"],
        wrap=400,
    )

    # Preset row (8-point curves). Mirrors the 3-point preset row but targets the
    # 8-point apply path: clicking sets the 16 sliders, repaints the plot, then
    # writes (all handled in shell.apply_*_sensitivity_preset_8point).
    dpg.add_text(t("controller.sticks.presets"), color=shell.COLORS["muted"])
    preset_callback = (
        shell.apply_left_sensitivity_preset_8point
        if side == "left"
        else shell.apply_right_sensitivity_preset_8point
    )
    with dpg.group(horizontal=True):
        for preset in SENSITIVITY_PRESETS_8POINT:
            label = t(f"controller.sticks.preset_8point.{preset.lower()}")
            dpg.add_button(label=label, width=92, callback=lambda *args, p=preset, cb=preset_callback: cb(p))

    # Reset affordances adjacent to the plot. Both route through EXISTING shell
    # paths (no new service call) and apply immediately, matching the preset
    # buttons above: "Reset to Linear" applies the bundled Linear preset (identity
    # y=x); "Reset to Defaults" reverts to the controller's last-read curve and
    # re-applies it (a "put it back the way it was" action, distinct from linear).
    with dpg.group(horizontal=True):
        dpg.add_button(
            label=t("controller.sticks.reset_linear"),
            width=140,
            tag=f"{tag_prefix}reset_linear_8point",
            callback=lambda *args, sh=shell, sd=side: _on_reset_sensitivity_8point_linear(sh, sd),
        )
        dpg.add_button(
            label=t("controller.sticks.reset_defaults"),
            width=150,
            tag=f"{tag_prefix}reset_defaults_8point",
            callback=lambda *args, sh=shell, sd=side: _on_reset_sensitivity_8point_defaults(sh, sd),
        )

    apply_callback = (
        shell.apply_left_sensitivity_curve_8point
        if side == "left"
        else shell.apply_right_sensitivity_curve_8point
    )
    apply_key = f"controller.sticks.{side}_apply_sensitivity_8point"
    dpg.add_button(label=t(apply_key), width=260, callback=lambda *args, cb=apply_callback: cb())

    # Exact-values panel — the precise twin of the graph drag. Collapsible and
    # COLLAPSED by default (#4: demote the numeric table so the graph is the
    # centerpiece) — the 16 anchor fields stay one click away (this is the
    # exact-value entry path, demoted not removed). Each anchor/axis row pairs an
    # exact-entry numeric box (type
    # a 0-100 value, commits on Enter) with the coarse drag slider. Both funnel
    # through shell._on_sensitivity_8point_edit, which clamps to 0-100, runs the
    # monotonic auto-assist (pushes neighbouring anchors so the curve stays
    # non-decreasing), keeps the input + slider lockstep, and repaints the plot
    # (which repositions the drag points). The slider fires live on drag; the input
    # commits on Enter so a multi-digit type can't collapse neighbours
    # mid-keystroke. Both callbacks use the slider-form lambda per the DPG dispatch
    # contract. Labels are compact ("A1 X") since the side is the column header.
    with dpg.tree_node(
        label=t("controller.sticks.exact_values"),
        default_open=False,
        tag=f"{tag_prefix}exact_values_tree_8point",
    ) as exact_values_tree:
        # Lift the disclosure label out of the default body font to the section
        # (h2) role so "Exact values" reads as a clear, clickable affordance
        # rather than a small inline link. Per-locale via the active locale's h2
        # handle (en Inter SemiBold / zh-CN Noto Sans SC SemiBold). Guarded like
        # typography._titled_text: font_for can return None (headless tests never
        # call register_fonts) and a recycled id under the shared-context shim
        # could be stale, so check before binding and never block the render.
        _h2_font = font_for("h2", get_locale())
        if _h2_font is not None and dpg.does_item_exist(_h2_font):
            dpg.bind_item_font(exact_values_tree, _h2_font)
        for anchor in range(1, 9):
            for axis in ("x", "y"):
                with dpg.group(horizontal=True):
                    dpg.add_input_int(
                        default_value=0,
                        min_value=0,
                        max_value=100,
                        min_clamped=True,
                        max_clamped=True,
                        step=0,
                        width=90,
                        on_enter=True,
                        tag=f"{tag_prefix}a{anchor}{axis}_input_8point",
                        callback=lambda _s=None, _value=None, _u=None, *, sh=shell, sd=side, ai=anchor, ax=axis: sh._on_sensitivity_8point_edit(sd, ai, ax, _value),
                    )
                    dpg.add_slider_int(
                        label=t("controller.sticks.anchor_axis_short", anchor=anchor, axis=axis.upper()),
                        default_value=0,
                        min_value=0,
                        max_value=100,
                        width=200,
                        tag=f"{tag_prefix}a{anchor}{axis}_slider_8point",
                        callback=lambda _s=None, _value=None, _u=None, *, sh=shell, sd=side, ai=anchor, ax=axis: sh._on_sensitivity_8point_edit(sd, ai, ax, _value),
                    )
    # The series + drag points are created flat ([0]*8 / origin, matching the
    # default sliders); the snapshot hydrate (_hydrate_sensitivity_8point →
    # _set_sensitivity_8point_plot_points) paints the real curve and moves the
    # drag points as soon as a reading lands, so no seed read is needed here.


def _render_axis_inversion(shell) -> None:
    section_title(t("controller.sticks.axis_inversion"))
    for side in ("left", "right"):
        dpg.add_text(t(f"controller.sticks.{side}"), color=shell.COLORS["muted"])
        dpg.add_checkbox(label=t(f"controller.sticks.{side}_x_inverted"), default_value=False, tag=f"axis_inv_{side}_x_checkbox")
        dpg.add_checkbox(label=t(f"controller.sticks.{side}_y_inverted"), default_value=False, tag=f"axis_inv_{side}_y_checkbox")
        callback = shell.apply_left_axis_inversion if side == "left" else shell.apply_right_axis_inversion
        dpg.add_button(label=t(f"controller.sticks.{side}_apply_inversion"), width=220, callback=lambda *args, cb=callback: cb())
    dpg.add_text(t("controller.sticks.inversion_note"), color=shell.COLORS["muted"], wrap=640)


def _render_buttons_tab(shell) -> None:
    _render_loading_hint(shell)
    _render_current_bindings_section(shell)
    section_title(t("controller.buttons.title"))
    dpg.add_text(t("controller.buttons.helper"), color=shell.COLORS["muted"], wrap=620)
    dpg.add_combo(
        items=BUTTON_SLOT_ITEMS,
        default_value="A",
        label=t("controller.buttons.source"),
        width=320,
        tag="binding_source_combo",
        callback=lambda _s, value: shell.on_binding_source_changed(value),
    )
    dpg.add_combo(
        items=BUTTON_TARGET_ITEMS,
        default_value="A",
        label=t("controller.buttons.target"),
        width=320,
        tag="binding_target_combo",
    )
    dpg.add_button(label=t("controller.buttons.apply"), width=220, callback=lambda: shell.apply_button_binding())
    dpg.add_text(t("controller.buttons.note"), color=shell.COLORS["muted"], wrap=700)
    _render_back_paddles_section(shell)
    # Back-view paddle map sits with the Back Paddles section it explains, so a
    # user can see which physical paddle each slot name maps to.
    _render_controller_diagram_panel(shell)


# Glyph shown for a button slot that the controller failed to read back (it is
# ABSENT from the snapshot's ``button_bindings`` dict — never None-valued). An em
# dash + tooltip, never a fabricated identity default: honest abstention is the
# project's core principle (cf. the axis_inversion fake-default incident).
_UNKNOWN_BINDING_GLYPH = "—"


def _slot_identity_target(slot: ButtonSlot) -> ControllerButtonTarget | None:
    """The target that means "this slot is unremapped" (e.g. A→A, UP→UP).

    Derived by NAME, never by hardcoded hex: ``ButtonSlot`` and
    ``ControllerButtonTarget`` share member names, so a slot's identity target is
    the target with the same name. Returns ``None`` only if no same-named target
    exists (defensive; all 16 current slots have one).
    """

    return ControllerButtonTarget.__members__.get(slot.name)


def _render_current_bindings_section(shell) -> None:
    """Read-only display of the controller's CURRENT per-button bindings.

    Surfaces the remaps already read into ``button_bindings`` (HID cat 0x02) so
    users can see their setup, not just the source→target picker below. Pure
    display: no device I/O, no write path.

    Each row's cell is built ONCE with stable-tagged widgets whose
    value/colour/visibility are then updated in place by
    :func:`refresh_current_bindings` — so an in-tab Apply Binding or a snapshot
    refresh updates the list without recreating any widget, and without the user
    leaving and re-entering the Buttons tab.
    """

    section_title(t("controller.buttons.current.title"))
    snapshot = shell.last_controller_snapshot
    if snapshot is None:
        # Never read yet — don't fabricate rows; tell the user how to populate it.
        dpg.add_text(
            t("controller.buttons.current.not_read"),
            color=shell.COLORS["muted"],
            wrap=620,
        )
        dpg.add_spacer(height=SPACE_MD)
        dpg.add_separator()
        dpg.add_spacer(height=SPACE_MD)
        return

    bindings = snapshot.button_bindings or {}
    for slot in ButtonSlot:
        with dpg.group(horizontal=True, tag=f"current_binding_row_{slot.name}"):
            dpg.add_text(slot.name, color=shell.COLORS["text"])
            dpg.add_text("→", color=shell.COLORS["muted"])
            _render_current_binding_target(shell, slot, bindings.get(slot))
    dpg.add_spacer(height=SPACE_MD)
    dpg.add_separator()
    dpg.add_spacer(height=SPACE_MD)


def refresh_current_bindings(shell) -> None:
    """Update the read-only Current Bindings list in place from the current
    snapshot.

    Writes only widget VALUES / colours / visibility (no widget creation), so it
    is safe to call from the value-only hydrate/refresh paths as well as after an
    in-tab Apply Binding — the list then reflects the device without the user
    leaving and re-entering the Buttons tab. Per-slot no-op when the row isn't
    mounted (a different screen/tab is active, or nothing has been read yet so no
    rows were built); those cases pick up the fresh snapshot when the Buttons tab
    is next built.
    """

    snapshot = shell.last_controller_snapshot
    bindings = (snapshot.button_bindings or {}) if snapshot is not None else {}
    for slot in ButtonSlot:
        _apply_current_binding_target_state(shell, slot, bindings.get(slot))


def _render_current_binding_target(shell, slot: ButtonSlot, mapping) -> None:
    """Build one slot's current-target cell as a FIXED set of stable-tagged
    widgets, then paint the current state.

    The cell is always the same three widgets — the target text, the
    couldn't-read tooltip, and the "(remapped)" tag — regardless of state, so
    :func:`_apply_current_binding_target_state` can switch between the
    absent/default/remap/raw states with value + visibility writes alone (no
    create/destroy). That is what keeps the live-refresh path free of widget
    creation, so it can run from the value-only hydrate/refresh code paths.
    """

    target_tag = f"current_binding_target_{slot.name}"
    target_item = dpg.add_text("", tag=target_tag)
    # Tooltip explaining an unreadable slot. Always built; shown only in the
    # absent state (toggled by the state applier below).
    with dpg.tooltip(target_item, tag=f"current_binding_tip_{slot.name}"):
        dpg.add_text(t("controller.buttons.current.unknown_tooltip"), wrap=320)
    # The "(remapped)" tag. Always built; its VALUE is set to the tag text only
    # for a real remap and blanked otherwise, so a default/absent row carries no
    # stray "(remapped)" label even though the widget persists for live updates.
    dpg.add_text(
        "",
        color=shell.COLORS["accent"],
        tag=f"current_binding_remap_{slot.name}",
    )
    _apply_current_binding_target_state(shell, slot, mapping)


def _apply_current_binding_target_state(shell, slot: ButtonSlot, mapping) -> None:
    """Paint one slot's current-target cell for ``mapping`` using value / colour
    / visibility writes only (no widget creation).

    Honest absent/default/remap/raw states, identical to the build-time
    rendering, but expressed as in-place updates so the same logic serves both
    the first render and every live refresh. The leading ``does_item_exist``
    guard makes it a safe no-op when the slot's row isn't mounted.
    """

    target_tag = f"current_binding_target_{slot.name}"
    if not dpg.does_item_exist(target_tag):
        return
    tip_tag = f"current_binding_tip_{slot.name}"
    remap_tag = f"current_binding_remap_{slot.name}"

    def _show_tip(show: bool) -> None:
        if dpg.does_item_exist(tip_tag):
            dpg.configure_item(tip_tag, show=show)

    def _set_remap_tag(text: str) -> None:
        if dpg.does_item_exist(remap_tag):
            dpg.set_value(remap_tag, text)

    if mapping is None:
        # Slot absent from the snapshot ⇒ it failed to read (timeout / over the
        # read budget). Honest unknown — NEVER a default for an unread slot.
        dpg.set_value(target_tag, _UNKNOWN_BINDING_GLYPH)
        dpg.configure_item(target_tag, color=shell.COLORS["muted"])
        _show_tip(True)
        _set_remap_tag("")
        return

    _show_tip(False)
    if mapping.target_kind != 0x01 or mapping.target_low != 0x00:
        # Read succeeded, but this slot holds a mapping KIND LegendCTL doesn't
        # model — a keyboard / macro / vendor kind set by the official app or a
        # future firmware. Its target_value byte can collide with a controller-
        # button value (B == 0x10 == ButtonMapping's default), so naming it would
        # FABRICATE a remap we never actually read. Show the raw bytes with no
        # "(remapped)" claim — honest-abstention over a pretty-but-wrong label.
        # Mirrors restore_field_formatting / app_shell / snapshot_codec, which all
        # gate the button name on kind 0x01 + low 0x00.
        dpg.set_value(
            target_tag,
            f"kind=0x{mapping.target_kind:02X} "
            f"low=0x{mapping.target_low:02X} "
            f"val=0x{mapping.target_value:02X}",
        )
        dpg.configure_item(target_tag, color=shell.COLORS["accent"])
        _set_remap_tag("")
        return
    try:
        target = ControllerButtonTarget(mapping.target_value)
    except ValueError:
        # Read succeeded but the device returned a target byte we don't model.
        # Show the raw byte rather than crashing or inventing a name.
        dpg.set_value(target_tag, f"0x{mapping.target_value:02X}")
        dpg.configure_item(target_tag, color=shell.COLORS["accent"])
        _set_remap_tag("")
        return

    identity = _slot_identity_target(slot)
    is_remap = identity is None or target.value != identity.value
    dpg.set_value(target_tag, target.name)
    if is_remap:
        # Real remap — emphasize so it pops against the muted defaults.
        dpg.configure_item(target_tag, color=shell.COLORS["accent"])
        _set_remap_tag(t("controller.buttons.current.remapped_tag"))
    else:
        dpg.configure_item(target_tag, color=shell.COLORS["muted"])
        _set_remap_tag("")


def _render_back_paddles_section(shell) -> None:
    dpg.add_separator()
    dpg.add_spacer(height=12)
    section_title(t("controller.back_paddles.title"))
    dpg.add_text(t("controller.back_paddles.subtitle"), color=shell.COLORS["muted"], wrap=680)
    dpg.add_spacer(height=4)
    # The key honest-display reframe: LegendCTL has NO read path for the
    # controller's existing paddle/macro state (get_all_back_paddle_bindings()
    # returns {}), so a row with no LegendCTL value must NEVER imply the device
    # paddle is empty. This note states what we can't see, and that Apply
    # OVERWRITES whatever is on the paddle — including a macro set elsewhere,
    # which LegendCTL deliberately doesn't read or support. warn-coloured because
    # it carries the destructive-overwrite caution, not just neutral context.
    dpg.add_text(
        t("controller.back_paddles.cannot_read_note"),
        color=shell.COLORS["warn"],
        wrap=720,
    )
    dpg.add_spacer(height=4)
    dpg.add_text(
        t("controller.back_paddles.compatibility_note"),
        color=shell.COLORS["muted"],
        wrap=720,
    )
    dpg.add_spacer(height=12)

    target_options = _back_paddle_target_options()
    bindings = {}
    if shell.last_controller_snapshot is not None:
        bindings = shell.last_controller_snapshot.back_paddle_bindings or {}

    for slot in MacroSlot:
        binding = bindings.get(slot)
        # Slot ABSENT from the snapshot => no LegendCTL-set value this session,
        # and we can't read the device's real paddle state. Show an honest
        # placeholder, NOT "Unbound" (which would falsely claim the device paddle
        # is empty). "Unbound" stays in the dropdown as an explicit clear ACTION;
        # a binding that IS present with target=None means the user picked Unbound
        # here this session, so that genuinely-known state still shows "Unbound".
        if binding is None:
            default_value = t("controller.back_paddles.not_set_here")
        else:
            default_value = _back_paddle_target_label(binding.target)
        with dpg.group(horizontal=True, tag=f"back_paddle_row_{slot.name}"):
            dpg.add_text(slot.name, color=shell.COLORS["text"])
            dpg.add_combo(
                items=target_options,
                default_value=default_value,
                width=220,
                tag=f"back_paddle_combo_{slot.name}",
                # Touching a row's dropdown lights up that paddle on the back-view
                # diagram below — the v1 highlight-on-select interaction (pure UI;
                # the spec-blessed fallback to fiddly DPG hover handlers). slot
                # rides ``user_data`` and ``shell`` is a free var, so the 3-arg
                # DPG dispatch form preserves both (test_dpg_callback_signature_audit).
                user_data=slot,
                callback=lambda _sender, _app_data, user_data: refresh_diagram_paddle_highlight(shell, user_data),
            )
            dpg.add_button(
                label=t("actions.apply"),
                width=90,
                tag=f"back_paddle_apply_{slot.name}",
                user_data=slot,
                callback=lambda _sender, _app_data, user_data: shell.apply_back_paddle_binding_from_combo(user_data),
            )


def _back_paddle_target_options() -> list[str]:
    return [t("controller.back_paddles.unbound")] + [
        _back_paddle_target_label(target)
        for target in BACK_PADDLE_TARGET_ORDER
    ]


def _back_paddle_target_label(target: ControllerButtonTarget | None) -> str:
    if target is None:
        return t("controller.back_paddles.unbound")
    return t(f"controller.back_paddles.target.{target.name}")


def _render_controller_diagram_panel(shell) -> None:
    """Compact, code-drawn BACK-view map of the six back macro slots.

    Each back slot is a tagged hotspot circle (``diagram_paddle_{slot.name}``)
    plus a tagged label (``diagram_paddle_label_{slot.name}``) drawn at its
    mirrored §6 position, sitting on a low-fidelity controller-body silhouette.
    M3/M4 are FRONT buttons (beside the sticks) and are deliberately NOT drawn —
    a short note points to them instead.

    Pure UI. The highlight (:func:`refresh_diagram_paddle_highlight`) recolors a
    hotspot in place via ``configure_item`` — the same proven live-refresh model
    as ``_apply_current_binding_target_state`` and the live_verify circularity
    plot — and reflects the back-paddle ROW the user is touching, never a device
    read (LegendCTL can't read the device's real paddle state). An always-on
    honest note states exactly that; any slot whose drawn position is the most
    schematic (``_BACK_PADDLE_APPROX``) gets a "*" marker + the approx caption.
    """

    dpg.add_spacer(height=12)
    section_title(t("controller.buttons.diagram.title"))
    with dpg.drawlist(width=_DIAGRAM_W, height=_DIAGRAM_H, tag="diagram_back_drawlist"):
        # Low-fidelity controller back silhouette so the hotspots read as
        # sitting on a controller, not floating. Subtle (panel_alt) so the
        # labelled hotspots stay the focus; rounded body + two angled grip hints.
        body = shell.COLORS["panel_alt"]
        dpg.draw_rectangle((22, 22), (278, 176), color=body, thickness=2, rounding=26)
        dpg.draw_line((62, 174), (44, 208), color=body, thickness=2)
        dpg.draw_line((238, 174), (256, 208), color=body, thickness=2)

        for slot, (cx, cy) in _BACK_PADDLE_POS.items():
            label = f"{slot.name}*" if slot in _BACK_PADDLE_APPROX else slot.name
            # Hotspot ring — recolored in place on highlight (default = muted).
            dpg.draw_circle(
                (cx, cy),
                _PADDLE_R,
                color=shell.COLORS["muted"],
                thickness=2.0,
                tag=f"diagram_paddle_{slot.name}",
            )
            # Label centred in the hotspot. draw_text anchors at the text's
            # top-left, so offset by ~half the estimated text extent (a few px of
            # drift is invisible on a schematic). Stays ``text``-coloured; only
            # the ring carries the highlight state.
            tx = cx - len(label) * _DIAGRAM_LABEL_SIZE * 0.25
            ty = cy - _DIAGRAM_LABEL_SIZE * 0.5
            dpg.draw_text(
                (tx, ty),
                label,
                color=shell.COLORS["text"],
                size=_DIAGRAM_LABEL_SIZE,
                tag=f"diagram_paddle_label_{slot.name}",
            )

    # Honest framing (ALWAYS shown): the layout is manufacturer-spec, mirrored,
    # and the highlight reflects YOUR selection — never a read of the device.
    dpg.add_text(
        t("controller.buttons.diagram.note"),
        color=shell.COLORS["muted"],
        wrap=720,
    )
    # M3/M4 are front buttons — explain why they aren't on the back view.
    dpg.add_text(
        t("controller.buttons.diagram.front_note"),
        color=shell.COLORS["muted"],
        wrap=720,
    )
    # Approx footnote only when some slot is flagged approximate (honest §7).
    if _BACK_PADDLE_APPROX:
        dpg.add_text(
            t("controller.buttons.diagram.approx_note"),
            color=shell.COLORS["muted"],
            wrap=720,
        )


def refresh_diagram_paddle_highlight(shell, slot_or_none) -> None:
    """Recolor the back-view diagram so the SELECTED back slot reads as accent
    and every other reads as muted — value-only, guarded, no widget creation.

    Mirrors ``_apply_current_binding_target_state``'s ``does_item_exist``-guarded
    in-place refresh: each slot whose hotspot tag isn't mounted is simply
    skipped, so this is a safe no-op when the Buttons tab (and so the diagram)
    isn't on screen, and it never recreates a widget. ``slot_or_none=None``
    clears the highlight (all muted). Pure UI — reflects the touched row, not a
    device read. M3/M4 have no hotspot tag, so the loop skips them.
    """

    for slot in MacroSlot:
        tag = f"diagram_paddle_{slot.name}"
        if not dpg.does_item_exist(tag):
            continue
        selected = slot is slot_or_none
        dpg.configure_item(
            tag,
            color=shell.COLORS["accent"] if selected else shell.COLORS["muted"],
            thickness=3.0 if selected else 2.0,
        )


def _render_lighting_tab(shell) -> None:
    _render_loading_hint(shell)
    section_title(t("controller.lighting.title"))
    dpg.add_text(t("controller.lighting.helper"), color=shell.COLORS["muted"], wrap=620)
    dpg.add_combo(
        items=LIGHTING_ZONE_ITEMS,
        default_value="Home",
        label=t("controller.lighting.zone"),
        width=320,
        tag="lighting_zone_combo",
        callback=lambda _s, value: shell.on_lighting_zone_changed(value),
    )
    dpg.add_checkbox(label=t("controller.lighting.on"), default_value=True, tag="lighting_on_checkbox")
    dpg.add_combo(items=LIGHTING_MODE_ITEMS, default_value="Always On", label=t("controller.lighting.mode"), width=320, tag="lighting_mode_combo")
    dpg.add_slider_int(label=t("controller.lighting.brightness"), default_value=100, min_value=0, max_value=255, width=320, tag="lighting_brightness_slider")
    dpg.add_slider_int(label=t("controller.lighting.red"), default_value=255, min_value=0, max_value=255, width=320, tag="lighting_r_slider")
    dpg.add_slider_int(label=t("controller.lighting.green"), default_value=255, min_value=0, max_value=255, width=320, tag="lighting_g_slider")
    dpg.add_slider_int(label=t("controller.lighting.blue"), default_value=255, min_value=0, max_value=255, width=320, tag="lighting_b_slider")
    dpg.add_button(label=t("controller.lighting.apply"), width=220, callback=lambda: shell.apply_lighting())
    dpg.add_text(t("controller.lighting.note"), color=shell.COLORS["muted"], wrap=700)


def _render_motion_tab(shell) -> None:
    motion = shell.last_controller_snapshot.motion_settings if shell.last_controller_snapshot else None
    section_title(t("controller.motion.title"))
    if motion is None:
        dpg.add_text(t("controller.motion.no_data"), color=shell.COLORS["muted"], wrap=520)
        _draw_skeleton(width=280)
        return
    dpg.add_text(t("controller.motion.subtitle"), color=shell.COLORS["muted"], wrap=620)
    _readonly_row(t("controller.motion.target_label"), _motion_target_display(motion.target))
    _readonly_row(t("controller.motion.trigger_key_label"), str(motion.trigger_key))
    _readonly_row(t("controller.motion.mode_label"), _motion_mode_display(motion.mode))
    _readonly_row(t("controller.motion.sensitivity_label"), str(motion.sensitivity))
    dpg.add_separator()
    dpg.add_text(t("controller.motion.read_only_note"), color=shell.COLORS["muted"], wrap=620)


def _render_profiles_tab(shell) -> None:
    profiles = shell.list_wrapper_profiles()
    section_title(t("controller.profiles.title"))
    dpg.add_text(t("controller.profiles.subtitle"), color=shell.COLORS["muted"], wrap=700)
    # Disambiguate the two profile namespaces: this tab lists LegendCTL-LOCAL
    # profiles (real names, stored on this PC); the controller's four on-device
    # slots are positional "Profile 1-4" whose names we can't read. Never let a
    # user read a local profile as living on the device, or vice versa.
    dpg.add_text(
        t("controller.profiles.device_distinction_note"),
        color=shell.COLORS["muted"],
        wrap=700,
    )
    skipped_count_fn = getattr(shell, "wrapper_profiles_skipped_count", None)
    skipped_count = skipped_count_fn() if callable(skipped_count_fn) else 0
    if skipped_count:
        dpg.add_text(
            t("profile.skipped_count", count=skipped_count),
            color=shell.COLORS["warn"],
            wrap=700,
        )
    dpg.add_spacer(height=8)
    if not profiles:
        table_empty_state(t("controller.profiles.empty"))
    else:
        # Routed through the shared table() builder: this legacy profiles table
        # had no row background before, so it now also picks up the standard
        # zebra rows + tinted header. The Delete action uses action_button(
        # destructive=True) for the red destructive style; Apply stays neutral.
        with table(
            [
                Column(t("controller.profiles.col_name"), weight=2.0),
                Column(t("controller.profiles.col_modified"), weight=1.4),
                Column(t("controller.profiles.col_actions"), weight=1.6),
            ],
            tag="controller_profiles_table",
        ):
            for index, profile in enumerate(profiles):
                with dpg.table_row():
                    with dpg.group():
                        dpg.add_text(profile.name)
                        safe_import_badges.render_badges(
                            safe_import_badges.badges_for_profile(profile),
                            tag_prefix=f"profile_row_{index}",
                        )
                    dpg.add_text(profile.last_modified_at[:16].replace("T", " "))
                    with dpg.group(horizontal=True):
                        action_button(
                            t("actions.apply"),
                            user_data=profile.name,
                            callback=lambda _sender, _app_data, name: shell.apply_named_wrapper_profile(name),
                        )
                        action_button(
                            t("actions.delete"),
                            destructive=True,
                            user_data=profile.name,
                            callback=lambda _sender, _app_data, name: shell.confirm_delete_named_wrapper_profile(name),
                        )
    dpg.add_spacer(height=12)
    with dpg.group(horizontal=True):
        dpg.add_button(label=t("controller.profiles.save_current"), width=240, callback=lambda: shell._open_save_as_modal())
        # Safe Import (profile sharing) is gated behind the developer panel
        # toggle pending further security hardening. The merged feature
        # remains functional; existing tests pass via does_item_exist + direct
        # callback invocation regardless of ``show``.
        dpg.add_button(
            label=t("controller.profiles.import"),
            width=160,
            tag="controller_profiles_import_button",
            callback=lambda: shell.open_safe_import(),
            show=shell.settings.developer_panels_visible,
        )


def _readonly_row(label: str, value: str) -> None:
    with dpg.group(horizontal=True):
        dpg.add_text(f"{label}:", color=(144, 153, 170, 255))
        dpg.add_text(value)


def _draw_skeleton(width: int = 220, height: int = 16) -> None:
    with dpg.drawlist(width=width, height=height):
        dpg.draw_rectangle((0, 0), (width, height), fill=(38, 44, 58, 255), color=(45, 51, 64, 255))


def _remember_active_tab(shell, selected_tab) -> None:
    shell.controller_active_tab = _tab_tag_to_id(selected_tab)


def _tab_id_to_tag(tab_id: str) -> str:
    if tab_id in CONTROLLER_TAB_IDS:
        return f"tab_{tab_id}"
    return "tab_vibration"


def _tab_tag_to_id(tab_tag) -> str:
    value = str(tab_tag)
    if value.startswith("tab_"):
        tab_id = value.removeprefix("tab_")
        if tab_id in CONTROLLER_TAB_IDS:
            return tab_id
    return "vibration"


def _motion_target_display(target: MotionMappingTarget) -> str:
    return {
        MotionMappingTarget.DISABLED: t("controller.motion.target.disabled"),
        MotionMappingTarget.LEFT_JOYSTICK: t("controller.motion.target.left"),
        MotionMappingTarget.RIGHT_JOYSTICK: t("controller.motion.target.right"),
    }.get(target, t("common.unknown"))


def _motion_mode_display(mode: MotionMappingMode) -> str:
    return {
        MotionMappingMode.INSTANT: t("controller.motion.mode.instant"),
        MotionMappingMode.CONTINUOUS: t("controller.motion.mode.continuous"),
    }.get(mode, t("common.unknown"))
