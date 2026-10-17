"""Isolated REAL-render gate: Chinese (Han) renders non-tofu under real DPG.

Runs OUTSIDE unittest discovery (no ``test_`` prefix), invoked per-method in a
subprocess by ``test_fonts`` — one DPG context per process (a second
create/destroy cycle hits the known teardown segfault).

Why this exists (2026-07-20 zh hardening): the font registration is otherwise
only mock-tested, so it cannot catch real-DPG behavior when CJK auto-load is
absent. Measured on dpg 2.2: WITHOUT an explicit glyph range, CJK text can
render as the fallback/tofu box, and ``get_text_size`` for such a string equals
a same-length private-use (U+E000, guaranteed unmapped) control. WITH the
Chinese range hint (the ``force_ranges`` path this gate exercises through the
real ``register_fonts``), Han glyphs rasterize at real, wider advances. This
gate renders the REAL font registry in a REAL viewport and asserts every
sampled Chinese string is strictly wider than its fallback control — i.e. the
glyphs are present in the atlas and drawn, not tofu.

HONEST LIMITS (stated per the mission):
- ``get_text_size`` reflects the atlas glyph ADVANCE, proving the glyph is built
  with a real (non-fallback) width. It does not pixel-verify glyph SHAPE; this
  gate proves only non-fallback atlas coverage for the sampled codepoints.
- This runs on the agent's dpg 2.2; production pins dpg 2.3. The
  ``force_ranges`` fix registers the Chinese range explicitly on BOTH, so the
  render path does not depend on version-specific CJK auto-load. A final
  glyph-shape eyeball on the real 2.3 build is still the recommended last
  confirmation.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import dearpygui.dearpygui as dpg

from zd_app.ui.fonts import font_for, register_fonts


_PUA = "\ue000"  # Private Use Area: no font maps it -> fallback/tofu advance.


def _han_only(text: str) -> str:
    def is_han(ch: str) -> bool:
        codepoint = ord(ch)
        return (
            0x3400 <= codepoint <= 0x4DBF  # CJK Unified Ideographs Extension A
            or 0x4E00 <= codepoint <= 0x9FFF  # CJK Unified Ideographs
            or 0xF900 <= codepoint <= 0xFAFF  # CJK Compatibility Ideographs
        )

    return "".join(ch for ch in text if is_han(ch))


class IsolatedZhFontRenderTest(unittest.TestCase):
    def _boot(self):
        register_fonts()
        dpg.create_viewport(title="zh font render gate", width=520, height=320)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        for _ in range(15):
            dpg.render_dearpygui_frame()

    def test_han_renders_non_tofu_through_register_fonts(self) -> None:
        dpg.create_context()
        try:
            self._boot()
            zh_body = font_for("body", "zh-CN")
            self.assertIsNotNone(zh_body, "zh-CN body font handle not registered")

            zh = json.loads(
                Path("zd_app/i18n/locales/zh-CN.json").read_text(encoding="utf-8")
            )
            # A short, a medium, and the long disclaimer — coincidental
            # advance-equality across all three is impossible.
            samples = [
                zh["language.zh-CN"],
                zh["trust_matrix.label.policy"],
                zh["about.disclaimer"],
            ]
            for original in samples:
                han = _han_only(original)
                self.assertGreaterEqual(len(han), 2, f"no Han in {original!r}")
                control = _PUA * len(han)
                w_real = dpg.get_text_size(han, font=zh_body)[0]
                w_tofu = dpg.get_text_size(control, font=zh_body)[0]
                with self.subTest(sample=original[:16]):
                    # Real Han advances must strictly exceed the fallback box;
                    # equality would mean the glyphs never loaded (tofu).
                    self.assertGreater(
                        w_real,
                        w_tofu,
                        f"Han renders as tofu: real width {w_real} == fallback "
                        f"{w_tofu} for {han[:12]!r} (glyphs not in atlas)",
                    )

            # And the zh-CN body font must be a distinct handle, not the en
            # fallback that font_for() would return if zh-CN were unregistered.
            self.assertNotEqual(
                zh_body,
                font_for("body", "en"),
                "zh-CN body resolved to the en fallback (zh-CN fonts not registered)",
            )
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
