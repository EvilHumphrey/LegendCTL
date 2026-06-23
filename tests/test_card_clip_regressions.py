"""Regression guards for the DPG card-clip lane (2026-06-21).

Several screen cards were built as ``dpg.child_window(height=<fixed px>)`` whose
hand-measured heights still clipped real content at the SHIPPED fonts (Inter 14
body / SemiBold 18 titles, Noto Sans SC for zh-CN). A real-viewport probe
(``tools/diag_dpg_card_clip.py``) rendered every screen with realistic content
in en + zh-CN at three window sizes and measured each card's overshoot.

Most clips were fixed by converting the card to content-fit (``auto_resize_y`` —
asserted in each screen's own test). The remaining cards are equal-height pairs
that must stay even (Modules passport / compare) or compact-by-design (Home top
row, Wear Ledger sparkline); those keep a SHARED fixed height, bumped to clear
the measured content floor. This module pins those measured floors so a future
shrink that would re-clip fails loudly here, and pins the What-To-Trust wrap so
it can't regress to a value wider than the minimum-window content area.

Pure-constant assertions (no DPG context needed): the numbers encode empirical
probe measurements, cited inline.
"""

from __future__ import annotations

import inspect
import re
import unittest

from zd_app.ui.screens import diagnostics as diagnostics_screen
from zd_app.ui.screens import home as home_screen
from zd_app.ui.screens import modules as modules_screen
from zd_app.ui.screens import readiness_check as readiness_check_screen
from zd_app.ui.screens import wear_ledger as wear_ledger_screen


def _first_child_window_args(func) -> str:
    """Return the argument text of the first ``dpg.child_window(...)`` call.

    Reads the builder's own source (the cards under test are inline
    ``dpg.child_window`` calls, not module-level height constants), then walks
    balanced parens so a multi-line call is captured whole. Lets the fit-contract
    guards below assert on the actual kwargs without standing up a DPG context.
    """

    src = inspect.getsource(func)
    marker = "dpg.child_window("
    start = src.index(marker) + len(marker)
    depth = 1
    i = start
    while depth and i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
        i += 1
    return src[start : i - 1]


class SharedHeightFloorTests(unittest.TestCase):
    """Equal-height / compact cards must clear their measured content floor."""

    def test_modules_side_card_clears_assigned_content_floor(self) -> None:
        # Probe: an assigned passport card (side label + 5 info rows + trend
        # badge + 3 rows of two buttons) measured 396px of content; the prior
        # 260 clipped the bottom button row by 136px. Shared so the left/right
        # pair stays even.
        self.assertGreaterEqual(
            modules_screen._SIDE_CARD_HEIGHT, 396,
            "Modules passport card height fell below the 396px content floor — "
            "the assigned-state buttons will clip again.",
        )

    def test_modules_compare_card_clears_populated_content_floor(self) -> None:
        # Probe: a populated compare column (heading + module rows + latest
        # status + full fingerprint detail + sparkline) measured 578px; the
        # prior 360 clipped by 218px.
        self.assertGreaterEqual(
            modules_screen._COMPARE_CARD_HEIGHT, 578,
            "Modules compare column height fell below the 578px content floor.",
        )

    def test_home_top_card_clears_profile_content_floor(self) -> None:
        # Probe: the taller top card (Profile: 3 metrics + NO_AUTOMATION badge
        # + 2 buttons) measured 248px in en AND zh-CN; the prior 200 clipped its
        # buttons by 48px. Shared so the Connection/Profile row stays even.
        self.assertGreaterEqual(
            home_screen._TOP_CARD_HEIGHT, 248,
            "Home top-card shared height fell below the 248px Profile floor.",
        )

    def test_wear_ledger_sparkline_clears_content_floor_but_stays_compact(self) -> None:
        # Probe: the verdict-trend card measured ~146px of content; the prior
        # 140 clipped it by 6px. It is deliberately a pinned COMPACT height (not
        # fit) so it doesn't reserve dead space above the event-log table, so it
        # is bounded on both sides.
        self.assertGreaterEqual(
            wear_ledger_screen._SPARKLINE_CARD_HEIGHT, 146,
            "Wear Ledger sparkline height fell below the 146px content floor.",
        )
        self.assertLessEqual(
            wear_ledger_screen._SPARKLINE_CARD_HEIGHT, 200,
            "Wear Ledger sparkline must stay compact (<=200) so it doesn't push "
            "the event-log table below the fold.",
        )


class TrustCardWrapTests(unittest.TestCase):
    """The What-To-Trust body wrap must fit the minimum-window content width."""

    def test_trust_body_wrap_fits_minimum_window(self) -> None:
        # The trust card now spans its own full-width row. At the minimum 1180
        # window the content region is ~875px wide; the wrap must stay below
        # that (with margin) so the prose never overruns the right edge — the
        # bug that the prior wrap=780-in-a-187px-column produced.
        self.assertLessEqual(
            diagnostics_screen._TRUST_BODY_WRAP, 860,
            "What-To-Trust wrap exceeds the minimum-window content width and "
            "will clip horizontally again.",
        )
        self.assertGreater(
            diagnostics_screen._TRUST_BODY_WRAP, 0,
            "What-To-Trust wrap must be a positive pixel width.",
        )


