"""Tests for R1 visual theme primitives."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from zd_app.ui import themes


def _relative_luminance(rgb) -> float:
    """WCAG 2.x relative luminance for an (R, G, B[, A]) 0-255 tuple."""

    def lin(channel: int) -> float:
        c = channel / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb[:3]
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(fg, bg) -> float:
    """WCAG contrast ratio between two 0-255 colors (order-independent)."""

    l1, l2 = _relative_luminance(fg), _relative_luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


class _Context:
    def __init__(self, value=None):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, exc_type, exc, tb):
        return False


class ThemeTests(unittest.TestCase):
    def test_colors_dict_has_all_required_keys(self) -> None:
        required = {
            "bg.base",
            "bg.surface",
            "bg.raised",
            "border.subtle",
            "border.strong",
            "text.primary",
            "text.secondary",
            "text.muted",
            "accent.primary",
            "accent.hover",
            "accent.muted",
            "success",
            "warning",
            "error",
        }

        self.assertTrue(required.issubset(themes.COLORS))

    def test_legacy_color_aliases_resolve(self) -> None:
        self.assertEqual(themes.LEGACY_COLOR_ALIASES["accent"], (16, 185, 129, 255))
        self.assertEqual(themes.LEGACY_COLOR_ALIASES["good"], themes.COLORS["success"])
        self.assertEqual(themes.LEGACY_COLOR_ALIASES["bad"], themes.COLORS["error"])

    def test_register_global_theme_runs_without_error(self) -> None:
        fake_dpg = SimpleNamespace(
            theme=MagicMock(return_value=_Context(42)),
            theme_component=MagicMock(return_value=_Context()),
            add_theme_color=MagicMock(),
            add_theme_style=MagicMock(),
            bind_theme=MagicMock(),
            mvAll=1,
            mvThemeCol_WindowBg=2,
            mvThemeCol_ChildBg=3,
            mvThemeCol_FrameBg=4,
            mvThemeCol_FrameBgHovered=5,
            mvThemeCol_FrameBgActive=6,
            mvThemeCol_Button=7,
            mvThemeCol_ButtonHovered=8,
            mvThemeCol_ButtonActive=9,
            mvThemeCol_Text=10,
            mvThemeCol_TextDisabled=11,
            mvThemeCol_Border=12,
            mvThemeCol_Separator=13,
            mvThemeCol_SliderGrab=14,
            mvThemeCol_SliderGrabActive=15,
            mvThemeCol_CheckMark=16,
            mvThemeCol_Header=17,
            mvThemeCol_HeaderHovered=18,
            mvThemeCol_HeaderActive=19,
            mvThemeCol_Tab=20,
            mvThemeCol_TabHovered=21,
            mvThemeCol_TabActive=22,
            mvStyleVar_WindowRounding=23,
            mvStyleVar_FrameRounding=24,
            mvStyleVar_ChildRounding=25,
            mvStyleVar_ItemSpacing=26,
            mvStyleVar_FramePadding=27,
            mvStyleVar_WindowPadding=28,
        )

        with patch.object(themes, "dpg", fake_dpg):
            self.assertEqual(themes.register_global_theme(), 42)

        fake_dpg.bind_theme.assert_called_once_with(42)


class ReadabilityFloorTests(unittest.TestCase):
    """Pins the 2026-05-31 readability-floor targets so a future palette or
    type-scale tweak can't silently regress contrast or shrink small text."""

    def test_small_end_font_floor_and_hierarchy(self) -> None:
        # Body + helper (operational / caption tiers) carry the 2026-06-22
        # readability floor: helper >= 14 and body >= 15 so body/description text
        # reads comfortably at 100% display scale on a 1440p panel (the prior
        # 14/13 read small + thin). The size hierarchy h1 > h2 > body >= helper
        # still holds. These floors guard the readability bump against a future
        # tweak silently shrinking it back down.
        self.assertGreaterEqual(themes.FONT_SIZE["helper"], 14)
        self.assertGreaterEqual(themes.FONT_SIZE["body"], 15)
        self.assertGreaterEqual(
            themes.FONT_SIZE["body"], themes.FONT_SIZE["helper"]
        )
        self.assertGreater(
            themes.FONT_SIZE["header.h2"], themes.FONT_SIZE["body"]
        )
        self.assertGreater(
            themes.FONT_SIZE["header.h1"], themes.FONT_SIZE["header.h2"]
        )

    def test_secondary_text_clears_wcag_aa_on_dark_surfaces(self) -> None:
        # Secondary carries operational data; require >= 4.5:1 against the base
        # background and both card fills (raised is the brightest container).
        for surface in ("bg.base", "bg.card", "bg.raised"):
            cr = _contrast(themes.COLORS["text.secondary"], themes.COLORS[surface])
            self.assertGreaterEqual(
                cr, 4.5,
                f"text.secondary vs {surface} = {cr:.2f}:1, below WCAG-AA 4.5:1",
            )

    def test_muted_text_is_legible_but_below_secondary(self) -> None:
        # Muted is the dimmer caption tier: legible (>= 3:1 on the card fill)
        # yet clearly lower-contrast than secondary so the hierarchy reads.
        muted_cr = _contrast(themes.COLORS["text.muted"], themes.COLORS["bg.card"])
        secondary_cr = _contrast(
            themes.COLORS["text.secondary"], themes.COLORS["bg.card"]
        )
        self.assertGreaterEqual(
            muted_cr, 3.0,
            f"text.muted vs bg.card = {muted_cr:.2f}:1, below the 3:1 floor",
        )
        self.assertLess(
            muted_cr, secondary_cr,
            f"text.muted ({muted_cr:.2f}:1) must stay below text.secondary "
            f"({secondary_cr:.2f}:1) so the tiers stay distinguishable",
        )

    def test_primary_text_is_brightest_tier(self) -> None:
        # Sanity: primary > secondary > muted by luminance, the intended scale.
        prim = _relative_luminance(themes.COLORS["text.primary"])
        sec = _relative_luminance(themes.COLORS["text.secondary"])
        muted = _relative_luminance(themes.COLORS["text.muted"])
        self.assertGreater(prim, sec)
        self.assertGreater(sec, muted)


if __name__ == "__main__":
    unittest.main()
