"""Tests for LocaleRouter.

Coordinator was extracted from zd_app/ui/app_shell.py. Tests prove the seam:
listener registry semantics, no-op-on-same-locale discipline, and DPG-freedom.
"""

from __future__ import annotations

import sys
import unittest

from zd_app.i18n import set_locale
from zd_app.services.locale_router import LocaleRouter


class LocaleRouterListenerTests(unittest.TestCase):
    def setUp(self) -> None:
        set_locale("en")

    def tearDown(self) -> None:
        set_locale("en")

    def test_set_locale_fires_listener_with_old_and_new(self) -> None:
        events: list[tuple[str, str]] = []
        router = LocaleRouter()
        router.subscribe(lambda old, new: events.append((old, new)))

        router.set_locale("zh-CN")

        self.assertEqual(events, [("en", "zh-CN")])

    def test_set_locale_no_op_when_already_current(self) -> None:
        events: list[tuple[str, str]] = []
        router = LocaleRouter()
        router.subscribe(lambda old, new: events.append((old, new)))

        # Already on en (set in setUp); no-op expected.
        router.set_locale("en")

        self.assertEqual(events, [], "no-change set_locale must not fire any listener")

    def test_unsupported_locale_falls_back_to_default_no_op_when_already_default(self) -> None:
        events: list[tuple[str, str]] = []
        router = LocaleRouter()
        router.subscribe(lambda old, new: events.append((old, new)))

        # Already on default (en); unsupported gets normalized to en; no-op.
        router.set_locale("xx-YY")

        self.assertEqual(events, [])

    def test_unsupported_locale_falls_back_to_default_when_currently_other(self) -> None:
        set_locale("zh-CN")
        events: list[tuple[str, str]] = []
        router = LocaleRouter()
        router.subscribe(lambda old, new: events.append((old, new)))

        router.set_locale("xx-YY")

        # Normalized to en; locale changes from zh-CN -> en; listener fires.
        self.assertEqual(events, [("zh-CN", "en")])

    def test_multiple_listeners_all_fire(self) -> None:
        calls_a: list[tuple[str, str]] = []
        calls_b: list[tuple[str, str]] = []
        router = LocaleRouter()
        router.subscribe(lambda old, new: calls_a.append((old, new)))
        router.subscribe(lambda old, new: calls_b.append((old, new)))

        router.set_locale("zh-CN")

        self.assertEqual(calls_a, [("en", "zh-CN")])
        self.assertEqual(calls_b, [("en", "zh-CN")])

    def test_unsubscribe_removes_listener(self) -> None:
        events: list[tuple[str, str]] = []
        router = LocaleRouter()
        listener = lambda old, new: events.append((old, new))
        router.subscribe(listener)
        router.unsubscribe(listener)

        router.set_locale("zh-CN")

        self.assertEqual(events, [])

    def test_get_locale_reflects_after_set(self) -> None:
        router = LocaleRouter()
        self.assertEqual(router.get_locale(), "en")

        router.set_locale("zh-CN")
        self.assertEqual(router.get_locale(), "zh-CN")


class LocaleRouterDpgFreeTests(unittest.TestCase):
    def test_module_does_not_import_dpg(self) -> None:
        for name in [n for n in list(sys.modules) if "dearpygui" in n]:
            sys.modules.pop(name, None)
        sys.modules.pop("zd_app.services.locale_router", None)

        import zd_app.services.locale_router  # noqa: F401

        for name in sys.modules:
            self.assertFalse(
                name.startswith("dearpygui"),
                f"importing LocaleRouter pulled in {name}; should be DPG-free",
            )


if __name__ == "__main__":
    unittest.main()
