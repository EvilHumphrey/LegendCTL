"""Tests for R1 font registration."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from zd_app.ui import fonts


class _Context:
    def __enter__(self):
        return 99

    def __exit__(self, exc_type, exc, tb):
        return False


class FontTests(unittest.TestCase):
    def setUp(self) -> None:
        # Clear both before and after every test. test_register_fonts_loads_
        # all_three_locales populates FONT_HANDLES via mocked dpg.add_font,
        # producing fake IDs that don't exist in any real DPG context. Without
        # a teardown the stale IDs leak into the rest of the discover run,
        # where typography helpers (screen_title / section_title / helper_text,
        # universal across screens) call bind_item_font with
        # them and raise "Item not found".
        fonts.FONT_HANDLES.clear()
        self.addCleanup(fonts.FONT_HANDLES.clear)

    def test_register_fonts_loads_all_three_locales(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            font_dir = Path(tmpdir)
            for name in (
                "Inter-Regular.ttf",
                "Inter-SemiBold.ttf",
                "JetBrainsMono-Regular.ttf",
                "NotoSansSC-Regular.otf",
                "NotoSansSC-SemiBold.otf",
            ):
                (font_dir / name).write_bytes(b"fake")

            counter = {"value": 10}

            def add_font(_path, _size):
                counter["value"] += 1
                return counter["value"]

            fake_dpg = SimpleNamespace(
                font_registry=MagicMock(return_value=_Context()),
                add_font=MagicMock(side_effect=add_font),
                add_font_range_hint=MagicMock(),
                mvFontRangeHint_Chinese_Full=123,
            )

            with patch.object(fonts, "dpg", fake_dpg), patch.object(
                fonts, "_font_dir", return_value=font_dir
            ), patch.object(fonts, "_needs_explicit_cjk_range", return_value=True):
                handles = fonts.register_fonts()

        self.assertIn(("header", "en"), handles)
        self.assertIn(("h2", "en"), handles)
        self.assertIn(("body", "en"), handles)
        self.assertIn(("helper", "en"), handles)
        self.assertIn(("header", "zh-CN"), handles)
        self.assertIn(("h2", "zh-CN"), handles)
        self.assertIn(("body", "zh-CN"), handles)
        self.assertIn(("helper", "zh-CN"), handles)
        self.assertIn(("mono", "en"), handles)
        self.assertIn(("mono", "zh-CN"), handles)
        self.assertEqual(handles[("mono", "en")], handles[("mono", "zh-CN")])
        # 4 CJK fonts now load via _add_font_with_cjk: header, h2, body, helper.
        self.assertEqual(fake_dpg.add_font_range_hint.call_count, 4)

    def test_register_fonts_handles_missing_file_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_dpg = SimpleNamespace(
                font_registry=MagicMock(return_value=_Context()),
                add_font=MagicMock(),
                add_font_range_hint=MagicMock(),
                mvFontRangeHint_Chinese_Full=123,
            )
            with patch.object(fonts, "dpg", fake_dpg), patch.object(
                fonts, "_font_dir", return_value=Path(tmpdir)
            ), self.assertLogs("zd_app.ui.fonts", level="WARNING"):
                handles = fonts.register_fonts()

        self.assertEqual(handles, {})
        fake_dpg.add_font.assert_not_called()

    def test_bind_default_font_uses_locale_font(self) -> None:
        fake_dpg = SimpleNamespace(bind_font=MagicMock())
        fonts.FONT_HANDLES[("body", "en")] = 11
        fonts.FONT_HANDLES[("body", "zh-CN")] = 22

        with patch.object(fonts, "dpg", fake_dpg):
            fonts.bind_default_font("zh-CN")

        fake_dpg.bind_font.assert_called_once_with(22)


if __name__ == "__main__":
    unittest.main()
