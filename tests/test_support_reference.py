from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
