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
) -> None:
    with dpg.group(horizontal=True, tag=f"{tag_prefix}_links"):
        for link in TRUST_FRONT_DOOR_LINKS:
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
