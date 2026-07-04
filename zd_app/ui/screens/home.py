"""R2 Home dashboard.

Component-vocabulary reference screen: the Connection / Profile / Recent Activity
/ Quick Actions panels are built from the reusable :mod:`zd_app.ui.components`
vocabulary (``card`` / ``section`` / ``metric``) rather than ad-hoc
``dpg.child_window(border=True)`` + ``section_title`` + ``add_text`` blocks.

Layout is width-flexible: the old hard-coded fixed-width cards are gone. The two
top panels live in a 2-column stretch table that splits the available width
50/50 and shrinks with the window; the full-width panels below fill the content
region. Buttons are auto-width so en + zh-CN labels both fit without per-locale
pixel tuning.
"""

from __future__ import annotations

import logging
from datetime import datetime

import dearpygui.dearpygui as dpg

from zd_app.i18n import t
from zd_app.ui import right_rail, safe_import_badges, trust_front_door, trust_labels
from zd_app.ui.components import card, metric, section
from zd_app.ui.safe_import_badges import BadgeKind
from zd_app.ui.themes import COLORS


logger = logging.getLogger(__name__)


STALE_WARNING_HEADLINE = "Controller state may have changed outside the app."
STALE_WARNING_HELPER = "Read the controller again before trusting current editors or summary fields."

# Shared height for the two top cards so the stretch-table row stays even
# regardless of which side has more content. Width is intentionally flexible
# (the table column stretches); only the height is pinned. The taller side is
# Device & profile is deliberately compacted into paired metric rows so all
# status values fit the shared top-row height. fit=True is unsuitable here: it
# would size each card to its own content and break the even orientation/status
# pair that anchors Home.
_TOP_CARD_HEIGHT = 257
_HOME_STACK_GAP = 4


def _format_last_read(raw):
    """Render the raw ISO-8601 ``last_read_time`` as ``YYYY-MM-DD HH:MM``.

    Empty/None degrades to the localized "Never"; an unparseable value is shown
    as-is rather than hidden, so a malformed timestamp never silently vanishes.
    """

    if not raw:
        return t("common.never")
    try:
        return datetime.fromisoformat(raw).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return raw   # unparseable -> show as-is rather than hide it


def build(shell, parent: str) -> None:
    state = shell.device_service.state
    with right_rail.rail_screen(
        shell,
        parent,
        screen_id="home",
        root_tag="home_root",
        work_tag="home_work_column",
    ):
        # ZD-required unaffiliated / warranty disclaimer, surfaced on the
        # landing screen so it is seen on launch without navigating to About.
        # Same verbatim string as the About screen (single source of truth in
        # the `about.zd_disclaimer` i18n key) — render it, do not paraphrase.
        dpg.add_text(
            t("about.zd_disclaimer"),
            wrap=1100,
            tag="home_zd_disclaimer",
        )
        _home_gap()

        _two_column_row(
            lambda: _orientation_card(shell),
            lambda: _device_profile_status_card(shell),
            tag="home_orientation_row",
        )
        _home_gap()
        _two_column_row(
            lambda: _trust_front_door_card(shell),
            lambda: _actions_card(shell),
            tag="home_next_step_row",
        )
        if _should_show_setup_drawer(shell):
            _home_gap()
            _setup_drawer_card(shell)
        _home_gap()
        _state_explainer(shell)

        _home_gap()
        _recent_activity(shell)

        if state.data_freshness == "stale":
            _home_gap()
            dpg.add_text(STALE_WARNING_HEADLINE, color=shell.COLORS["warn"])
            dpg.add_text(STALE_WARNING_HELPER, color=shell.COLORS["muted"])


def _two_column_row(left, right, *, tag: str) -> None:
    with dpg.table(header_row=False, policy=dpg.mvTable_SizingStretchSame, tag=tag):
        dpg.add_table_column()
        dpg.add_table_column()
        with dpg.table_row():
            left()
            right()


