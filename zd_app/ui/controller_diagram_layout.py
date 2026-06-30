"""Shared controller-diagram geometry for the LegendCTL UI."""

from __future__ import annotations

from zd_app.services.settings_service import MacroSlot


# Compact back-view canvas used by the Controller -> Buttons paddle map.
# Pixel literals: the app has no DPI scaling layer, so shared consumers should
# derive fixed coordinates from this map rather than redefining a second layout.
BACK_DIAGRAM_W = 300
BACK_DIAGRAM_H = 220
BACK_PADDLE_R = 14
BACK_LABEL_SIZE = 13

# Back-view physical positions, MIRRORED: drawn as you see the controller when
# you flip it over to look at its back. Diagram image-LEFT = player's RIGHT.
# See _spec_controller_diagram_2026-06-27 section 6 for the manual-confirmed
# layout. M3/M4 are front buttons and deliberately not included in this map.
BACK_PADDLE_POS: dict[MacroSlot, tuple[int, int]] = {
    MacroSlot.RK: (95, 38),
    MacroSlot.LK: (205, 38),
    MacroSlot.RM: (122, 102),
    MacroSlot.LM: (178, 102),
    MacroSlot.M2: (120, 144),
    MacroSlot.M1: (180, 144),
}

# Top-edge claws are projected onto a flat back-view canvas, so they carry the
# visible "*" marker in every consumer.
BACK_PADDLE_APPROX: frozenset[MacroSlot] = frozenset({MacroSlot.LK, MacroSlot.RK})


__all__ = [
    "BACK_DIAGRAM_H",
    "BACK_DIAGRAM_W",
    "BACK_LABEL_SIZE",
    "BACK_PADDLE_APPROX",
    "BACK_PADDLE_POS",
    "BACK_PADDLE_R",
]
