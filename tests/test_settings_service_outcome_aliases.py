"""Alias contract for the unified settings-write outcome enum.

The ten historical ``Set*Outcome`` names must stay importable and must all BE
``WriteOutcome`` (the same class object), so existing imports, ``isinstance``
checks, and cross-feature identity/equality comparisons keep working.
"""

from __future__ import annotations

import unittest

from zd_app.services.settings_service import (
    SetAxisInversionOutcome,
    SetBackPaddleBindingOutcome,
    SetButtonBindingOutcome,
    SetDeadzoneOutcome,
    SetLightingOutcome,
    SetPollingRateOutcome,
    SetSensitivityCurveOutcome,
    SetStepSizeOutcome,
    SetTriggerSettingsOutcome,
    SetVibrationOutcome,
    WriteOutcome,
)


_ALIASES = {
    "SetPollingRateOutcome": SetPollingRateOutcome,
    "SetButtonBindingOutcome": SetButtonBindingOutcome,
    "SetBackPaddleBindingOutcome": SetBackPaddleBindingOutcome,
    "SetDeadzoneOutcome": SetDeadzoneOutcome,
    "SetStepSizeOutcome": SetStepSizeOutcome,
    "SetSensitivityCurveOutcome": SetSensitivityCurveOutcome,
    "SetAxisInversionOutcome": SetAxisInversionOutcome,
    "SetTriggerSettingsOutcome": SetTriggerSettingsOutcome,
    "SetVibrationOutcome": SetVibrationOutcome,
    "SetLightingOutcome": SetLightingOutcome,
}


class TestWriteOutcomeAliases(unittest.TestCase):
    def test_all_ten_alias_names_are_write_outcome(self) -> None:
        self.assertEqual(len(_ALIASES), 10)
        for name, alias in _ALIASES.items():
            with self.subTest(alias=name):
                self.assertIs(alias, WriteOutcome)

    def test_member_value_strings(self) -> None:
        self.assertEqual(
            {member.name: member.value for member in WriteOutcome},
            {
                "OK": "ok",
                "OK_WITH_RETRY": "ok_with_retry",
                "DEVICE_NOT_FOUND": "device_not_found",
                "OPEN_FAILED": "open_failed",
                "WRITE_FAILED": "write_failed",
                # Added for the step_size verify-and-retry setter (revert-to-1
                # fix): a write the seam accepted but the device never committed.
                "VERIFY_FAILED": "verify_failed",
            },
        )

    def test_cross_alias_members_are_identical(self) -> None:
        self.assertIs(SetVibrationOutcome.OK, SetLightingOutcome.OK)
        self.assertEqual(
            SetPollingRateOutcome.DEVICE_NOT_FOUND,
            SetStepSizeOutcome.DEVICE_NOT_FOUND,
        )
