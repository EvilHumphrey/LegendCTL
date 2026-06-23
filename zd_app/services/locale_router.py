"""Locale state + listener registry for locale-switch coordination.

Wraps ``zd_app.i18n.set_locale`` so callers can swap the active locale and
have subscribers (e.g. AppShell rebuilding DPG widgets) get notified in one
hop. DPG-free at import time.

Extracted from ``zd_app/ui/app_shell.py``. The screen-rebuild orchestration
stays in AppShell as a registered listener, since DPG widget rebuilds are
UI-tier work.
"""

from __future__ import annotations

from typing import Callable

from zd_app.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, get_locale, set_locale


LocaleChangeListener = Callable[[str, str], None]
"""``(old_locale, new_locale)`` callback signature."""


class LocaleRouter:
    """Owns the active-locale state + a listener registry.

    ``set_locale`` is a no-op when called with the already-current locale,
    so subscribers don't pay rebuild cost on idempotent re-applies.
    """

    def __init__(self) -> None:
        self._listeners: list[LocaleChangeListener] = []

    def get_locale(self) -> str:
        return get_locale()

    def set_locale(self, new_locale: str) -> None:
        """Set the active locale; fall back to default if unsupported.

        Fires every listener with ``(old, new)`` exactly once if the locale
        actually changed. No-op (and no listener fires) if ``new_locale``
        equals the current locale, even after the unsupported-locale
        normalization.
        """
        if new_locale not in SUPPORTED_LOCALES:
            new_locale = DEFAULT_LOCALE
        old = get_locale()
        if old == new_locale:
            return
        set_locale(new_locale)
        for listener in list(self._listeners):
            listener(old, new_locale)

    def subscribe(self, listener: LocaleChangeListener) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: LocaleChangeListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)