def _orientation_card(shell) -> None:
    connected = shell.device_service.state.connection_state == "connected"
    with card(height=_TOP_CARD_HEIGHT, tag="home_orientation_card"):
        with section(t("home.orientation.title")):
            dpg.add_text(t("home.orientation.what"), wrap=470)
            dpg.add_text(t("home.orientation.stance"), color=shell.COLORS["muted"], wrap=470)
            dpg.add_spacer(height=8)
            if connected:
                dpg.add_text(
                    t("home.orientation.connected_cta"),
                    color=shell.COLORS["muted"],
                    wrap=470,
                )
                with dpg.group(horizontal=True):
                    dpg.add_button(
                        label=t("home.orientation.read_settings"),
                        tag="home_orientation_read",
                        width=190,
                        height=36,
                        callback=lambda: shell.refresh_from_controller(),
                    )
                    dpg.add_button(
                        label=t("home.orientation.open_live_verify"),
                        tag="home_orientation_live_verify",
                        width=170,
                        height=36,
                        callback=lambda: shell.switch_screen("live_verify"),
                    )
            else:
                dpg.add_text(
                    t("home.orientation.no_controller_cta"),
                    tag="home_orientation_no_controller",
                    color=shell.COLORS["muted"],
                    wrap=470,
                )


def _device_profile_status_card(shell) -> None:
    state = shell.device_service.state
    with card(height=_TOP_CARD_HEIGHT, tag="home_device_profile_status_card"):
        with section(t("home.status.title"), gap=4):
            with dpg.group(horizontal=True):
                dpg.add_text(
                    f"{_connection_state_label(state.connection_state)} - {state.connection_mode}",
                    color=shell.COLORS["good"] if state.connection_state == "connected" else shell.COLORS["warn"],
                    tag="home_status_connection",
                    wrap=300,
                )
                safe_import_badges.render_badges(
                    [BadgeKind.NO_AUTOMATION], tag_prefix="home_status"
                )
            dpg.add_text(state.product_name, tag="home_status_device_model", wrap=470)
            _paired_metrics(
                (
                    t("home.connection.firmware"),
                    _firmware_status_value(shell),
                    "home_status_firmware",
                    _firmware_status_color(shell),
                ),
                (
                    t("home.connection.battery"),
                    shell.device_service.format_battery_level(),
                    "home_status_battery",
                    None,
                ),
            )
            _paired_metrics(
                (
                    t("home.profile.active"),
                    _active_profile_status_value(shell),
                    "home_profile_active",
                    _active_profile_status_color(shell),
                ),
                (
                    t("home.profile.pending"),
                    shell.profile_service.pending_changes_count(),
                    "home_profile_pending",
                    shell.COLORS["muted"],
                ),
            )
            with dpg.tooltip("home_profile_active"):
                dpg.add_text(t("home.profile.device_slot_tooltip"), wrap=320)
            metric(
                t("home.profile.draft"),
                _localized_draft_label(shell.profile_service.current_draft),
                value_color=shell.COLORS["muted"],
                value_tag="home_profile_draft",
            )


def _paired_metrics(left: tuple[str, object, str, object], right: tuple[str, object, str, object]) -> None:
    with dpg.group(horizontal=True):
        _inline_metric(*left)
        dpg.add_spacer(width=24)
        _inline_metric(*right)


def _inline_metric(label: str, value, value_tag: str, value_color) -> None:
    dpg.add_text(label, color=COLORS["text.secondary"])
    kwargs = {"color": value_color or COLORS["text.primary"]}
    if value_tag:
        kwargs["tag"] = value_tag
    dpg.add_text(str(value), **kwargs)


def _connection_card(shell) -> None:
    state = shell.device_service.state
    with card(height=_TOP_CARD_HEIGHT):
        with section(t("home.connection.title")):
            if _show_connection_skeleton(shell):
                _draw_skeleton(width=260)
                _draw_skeleton(width=220)
                _draw_skeleton(width=280)
                return
            dpg.add_text(
                f"{_connection_state_label(state.connection_state)} - {state.connection_mode}",
                color=shell.COLORS["good"] if state.connection_state == "connected" else shell.COLORS["warn"],
            )
            dpg.add_text(state.product_name)
            metric(
                t("home.connection.firmware"),
                shell.device_service.format_firmware_version(),
            )
            metric(
                t("home.connection.battery"),
                shell.device_service.format_battery_level(),
            )
            metric(
                t("home.connection.last_read"),
                _format_last_read(state.last_read_time),
            )


