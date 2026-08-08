"""Font registry for Inter, Noto Sans SC, and JetBrains Mono."""

from __future__ import annotations

import logging
import warnings
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable, Optional

import dearpygui.dearpygui as dpg

from zd_app.ui.themes import FONT_SIZE


logger = logging.getLogger(__name__)

FONT_HANDLES: dict[tuple[str, str], int] = {}


def _font_dir() -> Path:
    import sys

    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "assets" / "fonts"
    return Path(__file__).resolve().parents[2] / "assets" / "fonts"


def _safe_add_font(path: Path, size: int, label: str) -> Optional[int]:
    if not path.exists():
        logger.warning("Font missing: %s (%s)", label, path)
        return None
    try:
        return dpg.add_font(str(path), size)
    except Exception as exc:
        logger.warning("Failed to load font %s: %s", path.name, exc)
        return None


def _add_font_with_ranges(
    path: Path,
    size: int,
    label: str,
    *range_hints: Callable[[], int],
    force_ranges: bool = False,
) -> Optional[int]:
    if not path.exists():
        logger.warning("CJK font missing: %s (%s)", label, path)
        return None
    try:
        font_id = dpg.add_font(str(path), size)
        # ``force_ranges`` registers the glyph ranges regardless of DPG version.
        # Measured on dpg 2.2 (2026-07-18 ko wiring): WITHOUT an explicit range
        # hint, Hangul renders as the fallback/tofu box (get_text_size for a
        # Hangul string equals a same-length private-use control), and WITH the
        # Korean range hint it rasterizes at real advances. The
        # ``_needs_explicit_cjk_range`` gate (skip on dpg>=2) assumes 2.x
        # auto-loads CJK, which holds only on ImGui-1.92+ dynamic-atlas builds;
        # forcing the range makes Korean render deterministically on every
        # dpg 2.x. The zh-CN path is deliberately left on the gate (proven on
        # the release-pinned dpg; forcing it too is tracked as later hardening).
        if force_ranges or _needs_explicit_cjk_range():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                for range_hint in range_hints:
                    dpg.add_font_range_hint(range_hint(), parent=font_id)
        return font_id
    except Exception as exc:
        logger.warning("Failed to load CJK font %s: %s", path.name, exc)
        return None


def _add_font_with_cjk(path: Path, size: int, label: str) -> Optional[int]:
    """Load Noto Sans SC for Chinese UI and the Korean picker autonym."""

    return _add_font_with_ranges(
        path,
        size,
        label,
        lambda: dpg.mvFontRangeHint_Chinese_Full,
        lambda: dpg.mvFontRangeHint_Korean,
    )


def _add_font_with_korean(path: Path, size: int, label: str) -> Optional[int]:
    # force_ranges: Korean must render on every dpg 2.x, not only where 2.x
    # auto-loads CJK glyphs (see _add_font_with_ranges). Chinese_Full is also
    # forced here (not just Korean) because it is the range hint that carries
    # General Punctuation (U+2000-U+206F): without it, em dash/en dash/ellipsis
    # in ko strings render as tofu even though Hangul itself is fine.
    return _add_font_with_ranges(
        path,
        size,
        label,
        lambda: dpg.mvFontRangeHint_Chinese_Full,
        lambda: dpg.mvFontRangeHint_Korean,
        force_ranges=True,
    )


def _record_font(key: tuple[str, str], font_id: Optional[int]) -> None:
    if font_id is not None:
        FONT_HANDLES[key] = font_id


def _needs_explicit_cjk_range() -> bool:
    try:
        major = int(version("dearpygui").split(".", 1)[0])
    except (PackageNotFoundError, ValueError):
        return True
    return major < 2


def register_fonts() -> dict[tuple[str, str], int]:
    """Load bundled fonts into Dear PyGui and return registered handles."""

    FONT_HANDLES.clear()
    fdir = _font_dir()
    logger.info("Loading fonts from %s", fdir)

    with dpg.font_registry():
        inter_regular = fdir / "Inter-Regular.ttf"
        inter_semibold = fdir / "Inter-SemiBold.ttf"
        jb_mono = fdir / "JetBrainsMono-Regular.ttf"
        noto_sc_regular = fdir / "NotoSansSC-Regular.otf"
        noto_sc_semibold = fdir / "NotoSansSC-SemiBold.otf"
        noto_kr_regular = fdir / "NotoSansKR-Regular.ttf"
        noto_kr_semibold = fdir / "NotoSansKR-SemiBold.ttf"

        _record_font(("header", "en"), _safe_add_font(inter_semibold, FONT_SIZE["header.h1"], "Inter SemiBold 24"))
        _record_font(("h2", "en"), _safe_add_font(inter_semibold, FONT_SIZE["header.h2"], "Inter SemiBold 18"))
        _record_font(("body", "en"), _safe_add_font(inter_regular, FONT_SIZE["body"], "Inter Regular 15"))
        _record_font(("helper", "en"), _safe_add_font(inter_regular, FONT_SIZE["helper"], "Inter Regular 14"))
        _record_font(("header", "zh-CN"), _add_font_with_cjk(noto_sc_semibold, FONT_SIZE["header.h1"], "Noto Sans SC SemiBold 24"))
        _record_font(("h2", "zh-CN"), _add_font_with_cjk(noto_sc_semibold, FONT_SIZE["header.h2"], "Noto Sans SC SemiBold 18"))
        _record_font(("body", "zh-CN"), _add_font_with_cjk(noto_sc_regular, FONT_SIZE["body"], "Noto Sans SC Regular 15"))
        _record_font(("helper", "zh-CN"), _add_font_with_cjk(noto_sc_regular, FONT_SIZE["helper"], "Noto Sans SC Regular 14"))
        _record_font(("header", "ko"), _add_font_with_korean(noto_kr_semibold, FONT_SIZE["header.h1"], "Noto Sans KR SemiBold 24"))
        _record_font(("h2", "ko"), _add_font_with_korean(noto_kr_semibold, FONT_SIZE["header.h2"], "Noto Sans KR SemiBold 18"))
        _record_font(("body", "ko"), _add_font_with_korean(noto_kr_regular, FONT_SIZE["body"], "Noto Sans KR Regular 15"))
        _record_font(("helper", "ko"), _add_font_with_korean(noto_kr_regular, FONT_SIZE["helper"], "Noto Sans KR Regular 14"))
        mono = _safe_add_font(jb_mono, FONT_SIZE["mono"], "JetBrains Mono Regular 13")
        _record_font(("mono", "en"), mono)
        _record_font(("mono", "zh-CN"), mono)

    return FONT_HANDLES


def bind_default_font(locale: str = "en") -> None:
    """Bind the body font for the given locale as the global default."""

    handle = font_for("body", locale)
    if handle is None:
        logger.warning("Body font for locale %s not loaded; using Dear PyGui default", locale)
        return
    dpg.bind_font(handle)


def font_for(purpose: str, locale: str) -> Optional[int]:
    """Look up a font handle by purpose and locale, falling back to English."""

    return FONT_HANDLES.get((purpose, locale)) or FONT_HANDLES.get((purpose, "en"))
