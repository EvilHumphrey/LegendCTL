"""Round-trip + schema tests for restore_point_models JSON codec."""

from __future__ import annotations

import unittest
from dataclasses import replace

from zd_app.services.restore_points import (
    CLAIM_BOUNDARY_PARAGRAPH,
    CLAIM_BOUNDARY_SHORT_UI,
)
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
from zd_app.storage.restore_point_models import (
    KIND,
    SCHEMA_VERSION,
    CaptureSource,
    CoverageCategory,
    CoverageState,
    DeviceIdentity,
    FieldCoverage,
    IdentityConfidence,
    RestoreAttemptRecord,
    RestoreFieldOutcome,
    RestorePoint,
    RestorePointCoverage,
    RestorePointParseError,
    RestorePointSchemaError,
    RestorePointTrigger,
    RestoreResult,
    RestoreResultLabel,
    restore_point_from_dict,
    restore_point_to_dict,
)


def _full_snapshot() -> ControllerSnapshot:
    return ControllerSnapshot(
        polling_rate=PollingRate.HZ_1000,
        vibration=VibrationSettings(10, 20, 30, 40, TriggerVibrationMode.NATIVE),
        deadzones=StickDeadzones(5, 6, 90, 91),
        axis_inversion_left=AxisInversion(True, False),
        axis_inversion_right=AxisInversion(False, True),
        sensitivity_left=(
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
        ),
        sensitivity_right=(
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
        ),
        trigger_left=TriggerSettings(0, 100, TriggerMode.LONG),
        trigger_right=TriggerSettings(0, 100, TriggerMode.LONG),
        button_bindings={
            ButtonSlot.A: ButtonMapping.controller_button(ControllerButtonTarget.B),
        },
        lighting_zones={
            LightingZone.HOME: LightingSettings(
                True, LightingMode.ALWAYS_ON, 200, RgbColor(10, 20, 30)
            ),
        },
        motion_settings=MotionSettings(
            MotionMappingTarget.LEFT_JOYSTICK,
            0x06,
            MotionMappingMode.INSTANT,
            42,
        ),
        back_paddle_bindings={
            MacroSlot.M1: BackPaddleBinding(target=ControllerButtonTarget.A),
        },
        step_size=128,
    )


def _full_restore_point() -> RestorePoint:
    coverage = RestorePointCoverage(
        captured_supported_count=12,
        total_supported_count=13,
        capture_source=CaptureSource.FRESH_READ,
        fields={
            "polling_rate": FieldCoverage(
                state=CoverageState.CAPTURED,
                readable=True,
                writable=True,
                category=CoverageCategory.DEVICE,
            ),
            "lighting_zones": FieldCoverage(
                state=CoverageState.PARTIAL,
                readable=True,
                writable=True,
                category=CoverageCategory.COSMETIC,
                note="Only the home zone was captured during this read.",
            ),
            "motion_settings": FieldCoverage(
                state=CoverageState.CAPTURED,
                readable=True,
                writable=False,
                category=CoverageCategory.UNSUPPORTED,
                note="Captured for record only; no supported write path in this app.",
            ),
        },
    )
    return RestorePoint(
        schema_version=SCHEMA_VERSION,
        kind=KIND,
        id="rp_20260524_190355_9b42de",
        created_at="2026-05-24T19:03:55Z",
        app_version="2.0.0",
        app_build_commit="abc1234",
        title="Before Safe Import — 2026-05-24 19:03",
        trigger=RestorePointTrigger(
            type="before_safe_import_apply",
            source_label="Safe Import",
            reason="Created before applying imported profile to controller",
        ),
        device_identity=DeviceIdentity(
            vid="413D",
            pid="2104",
            product_string="ZD Ultimate Legend",
            firmware_version="1.18",
            identity_confidence=IdentityConfidence.READABLE,
        ),
        snapshot=_full_snapshot(),
        coverage=coverage,
        last_restore_attempt=None,
    )