def _profile_card(shell) -> None:
    state = shell.device_service.state
    with card(height=_TOP_CARD_HEIGHT):
        with section(t("home.profile.title")):
            # No skeleton state here — _localized_active_config_label /
            # _localized_draft_label handle the "no snapshot yet" case
            # gracefully (Active: Not verified / Draft: Unsaved Draft /
            # Pending: 0). Showing populated-with-defaults beats the brief
            # flash of empty skeleton rectangles on first Home visit.
            #
            # The active config keeps the primary (emphasized) value color; draft
            # and pending stay muted. Tags ride the metric value items so the
            # zh-CN localization assertions read the value, not the label.
            metric(
                t("home.profile.active"),
                _localized_active_config_label(state),
                value_tag="home_profile_active",
            )
            # The active value is the controller's on-device profile SLOT by
            # number — the controller doesn't expose a custom name, so the tooltip
            # explains why only "Profile N" shows (honest abstention, never a
            # fabricated/blank name). A tooltip is zero layout cost; the card
            # height is pinned to stay even with the Connection card.
            with dpg.tooltip("home_profile_active"):
                dpg.add_text(t("home.profile.device_slot_tooltip"), wrap=320)
            metric(
                t("home.profile.draft"),
                _localized_draft_label(shell.profile_service.current_draft),
                value_color=shell.COLORS["muted"],
                value_tag="home_profile_draft",
            )
            metric(
                t("home.profile.pending"),
                shell.profile_service.pending_changes_count(),
                value_color=shell.COLORS["muted"],
                value_tag="home_profile_pending",
            )
            safe_import_badges.render_badges(
                [BadgeKind.NO_AUTOMATION], tag_prefix="home_profile"
            )
            # Auto-width buttons: the zh-CN labels ("另存为..." etc.) size
            # themselves, so no per-locale pixel tuning is needed anymore.
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label=t("home.quick.controller"),
                    callback=lambda: shell.switch_screen("controller"),
                )
                dpg.add_button(
                    label=t("footer.save_as"),
                    callback=lambda: shell._open_save_as_modal(),
                )


def _state_explainer(shell) -> None:
    count = shell.profile_service.pending_changes_count()
    with dpg.tree_node(
        label=t("home.state_explainer.title"),
        default_open=False,
        tag="home_state_explainer",
    ):
        for key, fmt in (
            ("home.state_explainer.connected", {}),
            ("home.state_explainer.firmware_unknown", {}),
            ("home.state_explainer.profile_not_verified", {}),
            ("home.state_explainer.pending_changes", {"count": count}),
        ):
            dpg.add_text(t(key, **fmt), color=shell.COLORS["muted"], wrap=1100)


def _trust_front_door_card(shell) -> None:
    with card(fit=True, tag="home_trust_front_door_card"):
        dpg.add_text(t("home.trust_front_door.title"), color=shell.COLORS["muted"], wrap=470)
        dpg.add_spacer(height=4)
        with dpg.group(horizontal=True):
            trust_front_door.add_trust_link_buttons(
                shell,
                tag_prefix="home_trust_front_door",
                button_width=135,
            )


def _recent_activity(shell) -> None:
    # Full-width stacked card with a variable number of recent-event lines (up
    # to 5) — fit to content so a full list never clips. Capped at 5 (was 10) as
    # a content-height trim: this is a dashboard glance; Diagnostics owns the
    # full history. The screen still scrolls if the page overflows.
    with card(fit=True):
        with section(t("home.recent.title")):
            events = shell.device_service.recent_events(5)
            dpg.add_text(
                tag="home_recent_events",
                default_value="\n".join(events) if events else t("home.recent.empty"),
                wrap=1100,
            )


