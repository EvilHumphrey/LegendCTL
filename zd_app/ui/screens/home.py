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
from zd_app.ui import safe_import_badges, trust_labels
from zd_app.ui.components import card, metric, section
from zd_app.ui.safe_import_badges import BadgeKind
from zd_app.ui.themes import SPACE_SM, section_gap


logger = logging.getLogger(__name__)


STALE_WARNING_HEADLINE = "Controller state may have changed outside the app."
STALE_WARNING_HELPER = "Read the controller again before trusting current editors or summary fields."

# Shared height for the two top cards so the stretch-table row stays even
# regardless of which side has more content. Width is intentionally flexible
# (the table column stretches); only the height is pinned. The taller side is
# Profile (3 metrics + the NO_AUTOMATION badge + 2 buttons); at the shipped fonts
# Connection and Profile measure the SAME content height in en AND zh-CN. The
# 2026-06-22 body 14->15 / helper 13->14 readability bump grew that content from
# 248px to 253px (DPG 2.3 card-clip probe, both locales), so the prior 252
# clipped by 1px. 257 = the re-measured 253px floor + a 4px safety margin: the
# row stays even and the buttons clear. fit=True is unsuitable here — it would
# size each card to its OWN content and break the even pair.
_TOP_CARD_HEIGHT = 257


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
    with dpg.child_window(parent=parent, autosize_y=True, border=False):
        # ZD-required unaffiliated / warranty disclaimer, surfaced on the
        # landing screen so it is seen on launch without navigating to About.
        # Same verbatim string as the About screen (single source of truth in
        # the `about.zd_disclaimer` i18n key) — render it, do not paraphrase.
        dpg.add_text(
            t("about.zd_disclaimer"),
            wrap=1100,
            tag="home_zd_disclaimer",
        )
        section_gap()

        # Two equal, width-flexible cards. A 2-column stretch table splits the
        # available width 50/50 and shrinks with the window instead of
        # overflowing at the old fixed 520+520px.
        with dpg.table(header_row=False, policy=dpg.mvTable_SizingStretchSame):
            dpg.add_table_column()
            dpg.add_table_column()
            with dpg.table_row():
                _connection_card(shell)
                _profile_card(shell)

        section_gap()
        _recent_activity(shell)

        # One consolidated actions card (the emphasized next step — Health Check
        # — plus the quick navigation/utility links). Replaces the former
        # separate "Quick Actions" and "Next step" cards, which overlapped (both
        # carried a "Read controller state" button). Folding them into one
        # content-fit card sheds a whole card + section gap, which is what keeps
        # Home within the un-maximized content clip (no far-right page scrollbar
        # at the default window size) — see _actions_card.
        section_gap()
        _actions_card(shell)

        if state.data_freshness == "stale":
            section_gap()
            dpg.add_text(STALE_WARNING_HEADLINE, color=shell.COLORS["warn"])
            dpg.add_text(STALE_WARNING_HELPER, color=shell.COLORS["muted"])


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
    """Consolidated actions hub: the emphasized next step + the quick links.

    Merges the former separate "Quick Actions" and "Next step" cards. They
    overlapped — both routed to "Read controller state", and the
    Controller-settings link is already on the Profile card — so a single card
    drops the duplicated chrome + button. It is ``fit=True`` (content-fit) so it
    never grows an inner scrollbar regardless of locale label widths, and
    shedding the second card + its section gap is what brings Home's content
    extent under the un-maximized clip (the far-right page scrollbar at the
    default window size was the operator's primary complaint).

    Health Check (the app's core maintenance action) is emphasized as the
    recommended next step via its lead position + larger 220x40 size — the same
    functional accent the prior CTA used. Every button + target from BOTH former
    cards is preserved: Health Check, Read controller state, Open Controller
    settings, View diagnostics, About.
    """

    with card(fit=True):
        with section(t("home.cta.title")):
            dpg.add_text(t("home.cta.helper"), color=shell.COLORS["muted"], wrap=1100)
            # Primary row: the emphasized maintenance action + read-state. (The
            # default 8px item spacing already separates the helper from the row;
            # the prior explicit spacer here was extra slack trimmed to keep Home
            # under the un-maximized clip.)
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label=t("nav.health_report"),
                    width=220,
                    height=40,
                    callback=lambda: shell.switch_screen("health_report"),
                )
                dpg.add_button(
                    label=t("home.quick.read"),
                    height=40,
                    callback=lambda: shell.refresh_from_controller(),
                )
            dpg.add_spacer(height=SPACE_SM)
            # Secondary row: quick navigation / utility links.
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label=t("home.quick.controller"),
                    callback=lambda: shell.switch_screen("controller"),
                )
                dpg.add_button(
                    label=t("home.quick.diagnostics"),
                    callback=lambda: shell.switch_screen("diagnostics"),
                )
                dpg.add_button(
                    label=t("home.quick.about"),
                    callback=lambda: shell.switch_screen("about"),
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
