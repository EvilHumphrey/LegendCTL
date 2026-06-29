"""Unit tests for the pure Current-vs-Selected snapshot differ.

The differ is the bulk of the Device-vs-Profile feature's value and the only
place the honest-measurement rules live, so this suite is exhaustive: it pins
the unreadable-vs-absent distinction (driven by provenance, never a raw
``None``), collection per-slot expansion, the write-only / read-only special
fields, the 8-point sensitivity fold, exact int equality, and the aggregate
counts + stable category order.
"""

from __future__ import annotations

import unittest

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
from zd_app.services.snapshot_diff import (
    STATUS_CHANGED,
    STATUS_CURRENT_UNREADABLE,
    STATUS_ENCODING_DIFFERS,
    STATUS_ONLY_IN_PROFILE,
    STATUS_ONLY_ON_DEVICE,
    STATUS_READ_ONLY_UNSUPPORTED,
    STATUS_SAME,
    STATUS_WRITE_ONLY,
    compute_snapshot_diff,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _snap(**kw) -> ControllerSnapshot:
    """Build a ControllerSnapshot with everything absent except the overrides."""

    base = dict(
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
    base.update(kw)
    return ControllerSnapshot(**base)


def _rows_by_name(diff) -> dict:
    return {r.field_name: r for r in diff.fields}


def _anchor_pair_count(rendered: str | None) -> int:
    if rendered is None:
        return 0
    return sum(1 for token in rendered.split() if "/" in token)


_SENS_3 = (
    SensitivityAnchor(0, 0),
    SensitivityAnchor(50, 50),
    SensitivityAnchor(100, 100),
)
_SENS_8A = tuple(SensitivityAnchor(i * 10, i * 10) for i in range(8))
_SENS_8B = tuple(SensitivityAnchor(i * 10, i * 11) for i in range(8))


def _mapping(value: int) -> ButtonMapping:
    return ButtonMapping(target_kind=0x01, target_low=0x00, target_value=value)


def _lighting(brightness: int) -> LightingSettings:
    return LightingSettings(
        light_on=True,
        mode=LightingMode.ALWAYS_ON,
        brightness_byte=brightness,
        color=RgbColor(10, 20, 30),
    )


def _motion(sens: int) -> MotionSettings:
    return MotionSettings(
        target=MotionMappingTarget.LEFT_JOYSTICK,
        trigger_key=0x06,
        mode=MotionMappingMode.INSTANT,
        sensitivity=sens,
    )


# ---------------------------------------------------------------------------
# Scalar changed / same / int-equality
# ---------------------------------------------------------------------------


class ScalarDiffTests(unittest.TestCase):
    def test_changed_scalar(self) -> None:
        cur = _snap(polling_rate=PollingRate.HZ_1000)
        sel = _snap(polling_rate=PollingRate.HZ_500)
        diff = compute_snapshot_diff(
            cur, sel, current_read_success={"polling_rate": True}, current_read_errors={}
        )
        row = _rows_by_name(diff)["polling_rate"]
        self.assertEqual(row.status, STATUS_CHANGED)
        self.assertEqual(row.category, "device")
        self.assertIsNotNone(row.current_value)
        self.assertIsNotNone(row.selected_value)

    def test_same_scalar(self) -> None:
        cur = _snap(polling_rate=PollingRate.HZ_1000)
        sel = _snap(polling_rate=PollingRate.HZ_1000)
        diff = compute_snapshot_diff(
            cur, sel, current_read_success={"polling_rate": True}, current_read_errors={}
        )
        self.assertEqual(_rows_by_name(diff)["polling_rate"].status, STATUS_SAME)

    def test_int_equality_is_exact_no_epsilon(self) -> None:
        # step_size is a raw byte. 128 vs 128 is same; 128 vs 129 is changed —
        # exact equality, never a tolerance band.
        succ = {"step_size": True}
        same = compute_snapshot_diff(
            _snap(step_size=128), _snap(step_size=128), current_read_success=succ
        )
        self.assertEqual(_rows_by_name(same)["step_size"].status, STATUS_SAME)
        changed = compute_snapshot_diff(
            _snap(step_size=128), _snap(step_size=129), current_read_success=succ
        )
        self.assertEqual(_rows_by_name(changed)["step_size"].status, STATUS_CHANGED)

    def test_only_on_device_when_profile_lacks_field(self) -> None:
        cur = _snap(step_size=64)
        sel = _snap()  # profile carries no step_size
        diff = compute_snapshot_diff(
            cur, sel, current_read_success={"step_size": True}, current_read_errors={}
        )
        row = _rows_by_name(diff)["step_size"]
        self.assertEqual(row.status, STATUS_ONLY_ON_DEVICE)
        self.assertIsNotNone(row.current_value)
        self.assertIsNone(row.selected_value)

    def test_vibration_row_is_cosmetic(self) -> None:
        # Vibration = COSMETIC everywhere. The
        # Device-vs-Profile screen sections rows by this category, so the
        # vibration row renders under Cosmetic (it lived under Feel before).
        cur = _snap(vibration=VibrationSettings(1, 2, 3, 4, TriggerVibrationMode.NATIVE))
        sel = _snap(vibration=VibrationSettings(5, 6, 7, 8, TriggerVibrationMode.NATIVE))
        diff = compute_snapshot_diff(
            cur, sel, current_read_success={"vibration": True}, current_read_errors={}
        )
        self.assertEqual(_rows_by_name(diff)["vibration"].category, "cosmetic")


# ---------------------------------------------------------------------------
# None is overloaded: unreadable (provenance) vs legitimately absent
# ---------------------------------------------------------------------------


class NoneOverloadTests(unittest.TestCase):
    def test_unreadable_driven_by_read_error_not_changed(self) -> None:
        # current polling_rate is None *because the read failed* — must be
        # current_unreadable, never changed.
        cur = _snap(polling_rate=None)
        sel = _snap(polling_rate=PollingRate.HZ_1000)
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={"polling_rate": False},
            current_read_errors={"polling_rate": "TimeoutError: HID read timed out"},
        )
        row = _rows_by_name(diff)["polling_rate"]
        self.assertEqual(row.status, STATUS_CURRENT_UNREADABLE)
        self.assertNotEqual(row.status, STATUS_CHANGED)
        self.assertIn("TimeoutError", row.note)
        self.assertIsNone(row.current_value)
        # The profile's value is still shown so the user sees what it would be.
        self.assertIsNotNone(row.selected_value)

    def test_unreadable_driven_by_success_false_without_error(self) -> None:
        # A soft None-return (no exception) still reads as unreadable when the
        # provenance success map flags the field as not-captured.
        diff = compute_snapshot_diff(
            _snap(polling_rate=None),
            _snap(polling_rate=PollingRate.HZ_1000),
            current_read_success={"polling_rate": False},
            current_read_errors={},
        )
        self.assertEqual(
            _rows_by_name(diff)["polling_rate"].status, STATUS_CURRENT_UNREADABLE
        )

    def test_legitimate_none_is_only_in_profile_not_changed(self) -> None:
        # current None with NO error and NOT flagged not-captured → legitimate
        # absence → only_in_profile (informational), never changed/unreadable.
        diff = compute_snapshot_diff(
            _snap(step_size=None),
            _snap(step_size=128),
            current_read_success={},
            current_read_errors={},
        )
        row = _rows_by_name(diff)["step_size"]
        self.assertEqual(row.status, STATUS_ONLY_IN_PROFILE)
        self.assertIsNone(row.current_value)
        self.assertEqual(row.selected_value, "128")

    def test_both_absent_counts_as_same(self) -> None:
        diff = compute_snapshot_diff(
            _snap(), _snap(), current_read_success={}, current_read_errors={}
        )
        row = _rows_by_name(diff)["polling_rate"]
        self.assertEqual(row.status, STATUS_SAME)
        self.assertIsNone(row.current_value)
        self.assertIsNone(row.selected_value)


# ---------------------------------------------------------------------------
# Collections: union expansion + per-slot provenance
# ---------------------------------------------------------------------------


class CollectionDiffTests(unittest.TestCase):
    def test_button_bindings_expand_to_union_of_slots(self) -> None:
        cur = _snap(button_bindings={ButtonSlot.A: _mapping(1)})
        sel = _snap(button_bindings={ButtonSlot.A: _mapping(2), ButtonSlot.B: _mapping(3)})
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={"button_bindings": {ButtonSlot.A}},
            current_read_errors={},
        )
        names = {r.field_name for r in diff.fields}
        self.assertIn("button_bindings[A]", names)
        self.assertIn("button_bindings[B]", names)

    def test_button_slot_changed_detected(self) -> None:
        cur = _snap(button_bindings={ButtonSlot.A: _mapping(1)})
        sel = _snap(button_bindings={ButtonSlot.A: _mapping(2)})
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={"button_bindings": {ButtonSlot.A}},
            current_read_errors={},
        )
        self.assertEqual(_rows_by_name(diff)["button_bindings[A]"].status, STATUS_CHANGED)

    def test_slot_read_error_is_unreadable_not_phantom_changed(self) -> None:
        # B is in the profile + would diff, but the device read missed it (not in
        # the success set, aggregate error present). It must be current_unreadable
        # — NOT a phantom "changed".
        cur = _snap(button_bindings={ButtonSlot.A: _mapping(1)})
        sel = _snap(button_bindings={ButtonSlot.A: _mapping(1), ButtonSlot.B: _mapping(9)})
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={"button_bindings": {ButtonSlot.A}},
            current_read_errors={"button_bindings": "OSError: read failed"},
        )
        rows = _rows_by_name(diff)
        self.assertEqual(rows["button_bindings[A]"].status, STATUS_SAME)
        b = rows["button_bindings[B]"]
        self.assertEqual(b.status, STATUS_CURRENT_UNREADABLE)
        self.assertNotEqual(b.status, STATUS_CHANGED)
        self.assertIn("OSError", b.note)

    def test_slot_only_on_device(self) -> None:
        cur = _snap(button_bindings={ButtonSlot.A: _mapping(1), ButtonSlot.B: _mapping(2)})
        sel = _snap(button_bindings={ButtonSlot.A: _mapping(1)})
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={"button_bindings": {ButtonSlot.A, ButtonSlot.B}},
            current_read_errors={},
        )
        self.assertEqual(
            _rows_by_name(diff)["button_bindings[B]"].status, STATUS_ONLY_ON_DEVICE
        )

    def test_lighting_zone_collection_expands(self) -> None:
        cur = _snap(lighting_zones={LightingZone.HOME: _lighting(10)})
        sel = _snap(lighting_zones={LightingZone.HOME: _lighting(20)})
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={"lighting_zones": {LightingZone.HOME}},
            current_read_errors={},
        )
        row = _rows_by_name(diff)["lighting_zones[HOME]"]
        self.assertEqual(row.status, STATUS_CHANGED)
        self.assertEqual(row.category, "cosmetic")


