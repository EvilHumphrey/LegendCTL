"""Shared navigation helpers for the in-app trust proof loop."""

from __future__ import annotations

from dataclasses import dataclass

import dearpygui.dearpygui as dpg

from zd_app.i18n import t


TRUST_FRONT_DOOR_FOCUS_ATTR = "_diagnostics_trust_front_door_focus"


@dataclass(frozen=True)
class TrustFrontDoorLink:
    target: str
    label_key: str


TRUST_FRONT_DOOR_LINKS = (
    TrustFrontDoorLink("self_check", "trust_front_door.link.self_check"),
    TrustFrontDoorLink("compat_report", "trust_front_door.link.compat_report"),
    TrustFrontDoorLink("evidence_card", "trust_front_door.link.evidence_card"),
    TrustFrontDoorLink("trust_matrix", "trust_front_door.link.trust_matrix"),
)


def button_tag(prefix: str, target: str) -> str:
    return f"{prefix}_{target}"


def open_trust_surface(shell, target: str) -> None:
    """Navigate to the existing Diagnostics proof-loop surface."""

    shell.diagnostics_active_tab = "guidance"
    setattr(shell, TRUST_FRONT_DOOR_FOCUS_ATTR, target)
    switch_screen = getattr(shell, "switch_screen", None)
    if callable(switch_screen):
        switch_screen("diagnostics")
    else:
        shell.current_screen = "diagnostics"


def add_trust_link_buttons(
    shell,
    *,
    tag_prefix: str,
    button_width: int = 170,
    max_per_row: int | None = None,
) -> None:
    """Render the front-door link buttons for a call site.

    ``max_per_row`` opts a call site into wrapping: links render in successive
    horizontal rows of at most that many buttons. Home uses two per row at
    shipped text size and stacks enlarged labels so every link remains
    visible. The default ``None`` keeps the single-row structure unchanged:
    the Diagnostics status strip
    is height-budgeted at 66px so it must NEVER grow a second row.
    """

    if max_per_row is None or max_per_row >= len(TRUST_FRONT_DOOR_LINKS):
        with dpg.group(horizontal=True, tag=f"{tag_prefix}_links"):
            _add_link_buttons(shell, TRUST_FRONT_DOOR_LINKS, tag_prefix, button_width)
        return
    per_row = max(1, max_per_row)
    with dpg.group(tag=f"{tag_prefix}_links"):
        for start in range(0, len(TRUST_FRONT_DOOR_LINKS), per_row):
            with dpg.group(
                horizontal=True,
                tag=f"{tag_prefix}_links_row_{start // per_row}",
            ):
                _add_link_buttons(
                    shell,
                    TRUST_FRONT_DOOR_LINKS[start : start + per_row],
                    tag_prefix,
                    button_width,
                )


def _add_link_buttons(shell, links, tag_prefix: str, button_width: int) -> None:
    for link in links:
        dpg.add_button(
            label=t(link.label_key),
            tag=button_tag(tag_prefix, link.target),
            width=button_width,
            callback=_callback_for(shell, link.target),
        )


def _callback_for(shell, target: str):
    return lambda _sender=None, _app_data=None, _user_data=None: open_trust_surface(
        shell,
        target,
    )


__all__ = [
    "TRUST_FRONT_DOOR_FOCUS_ATTR",
    "TRUST_FRONT_DOOR_LINKS",
    "TrustFrontDoorLink",
    "add_trust_link_buttons",
    "button_tag",
    "open_trust_surface",
]
