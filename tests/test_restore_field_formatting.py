"""Unit tests for :func:`zd_app.services.restore_field_formatting.format_field_value`.

Pin the compact one-line rendering of each controller-payload type so the
preview modal and result page never regress to the noisy default
``str(dataclass)`` form ("VibrationSettings(left_grip_strength=..., ...,
mode=<TriggerVibrationMode.NATIVE: 0>)"). The contract:

- compact output contains all key=value info,
- ≤80 chars per field,
- no Python class names leak into the user-facing string.

Scalars and enums keep the existing restore-result-enrichment convention
(``str(value)``) so the polling-rate / step-size paths don't change.
"""

from __future__ import annotations

import unittest

from zd_app.services.restore_field_formatting import format_field_value
from zd_app.services.settings_service import (
    AxisInversion,
    BackPaddleBinding,
    ButtonMapping,
    ControllerButtonTarget,
    LightingMode,
    LightingSettings,
    LightingZone,
    MotionMappingMode,
    MotionMappingTarget,
    MotionSettings,
    PollingRate,
    RgbColor,
    SensitivityAnchor,
    StickDeadzones,
    TriggerMode,
    TriggerSettings,
    TriggerVibrationMode,
    VibrationSettings,
)


# Class names that must NEVER leak into the rendered string — these are the
# default ``repr()``-style prefixes that ``str()`` on a frozen dataclass
# emits, which is exactly what this helper exists to suppress.
_LEAKED_CLASS_NAMES = (
    "VibrationSettings(",
    "StickDeadzones(",
    "AxisInversion(",
    "TriggerSettings(",
    "ButtonMapping(",
    "BackPaddleBinding(",
    "LightingSettings(",
    "MotionSettings(",
    "RgbColor(",
    "SensitivityAnchor(",
)


def _assert_no_class_leak(test: unittest.TestCase, rendered: str) -> None:
    for fragment in _LEAKED_CLASS_NAMES:
        test.assertNotIn(
            fragment,
            rendered,
            f"class name {fragment!r} leaked into compact render: {rendered!r}",
        )


def _assert_within_budget(test: unittest.TestCase, rendered: str) -> None:
    """Spec budget: ≤80 chars per field. Long enough to carry every key=value
    pair for the in-suite dataclasses; short enough to fit one line of the
    CONFIRM modal at the wrapper's default column width.
    """
    test.assertLessEqual(
        len(rendered),
        80,
        f"compact render exceeds 80 chars ({len(rendered)}): {rendered!r}",
    )


class VibrationSettingsTests(unittest.TestCase):
    def test_rendered_form_carries_all_four_strengths_and_mode(self) -> None:
        value = VibrationSettings(
            left_grip_strength=10,
            right_grip_strength=20,
            left_trigger_motor_strength=30,
            right_trigger_motor_strength=40,
            mode=TriggerVibrationMode.NATIVE,
        )
        rendered = format_field_value("vibration", value)
        for fragment in ("10", "20", "30", "40", "NATIVE"):
            self.assertIn(fragment, rendered)
        _assert_no_class_leak(self, rendered)
        _assert_within_budget(self, rendered)

    def test_mode_name_matches_enum_member(self) -> None:
        value = VibrationSettings(0, 0, 0, 0, TriggerVibrationMode.TRIGGER_VIBRATION)
        self.assertIn("TRIGGER_VIBRATION", format_field_value("vibration", value))


class StickDeadzonesTests(unittest.TestCase):
    def test_rendered_form_carries_all_four_values(self) -> None:
        value = StickDeadzones(
            left_center=5,
            right_center=6,
            left_outer=90,
            right_outer=91,
        )
        rendered = format_field_value("deadzones", value)
        for fragment in ("5", "6", "90", "91"):
            self.assertIn(fragment, rendered)
        _assert_no_class_leak(self, rendered)
        _assert_within_budget(self, rendered)


class AxisInversionTests(unittest.TestCase):
    def test_renders_both_axes(self) -> None:
        value = AxisInversion(x_inverted=True, y_inverted=False)
        rendered = format_field_value("axis_inversion_left", value)
        self.assertIn("True", rendered)
        self.assertIn("False", rendered)
        _assert_no_class_leak(self, rendered)
        _assert_within_budget(self, rendered)


