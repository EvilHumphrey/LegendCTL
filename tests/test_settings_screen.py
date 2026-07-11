"""Tests for Settings screen widget wiring."""

from __future__ import annotations

import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from zd_app import i18n
from zd_app.models import AppSettings
from zd_app.ui.screens import preferences as preferences_screen


class TestSettingsScreen(unittest.TestCase):
    def _build_with_patches(self, *, font_id, locale="en", settings=None):
        """Run preferences.build under stub patches; return the bind_item_font mock."""
        shell = SimpleNamespace(
            settings=settings or AppSettings(),
            COLORS={"accent": (46, 155, 255), "muted": (148, 163, 184), "warn": (245, 158, 11)},
            settings_service=None,
            update_language=MagicMock(),
            update_setting=MagicMock(),
            restore_app_defaults=MagicMock(),
            refresh_from_controller=MagicMock(),
        )

        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale(locale)

        with patch.object(preferences_screen.dpg, "child_window", return_value=nullcontext()), patch.object(
            preferences_screen.dpg,
            "group",
            return_value=nullcontext(),
        ), patch.object(
            preferences_screen.dpg,
            "add_combo",
            side_effect=["language_combo", "logging_combo"],
        ) as add_combo, patch.object(preferences_screen.dpg, "bind_item_font") as bind_item_font, patch.object(
            preferences_screen.dpg,
            "add_text",
        ), patch.object(
            preferences_screen.dpg,
            "add_spacer",
        ), patch.object(
            preferences_screen.dpg,
            "add_separator",
        ), patch.object(
            preferences_screen.dpg,
            "add_checkbox",
        ), patch.object(
            preferences_screen.dpg,
            "add_input_text",
        ), patch.object(
            preferences_screen.dpg,
            "add_button",
        ), patch.object(
            preferences_screen,
            "font_for",
            return_value=font_id,
        ) as font_for:
            preferences_screen.build(shell, parent="root")
        return shell, bind_item_font, font_for, add_combo

    def test_combos_bind_cjk_font_when_available(self) -> None:
        _shell, bind_item_font, font_for, _add_combo = self._build_with_patches(font_id=4242)
        font_for.assert_called_once_with("body", "zh-CN")
        # Both combos (language + logging verbosity) get the CJK font
        # binding so Mandarin labels render correctly.
        bind_item_font.assert_has_calls([
            call("language_combo", 4242),
            call("logging_combo", 4242),
        ])
        self.assertEqual(bind_item_font.call_count, 2)

    def test_combos_skip_font_bind_when_unavailable(self) -> None:
        _shell, bind_item_font, _font_for, _add_combo = self._build_with_patches(font_id=None)
        # If font_for returns None (e.g. fonts not registered in test
        # context), bind_item_font must be skipped on both combos.
        bind_item_font.assert_not_called()

    def test_language_combo_uses_shipped_locales_and_round_trips_selection(self) -> None:
        shell, _bind_item_font, _font_for, add_combo = self._build_with_patches(font_id=4242)

        language_combo = add_combo.call_args_list[0].kwargs
        self.assertEqual(language_combo["items"], ["English", "简体中文"])
        self.assertEqual(language_combo["default_value"], "English")

        language_combo["callback"]("language_combo", "简体中文")

        shell.update_language.assert_called_once_with("zh-CN")

    def test_language_combo_accepts_a_future_shipped_locale_without_code_changes(self) -> None:
        with patch.object(preferences_screen, "SUPPORTED_LOCALES", ("en", "zh-CN", "ko")):
            shell, _bind_item_font, _font_for, add_combo = self._build_with_patches(
                font_id=4242,
                settings=AppSettings(language="ko"),
            )

        language_combo = add_combo.call_args_list[0].kwargs
        self.assertEqual(language_combo["items"], ["English", "简体中文", "한국어"])
        self.assertEqual(language_combo["default_value"], "한국어")

        language_combo["callback"]("language_combo", "한국어")

        shell.update_language.assert_called_once_with("ko")


class TestLoggingVerbosityCombo(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_combo_options_localize_to_zh_cn(self) -> None:
        i18n.set_locale("zh-CN")
        labels = [
            i18n.t(preferences_screen.LOGGING_VERBOSITY_KEY_FOR[v])
            for v in preferences_screen.LOGGING_VERBOSITY_VALUES
        ]
        self.assertEqual(labels, ["安静", "正常", "详细"])

    def test_combo_options_localize_to_en(self) -> None:
        labels = [
            i18n.t(preferences_screen.LOGGING_VERBOSITY_KEY_FOR[v])
            for v in preferences_screen.LOGGING_VERBOSITY_VALUES
        ]
        self.assertEqual(labels, ["Quiet", "Normal", "Verbose"])

    def test_canonical_values_remain_latin(self) -> None:
        # The settings.json stores the canonical (Latin) value; only the
        # combo's display label localizes.
        self.assertEqual(
            preferences_screen.LOGGING_VERBOSITY_VALUES,
            ("Quiet", "Normal", "Verbose"),
        )


if __name__ == "__main__":
    unittest.main()
