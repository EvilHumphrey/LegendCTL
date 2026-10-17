"""Lightweight runtime i18n for the wrapper."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

SUPPORTED_LOCALES = ("en", "zh-CN", "ko")
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
# Reviewed context-different duplicates (2026-07-03 ambiguity audit). Each English
# literal maps to the EXACT set of keys reviewed for it. The guard suppresses the
# warning only while the live key-set is a SUBSET of the reviewed set: a NEW
# sibling key added later under an allowlisted literal (e.g. a fresh "Back"/"Home"
# key carrying a different zh translation) is not in the reviewed set, so it
# re-arms the warning instead of shipping silently un-warned (a literal-only
# allowlist would have suppressed it). These literals are intentionally not
# unified; raw DPG use must be keyed before it reaches translate_literal().
_REVIEWED_AMBIGUOUS: dict[str, frozenset[str]] = {
    "Back": frozenset(
        {
            "controller.back_paddles.target.BACK",
            "diagnostics.live_verify.workspace.back_view",
        }
    ),
    "Battery": frozenset(
        {
            "device.summary.field.battery",
            "shell.battery",
            "home.connection.battery",
        }
    ),
    "Controller": frozenset(
        {
            "nav.controller",
            "controller.title",
            "compat_report.section.controller",
        }
    ),
    # Lighting-zone Home 区域 is context-distinct from navigation/diagram Home.
    "Home": frozenset(
        {
            "ui.home_70f8bb9a",
            "nav.home",
            "restore_field.lighting_zone.home",
            "diagnostics.live_verify.face_diagram.home",
            "controller.choice.lighting_zone.home",
        }
    ),
    "Instant": frozenset(
        {
            "ui.instant_e5dd7083",
            "controller.sticks.preset.instant",
            "controller.motion.mode.instant",
        }
    ),
    "Layout": frozenset(
        {
            "safe_import.categories.layout",
            "restore_points.detail.field_category.layout",
            "device_vs_profile.section.layout",
        }
    ),
    # Lighting-zone 左侧灯 is context-distinct from directional/deadzone Left.
    "Left": frozenset(
        {
            "apply.side.left",
            "health_report.table.col.left",
            "restore_field.deadzones.left",
            "modules.side.left",
            "controller.choice.lighting_zone.left",
        }
    ),
    # Lighting-mode 关闭 is context-distinct from the generic lighting-state 关.
    "Off": frozenset(
        {
            "restore_field.lighting.off",
            "restore_field.lighting_mode.off",
            "controller.choice.lighting_mode.off",
        }
    ),
    # Lighting-zone 右侧灯 is context-distinct from directional/deadzone Right.
    "Right": frozenset(
        {
            "apply.side.right",
            "health_report.table.col.right",
            "restore_field.deadzones.right",
            "modules.side.right",
            "controller.choice.lighting_zone.right",
        }
    ),
    # Trigger-stroke 短行程 is context-distinct from the generic length 短.
    "Short": frozenset(
        {
            "ui.short_0fe7d82f",
            "restore_field.trigger_mode.short",
            "controller.choice.trigger_mode.short",
        }
    ),
    # Lighting-mode 常亮 is context-distinct from the setting-state 始终开启.
    "Always On": frozenset(
        {
            "ui.always_on_d50f042b",
            "controller.choice.lighting_mode.always_on",
        }
    ),
    # NOTE: The Phase-2 profile-Apply field-label literals (Deadzones, Left/Right
    # trigger, Left/Right stick axis inversion, Left/Right stick sensitivity,
    # Polling rate, Vibration) were allowlisted here ONLY while field.label.* held
    # English zh-CN placeholders. The v2.6.2 zh native-review round (2026-07-18) gave
    # those keys real zh translations that MATCH their sibling contexts (and ko
    # already matched), so each literal is now genuinely unambiguous and the guard
    # stays silent on its own. The allowlist entries were removed so a future
    # drift re-arms the ambiguity warning instead of being suppressed.
    # ko context-different duplicates (2026-07-18 ko wiring). The same English
    # literal carries slightly context-varied ko translations across these
    # keys (zh unified them; ko renders task/label/register-appropriate
    # variants). All are passed by key, never raw to translate_literal.
    "Actions": frozenset(
        {
            "controller.profiles.col_actions",
            "diagnostics.tab.actions",
            "restore_points.list.col.actions",
            "ui.actions_c3cd636a",
        }
    ),
    "Device vs Profile": frozenset(
        {
            "device_vs_profile.title",
            "nav.device_vs_profile",
        }
    ),
    "Generated": frozenset(
        {
            "compat_report.generated_label",
            "trust_self_check.generated_label",
        }
    ),
    "No diagnostics events yet.": frozenset(
        {
            "diagnostics.event_log.empty",
            "ui.no_diagnostics_events_yet_3957ece8",
        }
    ),
    "This file contained unsupported automation fields. They were blocked and will not be saved or written to the controller.": frozenset(
        {
            "badge.automation_blocked.tooltip",
            "safe_import.categories.automation_hint",
        }
    ),
}


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
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load locale %s: %s", locale, exc)
        data = {}
    _loaded[locale] = _string_table(data, locale)
    if locale == DEFAULT_LOCALE:
        _rebuild_reverse_en()
    return _loaded[locale]


def _string_table(data: object, locale: str) -> dict[str, str]:
    """Coerce a parsed locale file to the key->string table the app assumes.

    A locale file is a JSON object of string values everywhere else in this
    module, and nothing downstream re-checks it: ``_rebuild_reverse_en`` calls
    ``.items()`` on the English table the moment it is cached, and ``t()`` calls
    ``.format()`` on whatever it finds. So a root of ``[]`` / ``"x"`` / ``3`` /
    ``null`` crashed the load instead of taking the malformed-locale fallback
    two lines above, and a non-string value crashed later, at the call site.
    Both degrade here through the same warning path.

    A bad root yields an empty table; individual bad values are dropped rather
    than discarding the whole file, because one stray value should cost those
    keys their translation, not leave the entire UI unlabelled.
    """

    if not isinstance(data, dict):
        logger.warning(
            "Locale %s is not a JSON object (%s); ignoring it",
            locale,
            type(data).__name__,
        )
        return {}
    table = {
        key: value
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    if len(table) != len(data):
        logger.warning(
            "Locale %s: dropped %d entr(ies) that are not string values",
            locale,
            len(data) - len(table),
        )
    return table


def _other_locale_maps_for_ambiguity() -> tuple[tuple[str, dict[str, str]], ...]:
    """Read every shipped non-default locale for the ambiguity check.

    This intentionally bypasses ``_loaded`` so English loading keeps its lazy
    cache behavior. A future locale may be listed before its JSON ships; a
    missing or unreadable file is skipped rather than creating a false warning.
    """

    sibling_maps: list[tuple[str, dict[str, str]]] = []
    for locale in SUPPORTED_LOCALES:
        if locale == DEFAULT_LOCALE:
            continue
        path = _locale_dir() / f"{locale}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            # Same normalisation as ``_load``: the ambiguity check builds a set
            # of sibling values, and an unhashable value would raise there.
            sibling_maps.append((locale, _string_table(data, locale)))
    return tuple(sibling_maps)


def _reviewed_key_shipped_untranslated(
    value: str,
    keys: list[str],
    sibling_maps: tuple[tuple[str, dict[str, str]], ...],
) -> bool:
    """Return True if a reviewed key's sibling-locale value is still the raw
    English literal -- a silently-untranslated key hiding behind the
    ``_REVIEWED_AMBIGUOUS`` allowlist (e.g. a ko value that was never given a
    real translation and still reads "Home"). The allowlist only vouches for
    keys that were reviewed AND actually translated; an untranslated reviewed
    key was never reviewed in that state, so it must re-arm the warning
    rather than ride the allowlist silently."""

    return any(
        sibling_map.get(key) == value
        for key in keys
        for _locale, sibling_map in sibling_maps
    )


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
    sibling_maps = _other_locale_maps_for_ambiguity()
    for value, keys in value_to_keys.items():
        if len(keys) <= 1:
            continue
        reviewed_keys = _REVIEWED_AMBIGUOUS.get(value)
        reviewed_and_covered = reviewed_keys is not None and set(keys) <= reviewed_keys
        untranslated = reviewed_and_covered and _reviewed_key_shipped_untranslated(
            value, keys, sibling_maps
        )
        if reviewed_and_covered and not untranslated:
            # Exactly the reviewed context-different keys, and every sibling
            # locale actually translated each of them — intentional, not a
            # trap. A NEW sibling key (set(keys) not a subset of the reviewed
            # set), or a reviewed key that silently ships the raw English
            # literal in some locale, falls through so the checks below can
            # re-arm the warning.
            continue
        if (
            untranslated
            or any(
                len({sibling_map[key] for key in keys if key in sibling_map}) > 1
                for _locale, sibling_map in sibling_maps
            )
        ):
            _ambiguous_en[value] = tuple(keys)
    if _ambiguous_en:
        sample = ", ".join(repr(v) for v in sorted(_ambiguous_en)[:8])
        sibling_locales = tuple(locale for locale, _map in sibling_maps)
        locale_label = (
            sibling_locales[0]
            if len(sibling_locales) == 1
            else "non-default locale"
        )
        logger.warning(
            "i18n: %d English literal(s) map to multiple keys whose %s translation "
            "differs; translate_literal() resolves each to the first key in JSON "
            "order and may mistranslate the differing sibling. Ambiguous literals: %s%s",
            len(_ambiguous_en),
            locale_label,
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
