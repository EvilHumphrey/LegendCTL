"""Honest display formatting for cached controller button bindings.

The Buttons tab and Live Verify inspector both surface the same cached
``button_bindings`` snapshot. Keep the mapping-to-text decision here so the
H1 honest-abstention guard (unmodeled kinds render as raw bytes, never a
fabricated button name) cannot drift between screens.
"""

from __future__ import annotations

from dataclasses import dataclass

from zd_app.i18n import t
from zd_app.services.settings_service import (
    ButtonMapping,
    ButtonSlot,
    ControllerButtonTarget,
)


UNKNOWN_BINDING_GLYPH = "—"


@dataclass(frozen=True)
class ButtonBindingDisplay:
    text: str
    color_role: str
    show_unknown_tooltip: bool = False
    remapped_tag: str = ""


def slot_identity_target(slot: ButtonSlot) -> ControllerButtonTarget | None:
    """Return the target that means ``slot`` is unremapped, if modeled."""

    return ControllerButtonTarget.__members__.get(slot.name)


def format_button_binding(
    slot: ButtonSlot, mapping: ButtonMapping | None
) -> ButtonBindingDisplay:
    """Format one cached button binding for display, without DPG access."""

    if mapping is None:
        return ButtonBindingDisplay(
            text=UNKNOWN_BINDING_GLYPH,
            color_role="muted",
            show_unknown_tooltip=True,
        )

    if mapping.target_kind != 0x01 or mapping.target_low != 0x00:
        return ButtonBindingDisplay(
            text=(
                f"kind=0x{mapping.target_kind:02X} "
                f"low=0x{mapping.target_low:02X} "
                f"val=0x{mapping.target_value:02X}"
            ),
            color_role="accent",
        )

    try:
        target = ControllerButtonTarget(mapping.target_value)
    except ValueError:
        return ButtonBindingDisplay(
            text=f"0x{mapping.target_value:02X}",
            color_role="accent",
        )

    identity = slot_identity_target(slot)
    is_remap = identity is None or target.value != identity.value
    if is_remap:
        return ButtonBindingDisplay(
            text=target.name,
            color_role="accent",
            remapped_tag=t("controller.buttons.current.remapped_tag"),
        )
    return ButtonBindingDisplay(text=target.name, color_role="muted")


__all__ = [
    "ButtonBindingDisplay",
    "UNKNOWN_BINDING_GLYPH",
    "format_button_binding",
    "slot_identity_target",
]
