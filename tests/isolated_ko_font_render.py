"""Isolated REAL-render gate: Korean (Hangul) renders non-tofu under real DPG.

Runs OUTSIDE unittest discovery (no ``test_`` prefix), invoked per-method in a
subprocess by ``test_fonts`` — one DPG context per process (a second
create/destroy cycle hits the known teardown segfault).

Why this exists (2026-07-18 ko wiring): the font registration is otherwise only
mock-tested (``test_fonts`` patches ``_needs_explicit_cjk_range`` True), so it
cannot catch the real-DPG behavior. Measured on dpg 2.2: WITHOUT an explicit
glyph range, DPG renders Hangul as the fallback/tofu box, and ``get_text_size``
for a Hangul string equals a same-length private-use (U+E000, guaranteed
unmapped) control. WITH the Korean range hint (the ``force_ranges`` path this
gate exercises through the real ``register_fonts``), Hangul rasterizes at real,
wider advances. This gate renders the REAL font registry in a REAL viewport and
asserts every sampled Korean string is strictly wider than its fallback control
— i.e. the glyphs are present in the atlas and drawn, not tofu.

HONEST LIMITS (stated per the mission):
- ``get_text_size`` reflects the atlas glyph ADVANCE, proving the glyph is built
  with a real (non-fallback) width. It does not pixel-verify glyph SHAPE; that a
  Hangul codepoint maps to its correct glyph is established separately by the
  NotoSansKR cmap analysis (all 544 used syllables present).
- This runs on the agent's dpg 2.2; production pins dpg 2.3. The ``force_ranges``
  fix registers the Korean range explicitly on BOTH, so the render path does not
  depend on any version-specific CJK auto-load. A final glyph-shape eyeball on
  the real 2.3 build is still the recommended last confirmation.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import dearpygui.dearpygui as dpg

from zd_app.ui.fonts import font_for, register_fonts


_PUA = ""  # Private Use Area: no font maps it -> fallback/tofu advance.


def _hangul_only(text: str) -> str:
    return "".join(ch for ch in text if 0xAC00 <= ord(ch) <= 0xD7A3)


class IsolatedKoFontRenderTest(unittest.TestCase):
    def _boot(self):
        register_fonts()
        dpg.create_viewport(title="ko font render gate", width=520, height=320)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        for _ in range(15):
            dpg.render_dearpygui_frame()

    def test_hangul_renders_non_tofu_through_register_fonts(self) -> None:
        dpg.create_context()
        try:
            self._boot()
            ko_body = font_for("body", "ko")
            self.assertIsNotNone(ko_body, "ko body font handle not registered")

            ko = json.loads(
                Path("zd_app/i18n/locales/ko.json").read_text(encoding="utf-8")
            )
            # A short, a medium, and the long disclaimer — coincidental
            # advance-equality across all three is impossible.
            samples = [
                ko["language.ko"],
                ko["trust_matrix.label.policy"],
                ko["about.disclaimer"],
            ]
            for original in samples:
                hangul = _hangul_only(original)
                self.assertGreaterEqual(len(hangul), 2, f"no Hangul in {original!r}")
                control = _PUA * len(hangul)
                w_real = dpg.get_text_size(hangul, font=ko_body)[0]
                w_tofu = dpg.get_text_size(control, font=ko_body)[0]
                with self.subTest(sample=original[:16]):
                    # Real Hangul advances must strictly exceed the fallback
                    # box; equality would mean the glyphs never loaded (tofu).
                    self.assertGreater(
                        w_real,
                        w_tofu,
                        f"Hangul renders as tofu: real width {w_real} == fallback "
                        f"{w_tofu} for {hangul[:12]!r} (glyphs not in atlas)",
                    )

            # And the ko body font must be a distinct handle, not the en
            # fallback that font_for() would return if ko were unregistered.
            self.assertNotEqual(
                ko_body,
                font_for("body", "en"),
                "ko body resolved to the en fallback (ko fonts not registered)",
            )
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
