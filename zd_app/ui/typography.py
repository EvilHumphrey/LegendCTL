"""Typography helpers — bind the loaded type scale to titles and helper text.

Dear PyGui has no CSS cascade and fonts are pre-rasterized at fixed sizes, so a
"type scale" here means loading discrete font sizes (see ``fonts.py`` /
``FONT_SIZE``) and binding the right handle per item. These helpers wrap
``dpg.add_text`` + ``dpg.bind_item_font`` so every screen gets consistent title
sizing/weight/color without re-deciding it inline:

- ``screen_title()``  -> h1 (24px SemiBold), ``text.primary`` — one per screen.
- ``section_title()`` -> h2 (18px SemiBold), ``text.primary`` — section headings.
- ``helper_text()``   -> 13px helper font, ``text.secondary`` — muted helper lines.

Titles use ``text.primary`` (not accent) so size + weight carry the hierarchy
and accent-green is reserved for actionable/active elements.

If the requested font is not loaded (e.g. headless tests that never call
``register_fonts()``), binding is skipped and the text still renders at the
default font — mirroring the existing ``font_for`` / ``bind_item_font`` guard
pattern used elsewhere (about / safe_import / preferences).
"""

from __future__ import annotations

import logging
from typing import Optional

import dearpygui.dearpygui as dpg

from zd_app.i18n import get_locale
from zd_app.ui.fonts import font_for
from zd_app.ui.themes import COLORS


logger = logging.getLogger(__name__)


def _titled_text(text: str, purpose: str, color, locale: Optional[str], kwargs: dict) -> int:
    """Add a text item and bind the type-scale font for ``purpose``.

    ``color`` is applied unless the caller overrode it via ``kwargs``. Extra
    kwargs (``tag``, ``wrap``, ...) are forwarded to ``dpg.add_text``.
    """

    kwargs.setdefault("color", color)
    item = dpg.add_text(text, **kwargs)
    font = font_for(purpose, locale or get_locale())
    if font is not None:
        # Verify the handle still exists before binding. The suite's shared-
        # context shim recycles ids between tests, and a stale font id raises
        # "Item not found" on bind. Mirror components._bind_card_theme's guard:
        # check does_item_exist, swallow any cosmetic failure, never block the
        # render (the text still shows at the default font).
        try:
            if dpg.does_item_exist(font):
                dpg.bind_item_font(item, font)
        except Exception:  # pragma: no cover - cosmetic; never block a render
            logger.debug("Title font bind skipped", exc_info=True)
    return item


def screen_title(text: str, *, locale: Optional[str] = None, **kwargs) -> int:
    """Top-of-screen title: h1 (24px SemiBold) in primary text color."""

    return _titled_text(text, "header", COLORS["text.primary"], locale, kwargs)


def section_title(text: str, *, locale: Optional[str] = None, **kwargs) -> int:
    """Section / card heading: h2 (18px SemiBold) in primary text color."""

    return _titled_text(text, "h2", COLORS["text.primary"], locale, kwargs)


def helper_text(text: str, *, locale: Optional[str] = None, **kwargs) -> int:
    """Muted helper line: smaller (13px) helper font in secondary text color."""

    return _titled_text(text, "helper", COLORS["text.secondary"], locale, kwargs)