def _actions_card(shell) -> None:
    """State-branching next-step card with read/verify before write emphasis."""

    connected = shell.device_service.state.connection_state == "connected"
    with card(fit=True):
        with section(t("home.cta.title")):
            dpg.add_text(
                t(
                    "home.cta.connected_helper"
                    if connected
                    else "home.cta.no_controller_helper"
                ),
                color=shell.COLORS["muted"],
                wrap=470,
            )
            dpg.add_spacer(height=6)
            if connected:
                with dpg.group(horizontal=True):
                    dpg.add_button(
                        label=t("home.orientation.read_settings"),
                        width=190,
                        height=36,
                        callback=lambda: shell.refresh_from_controller(),
                    )
                    dpg.add_button(
                        label=t("home.orientation.open_live_verify"),
                        width=170,
                        height=36,
                        callback=lambda: shell.switch_screen("live_verify"),
                    )
            else:
                dpg.add_button(
                    label=t("home.quick.diagnostics"),
                    width=170,
                    height=36,
                    callback=lambda: shell.switch_screen("diagnostics"),
                )
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label=t("nav.health_report"),
                    width=170,
                    callback=lambda: shell.switch_screen("health_report"),
                )
                dpg.add_button(
                    label=t("home.quick.controller"),
                    width=190,
                    callback=lambda: shell.switch_screen("controller"),
                )


def _setup_drawer_card(shell) -> None:
    state = shell.device_service.state
    complete = _setup_drawer_complete(shell)
    with card(fit=True, tag="home_setup_drawer_card"):
        # Two pre-built regions, exactly one shown: the full step list while
        # any completable step is pending, or a compact single row once both
        # are done (with the drawer visible, Home overflows the reference
        # viewport by ~376px, so a finished checklist is dead weight —
        # operator-chosen auto-collapse). Both regions exist from build time so
        # refresh_setup_drawer can swap them live via show/hide when the last
        # step completes while Home is showing — the same in-place philosophy
        # as the step flips: never a rebuild_current_screen from a refresh
        # path (the DPG modal-teardown law).
        with dpg.group(tag="home_setup_drawer_full", show=not complete):
            with section(t("home.setup_drawer.title")):
                _setup_drawer_step(
                    shell,
                    "home_setup_drawer_detected",
                    True,
                    t("home.setup_drawer.detected_label"),
                    state.product_name,
                )
                _setup_drawer_read_step(shell)
                _setup_drawer_backup_step(shell)
                _setup_drawer_explore_step(shell)
                dpg.add_spacer(height=6)
                dpg.add_button(
                    label=t("home.setup_drawer.dismiss"),
                    tag="home_setup_drawer_dismiss",
                    callback=lambda: _dismiss_setup_drawer(shell),
                )
        with dpg.group(tag="home_setup_drawer_compact", show=complete):
            # No step rows / helpers / Explore buttons here — Health Check and
            # Live Verify already live in the Next-step card, so nothing is
            # lost. The dismiss button reuses the same i18n key + callback path
            # as the full variant; it only needs a distinct tag because DPG
            # tags are unique.
            with dpg.group(horizontal=True):
                dpg.add_text(
                    "✓",
                    color=shell.COLORS["good"],
                    tag="home_setup_drawer_compact_marker",
                )
                dpg.add_text(
                    t("home.setup_drawer.complete"),
                    tag="home_setup_drawer_compact_label",
                )
                dpg.add_button(
                    label=t("home.setup_drawer.dismiss"),
                    tag="home_setup_drawer_compact_dismiss",
                    callback=lambda: _dismiss_setup_drawer(shell),
                )


def _setup_drawer_complete(shell) -> bool:
    """True when every completable drawer step is done.

    Step 1 "Detected" is inherently satisfied whenever the drawer is visible
    (visibility requires a connected, recognized controller), so completeness
    reduces to step 2 (settings read this session) and step 3 (restore point
    created this session).
    """

    return _settings_read_this_session(shell) and _setup_drawer_restore_point_done(shell)