# ---------------------------------------------------------------------------
# Special fields: back_paddle (write-only) + motion (read-only/unsupported)
# ---------------------------------------------------------------------------


class SpecialFieldTests(unittest.TestCase):
    def test_back_paddle_bindings_are_write_only(self) -> None:
        cur = _snap(
            back_paddle_bindings={MacroSlot.M1: BackPaddleBinding(target=ControllerButtonTarget.A)}
        )
        sel = _snap(
            back_paddle_bindings={MacroSlot.M1: BackPaddleBinding(target=ControllerButtonTarget.B)}
        )
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={"back_paddle_bindings": {MacroSlot.M1}},
            current_read_errors={},
        )
        row = _rows_by_name(diff)["back_paddle_bindings[M1]"]
        self.assertEqual(row.status, STATUS_WRITE_ONLY)
        # Current is a dash (no honest read), selected shows the profile value.
        self.assertIsNone(row.current_value)
        self.assertIsNotNone(row.selected_value)
        self.assertIsNotNone(row.note)

    def test_motion_settings_is_read_only_unsupported(self) -> None:
        cur = _snap(motion_settings=_motion(10))
        sel = _snap(motion_settings=_motion(20))
        diff = compute_snapshot_diff(
            cur, sel, current_read_success={"motion_settings": True}, current_read_errors={}
        )
        row = _rows_by_name(diff)["motion_settings"]
        self.assertEqual(row.status, STATUS_READ_ONLY_UNSUPPORTED)
        self.assertEqual(row.category, "unsupported")
        # Values still shown, but never labelled "changed".
        self.assertIsNotNone(row.current_value)
        self.assertIsNotNone(row.selected_value)


