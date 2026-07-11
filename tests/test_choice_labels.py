"""Controller combo display-label boundary tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from zd_app import i18n
from zd_app.ui.choice_labels import CHOICE_LABEL_KEYS, display_items, to_canonical, to_display


class ChoiceLabelTests(unittest.TestCase):
    def tearDown(self) -> None:
        i18n.set_locale("en")

    def test_display_items_preserve_canonical_order(self) -> None:
        i18n.set_locale("en")

        for domain, canonical_to_key in CHOICE_LABEL_KEYS.items():
            with self.subTest(domain=domain):
                self.assertEqual(display_items(domain), list(canonical_to_key))

    def test_english_round_trip_is_identity_for_every_item(self) -> None:
        i18n.set_locale("en")

        for domain, canonical_to_key in CHOICE_LABEL_KEYS.items():
            for canonical in canonical_to_key:
                with self.subTest(domain=domain, canonical=canonical):
                    self.assertEqual(to_display(domain, canonical), canonical)
                    self.assertEqual(to_canonical(domain, canonical), canonical)

    def test_zh_round_trip_returns_canonical_then_same_display_label(self) -> None:
        i18n.set_locale("zh-CN")

        for domain, canonical_to_key in CHOICE_LABEL_KEYS.items():
            for canonical in canonical_to_key:
                with self.subTest(domain=domain, canonical=canonical):
                    display = to_display(domain, canonical)
                    self.assertEqual(to_canonical(domain, display), canonical)
                    self.assertEqual(to_display(domain, to_canonical(domain, display)), display)

    def test_unknown_values_pass_through_in_both_directions(self) -> None:
        i18n.set_locale("zh-CN")

        self.assertEqual(to_display("lighting_mode", "Future Mode"), "Future Mode")
        self.assertEqual(to_canonical("lighting_mode", "未来模式"), "未来模式")

    def test_reverse_lookup_rebuilds_after_locale_switch(self) -> None:
        i18n.set_locale("zh-CN")
        self.assertEqual(to_canonical("lighting_mode", "常亮"), "Always On")

        i18n.set_locale("en")
        self.assertEqual(to_canonical("lighting_mode", "Always On"), "Always On")

    def test_choice_zh_terms_match_corresponding_restore_field_terms(self) -> None:
        locale_dir = Path("zd_app/i18n/locales")
        zh = json.loads((locale_dir / "zh-CN.json").read_text(encoding="utf-8"))
        corresponding_keys = {
            "controller.choice.vibration_mode.stereo_resonance": "restore_field.vibration_mode.stereo_resonance",
            "controller.choice.vibration_mode.trigger_vibration": "restore_field.vibration_mode.trigger_vibration",
            "controller.choice.trigger_mode.short": "restore_field.trigger_mode.short",
            "controller.choice.trigger_mode.long": "restore_field.trigger_mode.long",
            "controller.choice.lighting_zone.home": "restore_field.lighting_zone.home",
            "controller.choice.lighting_zone.left": "restore_field.lighting_zone.left",
            "controller.choice.lighting_zone.right": "restore_field.lighting_zone.right",
            "controller.choice.lighting_mode.off": "restore_field.lighting_mode.off",
            "controller.choice.lighting_mode.always_on": "restore_field.lighting_mode.always_on",
            "controller.choice.lighting_mode.breath": "restore_field.lighting_mode.breath",
            "controller.choice.lighting_mode.fade": "restore_field.lighting_mode.fade",
            "controller.choice.lighting_mode.flow": "restore_field.lighting_mode.flow",
        }

        for choice_key, restore_field_key in corresponding_keys.items():
            with self.subTest(choice_key=choice_key):
                self.assertEqual(zh[choice_key], zh[restore_field_key])