def _setup_drawer_step(
    shell,
    tag_prefix: str,
    done: bool,
    label: str,
    helper: str | None = None,
) -> None:
    marker = "✓" if done else "-"
    marker_color = shell.COLORS["good"] if done else shell.COLORS["muted"]
    with dpg.group(horizontal=True, tag=f"{tag_prefix}_row"):
        dpg.add_text(marker, color=marker_color, tag=f"{tag_prefix}_marker")
        dpg.add_text(label, tag=f"{tag_prefix}_label")
    if helper:
        dpg.add_text(
            helper,
            color=shell.COLORS["muted"],
            wrap=1000,
            tag=f"{tag_prefix}_helper",
        )
    dpg.add_spacer(height=4)


def _setup_drawer_read_step(shell) -> None:
    if _settings_read_this_session(shell):
        _setup_drawer_step(
            shell,
            "home_setup_drawer_read",
            True,
            t("home.setup_drawer.settings_read_label"),
        )
        return
    _setup_drawer_step(
        shell,
        "home_setup_drawer_read",
        False,
        t("home.setup_drawer.settings_read_label"),
        t("home.setup_drawer.settings_read_helper"),
    )
    dpg.add_button(
        label=t("home.setup_drawer.read_now"),
        tag="home_setup_drawer_read_now",
        width=150,
        height=32,
        callback=lambda: shell.refresh_from_controller(),
    )
    dpg.add_spacer(height=4, tag="home_setup_drawer_read_after_button_spacer")


def _setup_drawer_backup_step(shell) -> None:
    if _setup_drawer_restore_point_done(shell):
        _setup_drawer_step(
            shell,
            "home_setup_drawer_backup",
            True,
            t("home.setup_drawer.restore_point_created"),
        )
        return
    _setup_drawer_step(
        shell,
        "home_setup_drawer_backup",
        False,
        t("home.setup_drawer.back_up_label"),
        t("home.setup_drawer.back_up_helper"),
    )
    dpg.add_button(
        label=t("home.setup_drawer.create_restore_point"),
        tag="home_setup_drawer_create_restore_point",
        width=180,
        height=32,
        callback=lambda: _create_setup_drawer_restore_point(shell),
    )
    dpg.add_spacer(height=4, tag="home_setup_drawer_backup_after_button_spacer")


def refresh_setup_drawer(shell) -> None:
    """Re-sync the setup drawer's read / back-up steps (and collapse) in place.

    The drawer renders each step's state ONCE at Home build time. Two events
    land AFTER that build and would otherwise leave a step stuck on its pending
    dash: the startup auto-read completes on the threaded HID executor ~3s after
    Home builds (step 2), and a restore point created via the drawer button
    happens while Home is already showing (step 3). ``AppShell.refresh_shell``
    calls this on every tick and at read-completion, so the ✓ appears live.
    Once every completable step is done the drawer auto-collapses to its
    pre-built compact "First steps complete" row (see ``_setup_drawer_card``).

    In-place tag updates only — never a ``rebuild_current_screen`` call. A full
    rebuild from the read-done path could collide with the DearPyGui
    modal-teardown law (an "Apply device settings?" modal may be up when a read
    completes); configuring existing tags is immune to that hazard.

    No-op when the drawer card tag is absent (a different screen is showing, the
    controller is disconnected so the drawer never built, or the operator
    dismissed it). Every dpg call is existence-guarded per the surrounding code.
    """

    if not dpg.does_item_exist("home_setup_drawer_card"):
        return
    if _settings_read_this_session(shell):
        _mark_setup_drawer_step_done(
            shell,
            "home_setup_drawer_read",
            t("home.setup_drawer.settings_read_label"),
            button_tag="home_setup_drawer_read_now",
            spacer_tag="home_setup_drawer_read_after_button_spacer",
        )
    if _setup_drawer_restore_point_done(shell):
        _mark_setup_drawer_step_done(
            shell,
            "home_setup_drawer_backup",
            t("home.setup_drawer.restore_point_created"),
            button_tag="home_setup_drawer_create_restore_point",
            spacer_tag="home_setup_drawer_backup_after_button_spacer",
        )
    if _setup_drawer_complete(shell):
        _collapse_setup_drawer()