# ---------------------------------------------------------------------------
# 8-point sensitivity fold
# ---------------------------------------------------------------------------


class EightPointFoldTests(unittest.TestCase):
    def test_8point_present_suppresses_3point_host(self) -> None:
        cur = _snap(sensitivity_left=_SENS_3, sensitivity_left_8point=_SENS_8A)
        sel = _snap(sensitivity_left=_SENS_3, sensitivity_left_8point=_SENS_8B)
        diff = compute_snapshot_diff(
            cur, sel, current_read_success={"sensitivity_left": True}, current_read_errors={}
        )
        names = {r.field_name for r in diff.fields}
        self.assertIn("sensitivity_left_8point", names)
        self.assertNotIn("sensitivity_left", names)  # host suppressed

    def test_8point_values_compared(self) -> None:
        cur = _snap(sensitivity_left=_SENS_3, sensitivity_left_8point=_SENS_8A)
        sel = _snap(sensitivity_left=_SENS_3, sensitivity_left_8point=_SENS_8B)
        diff = compute_snapshot_diff(
            cur, sel, current_read_success={"sensitivity_left": True}, current_read_errors={}
        )
        self.assertEqual(
            _rows_by_name(diff)["sensitivity_left_8point"].status, STATUS_CHANGED
        )

    def test_legacy_no_8point_compares_3point_host(self) -> None:
        cur = _snap(sensitivity_left=_SENS_3)
        sel = _snap(sensitivity_left=_SENS_3)
        diff = compute_snapshot_diff(
            cur, sel, current_read_success={"sensitivity_left": True}, current_read_errors={}
        )
        names = {r.field_name for r in diff.fields}
        self.assertIn("sensitivity_left", names)
        self.assertNotIn("sensitivity_left_8point", names)
        self.assertEqual(_rows_by_name(diff)["sensitivity_left"].status, STATUS_SAME)

    def test_8point_on_one_side_only_still_folds(self) -> None:
        # Profile carries an 8-point curve; legacy device has none. The fold
        # still applies (either side has it) and the 3-point host is suppressed.
        cur = _snap(sensitivity_right=_SENS_3)
        sel = _snap(sensitivity_right=_SENS_3, sensitivity_right_8point=_SENS_8A)
        diff = compute_snapshot_diff(
            cur, sel, current_read_success={"sensitivity_right": True}, current_read_errors={}
        )
        names = {r.field_name for r in diff.fields}
        self.assertIn("sensitivity_right_8point", names)
        self.assertNotIn("sensitivity_right", names)

    def test_never_both_encodings_one_row_per_stick(self) -> None:
        cur = _snap(sensitivity_left=_SENS_3, sensitivity_left_8point=_SENS_8A)
        sel = _snap(sensitivity_left=_SENS_3, sensitivity_left_8point=_SENS_8A)
        diff = compute_snapshot_diff(
            cur, sel, current_read_success={"sensitivity_left": True}, current_read_errors={}
        )
        sens_rows = [
            r for r in diff.fields if r.field_name.startswith("sensitivity_left")
        ]
        self.assertEqual(len(sens_rows), 1)


# ---------------------------------------------------------------------------
# Mixed-encoding fold: exactly one side carries the 8-point rider
# ---------------------------------------------------------------------------


