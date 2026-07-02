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
                    shell.device_service.format_firmware_version(),
                    "home_status_firmware",
                    None,
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
                    _localized_active_config_label(state),
                    "home_profile_active",
                    None,
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