class RoundTripTests(unittest.TestCase):
    def test_round_trip_preserves_full_restore_point(self) -> None:
        original = _full_restore_point()
        payload = restore_point_to_dict(original)
        revived = restore_point_from_dict(payload)
        self.assertEqual(revived, original)

    def test_round_trip_preserves_restore_attempt_record(self) -> None:
        original = replace(
            _full_restore_point(),
            last_restore_attempt=RestoreAttemptRecord(
                attempted_at="2026-05-24T19:05:11Z",
                label=RestoreResultLabel.RESTORED_WITH_WARNINGS,
                attempted=10,
                wrote_succeeded=9,
                write_failed=1,
                verified_matched=8,
                could_not_verify=1,
                mismatched=0,
            ),
        )
        revived = restore_point_from_dict(restore_point_to_dict(original))
        self.assertEqual(revived.last_restore_attempt, original.last_restore_attempt)

    def test_round_trip_preserves_field_coverage_without_note(self) -> None:
        rp = _full_restore_point()
        revived = restore_point_from_dict(restore_point_to_dict(rp))
        polling_coverage = revived.coverage.fields["polling_rate"]
        self.assertIsNone(polling_coverage.note)

    def test_payload_uses_observed_enum_values_not_names(self) -> None:
        payload = restore_point_to_dict(_full_restore_point())
        coverage_fields = payload["coverage"]["fields"]
        self.assertEqual(coverage_fields["polling_rate"]["state"], "captured")
        self.assertEqual(coverage_fields["lighting_zones"]["category"], "cosmetic")
        self.assertEqual(payload["coverage"]["capture_source"], "fresh_read")
        self.assertEqual(payload["device_identity"]["identity_confidence"], "readable")


class SchemaErrorTests(unittest.TestCase):
    def test_unknown_schema_version_raises_schema_error(self) -> None:
        payload = restore_point_to_dict(_full_restore_point())
        payload["schema_version"] = 999
        with self.assertRaises(RestorePointSchemaError):
            restore_point_from_dict(payload)

    def test_wrong_kind_raises_parse_error(self) -> None:
        payload = restore_point_to_dict(_full_restore_point())
        payload["kind"] = "zd_wrapper_profile"
        with self.assertRaises(RestorePointParseError):
            restore_point_from_dict(payload)

    def test_non_object_payload_raises_parse_error(self) -> None:
        with self.assertRaises(RestorePointParseError):
            restore_point_from_dict([])  # type: ignore[arg-type]

    def test_missing_required_field_raises_parse_error(self) -> None:
        payload = restore_point_to_dict(_full_restore_point())
        del payload["created_at"]
        with self.assertRaises(RestorePointParseError):
            restore_point_from_dict(payload)

    def test_schema_error_subclasses_parse_error(self) -> None:
        self.assertTrue(issubclass(RestorePointSchemaError, RestorePointParseError))

    def test_path_traversal_id_rejected(self) -> None:
        # The id becomes the export filename stem; a planted file whose id is a
        # traversal path must be rejected at load so it never reaches the writer.
        for bad_id in ("../../outside/owned", r"..\..\escape", "/etc/passwd", "rp_../x"):
            with self.subTest(bad_id=bad_id):
                payload = restore_point_to_dict(_full_restore_point())
                payload["id"] = bad_id
                with self.assertRaises(RestorePointParseError):
                    restore_point_from_dict(payload)

    def test_non_canonical_id_rejected(self) -> None:
        # Only the exact rp_YYYYMMDD_HHMMSS_<6 hex> shape the generator mints is
        # accepted (a non-str or wrong-shape id is a malformed/tampered file).
        for bad_id in ("rp_test", "rp_20260524_190355_NOTHEX", "rp_20260524_190355_9b42d", 12345):
            with self.subTest(bad_id=bad_id):
                payload = restore_point_to_dict(_full_restore_point())
                payload["id"] = bad_id
                with self.assertRaises(RestorePointParseError):
                    restore_point_from_dict(payload)

    def test_canonical_id_accepted(self) -> None:
        # Guard the regex isn't over-strict: the minted shape still loads.
        payload = restore_point_to_dict(_full_restore_point())
        payload["id"] = "rp_20260524_190355_9b42de"
        self.assertEqual(restore_point_from_dict(payload).id, "rp_20260524_190355_9b42de")