class MixedEncodingFoldTests(unittest.TestCase):
    """Hardware-confirmed 2026-06-10: a live 8-point-capable device vs a
    pre-8-point profile must not hide the 3-point side behind an
    only-on-one-side dash. Both values render under the rider name as
    ``encoding_differs`` — informational, never ``changed``."""

    def test_m1_device_rider_vs_profile_3point_renders_both(self) -> None:
        # The operator's real case: live fw-1.24 device (rider + 3-point
        # fallback) vs the pre-8-point profile "Apex Final" (3-point only).
        cur = _snap(sensitivity_left=_SENS_3, sensitivity_left_8point=_SENS_8A)
        sel = _snap(sensitivity_left=_SENS_3)
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={
                "sensitivity_left": True,
                "sensitivity_left_8point": True,
            },
            current_read_errors={},
        )
        rows = _rows_by_name(diff)
        self.assertNotIn("sensitivity_left", rows)  # host row still folded
        row = rows["sensitivity_left_8point"]
        self.assertEqual(row.status, STATUS_ENCODING_DIFFERS)
        self.assertIsNotNone(row.current_value)
        self.assertIsNotNone(row.selected_value)
        self.assertEqual(_anchor_pair_count(row.current_value), 8)  # the 8-point curve
        self.assertEqual(row.selected_value, "Linear")  # the 3-point host
        self.assertIn("not directly comparable", row.note)
        # Informational, never a change: the always-present motion row + this.
        self.assertEqual(diff.n_changed, 0)
        self.assertEqual(diff.n_informational, 2)
        self.assertEqual(diff.n_unreadable, 0)

    def test_m2_profile_rider_vs_device_3point_renders_both(self) -> None:
        cur = _snap(sensitivity_left=_SENS_3)
        sel = _snap(sensitivity_left=_SENS_3, sensitivity_left_8point=_SENS_8A)
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={"sensitivity_left": True},
            current_read_errors={},
        )
        rows = _rows_by_name(diff)
        self.assertNotIn("sensitivity_left", rows)
        row = rows["sensitivity_left_8point"]
        self.assertEqual(row.status, STATUS_ENCODING_DIFFERS)
        self.assertEqual(row.current_value, "Linear")
        self.assertEqual(_anchor_pair_count(row.selected_value), 8)
        self.assertIn("not directly comparable", row.note)
        self.assertEqual(diff.n_changed, 0)

    def test_m3_both_riders_still_compare_8point(self) -> None:
        cur = _snap(sensitivity_left=_SENS_3, sensitivity_left_8point=_SENS_8A)
        sel = _snap(sensitivity_left=_SENS_3, sensitivity_left_8point=_SENS_8B)
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={"sensitivity_left": True},
            current_read_errors={},
        )
        row = _rows_by_name(diff)["sensitivity_left_8point"]
        self.assertEqual(row.status, STATUS_CHANGED)
        self.assertIsNone(row.note)
        self.assertEqual(diff.n_changed, 1)

    def test_m4_no_riders_still_compare_3point_host(self) -> None:
        cur = _snap(sensitivity_left=_SENS_3)
        sel = _snap(sensitivity_left=_SENS_3)
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={"sensitivity_left": True},
            current_read_errors={},
        )
        rows = _rows_by_name(diff)
        self.assertNotIn("sensitivity_left_8point", rows)
        self.assertEqual(rows["sensitivity_left"].status, STATUS_SAME)

    def test_m5_rider_vs_nothing_keeps_only_on_device(self) -> None:
        cur = _snap(sensitivity_left_8point=_SENS_8A)
        sel = _snap()  # neither rider nor host for the stick
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={"sensitivity_left_8point": True},
            current_read_errors={},
        )
        row = _rows_by_name(diff)["sensitivity_left_8point"]
        self.assertEqual(row.status, STATUS_ONLY_ON_DEVICE)
        self.assertIsNotNone(row.current_value)
        self.assertIsNone(row.selected_value)

    def test_m5_mirror_rider_vs_nothing_keeps_only_in_profile(self) -> None:
        cur = _snap()
        sel = _snap(sensitivity_left_8point=_SENS_8A)
        diff = compute_snapshot_diff(
            cur, sel, current_read_success={}, current_read_errors={}
        )
        row = _rows_by_name(diff)["sensitivity_left_8point"]
        self.assertEqual(row.status, STATUS_ONLY_IN_PROFILE)
        self.assertIsNone(row.current_value)
        self.assertIsNotNone(row.selected_value)

    def test_m6_unreadable_current_host_wins_over_encoding_differs(self) -> None:
        # The profile carries the rider; the live read FAILED for the 3-point
        # host. Claiming "formats differ" would guess — unreadable wins.
        cur = _snap()
        sel = _snap(sensitivity_left=_SENS_3, sensitivity_left_8point=_SENS_8A)
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={"sensitivity_left": False},
            current_read_errors={"sensitivity_left": "TimeoutError: HID read timed out"},
        )
        row = _rows_by_name(diff)["sensitivity_left_8point"]
        self.assertEqual(row.status, STATUS_CURRENT_UNREADABLE)
        self.assertIn("TimeoutError", row.note)
        self.assertIsNone(row.current_value)
        # The profile's curve is still shown so the user sees what it holds.
        self.assertIsNotNone(row.selected_value)
        self.assertEqual(diff.n_unreadable, 1)

    def test_m6_unreadable_current_rider_wins_over_present_host(self) -> None:
        # Probe-skip scenario: the rider read was skipped (read budget) while
        # the 3-point host read fine. The rider gap must read unreadable — a
        # confident formats-differ verdict would guess the device has no curve.
        cur = _snap(sensitivity_left=_SENS_3)
        sel = _snap(sensitivity_left=_SENS_3, sensitivity_left_8point=_SENS_8A)
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={
                "sensitivity_left": True,
                "sensitivity_left_8point": False,
            },
            current_read_errors={
                "sensitivity_left_8point": "skipped: read budget exhausted"
            },
        )
        row = _rows_by_name(diff)["sensitivity_left_8point"]
        self.assertEqual(row.status, STATUS_CURRENT_UNREADABLE)
        self.assertIn("read budget", row.note)


