from __future__ import annotations

import unittest

from zd_app import i18n
from zd_app.ui import support_reference


class SupportReferenceTests(unittest.TestCase):
    def test_firmware_guide_uses_target_aware_recovery_copy(self) -> None:
        guide = support_reference.get_guide("firmware")

        self.assertEqual(guide.title, "Firmware Targets")
        self.assertIn("target lanes", guide.summary)
        self.assertTrue(any("Left Joystick (L3)" in bullet for bullet in guide.bullets))
        self.assertTrue(any("Right Joystick (R3)" in bullet for bullet in guide.bullets))
        self.assertTrue(any("\u624b\u67c4\u56fa\u4ef6" in bullet for bullet in guide.bullets))
        self.assertTrue(any("side lanes as L3 and R3" in bullet for bullet in guide.bullets))
        self.assertTrue(any("Dongle (Receiver) Upgrade Instructions" in bullet for bullet in guide.bullets))
        self.assertTrue(any("separate maintenance lanes" in bullet for bullet in guide.bullets))
        self.assertTrue(any("Direct App Flashing Not Available" in bullet for bullet in guide.bullets))
        self.assertFalse(any("IAP_Programmer" in bullet for bullet in guide.bullets))
        self.assertFalse(any("ZDIU" in bullet for bullet in guide.bullets))
        self.assertFalse(any("F405" in bullet for bullet in guide.bullets))
        self.assertIn("official Windows update bundles", guide.evidence_note)
        self.assertIn("ZD firmware/download references", guide.evidence_note)

    def test_windows_component_guide_is_registered(self) -> None:
        guide = support_reference.get_guide("windows_component_model")

        self.assertEqual(guide.title, "Windows Support Stack")
        self.assertIn("multiple PC packages", guide.summary)
        self.assertTrue(any("receiver firmware" in bullet for bullet in guide.bullets))
        self.assertTrue(any("Left Joystick (L3)" in bullet for bullet in guide.bullets))
        self.assertTrue(any("Right Joystick (R3)" in bullet for bullet in guide.bullets))
        self.assertTrue(any("January and March Windows update bundles" in bullet for bullet in guide.bullets))
        self.assertTrue(any("ZD Game Zone 3.7" in bullet for bullet in guide.bullets))
        self.assertTrue(any("separate L3 and R3 maintenance lanes" in bullet for bullet in guide.bullets))
        self.assertTrue(any("deeper local package terms" in bullet for bullet in guide.bullets))
        self.assertFalse(any("receiver or dongle firmware" in bullet for bullet in guide.bullets))
        self.assertFalse(any("IAP_Programmer" in bullet for bullet in guide.bullets))
        self.assertFalse(any("ZDIU" in bullet for bullet in guide.bullets))
        self.assertFalse(any("F405" in bullet for bullet in guide.bullets))
        self.assertIn("official Windows update bundles", guide.evidence_note)
        self.assertIn("ZD package listings", guide.evidence_note)

    def test_firmware_routing_guide_is_registered(self) -> None:
        guide = support_reference.get_guide("firmware_routing")

        self.assertEqual(guide.title, "Choose the Right Target")
        self.assertIn("route by symptom first", guide.summary)
        self.assertTrue(any("Receiver lane" in bullet for bullet in guide.bullets))
        self.assertTrue(any("Left Joystick (L3)" in bullet for bullet in guide.bullets))
        self.assertTrue(any("Right Joystick (R3)" in bullet for bullet in guide.bullets))
        self.assertTrue(any("does not flash" in bullet for bullet in guide.bullets))
        self.assertFalse(any("IAP_Programmer" in bullet for bullet in guide.bullets))
        self.assertFalse(any("ZDIU" in bullet for bullet in guide.bullets))
        self.assertFalse(any("F405" in bullet for bullet in guide.bullets))
        self.assertIn("firmware-target routing model", guide.evidence_note)
        self.assertIn("official ZD support flows", guide.evidence_note)

    def test_config_slots_guide_is_registered(self) -> None:
        guide = support_reference.get_guide("config_slots")

        self.assertEqual(guide.title, "Config Slots")
        self.assertIn("three separate things", guide.summary)
        self.assertTrue(any("\u914d\u7f6e" in bullet for bullet in guide.bullets))
        self.assertTrue(any("\u677f\u8f7d\u914d\u7f6e\u5207\u6362" in bullet for bullet in guide.bullets))
        self.assertTrue(any("\u5b58\u68631-4" in bullet for bullet in guide.bullets))
        self.assertTrue(any("highlight the exact target slot" in bullet for bullet in guide.bullets))
        self.assertIn("onboard slot", guide.evidence_note)
        self.assertIn("exact target", guide.evidence_note)

    def test_bluetooth_pairing_guide_is_registered(self) -> None:
        guide = support_reference.get_guide("bluetooth_pairing")

        self.assertEqual(guide.title, "Bluetooth Pairing")
        self.assertIn("Bluetooth specifically", guide.summary)
        self.assertTrue(any("\u6a21\u5f0f\u5207\u6362" in bullet for bullet in guide.bullets))
        self.assertTrue(any("\u84dd\u7259\u8fde\u63a5" in bullet for bullet in guide.bullets))
        self.assertTrue(any("remove stale pairing data" in bullet for bullet in guide.bullets))
        self.assertTrue(any("not receiver or wired" in bullet for bullet in guide.bullets))
        self.assertIn("official ZD support tasks", guide.evidence_note)

    def test_receiver_pairing_guide_is_registered(self) -> None:
        guide = support_reference.get_guide("receiver_pairing")

        self.assertEqual(guide.title, "Receiver Pairing")
        self.assertIn("distinct transport flow", guide.summary)
        self.assertTrue(any("\u63a5\u6536\u5668" in bullet for bullet in guide.bullets))
        self.assertTrue(any("\u914d\u5bf9\u952e" in bullet for bullet in guide.bullets))
        self.assertTrue(any("route to receiver repair" in bullet for bullet in guide.bullets))
        self.assertIn("official ZD support tasks", guide.evidence_note)

    def test_restore_defaults_guide_is_registered(self) -> None:
        guide = support_reference.get_guide("restore_defaults")

        self.assertEqual(guide.title, "Restore Defaults")
        self.assertIn("supported recovery path", guide.summary)
        self.assertTrue(any("\u6062\u590d\u9ed8\u8ba4" in bullet for bullet in guide.bullets))
        self.assertTrue(any("\u91cd\u7f6e" in bullet for bullet in guide.bullets))
        self.assertTrue(any("not as a hidden panic button" in bullet for bullet in guide.bullets))
        self.assertIn("official ZD support flows", guide.evidence_note)

    def test_mode_switching_guide_is_registered(self) -> None:
        guide = support_reference.get_guide("mode_switching")

        self.assertEqual(guide.title, "Mode Switching")
        self.assertIn("transport-plus-protocol mismatch", guide.summary)
        self.assertTrue(any("\u6a21\u5f0f\u5207\u6362" in bullet for bullet in guide.bullets))
        self.assertTrue(any("transport plus mode" in bullet for bullet in guide.bullets))
        self.assertIn("official ZD support tasks", guide.evidence_note)
        self.assertIn("broader official workflow pattern", guide.evidence_note)

    def test_receiver_repair_guide_is_registered(self) -> None:
        guide = support_reference.get_guide("receiver_repair")

        self.assertEqual(guide.title, "Receiver Repair")
        self.assertIn("recovery lane", guide.summary)
        self.assertTrue(any("\u63a5\u6536\u5668\u56fa\u4ef6" in bullet for bullet in guide.bullets))
        self.assertTrue(any("triangle button" in bullet for bullet in guide.bullets))
        self.assertTrue(any("verified gameplay pairing success" in bullet for bullet in guide.bullets))
        self.assertIn("official receiver-firmware/tutorial pattern", guide.evidence_note)