class TriggerSettingsTests(unittest.TestCase):
    def test_renders_range_and_mode(self) -> None:
        value = TriggerSettings(range_min=15, range_max=200, mode=TriggerMode.SHORT)
        rendered = format_field_value("trigger_left", value)
        self.assertIn("15", rendered)
        self.assertIn("200", rendered)
        self.assertIn("SHORT", rendered)
        _assert_no_class_leak(self, rendered)
        _assert_within_budget(self, rendered)


class SensitivityCurveTests(unittest.TestCase):
    def test_renders_each_anchor_as_xy_pair(self) -> None:
        value = (
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
        )
        rendered = format_field_value("sensitivity_left", value)
        self.assertIn("0,0", rendered)
        self.assertIn("50,50", rendered)
        self.assertIn("100,100", rendered)
        _assert_no_class_leak(self, rendered)
        _assert_within_budget(self, rendered)

    def test_empty_anchor_tuple_falls_back_to_str(self) -> None:
        # The defined SensitivityAnchorTuple shape is always length-3, but
        # the helper checks for the anchor-sequence shape via runtime
        # isinstance; an empty tuple is not a curve and should fall through
        # to the default str() path (returning "()") rather than raising.
        rendered = format_field_value("sensitivity_left", ())
        self.assertEqual(rendered, "()")

    def test_renders_all_eight_anchors_of_8point_curve(self) -> None:
        # The 1.2.9 / fw-1.24 8-point curve uses the same anchor-sequence shape
        # as the 3-point curve, so format_field_value renders it via the same
        # path with no special-casing — each of the 8 anchors must appear as an
        # (x,y) pair so an RP preview/diff on a fw-1.24 controller is accurate.
        value = (
            SensitivityAnchor(0, 0),
            SensitivityAnchor(14, 10),
            SensitivityAnchor(28, 22),
            SensitivityAnchor(42, 38),
            SensitivityAnchor(57, 55),
            SensitivityAnchor(71, 72),
            SensitivityAnchor(85, 88),
            SensitivityAnchor(100, 100),
        )
        rendered = format_field_value("sensitivity_left_8point", value)
        for pair in ("(0,0)", "(14,10)", "(42,38)", "(71,72)", "(85,88)", "(100,100)"):
            self.assertIn(pair, rendered)
        _assert_no_class_leak(self, rendered)
        _assert_within_budget(self, rendered)


class ButtonMappingTests(unittest.TestCase):
    def test_controller_button_renders_target_name_with_arrow(self) -> None:
        value = ButtonMapping.controller_button(ControllerButtonTarget.B)
        rendered = format_field_value("button_bindings[A]", value)
        self.assertIn("B", rendered)
        _assert_no_class_leak(self, rendered)
        _assert_within_budget(self, rendered)

    def test_unknown_target_value_falls_back_to_hex(self) -> None:
        # An unrecognized target byte under the standard kind/low envelope
        # must still produce a readable diagnostic line — fall back to hex
        # rather than crashing or rendering "ControllerButtonTarget.None".
        value = ButtonMapping(target_kind=0x01, target_low=0x00, target_value=0xFE)
        rendered = format_field_value("button_bindings[A]", value)
        self.assertIn("0xfe", rendered)
        _assert_no_class_leak(self, rendered)

    def test_non_standard_envelope_renders_raw_bytes(self) -> None:
        value = ButtonMapping(target_kind=0x02, target_low=0x01, target_value=0x40)
        rendered = format_field_value("button_bindings[A]", value)
        self.assertIn("0x02", rendered)
        self.assertIn("0x01", rendered)
        self.assertIn("0x40", rendered)
        _assert_no_class_leak(self, rendered)


class BackPaddleBindingTests(unittest.TestCase):
    def test_bound_target_renders_name(self) -> None:
        value = BackPaddleBinding(target=ControllerButtonTarget.X)
        rendered = format_field_value("back_paddle_bindings[M1]", value)
        self.assertIn("X", rendered)
        _assert_no_class_leak(self, rendered)
        _assert_within_budget(self, rendered)

    def test_unbound_renders_unbound_literal(self) -> None:
        value = BackPaddleBinding(target=None)
        self.assertEqual(
            format_field_value("back_paddle_bindings[M1]", value),
            "unbound",
        )