# ---------------------------------------------------------------------------
# Defensive equality fallback
# ---------------------------------------------------------------------------


class _BoomEq:
    """A value whose ``!=`` raises — forces the string-compare fallback path."""

    def __init__(self, label: str) -> None:
        self.label = label

    def __ne__(self, other):  # noqa: D105
        raise RuntimeError("eq exploded")

    __eq__ = __ne__

    def __hash__(self):  # noqa: D105
        return id(self)

    def __str__(self) -> str:
        return self.label


class DefensiveEqualityTests(unittest.TestCase):
    def test_eq_raises_falls_back_to_string_compare(self) -> None:
        # Distinct string forms → changed via the fallback (no exception escapes).
        cur = _snap(button_bindings={ButtonSlot.A: _BoomEq("left")})
        sel = _snap(button_bindings={ButtonSlot.A: _BoomEq("right")})
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={"button_bindings": {ButtonSlot.A}},
            current_read_errors={},
        )
        self.assertEqual(_rows_by_name(diff)["button_bindings[A]"].status, STATUS_CHANGED)

    def test_eq_raises_same_string_is_same(self) -> None:
        cur = _snap(button_bindings={ButtonSlot.A: _BoomEq("same")})
        sel = _snap(button_bindings={ButtonSlot.A: _BoomEq("same")})
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={"button_bindings": {ButtonSlot.A}},
            current_read_errors={},
        )
        self.assertEqual(_rows_by_name(diff)["button_bindings[A]"].status, STATUS_SAME)


# ---------------------------------------------------------------------------
# Aggregate counts + stable category-grouped order
# ---------------------------------------------------------------------------


class AggregateAndOrderTests(unittest.TestCase):
    def test_counts_partition_all_rows(self) -> None:
        cur = _snap(
            polling_rate=PollingRate.HZ_1000,  # changed
            step_size=64,  # same
            vibration=None,  # unreadable (flagged)
            motion_settings=_motion(5),  # read_only -> informational
        )
        sel = _snap(
            polling_rate=PollingRate.HZ_500,
            step_size=64,
            vibration=VibrationSettings(1, 2, 3, 4, TriggerVibrationMode.NATIVE),
            motion_settings=_motion(9),
        )
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={
                "polling_rate": True,
                "step_size": True,
                "vibration": False,
                "motion_settings": True,
            },
            current_read_errors={"vibration": "TimeoutError: x"},
        )
        # Every row falls into exactly one of the four buckets.
        total = diff.n_changed + diff.n_same + diff.n_unreadable + diff.n_informational
        self.assertEqual(total, len(diff.fields))
        self.assertGreaterEqual(diff.n_changed, 1)
        self.assertGreaterEqual(diff.n_unreadable, 1)
        self.assertGreaterEqual(diff.n_informational, 1)

    def test_category_grouped_stable_order(self) -> None:
        # device fields first, unsupported (motion) last, regardless of the
        # scalar walk order where motion sits in the middle of the registry.
        cur = _snap(polling_rate=PollingRate.HZ_1000, motion_settings=_motion(1))
        sel = _snap(polling_rate=PollingRate.HZ_1000, motion_settings=_motion(1))
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={"polling_rate": True, "motion_settings": True},
            current_read_errors={},
        )
        categories = [r.category for r in diff.fields]
        # Categories appear in the fixed order with no interleaving.
        rank = {"device": 0, "feel": 1, "layout": 2, "cosmetic": 3, "unsupported": 4}
        ranks = [rank[c] for c in categories]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(categories[0], "device")
        self.assertEqual(categories[-1], "unsupported")

    def test_device_section_holds_polling_and_step_size(self) -> None:
        diff = compute_snapshot_diff(
            _snap(polling_rate=PollingRate.HZ_1000, step_size=64),
            _snap(polling_rate=PollingRate.HZ_1000, step_size=64),
            current_read_success={"polling_rate": True, "step_size": True},
        )
        device_fields = [r.field_name for r in diff.fields if r.category == "device"]
        self.assertEqual(device_fields, ["polling_rate", "step_size"])


# ---------------------------------------------------------------------------
# Phase 2 — the Last-Applied annotation (last_applied / last_applied_failed)
# ---------------------------------------------------------------------------
# Appended as a self-contained block (imports included) so the Phase-1 suite
# above stays byte-identical.

from zd_app.services.snapshot_diff import (  # noqa: E402 - Phase-2 block import
    DRIFT_DRIFTED,
    DRIFT_MATCHES,
)