class LocalizedGuideTests(unittest.TestCase):
    """The three Diagnostics-reachable guides are fully localized (I-02).

    Contract: for a guide registered in ``_LOCALIZED_GUIDES``, every surface
    (title, summary, bullets, evidence note) resolves through i18n in BOTH
    locales; the EN values are byte-identical to the dataclass strings so the
    localization layer can never drift from the canonical guide content; and
    the zh values actually differ from EN (no untranslated pass-through).
    Unregistered guides keep their dataclass strings on every surface.
    """

    LOCALIZED_KEYS = ("calibration", "firmware", "windows_component_model")

    def setUp(self) -> None:
        i18n.set_locale("en")

    def tearDown(self) -> None:
        i18n.set_locale("en")

    def test_en_localized_surfaces_match_dataclass_byte_exact(self) -> None:
        # Calibration's EN summary/bullets were deliberately re-curated when it
        # was first localized (English labels instead of the dataclass's
        # embedded zh terms), so the byte-exact contract covers the guides
        # localized from the dataclass verbatim, plus calibration's title,
        # evidence note, and bullet count.
        for key in ("firmware", "windows_component_model"):
            guide = support_reference.get_guide(key)
            self.assertEqual(support_reference.localized_title(guide), guide.title)
            self.assertEqual(support_reference.localized_summary(guide), guide.summary)
            self.assertEqual(
                support_reference.localized_bullets(guide), tuple(guide.bullets)
            )
            self.assertEqual(
                support_reference.localized_evidence_note(guide), guide.evidence_note
            )
        calibration = support_reference.get_guide("calibration")
        self.assertEqual(
            support_reference.localized_title(calibration), calibration.title
        )
        self.assertEqual(
            support_reference.localized_evidence_note(calibration),
            calibration.evidence_note,
        )
        self.assertEqual(
            len(support_reference.localized_bullets(calibration)),
            len(calibration.bullets),
        )

    def test_zh_localized_surfaces_are_translated_and_complete(self) -> None:
        i18n.set_locale("zh-CN")
        for key in self.LOCALIZED_KEYS:
            guide = support_reference.get_guide(key)
            bullets = support_reference.localized_bullets(guide)
            self.assertEqual(len(bullets), len(guide.bullets), key)
            self.assertNotEqual(
                support_reference.localized_summary(guide), guide.summary, key
            )
            self.assertNotEqual(
                support_reference.localized_evidence_note(guide),
                guide.evidence_note,
                key,
            )
            for index, bullet in enumerate(bullets):
                self.assertTrue(bullet.strip(), f"{key} bullet {index} empty")
                self.assertNotEqual(
                    bullet, guide.bullets[index], f"{key} bullet {index} untranslated"
                )

    def test_zh_titles_are_translated_except_windows_stack_brand_term(self) -> None:
        i18n.set_locale("zh-CN")
        calibration = support_reference.get_guide("calibration")
        firmware = support_reference.get_guide("firmware")
        stack = support_reference.get_guide("windows_component_model")
        self.assertNotEqual(
            support_reference.localized_title(calibration), calibration.title
        )
        self.assertNotEqual(
            support_reference.localized_title(firmware), firmware.title
        )
        # "Windows" stays verbatim by terminology rule; the rest translates.
        self.assertNotEqual(support_reference.localized_title(stack), stack.title)
        self.assertIn("Windows", support_reference.localized_title(stack))

    def test_unlocalized_guides_fall_back_to_dataclass_in_zh(self) -> None:
        i18n.set_locale("zh-CN")
        guide = support_reference.get_guide("config_slots")
        self.assertEqual(support_reference.localized_title(guide), guide.title)
        self.assertEqual(support_reference.localized_summary(guide), guide.summary)
        self.assertEqual(
            support_reference.localized_bullets(guide), tuple(guide.bullets)
        )
        self.assertEqual(
            support_reference.localized_evidence_note(guide), guide.evidence_note
        )


if __name__ == "__main__":
    unittest.main()
