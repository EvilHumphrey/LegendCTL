"""Tests for wrapper-profile ControllerSnapshot serialization."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from zd_app.services.settings_service import (
    AxisInversion,
    BackPaddleBinding,
    ButtonMapping,
    ButtonSlot,
    ControllerButtonTarget,
    ControllerSnapshot,
    LightingMode,
    LightingSettings,
    LightingZone,
    MacroSlot,
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
from zd_app.storage.snapshot_codec import snapshot_from_dict, snapshot_to_dict


def _anchors():
    return (
        SensitivityAnchor(0, 0),
        SensitivityAnchor(50, 72),
        SensitivityAnchor(100, 100),
    )


def _anchors_8point():
    # A realistic 1.2.9 / fw-1.24 8-point curve: 8 anchors, non-decreasing in
    # both axes (the shape the apply-path validator enforces). Values are all
    # distinct so a round-trip that dropped, duplicated, or reordered an anchor
    # would fail the equality assertion.
    return (
        SensitivityAnchor(0, 0),
        SensitivityAnchor(14, 10),
        SensitivityAnchor(28, 22),
        SensitivityAnchor(42, 38),
        SensitivityAnchor(57, 55),
        SensitivityAnchor(71, 72),
        SensitivityAnchor(85, 88),
        SensitivityAnchor(100, 100),
    )


def _anchors_8point_alt():
    # A second 8-point curve distinct from :func:`_anchors_8point` so the
    # left/right sticks can't accidentally pass by sharing one tuple.
    return (
        SensitivityAnchor(0, 0),
        SensitivityAnchor(10, 16),
        SensitivityAnchor(20, 33),
        SensitivityAnchor(35, 47),
        SensitivityAnchor(50, 61),
        SensitivityAnchor(66, 78),
        SensitivityAnchor(80, 90),
        SensitivityAnchor(100, 100),
    )


def _full_snapshot() -> ControllerSnapshot:
    return ControllerSnapshot(
        polling_rate=PollingRate.HZ_8000,
        vibration=VibrationSettings(
            left_grip_strength=20,
            right_grip_strength=30,
            left_trigger_motor_strength=40,
            right_trigger_motor_strength=50,
            mode=TriggerVibrationMode.TRIGGER_VIBRATION,
        ),
        deadzones=StickDeadzones(
            left_center=5,
            right_center=6,
            left_outer=90,
            right_outer=91,
        ),
        axis_inversion_left=AxisInversion(x_inverted=True, y_inverted=False),
        axis_inversion_right=AxisInversion(x_inverted=False, y_inverted=True),
        sensitivity_left=_anchors(),
        sensitivity_right=(
            SensitivityAnchor(0, 0),
            SensitivityAnchor(60, 80),
            SensitivityAnchor(100, 100),
        ),
        trigger_left=TriggerSettings(range_min=3, range_max=77, mode=TriggerMode.SHORT),
        trigger_right=TriggerSettings(range_min=4, range_max=88, mode=TriggerMode.LONG),
        button_bindings={
            ButtonSlot.A: ButtonMapping.controller_button(ControllerButtonTarget.B),
            ButtonSlot.X: ButtonMapping.controller_button(ControllerButtonTarget.Y),
        },
        lighting_zones={
            LightingZone.HOME: LightingSettings(
                light_on=True,
                mode=LightingMode.ALWAYS_ON,
                brightness_byte=180,
                color=RgbColor(1, 2, 3),
            ),
            LightingZone.LEFT_LIGHT: LightingSettings(
                light_on=False,
                mode=LightingMode.BREATH,
                brightness_byte=40,
                color=RgbColor(10, 20, 30),
            ),
        },
        motion_settings=MotionSettings(
            target=MotionMappingTarget.LEFT_JOYSTICK,
            trigger_key=0x06,
            mode=MotionMappingMode.CONTINUOUS,
            sensitivity=50,
        ),
        back_paddle_bindings={
            MacroSlot.M1: BackPaddleBinding(ControllerButtonTarget.A),
            MacroSlot.M2: BackPaddleBinding(None),
        },
    )


def _partial_snapshot() -> ControllerSnapshot:
    return ControllerSnapshot(
        polling_rate=None,
        vibration=None,
        deadzones=None,
        axis_inversion_left=None,
        axis_inversion_right=None,
        sensitivity_left=None,
        sensitivity_right=None,
        trigger_left=None,
        trigger_right=None,
        button_bindings={},
        lighting_zones={},
    )


def _json_round_trip(snapshot: ControllerSnapshot) -> ControllerSnapshot:
    payload = json.loads(json.dumps(snapshot_to_dict(snapshot)))
    return snapshot_from_dict(payload)


class SnapshotCodecTests(unittest.TestCase):
    def test_full_snapshot_round_trip(self) -> None:
        snapshot = _full_snapshot()

        self.assertEqual(_json_round_trip(snapshot), snapshot)

    def test_optional_fields_preserved_as_none(self) -> None:
        snapshot = _partial_snapshot()

        self.assertEqual(_json_round_trip(snapshot), snapshot)

    def test_vibration_round_trip(self) -> None:
        snapshot = replace(
            _partial_snapshot(),
            vibration=VibrationSettings(11, 22, 33, 44, TriggerVibrationMode.STEREO_RESONANCE),
        )

        self.assertEqual(_json_round_trip(snapshot).vibration, snapshot.vibration)

    def test_trigger_round_trip(self) -> None:
        snapshot = replace(_partial_snapshot(), trigger_left=TriggerSettings(12, 87, TriggerMode.SHORT))

        self.assertEqual(_json_round_trip(snapshot).trigger_left, snapshot.trigger_left)

    def test_deadzone_round_trip(self) -> None:
        snapshot = replace(_partial_snapshot(), deadzones=StickDeadzones(1, 2, 98, 99))

        self.assertEqual(_json_round_trip(snapshot).deadzones, snapshot.deadzones)

    def test_axis_inversion_round_trip(self) -> None:
        snapshot = replace(_partial_snapshot(), axis_inversion_right=AxisInversion(True, True))

        self.assertEqual(
            _json_round_trip(snapshot).axis_inversion_right,
            snapshot.axis_inversion_right,
        )

    def test_sensitivity_round_trip(self) -> None:
        snapshot = replace(_partial_snapshot(), sensitivity_left=_anchors())

        self.assertEqual(_json_round_trip(snapshot).sensitivity_left, snapshot.sensitivity_left)

    def test_8point_sensitivity_round_trip(self) -> None:
        # WITH 8-point curves on both sticks, layered onto an otherwise-full
        # snapshot: serialize -> deserialize -> identical, and every existing
        # field (the 3-point curves included) survives unchanged.
        snapshot = replace(
            _full_snapshot(),
            sensitivity_left_8point=_anchors_8point(),
            sensitivity_right_8point=_anchors_8point_alt(),
        )

        restored = _json_round_trip(snapshot)

        self.assertEqual(restored, snapshot)
        # All 8 anchors per stick, in order, on the correct side.
        self.assertEqual(restored.sensitivity_left_8point, _anchors_8point())
        self.assertEqual(restored.sensitivity_right_8point, _anchors_8point_alt())
        self.assertEqual(len(restored.sensitivity_left_8point), 8)
        self.assertEqual(len(restored.sensitivity_right_8point), 8)
        # The legacy 3-point fields are untouched by the 8-point round-trip.
        self.assertEqual(restored.sensitivity_left, snapshot.sensitivity_left)
        self.assertEqual(restored.sensitivity_right, snapshot.sensitivity_right)

    def test_8point_sensitivity_one_stick_only_round_trip(self) -> None:
        # Mixed capability: left has an 8-point curve, right doesn't. Both the
        # populated side and the None side must round-trip exactly.
        snapshot = replace(
            _partial_snapshot(),
            sensitivity_left_8point=_anchors_8point(),
        )

        restored = _json_round_trip(snapshot)

        self.assertEqual(restored.sensitivity_left_8point, _anchors_8point())
        self.assertIsNone(restored.sensitivity_right_8point)

    def test_8point_sensitivity_optional_none_round_trips(self) -> None:
        snapshot = replace(
            _partial_snapshot(),
            sensitivity_left_8point=None,
            sensitivity_right_8point=None,
        )

        restored = _json_round_trip(snapshot)

        self.assertIsNone(restored.sensitivity_left_8point)
        self.assertIsNone(restored.sensitivity_right_8point)

    def test_legacy_profile_without_8point_loads(self) -> None:
        # Backward-compat: a snapshot serialized before the 8-point fields
        # existed has no ``sensitivity_*_8point`` keys. It must still load,
        # with the 8-point fields defaulting to None and every existing field
        # (here the populated 3-point curve) intact — i.e. it deserializes to
        # exactly the legacy snapshot it came from.
        payload = snapshot_to_dict(_full_snapshot())
        payload.pop("sensitivity_left_8point")
        payload.pop("sensitivity_right_8point")

        restored = snapshot_from_dict(payload)

        self.assertIsNone(restored.sensitivity_left_8point)
        self.assertIsNone(restored.sensitivity_right_8point)
        self.assertEqual(restored.sensitivity_left, _anchors())
        self.assertEqual(restored.polling_rate, PollingRate.HZ_8000)
        self.assertEqual(restored, _full_snapshot())

    def test_button_binding_round_trip(self) -> None:
        snapshot = replace(
            _partial_snapshot(),
            button_bindings={
                ButtonSlot.LB: ButtonMapping.controller_button(ControllerButtonTarget.RB),
            },
        )

        self.assertEqual(_json_round_trip(snapshot).button_bindings, snapshot.button_bindings)

    def test_back_paddle_bindings_round_trip(self) -> None:
        snapshot = replace(
            _partial_snapshot(),
            back_paddle_bindings={
                slot: BackPaddleBinding(ControllerButtonTarget.A)
                for slot in MacroSlot
            },
        )

        self.assertEqual(
            _json_round_trip(snapshot).back_paddle_bindings,
            snapshot.back_paddle_bindings,
        )

    def test_back_paddle_bindings_partial_round_trip(self) -> None:
        snapshot = replace(
            _partial_snapshot(),
            back_paddle_bindings={
                MacroSlot.M1: BackPaddleBinding(ControllerButtonTarget.X),
                MacroSlot.LM: BackPaddleBinding(ControllerButtonTarget.BACK),
            },
        )

        self.assertEqual(
            _json_round_trip(snapshot).back_paddle_bindings,
            snapshot.back_paddle_bindings,
        )

    def test_back_paddle_bindings_unbound_round_trip(self) -> None:
        snapshot = replace(
            _partial_snapshot(),
            back_paddle_bindings={MacroSlot.RK: BackPaddleBinding(None)},
        )

        self.assertEqual(
            _json_round_trip(snapshot).back_paddle_bindings,
            {MacroSlot.RK: BackPaddleBinding(None)},
        )

    def test_lighting_round_trip(self) -> None:
        snapshot = replace(
            _partial_snapshot(),
            lighting_zones={
                LightingZone.RIGHT_LIGHT: LightingSettings(
                    light_on=True,
                    mode=LightingMode.FLOW,
                    brightness_byte=255,
                    color=RgbColor(240, 128, 64),
                ),
            },
        )

        self.assertEqual(_json_round_trip(snapshot).lighting_zones, snapshot.lighting_zones)

    def test_motion_round_trip(self) -> None:
        cases = [
            MotionSettings(
                target=MotionMappingTarget.DISABLED,
                trigger_key=0x06,
                mode=MotionMappingMode.INSTANT,
                sensitivity=0,
            ),
            MotionSettings(
                target=MotionMappingTarget.LEFT_JOYSTICK,
                trigger_key=0x06,
                mode=MotionMappingMode.CONTINUOUS,
                sensitivity=50,
            ),
            MotionSettings(
                target=MotionMappingTarget.RIGHT_JOYSTICK,
                trigger_key=0x06,
                mode=MotionMappingMode.CONTINUOUS,
                sensitivity=100,
            ),
        ]

        for motion in cases:
            with self.subTest(motion=motion):
                snapshot = replace(_partial_snapshot(), motion_settings=motion)

                self.assertEqual(_json_round_trip(snapshot).motion_settings, motion)

    def test_motion_optional_none_round_trips(self) -> None:
        snapshot = replace(_partial_snapshot(), motion_settings=None)

        self.assertIsNone(_json_round_trip(snapshot).motion_settings)

    def test_legacy_profile_without_motion_loads(self) -> None:
        payload = snapshot_to_dict(_full_snapshot())
        payload.pop("motion_settings")

        restored = snapshot_from_dict(payload)

        self.assertIsNone(restored.motion_settings)
        self.assertEqual(restored.polling_rate, PollingRate.HZ_8000)

    def test_legacy_profile_without_back_paddles_loads(self) -> None:
        payload = snapshot_to_dict(_full_snapshot())
        payload.pop("back_paddle_bindings")

        restored = snapshot_from_dict(payload)

        self.assertEqual(restored.back_paddle_bindings, {})
        self.assertEqual(restored.polling_rate, PollingRate.HZ_8000)


class SnapshotLoadValidationTests(unittest.TestCase):
    """M1: out-of-range / wrong-type values are rejected at the load boundary."""

    def _payload(self) -> dict:
        return snapshot_to_dict(_full_snapshot())

    def test_deadzone_out_of_range_rejected(self) -> None:
        payload = self._payload()
        payload["deadzones"]["left_center"] = 99999
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_brightness_negative_rejected(self) -> None:
        payload = self._payload()
        zone_key = next(iter(payload["lighting_zones"]))
        payload["lighting_zones"][zone_key]["brightness_byte"] = -1
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_rgb_out_of_range_rejected(self) -> None:
        payload = self._payload()
        zone_key = next(iter(payload["lighting_zones"]))
        payload["lighting_zones"][zone_key]["color"]["r"] = 256
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_vibration_out_of_range_rejected(self) -> None:
        payload = self._payload()
        payload["vibration"]["left_grip_strength"] = 101
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_trigger_min_greater_than_max_rejected(self) -> None:
        payload = self._payload()
        payload["trigger_left"]["range_min"] = 80
        payload["trigger_left"]["range_max"] = 10
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_sensitivity_anchor_out_of_range_rejected(self) -> None:
        payload = self._payload()
        payload["sensitivity_left"][0]["x"] = 200
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_8point_sensitivity_anchor_out_of_range_rejected(self) -> None:
        # The 8-point deserializer range-checks each anchor as a percent, same
        # as the 3-point path. ``_full_snapshot`` has no 8-point data, so build
        # a payload that does, then corrupt one anchor.
        payload = snapshot_to_dict(
            replace(_full_snapshot(), sensitivity_left_8point=_anchors_8point())
        )
        payload["sensitivity_left_8point"][3]["y"] = 200
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_motion_value_out_of_range_rejected(self) -> None:
        payload = self._payload()
        payload["motion_settings"]["sensitivity"] = 999
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_boolean_where_int_expected_rejected(self) -> None:
        payload = self._payload()
        payload["deadzones"]["left_center"] = True
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_non_dict_snapshot_rejected_as_value_error(self) -> None:
        with self.assertRaises(ValueError):
            snapshot_from_dict([1, 2, 3])

    def test_missing_required_subkey_rejected_as_value_error(self) -> None:
        """A vibration dict missing its required sub-keys previously escaped as
        KeyError('left_grip_strength') despite the normalize-to-ValueError
        contract; it must surface as ValueError naming the missing key."""
        with self.assertRaises(ValueError) as ctx:
            snapshot_from_dict({"vibration": {}})
        self.assertIn("left_grip_strength", str(ctx.exception))

    def test_unknown_back_paddle_slot_rejected_as_value_error(self) -> None:
        """An unknown MacroSlot name in back_paddle_bindings previously escaped
        as KeyError('NOPE'); it must surface as ValueError naming the key."""
        with self.assertRaises(ValueError) as ctx:
            snapshot_from_dict({"back_paddle_bindings": {"NOPE": {"target": None}}})
        self.assertIn("NOPE", str(ctx.exception))

    def test_non_bool_axis_inversion_rejected_as_value_error(self) -> None:
        payload = self._payload()
        payload["axis_inversion_left"]["x_inverted"] = 1
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_non_bool_light_on_rejected(self) -> None:
        # LightingSettings has no __post_init__, so the codec must reject a
        # truthy-int light_on itself (1 != True) at the decode boundary.
        payload = self._payload()
        zone_key = next(iter(payload["lighting_zones"]))
        payload["lighting_zones"][zone_key]["light_on"] = 1
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_bool_collection_key_rejected(self) -> None:
        # ``True``/``False`` are ints in Python, so a bool collection key would
        # fold onto slot 0/1; reject it before the int coercion (matching the
        # _percent/_byte bool guard).
        payload = self._payload()
        mapping = next(iter(payload["button_bindings"].values()))
        payload["button_bindings"] = {True: dict(mapping)}
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_button_mapping_unsupported_target_kind_rejected(self) -> None:
        payload = self._payload()
        slot_key = next(iter(payload["button_bindings"]))
        payload["button_bindings"][slot_key]["target_kind"] = 0x02
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_button_mapping_nonzero_target_low_rejected(self) -> None:
        payload = self._payload()
        slot_key = next(iter(payload["button_bindings"]))
        payload["button_bindings"][slot_key]["target_low"] = 0x01
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_button_mapping_unsupported_target_value_rejected(self) -> None:
        # 0x05 is in byte range but not a ControllerButtonTarget; an in-range
        # foreign value used to slip through and only fail (silently) at apply.
        payload = self._payload()
        slot_key = next(iter(payload["button_bindings"]))
        payload["button_bindings"][slot_key]["target_value"] = 0x05
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_button_mapping_supported_target_round_trips(self) -> None:
        # Guard the validator isn't over-broad: a real 01 00 TT remap still loads
        # and preserves the target value.
        snapshot = replace(
            _partial_snapshot(),
            button_bindings={
                ButtonSlot.A: ButtonMapping(0x01, 0x00, ControllerButtonTarget.START.value)
            },
        )
        restored = _json_round_trip(snapshot)
        self.assertEqual(
            restored.button_bindings[ButtonSlot.A].target_value,
            ControllerButtonTarget.START.value,
        )

    def test_build_snapshot_ignores_arbitrary_extra_keys(self) -> None:
        # SECURITY BOUNDARY (deny-by-default): _build_snapshot reads only the
        # known snapshot keys; arbitrary extra keys — automation-looking or
        # otherwise — are silently dropped, so a spiked payload deserializes to
        # exactly the same snapshot as the clean one. If this ever fails, the
        # codec has started looking at unknown keys (e.g. someone splatted the
        # payload) and the Safe-Import guarantee is breached.
        base = snapshot_to_dict(_full_snapshot())
        spiked = dict(base)
        spiked["rapid_fire"] = {"enabled": True}
        spiked["turbo_enabled"] = True
        spiked["macro_binding"] = "A->B->A"
        spiked["script"] = "do_evil()"
        spiked["__proto__"] = "evil"
        self.assertEqual(snapshot_from_dict(spiked), snapshot_from_dict(base))

    def test_step_size_round_trip(self) -> None:
        snapshot = replace(_partial_snapshot(), step_size=146)

        self.assertEqual(_json_round_trip(snapshot).step_size, 146)

    def test_step_size_none_round_trips(self) -> None:
        snapshot = replace(_partial_snapshot(), step_size=None)

        self.assertIsNone(_json_round_trip(snapshot).step_size)

    def test_step_size_boundary_values_accepted(self) -> None:
        for value in (1, 255):
            with self.subTest(value=value):
                snapshot = replace(_partial_snapshot(), step_size=value)
                self.assertEqual(_json_round_trip(snapshot).step_size, value)

    def test_step_size_zero_rejected(self) -> None:
        payload = self._payload()
        payload["step_size"] = 0
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_step_size_above_byte_max_rejected(self) -> None:
        payload = self._payload()
        payload["step_size"] = 256
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_step_size_non_int_rejected(self) -> None:
        payload = self._payload()
        payload["step_size"] = "146"
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_step_size_bool_rejected(self) -> None:
        payload = self._payload()
        payload["step_size"] = True
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_legacy_profile_without_step_size_loads(self) -> None:
        # Device-settings migration: a pre-device-settings profile JSON has no ``step_size`` key →
        # loads with step_size=None (keep data, prompt on next save).
        payload = snapshot_to_dict(_full_snapshot())
        payload.pop("step_size", None)

        restored = snapshot_from_dict(payload)

        self.assertIsNone(restored.step_size)
        self.assertEqual(restored.polling_rate, PollingRate.HZ_8000)

    def test_boundary_values_accepted(self) -> None:
        payload = self._payload()
        payload["deadzones"]["left_center"] = 0
        payload["deadzones"]["right_outer"] = 100
        zone_key = next(iter(payload["lighting_zones"]))
        payload["lighting_zones"][zone_key]["brightness_byte"] = 255
        payload["lighting_zones"][zone_key]["color"]["r"] = 0

        restored = snapshot_from_dict(payload)

        self.assertEqual(restored.deadzones.left_center, 0)
        self.assertEqual(restored.deadzones.right_outer, 100)

    def test_duplicate_normalized_button_slot_keys_rejected(self) -> None:
        """B1: two distinct JSON string keys that ``int()`` to the same value
        ("5" and "05") would collapse to one member (last-wins), silently
        dropping the other. The ambiguous profile must be rejected via the
        codec's existing ValueError path instead of being silently lossy."""
        payload = self._payload()
        mapping = next(iter(payload["button_bindings"].values()))
        payload["button_bindings"] = {"5": dict(mapping), "05": dict(mapping)}
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_non_canonical_collection_key_rejected(self) -> None:
        """B1: a non-canonical collection key (``str(int(raw)) != raw``, e.g.
        "05" or " 5") that ``int()`` folds is rejected even without a colliding
        sibling — canonical enforcement makes the silent drop impossible."""
        payload = self._payload()
        settings = next(iter(payload["lighting_zones"].values()))
        payload["lighting_zones"] = {"05": dict(settings)}
        with self.assertRaises(ValueError):
            snapshot_from_dict(payload)

    def test_enum_field_bool_rejected(self) -> None:
        """P1: every enum field decoded via ``_enum_from`` must reject a JSON
        bool. ``True``/``False`` are ints, so ``PollingRate(True)`` used to
        coerce to a valid member (HZ_250) and load a garbled import silently
        rather than rejecting it. One guard covers all six enum fields."""

        def _polling(p: dict) -> None:
            p["polling_rate"] = True

        def _vibration_mode(p: dict) -> None:
            p["vibration"]["mode"] = True

        def _motion_target(p: dict) -> None:
            p["motion_settings"]["target"] = True

        def _motion_mode(p: dict) -> None:
            p["motion_settings"]["mode"] = True

        def _trigger_mode(p: dict) -> None:
            p["trigger_left"]["mode"] = True

        def _lighting_mode(p: dict) -> None:
            zone_key = next(iter(p["lighting_zones"]))
            p["lighting_zones"][zone_key]["mode"] = True

        for field_name, mutate in (
            ("polling_rate", _polling),
            ("vibration.mode", _vibration_mode),
            ("motion.target", _motion_target),
            ("motion.mode", _motion_mode),
            ("trigger.mode", _trigger_mode),
            ("lighting.mode", _lighting_mode),
        ):
            with self.subTest(field=field_name):
                payload = self._payload()
                mutate(payload)
                with self.assertRaises(ValueError):
                    snapshot_from_dict(payload)

    def test_back_paddle_malformed_binding_rejected(self) -> None:
        """P2: a back-paddle binding payload must be a mapping with an explicit
        ``target`` key. Falsy payloads (``false`` / ``""`` / ``[]`` / ``{}``)
        used to be a silent unbind, and ``0`` / ``""`` target values coerced to
        unbound while truthy-invalid raised — an inconsistent boundary."""
        for bad in (False, "", [], {}, {"target": 0}, {"target": ""}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    snapshot_from_dict({"back_paddle_bindings": {"M1": bad}})

    def test_back_paddle_explicit_null_unbinds(self) -> None:
        """P2 guard: the well-formed explicit-unbind shape ``{"target": null}``
        still round-trips to an unbound binding (only ``null`` unbinds now)."""
        restored = snapshot_from_dict(
            {"back_paddle_bindings": {"M1": {"target": None}}}
        )
        self.assertIn(MacroSlot.M1, restored.back_paddle_bindings)
        self.assertIsNone(restored.back_paddle_bindings[MacroSlot.M1].target)


if __name__ == "__main__":
    unittest.main()