class LastAppliedDefaultsTests(unittest.TestCase):
    """No record → every Phase-1 row keeps the annotation defaults."""

    def test_phase1_call_shape_output_unchanged(self) -> None:
        cur = _snap(polling_rate=PollingRate.HZ_1000, step_size=64)
        sel = _snap(polling_rate=PollingRate.HZ_500, step_size=64)
        provenance = {"polling_rate": True, "step_size": True}
        without = compute_snapshot_diff(cur, sel, current_read_success=provenance)
        explicit_none = compute_snapshot_diff(
            cur,
            sel,
            current_read_success=provenance,
            last_applied=None,
            last_applied_failed=frozenset(),
        )
        self.assertEqual(without, explicit_none)
        self.assertEqual(without.n_drifted, 0)
        for row in without.fields:
            self.assertIsNone(row.last_applied_value, row.field_name)
            self.assertIsNone(row.drift, row.field_name)
            self.assertFalse(row.last_applied_failed, row.field_name)

    def test_phase1_statuses_and_counts_unaffected_by_record(self) -> None:
        # The annotation never changes a Phase-1 column, status, or count.
        cur = _snap(polling_rate=PollingRate.HZ_1000, step_size=64)
        sel = _snap(polling_rate=PollingRate.HZ_500, step_size=64)
        provenance = {"polling_rate": True, "step_size": True}
        base = compute_snapshot_diff(cur, sel, current_read_success=provenance)
        annotated = compute_snapshot_diff(
            cur,
            sel,
            current_read_success=provenance,
            last_applied=_snap(polling_rate=PollingRate.HZ_8000),
        )
        self.assertEqual(
            [(r.field_name, r.status, r.current_value, r.selected_value) for r in base.fields],
            [(r.field_name, r.status, r.current_value, r.selected_value) for r in annotated.fields],
        )
        self.assertEqual(base.n_changed, annotated.n_changed)
        self.assertEqual(base.n_same, annotated.n_same)
        self.assertEqual(base.n_unreadable, annotated.n_unreadable)
        self.assertEqual(base.n_informational, annotated.n_informational)


class LastAppliedScalarDriftTests(unittest.TestCase):
    def test_scalar_drift_detected(self) -> None:
        diff = compute_snapshot_diff(
            _snap(polling_rate=PollingRate.HZ_1000),
            _snap(polling_rate=PollingRate.HZ_1000),
            current_read_success={"polling_rate": True},
            last_applied=_snap(polling_rate=PollingRate.HZ_8000),
        )
        row = _rows_by_name(diff)["polling_rate"]
        self.assertEqual(row.drift, DRIFT_DRIFTED)
        self.assertIsNotNone(row.last_applied_value)
        self.assertFalse(row.last_applied_failed)
        self.assertEqual(diff.n_drifted, 1)

    def test_scalar_matches(self) -> None:
        diff = compute_snapshot_diff(
            _snap(step_size=64),
            _snap(step_size=64),
            current_read_success={"step_size": True},
            last_applied=_snap(step_size=64),
        )
        row = _rows_by_name(diff)["step_size"]
        self.assertEqual(row.drift, DRIFT_MATCHES)
        self.assertEqual(row.last_applied_value, "64")
        self.assertEqual(diff.n_drifted, 0)

    def test_drift_independent_of_selected_column(self) -> None:
        # Drift is Current-vs-Last-Applied: a row can be only_on_device for
        # the Selected comparison and still carry a drift verdict.
        diff = compute_snapshot_diff(
            _snap(step_size=100),
            _snap(),  # selected profile lacks the field entirely
            current_read_success={"step_size": True},
            last_applied=_snap(step_size=64),
        )
        row = _rows_by_name(diff)["step_size"]
        self.assertEqual(row.status, STATUS_ONLY_ON_DEVICE)
        self.assertEqual(row.drift, DRIFT_DRIFTED)

    def test_failed_at_apply_renders_value_but_never_drift(self) -> None:
        diff = compute_snapshot_diff(
            _snap(vibration=VibrationSettings(10, 10, 10, 10, TriggerVibrationMode.NATIVE)),
            _snap(vibration=VibrationSettings(10, 10, 10, 10, TriggerVibrationMode.NATIVE)),
            current_read_success={"vibration": True},
            last_applied=_snap(
                vibration=VibrationSettings(90, 90, 90, 90, TriggerVibrationMode.NATIVE)
            ),
            last_applied_failed=frozenset({"vibration"}),
        )
        row = _rows_by_name(diff)["vibration"]
        self.assertTrue(row.last_applied_failed)
        self.assertIsNotNone(row.last_applied_value)  # what was *sent* still shows
        self.assertIsNone(row.drift)  # but is never compared
        self.assertEqual(diff.n_drifted, 0)

    def test_unreadable_current_gives_none_drift(self) -> None:
        diff = compute_snapshot_diff(
            _snap(),
            _snap(polling_rate=PollingRate.HZ_500),
            current_read_errors={"polling_rate": "timeout"},
            last_applied=_snap(polling_rate=PollingRate.HZ_8000),
        )
        row = _rows_by_name(diff)["polling_rate"]
        self.assertEqual(row.status, STATUS_CURRENT_UNREADABLE)
        self.assertIsNone(row.drift)
        self.assertIsNotNone(row.last_applied_value)  # the record still shows

    def test_legitimately_absent_current_gives_none_drift(self) -> None:
        # Absence is not called a drift (mirrors Phase 1 never calling
        # absence a change).
        diff = compute_snapshot_diff(
            _snap(),  # step_size legitimately None, no read error
            _snap(step_size=64),
            current_read_success={"step_size": True},
            last_applied=_snap(step_size=64),
        )
        row = _rows_by_name(diff)["step_size"]
        self.assertEqual(row.status, STATUS_ONLY_IN_PROFILE)
        self.assertIsNone(row.drift)

    def test_record_lacking_field_gives_no_value_and_none_drift(self) -> None:
        diff = compute_snapshot_diff(
            _snap(step_size=64),
            _snap(step_size=64),
            current_read_success={"step_size": True},
            last_applied=_snap(),  # record never carried step_size
        )
        row = _rows_by_name(diff)["step_size"]
        self.assertIsNone(row.last_applied_value)
        self.assertIsNone(row.drift)

    def test_motion_settings_never_annotated(self) -> None:
        # The apply pipeline never writes motion_settings — a recorded value
        # must not be presented as "last applied".
        diff = compute_snapshot_diff(
            _snap(motion_settings=_motion(5)),
            _snap(motion_settings=_motion(5)),
            current_read_success={"motion_settings": True},
            last_applied=_snap(motion_settings=_motion(9)),
        )
        row = _rows_by_name(diff)["motion_settings"]
        self.assertEqual(row.status, STATUS_READ_ONLY_UNSUPPORTED)
        self.assertIsNone(row.last_applied_value)
        self.assertIsNone(row.drift)