class LightingSettingsTests(unittest.TestCase):
    def test_rendered_form_carries_state_mode_brightness_and_rgb(self) -> None:
        value = LightingSettings(
            light_on=True,
            mode=LightingMode.ALWAYS_ON,
            brightness_byte=200,
            color=RgbColor(10, 20, 30),
        )
        rendered = format_field_value("lighting_zones[HOME]", value)
        self.assertIn("ALWAYS_ON", rendered)
        self.assertIn("200", rendered)
        # RGB bytes must be present; the compact RGB(r,g,b) sub-form is
        # asserted via its individual numbers so a future spacing tweak
        # doesn't break the test.
        self.assertIn("10", rendered)
        self.assertIn("20", rendered)
        self.assertIn("30", rendered)
        _assert_no_class_leak(self, rendered)
        _assert_within_budget(self, rendered)

    def test_light_off_renders_off_literal(self) -> None:
        value = LightingSettings(
            light_on=False,
            mode=LightingMode.OFF,
            brightness_byte=0,
            color=RgbColor(0, 0, 0),
        )
        rendered = format_field_value("lighting_zones[HOME]", value)
        self.assertIn("off", rendered)
        self.assertIn("OFF", rendered)


class MotionSettingsTests(unittest.TestCase):
    def test_renders_target_mode_trigger_key_and_sens(self) -> None:
        value = MotionSettings(
            target=MotionMappingTarget.LEFT_JOYSTICK,
            trigger_key=0x06,
            mode=MotionMappingMode.INSTANT,
            sensitivity=42,
        )
        rendered = format_field_value("motion_settings", value)
        self.assertIn("LEFT_JOYSTICK", rendered)
        self.assertIn("INSTANT", rendered)
        self.assertIn("0x06", rendered)
        self.assertIn("42", rendered)
        _assert_no_class_leak(self, rendered)
        _assert_within_budget(self, rendered)


class RgbColorTests(unittest.TestCase):
    def test_renders_rgb_triple(self) -> None:
        value = RgbColor(r=255, g=128, b=0)
        rendered = format_field_value("color", value)
        self.assertIn("255", rendered)
        self.assertIn("128", rendered)
        self.assertIn("0", rendered)
        _assert_no_class_leak(self, rendered)
        _assert_within_budget(self, rendered)


class ScalarAndEnumPassthroughTests(unittest.TestCase):
    """Scalars and enums keep the existing ``str(value)`` convention so the
    restore-result-enrichment / pre-restore-preview behavior for
    polling_rate and step_size is unchanged.
    """

    def test_int_renders_via_str(self) -> None:
        self.assertEqual(format_field_value("step_size", 128), "128")

    def test_bool_renders_via_str(self) -> None:
        self.assertEqual(format_field_value("any", True), "True")

    def test_polling_rate_enum_keeps_existing_render(self) -> None:
        # Existing test in ComputeRestorePreviewTests asserts the rendered
        # form contains the variant name ("HZ_1000") — the compact helper
        # must preserve that.
        rendered = format_field_value("polling_rate", PollingRate.HZ_1000)
        self.assertIn("HZ_1000", rendered)

    def test_lighting_zone_enum_keeps_existing_render(self) -> None:
        rendered = format_field_value("lighting_zones", LightingZone.HOME)
        self.assertIn("HOME", rendered)

    def test_none_renders_as_none_literal(self) -> None:
        self.assertEqual(format_field_value("any", None), "None")


class FieldNameParameterTests(unittest.TestCase):
    """The ``name`` parameter is accepted for forward-compatibility but
    type-based dispatch is sufficient for every case the spec covers.
    Pin that today: the rendered form is identical regardless of ``name``.
    """

    def test_name_does_not_alter_render_today(self) -> None:
        value = StickDeadzones(1, 2, 3, 4)
        self.assertEqual(
            format_field_value("deadzones", value),
            format_field_value("any_other_name", value),
        )


if __name__ == "__main__":
    unittest.main()