def _collapse_setup_drawer() -> None:
    """Swap the drawer's full step list for the compact complete row in place.

    Both regions were pre-built inside the card (one shown), so the collapse is
    a pure show/hide toggle — no rebuild, no new items. Idempotent: repeat
    calls re-hide/re-show the same regions harmlessly, and the existence guards
    keep this a no-op on any build that predates the two-region structure.
    """

    if dpg.does_item_exist("home_setup_drawer_full"):
        dpg.hide_item("home_setup_drawer_full")
    if dpg.does_item_exist("home_setup_drawer_compact"):
        dpg.show_item("home_setup_drawer_compact")


def _mark_setup_drawer_step_done(
    shell,
    tag_prefix: str,
    done_label: str,
    *,
    button_tag: str,
    spacer_tag: str,
) -> None:
    """Flip a pending drawer step to its checked state without rebuilding.

    Turns the marker green ✓, swaps the label to its done copy, and hides the
    pending-only helper text, action button, and trailing spacer. Idempotent:
    once a step is already done (or was built done, so its button/helper never
    existed) the existence guards make repeat calls harmless no-ops.
    """

    marker_tag = f"{tag_prefix}_marker"
    if dpg.does_item_exist(marker_tag):
        dpg.set_value(marker_tag, "✓")
        dpg.configure_item(marker_tag, color=shell.COLORS["good"])
    label_tag = f"{tag_prefix}_label"
    if dpg.does_item_exist(label_tag):
        dpg.set_value(label_tag, done_label)
    for tag in (f"{tag_prefix}_helper", button_tag, spacer_tag):
        if dpg.does_item_exist(tag):
            dpg.hide_item(tag)


def _setup_drawer_explore_step(shell) -> None:
    dpg.add_text(t("home.setup_drawer.explore_label"), tag="home_setup_drawer_explore_label")
    with dpg.group(horizontal=True, tag="home_setup_drawer_explore_buttons"):
        dpg.add_button(
            label=t("home.setup_drawer.health_check"),
            tag="home_setup_drawer_health_check",
            width=150,
            height=32,
            callback=lambda: shell.switch_screen("health_report"),
        )
        dpg.add_button(
            label=t("home.setup_drawer.live_verify"),
            tag="home_setup_drawer_live_verify",
            width=150,
            height=32,
            callback=lambda: shell.switch_screen("live_verify"),
        )


def _should_show_setup_drawer(shell) -> bool:
    state = shell.device_service.state
    return (
        state.connection_state == "connected"
        and state.device_class == "zd_ultimate_legend"
        and not _setup_drawer_dismissed(shell)
    )


def _drawer_current_identity(shell) -> str | None:
    """stable_identifier of the controller the drawer is describing, or None.

    The drawer only renders with a recognized controller connected, so this is
    normally a real identifier; the guard keeps the completion predicates honest
    if it is ever evaluated without one.
    """
    service = getattr(shell, "device_service", None)
    state = getattr(service, "state", None)
    identity = getattr(state, "stable_identifier", None)
    if not identity or identity == "unknown":
        return None
    return identity


def _drawer_flag_matches_current(shell, recorded: object) -> bool:
    """True when a session flag stamped ``recorded`` belongs to the unit
    connected now, so swapping physical controllers mid-session cannot let the
    new unit inherit the previous one's completed steps."""
    current = _drawer_current_identity(shell)
    return current is not None and recorded == current


def _setup_drawer_restore_point_done(shell) -> bool:
    return bool(
        getattr(shell, "setup_drawer_restore_point_created", False)
    ) and _drawer_flag_matches_current(
        shell, getattr(shell, "setup_drawer_restore_point_identity", None)
    )


def _settings_read_this_session(shell) -> bool:
    if (
        getattr(shell, "last_controller_snapshot", None) is None
        or getattr(shell, "last_snapshot_ts", None) is None
    ):
        return False
    # Scope to the connected unit: a snapshot read from a DIFFERENT physical
    # controller (swapped mid-session) must not count as this one's settings read.
    return _drawer_flag_matches_current(
        shell, getattr(shell, "last_snapshot_identity", None)
    )