class LastAppliedCollectionTests(unittest.TestCase):
    def test_slot_drift_and_match(self) -> None:
        cur = _snap(
            button_bindings={ButtonSlot.A: _mapping(1), ButtonSlot.B: _mapping(2)}
        )
        sel = _snap(
            button_bindings={ButtonSlot.A: _mapping(1), ButtonSlot.B: _mapping(2)}
        )
        la = _snap(
            button_bindings={ButtonSlot.A: _mapping(1), ButtonSlot.B: _mapping(9)}
        )
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={"button_bindings": {ButtonSlot.A, ButtonSlot.B}},
            last_applied=la,
        )
        rows = _rows_by_name(diff)
        self.assertEqual(rows["button_bindings[A]"].drift, DRIFT_MATCHES)
        self.assertEqual(rows["button_bindings[B]"].drift, DRIFT_DRIFTED)
        self.assertEqual(diff.n_drifted, 1)

    def test_failed_slot_label_vocabulary(self) -> None:
        # failed_fields carries the coordinator's "binding_<SLOT>" labels.
        cur = _snap(button_bindings={ButtonSlot.A: _mapping(1)})
        la = _snap(button_bindings={ButtonSlot.A: _mapping(2)})
        diff = compute_snapshot_diff(
            cur,
            _snap(button_bindings={ButtonSlot.A: _mapping(1)}),
            current_read_success={"button_bindings": {ButtonSlot.A}},
            last_applied=la,
            last_applied_failed=frozenset({"binding_A"}),
        )
        row = _rows_by_name(diff)["button_bindings[A]"]
        self.assertTrue(row.last_applied_failed)
        self.assertIsNone(row.drift)

    def test_unreadable_slot_gives_none_drift(self) -> None:
        # Slot missing from the per-slot success set → honest read miss; the
        # record's value renders but no drift verdict is asserted.
        cur = _snap(button_bindings={ButtonSlot.A: _mapping(1)})
        la = _snap(
            button_bindings={ButtonSlot.A: _mapping(1), ButtonSlot.B: _mapping(9)}
        )
        diff = compute_snapshot_diff(
            cur,
            _snap(button_bindings={ButtonSlot.B: _mapping(2)}),
            current_read_success={"button_bindings": {ButtonSlot.A}},
            last_applied=la,
        )
        row = _rows_by_name(diff)["button_bindings[B]"]
        self.assertEqual(row.status, STATUS_CURRENT_UNREADABLE)
        self.assertIsNone(row.drift)
        self.assertIsNotNone(row.last_applied_value)

    def test_back_paddles_render_record_value_with_none_drift(self) -> None:
        # Write-only surface: the record is the only honest value source and
        # there is never a drift verdict.
        binding = BackPaddleBinding(target=ControllerButtonTarget.A)
        diff = compute_snapshot_diff(
            _snap(),
            _snap(back_paddle_bindings={MacroSlot.M1: binding}),
            last_applied=_snap(back_paddle_bindings={MacroSlot.M1: binding}),
        )
        row = _rows_by_name(diff)["back_paddle_bindings[M1]"]
        self.assertEqual(row.status, STATUS_WRITE_ONLY)
        self.assertIsNotNone(row.last_applied_value)
        self.assertIsNone(row.drift)

    def test_lighting_zone_drift(self) -> None:
        cur = _snap(lighting_zones={LightingZone.HOME: _lighting(100)})
        la = _snap(lighting_zones={LightingZone.HOME: _lighting(200)})
        diff = compute_snapshot_diff(
            cur,
            _snap(lighting_zones={LightingZone.HOME: _lighting(100)}),
            current_read_success={"lighting_zones": {LightingZone.HOME}},
            last_applied=la,
        )
        row = _rows_by_name(diff)["lighting_zones[HOME]"]
        self.assertEqual(row.drift, DRIFT_DRIFTED)


