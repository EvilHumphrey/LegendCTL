"""Visual theme: colors, font sizes, and Dear PyGui theme primitives."""

from __future__ import annotations

import dearpygui.dearpygui as dpg


COLORS = {
    "bg.base": (14, 17, 22, 255),
    "bg.surface": (26, 31, 42, 255),
    # Card surface sits one subtle step above bg.surface (the content-region
    # fill) so component cards read as raised panels, while staying below
    # bg.raised so buttons/frames inside a card still lift against it.
    "bg.card": (32, 38, 50, 255),
    "bg.raised": (38, 44, 58, 255),
    "border.subtle": (45, 51, 64, 255),
    "border.strong": (61, 70, 88, 255),
    "text.primary": (229, 233, 240, 255),
    # Readability floor (2026-05-31 UX pass). Secondary carries operational
    # data (table rows, metric values, diagnostics prose) so it clears WCAG-AA
    # 4.5:1 against every container with headroom — measured vs bg.base/surface/
    # card/raised: 8.6 / 7.5 / 6.9 / 6.4:1. Muted is the dimmer caption tier;
    # raised to stay legible (~4.0–5.5:1) while still reading clearly below
    # secondary so the three-tier hierarchy (primary > secondary > muted) holds.
    "text.secondary": (166, 176, 191, 255),
    "text.muted": (128, 139, 156, 255),
    "accent.primary": (46, 155, 255, 255),
    "accent.hover": (91, 184, 255, 255),
    "accent.muted": (16, 60, 100, 255),
    "success": (74, 222, 128, 255),
    "warning": (251, 191, 36, 255),
    "error": (248, 113, 113, 255),
}

# Type scale (px). fonts.py rasterizes one font per (purpose, locale) at the
# size keyed here, so every value below is a real loaded handle — never
# reference a size that isn't in this dict. Readability floor (2026-06-22 UX
# pass): bumped body 14->15 and helper 13->14 — at 100% display scale on a 1440p
# panel the prior 14/13 read small + thin for body/description prose. The small
# end (helper) now sits at 14 so muted helper/caption text stays comfortable;
# the hierarchy h1(24) > h2(18) > body(15) > helper(14) is preserved (h2 keeps a
# clear step above body via +3px and SemiBold weight).
FONT_SIZE = {
    "header.h1": 24,
    "header.h2": 18,
    "body": 15,
    "helper": 14,
    "mono": 13,
}

# Spacing scale (pixels). Use these instead of ad-hoc add_spacer magic numbers
# so vertical rhythm stays consistent across screens. SPACE_LG is the standard
# gap between sections (see section_gap()).
SPACE_SM = 4
SPACE_MD = 8
SPACE_LG = 16
SPACE_XL = 24

LEGACY_COLOR_ALIASES = {
    "bg": COLORS["bg.base"],
    "panel": COLORS["bg.surface"],
    "panel_alt": COLORS["bg.raised"],
    "accent": COLORS["accent.primary"],
    "accent_hover": COLORS["accent.hover"],
    "muted": COLORS["text.secondary"],
    "warn": COLORS["warning"],
    "bad": COLORS["error"],
    "good": COLORS["success"],
    "error": COLORS["error"],
    "success": COLORS["success"],
    "text": COLORS["text.primary"],
}


def section_gap() -> None:
    """Add the standard vertical gap between two sections of a screen."""

    dpg.add_spacer(height=SPACE_LG)


def register_global_theme() -> int:
    """Build and bind the global Dear PyGui theme."""

    with dpg.theme() as theme_id:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, COLORS["bg.base"])
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, COLORS["bg.surface"])
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, COLORS["bg.surface"])
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, COLORS["bg.raised"])
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, COLORS["bg.raised"])
            dpg.add_theme_color(dpg.mvThemeCol_Button, COLORS["bg.raised"])
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, COLORS["accent.muted"])
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COLORS["accent.primary"])
            dpg.add_theme_color(dpg.mvThemeCol_Text, COLORS["text.primary"])
            dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, COLORS["text.muted"])
            dpg.add_theme_color(dpg.mvThemeCol_Border, COLORS["border.subtle"])
            dpg.add_theme_color(dpg.mvThemeCol_Separator, COLORS["border.subtle"])
            dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, COLORS["accent.primary"])
            dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, COLORS["accent.hover"])
            dpg.add_theme_color(dpg.mvThemeCol_CheckMark, COLORS["accent.primary"])
            # Disclosure widgets (collapsing_header / tree_node) AND selectables
            # share these Header colors. Selected/open state is carried by the
            # RESTING Header (bg.raised) — not by hover/active — so muting the
            # hover + active highlights to a calm neutral lift (border.strong)
            # instead of the bright accent green keeps the "Exact values"
            # sensitivity panel, the About "Licenses" sections, and the Safe
            # Import "advanced diff" disclosure from flashing a jarring full-width
            # green slab on hover, while still giving clear hover feedback. The
            # selectable's SELECTED row still shows via the unchanged resting
            # Header, so list selection reads exactly as before.
            dpg.add_theme_color(dpg.mvThemeCol_Header, COLORS["bg.raised"])
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, COLORS["border.strong"])
            dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, COLORS["border.strong"])
            dpg.add_theme_color(dpg.mvThemeCol_Tab, COLORS["bg.raised"])
            dpg.add_theme_color(dpg.mvThemeCol_TabHovered, COLORS["accent.muted"])
            dpg.add_theme_color(dpg.mvThemeCol_TabActive, COLORS["accent.primary"])

            dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 6.0)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4.0)
            dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 4.0)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 8)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 10, 6)
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 18, 18)

    dpg.bind_theme(theme_id)
    return theme_id