def _setup_drawer_dismissed(shell) -> bool:
    settings = getattr(shell, "settings", None)
    if hasattr(settings, "setup_drawer_dismissed"):
        return bool(getattr(settings, "setup_drawer_dismissed"))
    store = getattr(shell, "settings_store", None)
    getter = getattr(store, "get_setup_drawer_dismissed", None)
    if not callable(getter):
        return False
    try:
        return getter() is True
    except Exception:
        logger.exception("Setup drawer dismissed-state read failed")
        return False


def _dismiss_setup_drawer(shell) -> None:
    if hasattr(shell, "settings"):
        setattr(shell.settings, "setup_drawer_dismissed", True)
    store = getattr(shell, "settings_store", None)
    setter = getattr(store, "set_setup_drawer_dismissed", None)
    if callable(setter):
        setter(True)
    else:
        store.save(shell.settings)
    shell.rebuild_current_screen()


def _create_setup_drawer_restore_point(shell) -> None:
    rp = shell.manual_save_restore_point()
    if rp is not None:
        shell.setup_drawer_restore_point_created = True
        shell.setup_drawer_restore_point_identity = _drawer_current_identity(shell)
    shell.rebuild_current_screen()


def _draw_skeleton(width: int = 200, height: int = 18) -> None:
    with dpg.drawlist(width=width, height=height):
        dpg.draw_rectangle(
            (0, 0),
            (width, height),
            fill=(38, 44, 58, 255),
            color=(45, 51, 64, 255),
        )
    dpg.add_spacer(height=6)


def _show_connection_skeleton(shell) -> bool:
    state = shell.device_service.state
    return state.connection_state == "no_device" and state.last_read_time is None


def _home_gap() -> None:
    dpg.add_spacer(height=_HOME_STACK_GAP)


def _connection_state_label(connection_state: str) -> str:
    return trust_labels.connection_state_label(connection_state)


def _active_config_label(state) -> str:
    return trust_labels.active_config_label(state)


def _localized_active_config_label(state) -> str:
    label = _active_config_label(state)
    if label == "Not verified":
        return t("profile.config_state.not_verified")
    if label.startswith("Config "):
        return t("profile.config_state.config", n=label.removeprefix("Config "))
    return label


def _firmware_status_value(shell) -> str:
    value = shell.device_service.format_firmware_version()
    if _has_retained_firmware(shell.device_service.state) and not _is_connected(shell):
        return t("device.value.last_read", value=value)
    return value


def _firmware_status_color(shell):
    if _has_retained_firmware(shell.device_service.state) and not _is_connected(shell):
        return shell.COLORS["muted"]
    return None


def _active_profile_status_value(shell) -> str:
    value = _localized_active_config_label(shell.device_service.state)
    if _has_retained_active_profile(shell.device_service.state) and not _is_connected(
        shell
    ):
        return t("device.value.last_read", value=value)
    return value


def _active_profile_status_color(shell):
    if _has_retained_active_profile(shell.device_service.state) and not _is_connected(
        shell
    ):
        return shell.COLORS["muted"]
    return None


def _is_connected(shell) -> bool:
    return shell.device_service.state.connection_state == "connected"


def _has_retained_firmware(state) -> bool:
    firmware = str(getattr(state, "firmware_version", "") or "").strip()
    return bool(firmware and firmware != "Unknown")


def _has_retained_active_profile(state) -> bool:
    sources = getattr(state, "summary_sources", {}) or {}
    return sources.get("active_profile", "unknown") != "unknown"


def _localized_draft_label(profile) -> str:
    if profile is None:
        return t("profile.draft.empty")
    if getattr(profile, "dirty", False):
        return t("profile.draft.dirty")

    display_name = getattr(profile, "display_name", "") or ""
    if not display_name:
        return t("profile.draft.empty")
    if display_name == "Unsaved Draft":
        return t("profile.draft.unsaved")
    if display_name == "Safe Defaults Draft":
        return t("profile.draft.safe_defaults")
    if display_name.startswith("Draft aligned to Config "):
        return t(
            "profile.draft.aligned_to_config",
            n=display_name.removeprefix("Draft aligned to Config "),
        )
    return display_name