class LastAppliedSensitivityFoldTests(unittest.TestCase):
    def test_record_rider_attaches_to_folded_rider_row(self) -> None:
        # Current carries the 8-point curve → the stick's single row is the
        # rider; the record's 8-point value is compared rider-vs-rider.
        diff = compute_snapshot_diff(
            _snap(sensitivity_left=_SENS_3, sensitivity_left_8point=_SENS_8A),
            _snap(sensitivity_left=_SENS_3, sensitivity_left_8point=_SENS_8A),
            current_read_success={
                "sensitivity_left": True,
                "sensitivity_left_8point": True,
            },
            last_applied=_snap(
                sensitivity_left=_SENS_3, sensitivity_left_8point=_SENS_8B
            ),
        )
        rows = _rows_by_name(diff)
        self.assertNotIn("sensitivity_left", rows)  # host stays folded
        row = rows["sensitivity_left_8point"]
        self.assertEqual(row.drift, DRIFT_DRIFTED)
        self.assertEqual(_anchor_pair_count(row.last_applied_value), 8)

    def test_record_rider_matches_folded_rider_row(self) -> None:
        diff = compute_snapshot_diff(
            _snap(sensitivity_left_8point=_SENS_8A),
            _snap(sensitivity_left_8point=_SENS_8A),
            current_read_success={"sensitivity_left_8point": True},
            last_applied=_snap(sensitivity_left_8point=_SENS_8A),
        )
        row = _rows_by_name(diff)["sensitivity_left_8point"]
        self.assertEqual(row.drift, DRIFT_MATCHES)

    def test_record_8point_on_3point_host_row_is_cross_encoding(self) -> None:
        # Current + Selected are legacy 3-point (no fold) but the record holds
        # an 8-point curve: the host row shows the record's richest encoding
        # with no drift verdict (encodings are not directly comparable). The
        # rider never becomes a second row for the stick.
        diff = compute_snapshot_diff(
            _snap(sensitivity_left=_SENS_3),
            _snap(sensitivity_left=_SENS_3),
            current_read_success={"sensitivity_left": True},
            last_applied=_snap(sensitivity_left_8point=_SENS_8A),
        )
        rows = _rows_by_name(diff)
        self.assertIn("sensitivity_left", rows)
        self.assertNotIn("sensitivity_left_8point", rows)
        row = rows["sensitivity_left"]
        self.assertEqual(_anchor_pair_count(row.last_applied_value), 8)
        self.assertIsNone(row.drift)

    def test_record_3point_under_folded_rider_row_is_cross_encoding(self) -> None:
        # Current is 8-point (folded rider row); the record only carried the
        # 3-point host → value renders, drift unknowable.
        diff = compute_snapshot_diff(
            _snap(sensitivity_left_8point=_SENS_8A),
            _snap(sensitivity_left_8point=_SENS_8A),
            current_read_success={"sensitivity_left_8point": True},
            last_applied=_snap(sensitivity_left=_SENS_3),
        )
        row = _rows_by_name(diff)["sensitivity_left_8point"]
        self.assertEqual(row.last_applied_value, "Linear")
        self.assertIsNone(row.drift)

    def test_record_3point_on_3point_host_row_compares(self) -> None:
        # Fully legacy on all three sides → plain host-vs-host compare.
        drifted_3 = (
            SensitivityAnchor(0, 0),
            SensitivityAnchor(40, 60),
            SensitivityAnchor(100, 100),
        )
        diff = compute_snapshot_diff(
            _snap(sensitivity_left=_SENS_3),
            _snap(sensitivity_left=_SENS_3),
            current_read_success={"sensitivity_left": True},
            last_applied=_snap(sensitivity_left=drifted_3),
        )
        row = _rows_by_name(diff)["sensitivity_left"]
        self.assertEqual(row.drift, DRIFT_DRIFTED)

    def test_failed_sens_label_covers_both_encodings(self) -> None:
        # The coordinator writes either encoding under sens_left — a failed
        # apply marks the stick's row regardless of which encoding rode it.
        diff = compute_snapshot_diff(
            _snap(sensitivity_left_8point=_SENS_8A),
            _snap(sensitivity_left_8point=_SENS_8A),
            current_read_success={"sensitivity_left_8point": True},
            last_applied=_snap(sensitivity_left_8point=_SENS_8B),
            last_applied_failed=frozenset({"sens_left"}),
        )
        row = _rows_by_name(diff)["sensitivity_left_8point"]
        self.assertTrue(row.last_applied_failed)
        self.assertIsNone(row.drift)


class LastAppliedAggregateTests(unittest.TestCase):
    def test_n_drifted_counts_only_verified_drift(self) -> None:
        cur = _snap(
            polling_rate=PollingRate.HZ_1000,  # drifted
            step_size=64,  # matches
            vibration=VibrationSettings(10, 10, 10, 10, TriggerVibrationMode.NATIVE),
        )
        sel = _snap(polling_rate=PollingRate.HZ_1000, step_size=64)
        la = _snap(
            polling_rate=PollingRate.HZ_8000,
            step_size=64,
            vibration=VibrationSettings(50, 50, 50, 50, TriggerVibrationMode.NATIVE),
        )
        diff = compute_snapshot_diff(
            cur,
            sel,
            current_read_success={
                "polling_rate": True,
                "step_size": True,
                "vibration": True,
            },
            last_applied=la,
            last_applied_failed=frozenset({"vibration"}),  # excluded from drift
        )
        rows = _rows_by_name(diff)
        self.assertEqual(rows["polling_rate"].drift, DRIFT_DRIFTED)
        self.assertEqual(rows["step_size"].drift, DRIFT_MATCHES)
        self.assertIsNone(rows["vibration"].drift)
        self.assertEqual(diff.n_drifted, 1)


if __name__ == "__main__":  # pragma: no cover - manual driver
    unittest.main()
