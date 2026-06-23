"""Tests for the typography type-scale helpers (screen/section title + helper)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from zd_app.i18n import set_locale
from zd_app.ui import fonts, typography
from zd_app.ui.themes import COLORS


def _fake_dpg(item_id: int = 777):
    return SimpleNamespace(
        add_text=MagicMock(return_value=item_id),
        bind_item_font=MagicMock(),
        # The font-bind guard checks the handle still exists before binding
        # (mirrors components._bind_card_theme); default to "live" so the
        # happy-path tests bind as before.
        does_item_exist=MagicMock(return_value=True),
    )


class TypographyTests(unittest.TestCase):
    def setUp(self) -> None:
        fonts.FONT_HANDLES.clear()
        fonts.FONT_HANDLES.update(
            {
                ("header", "en"): 24,
                ("h2", "en"): 18,
                ("helper", "en"): 12,
                ("h2", "zh-CN"): 188,
            }
        )
        self.addCleanup(fonts.FONT_HANDLES.clear)
        self.addCleanup(set_locale, "en")
        set_locale("en")

    def test_screen_title_binds_h1_header_font_in_primary(self) -> None:
        fake = _fake_dpg(item_id=501)
        with patch.object(typography, "dpg", fake):
            item = typography.screen_title("Home")

        self.assertEqual(item, 501)
        fake.add_text.assert_called_once_with("Home", color=COLORS["text.primary"])
        fake.bind_item_font.assert_called_once_with(501, 24)

    def test_section_title_binds_h2_font_in_primary(self) -> None:
        fake = _fake_dpg(item_id=502)
        with patch.object(typography, "dpg", fake):
            item = typography.section_title("Connection")

        self.assertEqual(item, 502)
        fake.add_text.assert_called_once_with("Connection", color=COLORS["text.primary"])
        fake.bind_item_font.assert_called_once_with(502, 18)

    def test_helper_text_binds_helper_font_in_secondary(self) -> None:
        fake = _fake_dpg(item_id=503)
        with patch.object(typography, "dpg", fake):
            typography.helper_text("Read the controller first.")

        fake.add_text.assert_called_once_with(
            "Read the controller first.", color=COLORS["text.secondary"]
        )
        fake.bind_item_font.assert_called_once_with(503, 12)

    def test_section_title_uses_active_locale_for_cjk_font(self) -> None:
        set_locale("zh-CN")
        fake = _fake_dpg(item_id=504)
        with patch.object(typography, "dpg", fake):
            typography.section_title("连接")  # "Connection" in zh-CN

        fake.bind_item_font.assert_called_once_with(504, 188)

    def test_explicit_locale_overrides_active_locale(self) -> None:
        fake = _fake_dpg(item_id=505)
        with patch.object(typography, "dpg", fake):
            typography.section_title("X", locale="zh-CN")

        fake.bind_item_font.assert_called_once_with(505, 188)

    def test_missing_font_skips_bind_and_still_returns_item(self) -> None:
        fonts.FONT_HANDLES.clear()  # headless: register_fonts() never ran
        fake = _fake_dpg(item_id=506)
        with patch.object(typography, "dpg", fake):
            item = typography.section_title("X")

        self.assertEqual(item, 506)
        fake.add_text.assert_called_once()
        fake.bind_item_font.assert_not_called()

    def test_stale_font_id_skips_bind_and_still_returns_item(self) -> None:
        # A font handle exists in FONT_HANDLES but its id was recycled by the
        # shared-context shim (does_item_exist -> False). The guard must skip
        # the bind (binding a stale id raises "Item not found") yet still
        # return the rendered item — mirrors components._bind_card_theme.
        fake = _fake_dpg(item_id=508)
        fake.does_item_exist = MagicMock(return_value=False)
        with patch.object(typography, "dpg", fake):
            item = typography.section_title("X")

        self.assertEqual(item, 508)
        fake.add_text.assert_called_once()
        fake.does_item_exist.assert_called_once_with(18)  # ("h2", "en") handle
        fake.bind_item_font.assert_not_called()

    def test_extra_kwargs_forwarded_to_add_text(self) -> None:
        fake = _fake_dpg(item_id=507)
        with patch.object(typography, "dpg", fake):
            typography.section_title("X", tag="my_title")

        fake.add_text.assert_called_once_with(
            "X", tag="my_title", color=COLORS["text.primary"]
        )


if __name__ == "__main__":
    unittest.main()
