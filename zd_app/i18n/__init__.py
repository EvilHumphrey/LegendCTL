"""Lightweight runtime i18n for the wrapper."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

SUPPORTED_LOCALES = ("en", "zh-CN")
DEFAULT_LOCALE = "en"

_current_locale = DEFAULT_LOCALE
_loaded: dict[str, dict[str, str]] = {}
_reverse_en: dict[str, str] = {}
# English literals that map to more than one key. ``translate_literal`` resolves
# each to the first JSON-order key (see ``_reverse_en``), so any sibling key
# whose other-locale translation differs would be mistranslated the moment a
# contributor passes that raw English literal. Populated at en-load purely as a
# guard surface; it never changes resolution.
_ambiguous_en: dict[str, tuple[str, ...]] = {}


def _locale_dir() -> Path:
    import sys

    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "zd_app" / "i18n" / "locales"
    return Path(__file__).resolve().parent / "locales"


def _load(locale: str) -> dict[str, str]:
    if locale in _loaded:
        return _loaded[locale]
    path = _locale_dir() / f"{locale}.json"
    if not path.exists():
        logger.warning("Locale file missing: %s; falling back to en", path)
        if locale != DEFAULT_LOCALE:
            return _load(DEFAULT_LOCALE)
        _loaded[DEFAULT_LOCALE] = {}
        return {}
    try:
        _loaded[locale] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load locale %s: %s", locale, exc)
        _loaded[locale] = {}
    if locale == DEFAULT_LOCALE:
        _rebuild_reverse_en()
    return _loaded[locale]


def _other_locale_map_for_ambiguity() -> dict[str, str]:
    """Read the non-default shipped locale's raw key->value map for the
    ambiguity check, WITHOUT touching the ``_loaded`` cache (so lazy locale
    loading is unchanged). Returns ``{}`` if it is missing/unreadable — with no
    sibling locale there is no mistranslation trap to warn about.
    """

    for locale in SUPPORTED_LOCALES:
        if locale == DEFAULT_LOCALE:
            continue
        path = _locale_dir() / f"{locale}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _rebuild_reverse_en() -> None:
    """Rebuild the English->key reverse map and the ambiguity guard surface.

    Resolution is unchanged: ``setdefault`` keeps first-JSON-order-key-wins, so
    every current ``translate_literal`` call site resolves exactly as before. The
    only addition is the guard surface: English literals shared by multiple keys
    *whose other-locale translation actually differs* are the latent contributor
    trap (passing that raw English literal would silently resolve to the first
    key's — possibly wrong-context — translation). Duplicate literals whose
    siblings translate identically are harmless and are deliberately NOT flagged,
    so the one warning points at the real ambiguities instead of every duplicate.
    """

    _reverse_en.clear()
    _ambiguous_en.clear()
    value_to_keys: dict[str, list[str]] = {}
    for key, value in _loaded[DEFAULT_LOCALE].items():
        _reverse_en.setdefault(value, key)
        value_to_keys.setdefault(value, []).append(key)
    siblings = _other_locale_map_for_ambiguity()
    for value, keys in value_to_keys.items():
        if len(keys) <= 1:
            continue
        distinct = {siblings[k] for k in keys if k in siblings}
        if len(distinct) > 1:
            _ambiguous_en[value] = tuple(keys)
    if _ambiguous_en:
        sample = ", ".join(repr(v) for v in sorted(_ambiguous_en)[:8])
        logger.warning(
            "i18n: %d English literal(s) map to multiple keys whose %s translation "
            "differs; translate_literal() resolves each to the first key in JSON "
            "order and may mistranslate the differing sibling. Ambiguous literals: %s%s",
            len(_ambiguous_en),
            next((loc for loc in SUPPORTED_LOCALES if loc != DEFAULT_LOCALE), "other-locale"),
            sample,
            "" if len(_ambiguous_en) <= 8 else ", ...",
        )


def set_locale(locale: str) -> None:
    """Set the active locale, falling back to English if unsupported."""

    global _current_locale
    if locale not in SUPPORTED_LOCALES:
        logger.warning("Unsupported locale %s; using %s", locale, DEFAULT_LOCALE)
        locale = DEFAULT_LOCALE
    _current_locale = locale
    _load(locale)


def get_locale() -> str:
    return _current_locale


def t(key: str, **kwargs: Any) -> str:
    """Look up a localized string by key."""

    table = _load(_current_locale)
    value = table.get(key)
    if (value is None or _is_tombstone_text(value)) and _current_locale != DEFAULT_LOCALE:
        value = _load(DEFAULT_LOCALE).get(key)
    if value is None:
        return f"[{key}]"
    if kwargs:
        try:
            return value.format(**kwargs)
        except (KeyError, IndexError):
            return value
    return value


def translate_literal(value: str) -> str:
    """Translate an existing English UI literal via the locale JSON map."""

    if _current_locale == DEFAULT_LOCALE:
        return value
    if not _reverse_en:
        _load(DEFAULT_LOCALE)
    key = _reverse_en.get(value)
    if key is None:
        return value
    return t(key)


def _is_tombstone_text(value: str) -> bool:
    """Return True for corrupted placeholder labels such as ``????``."""

    stripped = value.strip()
    return bool(stripped) and set(stripped) == {"?"}


def translate_ui_value(value):
    """Translate string UI values without touching non-string payloads."""

    if isinstance(value, str):
        return translate_literal(value)
    return value