class RestoreResultDataclassTests(unittest.TestCase):
    """The RestoreResult dataclass isn't part of the codec but the service
    relies on its shape; lock the field set here so the restore-point UI
    integration can trust it.
    """

    def test_restore_result_has_expected_counts(self) -> None:
        result = RestoreResult(
            label=RestoreResultLabel.VERIFIED,
            attempted=3,
            wrote_succeeded=3,
            write_failed=0,
            verified_matched=3,
            could_not_verify=0,
            mismatched=0,
            fields=(
                RestoreFieldOutcome(
                    field_name="polling_rate",
                    write_succeeded=True,
                    write_error=None,
                    verify_matched=True,
                ),
            ),
            before_restore_point_id="rp_20260524_190900_aaaaaa",
            completed_at="2026-05-24T19:09:30Z",
        )
        self.assertEqual(result.label, RestoreResultLabel.VERIFIED)
        self.assertEqual(result.fields[0].field_name, "polling_rate")


class SchemaV2ClaimBoundaryTests(unittest.TestCase):
    """Schema v2 adds two top-level fields (``claim_boundary_paragraph`` +
    ``claim_boundary_short``) so the verbatim denial language travels
    with every JSON export — per the "Required claim-boundary statement" rule:
    "Put this in the Restore UI, detail view, and every export." The
    restore-point UI-integration acceptance criterion #81 (this was the 'partial'
    one) is closed by these tests + the codec changes they cover.
    """

    def test_schema_version_is_2(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 2)

    def test_to_dict_includes_claim_boundary_fields(self) -> None:
        payload = restore_point_to_dict(_full_restore_point())
        self.assertEqual(payload["claim_boundary_paragraph"], CLAIM_BOUNDARY_PARAGRAPH)
        self.assertEqual(payload["claim_boundary_short"], CLAIM_BOUNDARY_SHORT_UI)

    def test_from_dict_v2_round_trips(self) -> None:
        original = _full_restore_point()
        payload = restore_point_to_dict(original)
        self.assertEqual(payload["schema_version"], 2)
        revived = restore_point_from_dict(payload)
        self.assertEqual(revived, original)
        self.assertEqual(restore_point_to_dict(revived), payload)

    def test_from_dict_v1_synthesizes_missing_claim_boundary(self) -> None:
        # Simulate an on-disk v1 file: schema_version=1 and no
        # claim_boundary_* keys at the top level. The codec must accept it
        # and fill the two new fields from the boundary constants.
        payload = restore_point_to_dict(_full_restore_point())
        payload["schema_version"] = 1
        del payload["claim_boundary_paragraph"]
        del payload["claim_boundary_short"]
        revived = restore_point_from_dict(payload)
        self.assertEqual(revived.schema_version, 1)
        self.assertEqual(revived.claim_boundary_paragraph, CLAIM_BOUNDARY_PARAGRAPH)
        self.assertEqual(revived.claim_boundary_short, CLAIM_BOUNDARY_SHORT_UI)

    def test_from_dict_v3_raises_schema_error(self) -> None:
        payload = restore_point_to_dict(_full_restore_point())
        payload["schema_version"] = 3
        with self.assertRaises(RestorePointSchemaError):
            restore_point_from_dict(payload)

    def test_claim_boundary_paragraph_matches_boundary_constant(self) -> None:
        # Catch accidental edits to the dataclass default. The default is
        # the source-of-truth constant; if a future refactor inlines a
        # different string here, the verbatim contract silently breaks.
        rp = _full_restore_point()
        self.assertEqual(rp.claim_boundary_paragraph, CLAIM_BOUNDARY_PARAGRAPH)
        self.assertEqual(rp.claim_boundary_short, CLAIM_BOUNDARY_SHORT_UI)


if __name__ == "__main__":
    unittest.main()