class FitContractTests(unittest.TestCase):
    """Stacked cards that vary in height must stay CONTENT-FIT, never re-clip.

    The 2026-06-21 follow-up probe reached states the original card-clip probe
    missed — an EXPANDED fingerprint (active + archived), the trend attention
    banner / per-metric rows, the archived list, and the readiness DONE verdict —
    and measured each fixed-height card clipping its content vertically (overshoot
    in px, both locales, all three window sizes):

        modules attention banner  (was h=80)   V=24px
        modules fingerprint row   (was 60+24*8) V=116px   (active AND archived)
        modules archived row      (was h=130)  V=34px
        modules trend metric row  (was h=130)  V=34px
        readiness verdict card    (was h=200)  V=14px

    Each was converted to the DPG-2.x content-fit contract (``auto_resize_y=True``
    + ``autosize_y=False``, no fixed ``height``) so the card shrinks/grows to its
    content and can never grow an inner vertical scrollbar. These are stacked
    siblings in a vertical list (NOT equal-height pairs), so fitting is safe.
    A future edit that reintroduces a fixed height — or drops the fit flag —
    fails here loudly.
    """

    # (label, builder function) for every card converted in this lane.
    _FIT_CARDS = (
        ("modules attention banner", modules_screen._build_trend_attention_banner),
        ("modules fingerprint row (active)", modules_screen._build_fingerprint_row),
        ("modules archived row", modules_screen._build_archived_row),
        (
            "modules fingerprint row (archived)",
            modules_screen._build_archived_fingerprint_row,
        ),
        ("modules trend metric row", modules_screen._build_trend_metric_row),
        ("readiness verdict card", readiness_check_screen._build_verdict_card),
    )

    def test_converted_cards_use_content_fit_contract(self) -> None:
        for label, func in self._FIT_CARDS:
            with self.subTest(card=label):
                args = _first_child_window_args(func)
                self.assertIn(
                    "auto_resize_y=True", args,
                    f"{label}: lost the DPG-2.x content-fit flag — it will clip "
                    "its content vertically again.",
                )
                self.assertIn(
                    "autosize_y=False", args,
                    f"{label}: must suppress the legacy autosize_y fill flag so "
                    "auto_resize_y governs the height.",
                )

    def test_converted_cards_have_no_fixed_height(self) -> None:
        for label, func in self._FIT_CARDS:
            with self.subTest(card=label):
                args = _first_child_window_args(func)
                self.assertNotRegex(
                    args, r"\bheight\s*=",
                    f"{label}: a fixed height on a content-fit card reintroduces "
                    "the clip this lane removed.",
                )

    def test_expanded_fingerprint_rows_dropped_magic_height_formula(self) -> None:
        # Both fingerprint-row builders carried ``row_height = 60 + (24 * 8 ...)``
        # which under-measured the 8-metric expanded detail by ~116px. The
        # content-fit conversion deletes the formula outright.
        for label, func in (
            ("active", modules_screen._build_fingerprint_row),
            ("archived", modules_screen._build_archived_fingerprint_row),
        ):
            with self.subTest(row=label):
                src = inspect.getsource(func)
                self.assertNotIn(
                    "24 * 8", src,
                    f"{label} fingerprint row resurrected the 60+24*8 magic "
                    "height — the expanded detail will clip again.",
                )

    def test_verdict_card_keeps_its_width_budget(self) -> None:
        # Width is intentional (the bullets wrap at 480 inside a 520-wide card);
        # only the HEIGHT became fit. Guard the width so a future edit can't drop
        # it and change the wrap behaviour.
        args = _first_child_window_args(readiness_check_screen._build_verdict_card)
        self.assertIn(
            "width=520", args,
            "readiness verdict card lost its 520 width — the 480 bullet wrap "
            "budget would change.",
        )

    def test_home_stacked_cards_use_content_fit(self) -> None:
        # Home's variable-height stacked cards — Recent Activity and the merged
        # actions card (former "Quick Actions" + "Next step") — are content-fit
        # so they never grow an inner scrollbar, and the merge is what keeps
        # Home's content extent under the un-maximized clip (measured 950 -> 816
        # at the shipped fonts, DPG 2.3). They build via the card(fit=True)
        # helper, not a fixed card(height=...).
        for label, func in (
            ("recent activity", home_screen._recent_activity),
            ("actions card", home_screen._actions_card),
        ):
            with self.subTest(card=label):
                src = inspect.getsource(func)
                self.assertIn(
                    "card(fit=True)", src,
                    f"home {label}: must build via card(fit=True) so it can't clip.",
                )
                self.assertNotRegex(
                    src, r"card\(height=",
                    f"home {label}: a fixed card height reintroduces the clip / "
                    "page-overflow this trim removed.",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
