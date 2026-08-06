"""Tests for R1 font registration."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

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
                "NotoSansKR-Regular.ttf",
                "NotoSansKR-SemiBold.ttf",
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
                mvFontRangeHint_Korean=456,
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
        self.assertIn(("header", "ko"), handles)
        self.assertIn(("h2", "ko"), handles)
        self.assertIn(("body", "ko"), handles)
        self.assertIn(("helper", "ko"), handles)
        self.assertIn(("mono", "en"), handles)
        self.assertIn(("mono", "zh-CN"), handles)
        self.assertEqual(handles[("mono", "en")], handles[("mono", "zh-CN")])
        # Noto Sans SC and Noto Sans KR both receive Chinese_Full + Korean:
        # SC for picker autonyms; KR so General Punctuation (em dash, ellipsis)
        # in ko strings renders instead of tofu. Four rows each, both ranges.
        self.assertEqual(fake_dpg.add_font_range_hint.call_count, 16)
        self.assertEqual(
            [call.args[0] for call in fake_dpg.add_font_range_hint.call_args_list],
            [123, 456, 123, 456, 123, 456, 123, 456, 123, 456, 123, 456, 123, 456, 123, 456],
        )

    def test_register_fonts_handles_missing_file_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_dpg = SimpleNamespace(
                font_registry=MagicMock(return_value=_Context()),
                add_font=MagicMock(),
                add_font_range_hint=MagicMock(),
                mvFontRangeHint_Chinese_Full=123,
                mvFontRangeHint_Korean=456,
            )
            with patch.object(fonts, "dpg", fake_dpg), patch.object(
                fonts, "_font_dir", return_value=Path(tmpdir)
            ), self.assertLogs("zd_app.ui.fonts", level="WARNING"):
                handles = fonts.register_fonts()

        self.assertEqual(handles, {})
        fake_dpg.add_font.assert_not_called()

    def test_register_fonts_warns_and_skips_only_missing_korean_assets(self) -> None:
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

            fake_dpg = SimpleNamespace(
                font_registry=MagicMock(return_value=_Context()),
                add_font=MagicMock(return_value=10),
                add_font_range_hint=MagicMock(),
                mvFontRangeHint_Chinese_Full=123,
                mvFontRangeHint_Korean=456,
            )
            with patch.object(fonts, "dpg", fake_dpg), patch.object(
                fonts, "_font_dir", return_value=font_dir
            ), patch.object(fonts, "_needs_explicit_cjk_range", return_value=True), self.assertLogs(
                "zd_app.ui.fonts", level="WARNING"
            ) as captured:
                handles = fonts.register_fonts()

        self.assertIn(("body", "en"), handles)
        self.assertIn(("body", "zh-CN"), handles)
        self.assertNotIn(("body", "ko"), handles)
        self.assertIn("Noto Sans KR", "\n".join(captured.output))

    def test_bind_default_font_uses_locale_font(self) -> None:
        fake_dpg = SimpleNamespace(bind_font=MagicMock())
        fonts.FONT_HANDLES[("body", "en")] = 11
        fonts.FONT_HANDLES[("body", "zh-CN")] = 22
        fonts.FONT_HANDLES[("body", "ko")] = 33

        with patch.object(fonts, "dpg", fake_dpg):
            fonts.bind_default_font("en")
            fonts.bind_default_font("zh-CN")
            fonts.bind_default_font("ko")
            fonts.bind_default_font("unshipped")

        fake_dpg.bind_font.assert_has_calls([call(11), call(22), call(33), call(11)])
        self.assertEqual(fake_dpg.bind_font.call_count, 4)


class KoFontRenderIsolatedTests(unittest.TestCase):
    """Real-render gate: Hangul renders non-tofu through the real font registry.

    The mocked tests above patch ``_needs_explicit_cjk_range`` True and so
    cannot see the real-DPG behavior where CJK ranges are otherwise skipped on
    dpg>=2 (Hangul then renders as the fallback/tofu box). The isolated child
    renders the REAL ``register_fonts`` output in a REAL viewport and asserts
    Korean strings are strictly wider than a same-length unmapped control. One
    subprocess per method — a second DPG context in one process hits the known
    teardown segfault. Timeout-wrapped for the native render-hang class."""

    _METHODS = (
        "test_hangul_renders_non_tofu_through_register_fonts",
        "test_general_punctuation_renders_non_tofu_through_register_fonts",
    )

    def test_isolated_real_render_ko_font(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        for method in self._METHODS:
            with self.subTest(method=method):
                test_id = (
                    "tests.isolated_ko_font_render."
                    f"IsolatedKoFontRenderTest.{method}"
                )
                try:
                    result = subprocess.run(
                        [sys.executable, "-m", "unittest", test_id],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                except subprocess.TimeoutExpired as exc:
                    output_parts: list[str] = []
                    for part in (exc.stdout, exc.stderr):
                        if isinstance(part, bytes):
                            output_parts.append(part.decode(errors="replace"))
                        elif part:
                            output_parts.append(part)
                    self.fail(
                        "Isolated ko-font-render child hung (native render hang "
                        "class).\n\nChild output before timeout:\n"
                        + "\n".join(output_parts)
                    )

                output = "\n".join(
                    part for part in (result.stdout, result.stderr) if part
                )
                if any(line.strip() == "OK" for line in output.splitlines()):
                    continue
                self.fail(
                    "Isolated ko-font-render test did not report unittest OK.\n"
                    f"Return code: {result.returncode}\n"
                    f"Command: {sys.executable} -m unittest {test_id}\n\n"
                    f"Child output:\n{output}"
                )


if __name__ == "__main__":
    unittest.main()
