"""Tests for the RestorePointService capture + restore flows.

The service is exercised against a stub SettingsService that lets each test
choose which fresh-read getters return a value vs raise / return None. The
apply coordinator is a real :class:`SettingsApplyCoordinator` wrapping the
same stub, so the test setup mirrors the real wiring with no production-code
mocks.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from zd_app.services.restore_point_service import (
    FRESH_READ_MAX_AGE_S,
    RestorePointService,
    _build_field_outcomes,
    verify_applied_snapshot,
)
from zd_app.services.settings_apply_coordinator import (
    ApplyResult,
    SettingsApplyCoordinator,
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
    SENSITIVITY_STICK_LEFT,
    SENSITIVITY_STICK_RIGHT,
    SensitivityAnchor,
    SetAxisInversionOutcome,
    SetAxisInversionResult,
    SetBackPaddleBindingOutcome,
    SetBackPaddleBindingResult,
    SetButtonBindingOutcome,
    SetButtonBindingResult,
    SetDeadzoneOutcome,
    SetDeadzoneResult,
    SetLightingOutcome,
    SetLightingResult,
    SetPollingRateOutcome,
    SetPollingRateResult,
    SetSensitivityCurveOutcome,
    SetSensitivityCurveResult,
    SetStepSizeOutcome,
    SetStepSizeResult,
    SetTriggerSettingsOutcome,
    SetTriggerSettingsResult,
    SetVibrationOutcome,
    SetVibrationResult,
    StickDeadzones,
    TriggerMode,
    TriggerSettings,
    TriggerVibrationMode,
    VibrationSettings,
)
from zd_app.storage.restore_point_models import (
    CaptureSource,
    CoverageState,
    DeviceIdentity,
    IdentityConfidence,
    RestorePointTrigger,
    RestoreResult,
    RestoreResultLabel,
)
from zd_app.storage.restore_point_store import RestorePointStore


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubSettingsService:
    """Captures the read-path return values + records the write calls.

    A real :class:`SettingsService` requires a Windows HID device. The stub
    is intentionally tight: every getter returns the value its public
    ``get_*`` would return on success (or None for "read failed"), and every
    setter returns a real ``Set*Result`` so the apply coordinator's success
    classification can run unchanged.
    """

    def __init__(self) -> None:
        self.polling_rate: Optional[PollingRate] = None
        self.vibration: Optional[VibrationSettings] = None
        self.deadzones: Optional[StickDeadzones] = None
        self.axis_inversion: Optional[tuple[AxisInversion, AxisInversion]] = None
        self.sensitivity: Optional[tuple[
            tuple[SensitivityAnchor, SensitivityAnchor, SensitivityAnchor],
            tuple[SensitivityAnchor, SensitivityAnchor, SensitivityAnchor],
        ]] = None
        self.trigger: Optional[tuple[TriggerSettings, TriggerSettings]] = None
        self.motion: Optional[MotionSettings] = None
        self.step_size: Optional[int] = None
        self.button_bindings: dict[ButtonSlot, ButtonMapping] = {}
        self.lighting_zones: dict[LightingZone, LightingSettings] = {}
        self.back_paddles: dict[MacroSlot, BackPaddleBinding] = {}

        # 8-point (cat 0x86) sensitivity, modelled like the real service: a
        # legacy device reports not-capable and returns None for both curves.
        # A capable-device test flips ``supports_8point`` True and sets the two
        # curves. ``get_sensitivity_curve_8point`` keys off the same stick
        # selectors the production read path passes.
        self.supports_8point: bool = False
        self.sensitivity_8point: Optional[tuple[
            tuple[SensitivityAnchor, ...],
            tuple[SensitivityAnchor, ...],
        ]] = None

        # Failure injection: setter labels that should fail.
        self.fail_labels: set[str] = set()
        self.write_calls: list[str] = []

    # -- getters ------------------------------------------------------------

    def get_polling_rate(self) -> Optional[PollingRate]:
        return self.polling_rate

    def get_vibration(self) -> Optional[VibrationSettings]:
        return self.vibration

    def get_deadzones(self) -> Optional[StickDeadzones]:
        return self.deadzones

    def get_axis_inversion(self):
        return self.axis_inversion

    def get_sensitivity_curves(self):
        return self.sensitivity

    def supports_8point_sensitivity(self) -> bool:
        return self.supports_8point

    def get_sensitivity_curve_8point(self, stick: int):
        if self.sensitivity_8point is None:
            return None
        if stick == SENSITIVITY_STICK_LEFT:
            return self.sensitivity_8point[0]
        if stick == SENSITIVITY_STICK_RIGHT:
            return self.sensitivity_8point[1]
        return None

    def get_trigger_settings(self):
        return self.trigger

    def get_motion_settings(self):
        return self.motion

    def get_step_size(self) -> Optional[int]:
        return self.step_size

    def get_button_binding(self, slot: ButtonSlot) -> Optional[ButtonMapping]:
        return self.button_bindings.get(slot)

    def get_zone_lighting(self, zone: LightingZone) -> Optional[LightingSettings]:
        return self.lighting_zones.get(zone)

    def get_all_back_paddle_bindings(self):
        return dict(self.back_paddles)

    # -- setters ------------------------------------------------------------

    def _result_for(
        self,
        label: str,
        ok_factory: Callable[[], Any],
        fail_factory: Callable[[], Any],
    ):
        self.write_calls.append(label)
        if label in self.fail_labels:
            return fail_factory()
        return ok_factory()

    def set_polling_rate(self, rate: PollingRate) -> SetPollingRateResult:
        return self._result_for(
            "polling",
            lambda: SetPollingRateResult(
                outcome=SetPollingRateOutcome.OK,
                rate=rate,
                error_code=None,
                bytes_written=65,
                payload_hex=None,
                elapsed_ms=1,
            ),
            lambda: SetPollingRateResult(
                outcome=SetPollingRateOutcome.WRITE_FAILED,
                rate=rate,
                error_code=5,
                bytes_written=0,
                payload_hex=None,
                elapsed_ms=1,
            ),
        )

    def set_step_size(self, value: int) -> SetStepSizeResult:
        return self._result_for(
            "step_size",
            lambda: SetStepSizeResult(
                outcome=SetStepSizeOutcome.OK,
                value=value,
                error_code=None,
                bytes_written=65,
                payload_hex=None,
                elapsed_ms=1,
            ),
            lambda: SetStepSizeResult(
                outcome=SetStepSizeOutcome.WRITE_FAILED,
                value=value,
                error_code=5,
                bytes_written=0,
                payload_hex=None,
                elapsed_ms=1,
            ),
        )

    def set_step_size_verified(self, value: int, attempts: int = 3, settle_s: float = 0.1) -> SetStepSizeResult:
        # The apply pipeline calls the verified setter; this fake stands in for
        # the whole write+read-back op via its scripted set_step_size outcome.
        return self.set_step_size(value)

    def set_vibration(self, settings: VibrationSettings) -> SetVibrationResult:
        return self._result_for(
            "vibration",
            lambda: SetVibrationResult(
                outcome=SetVibrationOutcome.OK,
                settings=settings,
                error_code=None,
                bytes_written=65,
                payload_hex=None,
                elapsed_ms=1,
            ),
            lambda: SetVibrationResult(
                outcome=SetVibrationOutcome.WRITE_FAILED,
                settings=settings,
                error_code=5,
                bytes_written=0,
                payload_hex=None,
                elapsed_ms=1,
            ),
        )

    def set_all_deadzones(self, deadzones: StickDeadzones) -> SetDeadzoneResult:
        return self._result_for(
            "deadzones",
            lambda: SetDeadzoneResult(
                outcome=SetDeadzoneOutcome.OK,
                deadzones=deadzones,
                error_code=None,
                bytes_written=65,
                payload_hex=None,
                elapsed_ms=1,
            ),
            lambda: SetDeadzoneResult(
                outcome=SetDeadzoneOutcome.WRITE_FAILED,
                deadzones=deadzones,
                error_code=5,
                bytes_written=0,
                payload_hex=None,
                elapsed_ms=1,
            ),
        )

    def _axis_result(self, label: str, inversion: AxisInversion) -> SetAxisInversionResult:
        return self._result_for(
            label,
            lambda: SetAxisInversionResult(
                outcome=SetAxisInversionOutcome.OK,
                stick=label,
                inversion=inversion,
                error_code=None,
                bytes_written=65,
                payload_hex=None,
                elapsed_ms=1,
            ),
            lambda: SetAxisInversionResult(
                outcome=SetAxisInversionOutcome.WRITE_FAILED,
                stick=label,
                inversion=inversion,
                error_code=5,
                bytes_written=0,
                payload_hex=None,
                elapsed_ms=1,
            ),
        )

    def set_left_stick_inversion(self, inv: AxisInversion):
        return self._axis_result("axis_inv_left", inv)

    def set_right_stick_inversion(self, inv: AxisInversion):
        return self._axis_result("axis_inv_right", inv)

    def _sens_result(self, label, anchors) -> SetSensitivityCurveResult:
        return self._result_for(
            label,
            lambda: SetSensitivityCurveResult(
                outcome=SetSensitivityCurveOutcome.OK,
                anchors=anchors,
                error_code=None,
                bytes_written=65,
                payload_hex=None,
                elapsed_ms=1,
            ),
            lambda: SetSensitivityCurveResult(
                outcome=SetSensitivityCurveOutcome.WRITE_FAILED,
                anchors=anchors,
                error_code=5,
                bytes_written=0,
                payload_hex=None,
                elapsed_ms=1,
            ),
        )

    def set_left_stick_sensitivity_curve(self, anchors):
        return self._sens_result("sens_left", anchors)

    def set_right_stick_sensitivity_curve(self, anchors):
        return self._sens_result("sens_right", anchors)

    def set_left_stick_sensitivity_curve_8point(self, anchors):
        # Same coordinator label as the 3-point writer — the apply path emits
        # ``sens_left`` whether it writes 0x06 or 0x86 for that stick.
        return self._sens_result("sens_left", anchors)

    def set_right_stick_sensitivity_curve_8point(self, anchors):
        return self._sens_result("sens_right", anchors)

    def _trigger_result(self, label, settings):
        return self._result_for(
            label,
            lambda: SetTriggerSettingsResult(
                outcome=SetTriggerSettingsOutcome.OK,
                trigger=label,
                settings=settings,
                error_code=None,
                bytes_written=65,
                payload_hex=None,
                elapsed_ms=1,
            ),
            lambda: SetTriggerSettingsResult(
                outcome=SetTriggerSettingsOutcome.WRITE_FAILED,
                trigger=label,
                settings=settings,
                error_code=5,
                bytes_written=0,
                payload_hex=None,
                elapsed_ms=1,
            ),
        )

    def set_left_trigger_settings(self, settings):
        return self._trigger_result("trigger_left", settings)

    def set_right_trigger_settings(self, settings):
        return self._trigger_result("trigger_right", settings)

    def set_button_binding(self, slot: ButtonSlot, mapping: ButtonMapping):
        label = f"binding_{slot.name}"
        return self._result_for(
            label,
            lambda: SetButtonBindingResult(
                outcome=SetButtonBindingOutcome.OK,
                slot=slot,
                mapping=mapping,
                error_code=None,
                bytes_written=65,
                payload_hex=None,
                elapsed_ms=1,
            ),
            lambda: SetButtonBindingResult(
                outcome=SetButtonBindingOutcome.WRITE_FAILED,
                slot=slot,
                mapping=mapping,
                error_code=5,
                bytes_written=0,
                payload_hex=None,
                elapsed_ms=1,
            ),
        )

    def set_zone_lighting(self, zone: LightingZone, settings: LightingSettings):
        label = f"lighting_{zone.name}"
        return self._result_for(
            label,
            lambda: SetLightingResult(
                outcome=SetLightingOutcome.OK,
                zone=zone.name,
                settings=settings,
                error_code=None,
                bytes_written=65,
                payload_hex=None,
                elapsed_ms=1,
            ),
            lambda: SetLightingResult(
                outcome=SetLightingOutcome.WRITE_FAILED,
                zone=zone.name,
                settings=settings,
                error_code=5,
                bytes_written=0,
                payload_hex=None,
                elapsed_ms=1,
            ),
        )

    def set_zone_lighting_verified(self, zone: LightingZone, settings: LightingSettings, attempts: int = 3, settle_s: float = 0.1):
        # The apply pipeline calls the verified setter; this fake stands in for the
        # whole write+read-back op via its scripted set_zone_lighting outcome.
        return self.set_zone_lighting(zone, settings)

    def set_back_paddle_binding(self, slot, target):
        label = f"back_paddle_{slot.name}"
        return self._result_for(
            label,
            lambda: SetBackPaddleBindingResult(
                outcome=SetBackPaddleBindingOutcome.OK,
                slot=slot,
                binding=BackPaddleBinding(target=target),
                error_code=None,
                bytes_written=65,
                payload_hex=None,
                elapsed_ms=1,
            ),
            lambda: SetBackPaddleBindingResult(
                outcome=SetBackPaddleBindingOutcome.WRITE_FAILED,
                slot=slot,
                binding=BackPaddleBinding(target=target),
                error_code=5,
                bytes_written=0,
                payload_hex=None,
                elapsed_ms=1,
            ),
        )


def _make_service(
    *,
    tmpdir: str,
    settings_stub: Optional[_StubSettingsService] = None,
    clock_now: float = 1000.0,
) -> tuple[RestorePointService, _StubSettingsService, RestorePointStore]:
    stub = settings_stub if settings_stub is not None else _StubSettingsService()
    store = RestorePointStore(tmpdir)
    coordinator = SettingsApplyCoordinator(stub)  # type: ignore[arg-type]
    id_counter = {"n": 0}

    def _next_id(_moment) -> str:
        n = id_counter["n"]
        id_counter["n"] += 1
        # Hex tail must be exactly 6 chars per FILENAME_PATTERN.
        return f"rp_20260524_190355_{n:06x}"

    service = RestorePointService(
        store=store,
        settings_service=stub,  # type: ignore[arg-type]
        apply_coordinator=coordinator,
        app_version="2.0.0-test",
        app_build_commit="abc1234",
        clock=lambda: clock_now,
        utc_now=lambda: datetime(2026, 5, 24, 19, 3, 55, tzinfo=timezone.utc),
        id_factory=_next_id,
    )
    return service, stub, store


def _full_populated_stub() -> _StubSettingsService:
    stub = _StubSettingsService()
    stub.polling_rate = PollingRate.HZ_1000
    stub.step_size = 128
    stub.vibration = VibrationSettings(10, 20, 30, 40, TriggerVibrationMode.NATIVE)
    stub.deadzones = StickDeadzones(5, 6, 90, 91)
    stub.axis_inversion = (AxisInversion(False, False), AxisInversion(False, False))
    stub.sensitivity = (
        (SensitivityAnchor(0, 0), SensitivityAnchor(50, 50), SensitivityAnchor(100, 100)),
        (SensitivityAnchor(0, 0), SensitivityAnchor(50, 50), SensitivityAnchor(100, 100)),
    )
    stub.trigger = (
        TriggerSettings(0, 100, TriggerMode.LONG),
        TriggerSettings(0, 100, TriggerMode.LONG),
    )
    stub.motion = MotionSettings(
        MotionMappingTarget.LEFT_JOYSTICK,
        0x06,
        MotionMappingMode.INSTANT,
        42,
    )
    stub.button_bindings = {
        ButtonSlot.A: ButtonMapping.controller_button(ControllerButtonTarget.B),
    }
    stub.lighting_zones = {
        LightingZone.HOME: LightingSettings(
            True, LightingMode.ALWAYS_ON, 200, RgbColor(10, 20, 30)
        ),
    }
    return stub


# 8-point curves for the capable-device restore tests. Distinct per stick (and
# distinct from the 3-point curve in _full_populated_stub) so a verify mismatch
# is unambiguous.
_LEFT_8POINT = tuple(SensitivityAnchor(i * 10, i * 10) for i in range(8))
_RIGHT_8POINT = tuple(SensitivityAnchor(i * 10, min(100, i * 12)) for i in range(8))


def _full_populated_stub_8point() -> _StubSettingsService:
    """Capable 1.2.9 / fw-1.24 stub: everything :func:`_full_populated_stub`
    records, plus cat-0x86 capability and a distinct 8-point curve per stick.
    """

    stub = _full_populated_stub()
    stub.supports_8point = True
    stub.sensitivity_8point = (_LEFT_8POINT, _RIGHT_8POINT)
    return stub


def _basic_trigger() -> RestorePointTrigger:
    return RestorePointTrigger(
        type="before_safe_import_apply",
        source_label="Safe Import",
        reason="Created before applying imported profile to controller",
    )


def _identity() -> DeviceIdentity:
    return DeviceIdentity(
        vid="413D",
        pid="2104",
        product_string="ZD Ultimate Legend",
        firmware_version="1.18",
        identity_confidence=IdentityConfidence.READABLE,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class CaptureFreshReadTests(unittest.TestCase):
    def test_fresh_read_builds_restore_point_with_fresh_capture_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, store = _make_service(
                tmpdir=tmp, settings_stub=_full_populated_stub()
            )
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            self.assertIsNotNone(rp)
            assert rp is not None
            self.assertEqual(rp.coverage.capture_source, CaptureSource.FRESH_READ)
            self.assertEqual(rp.app_version, "2.0.0-test")
            self.assertEqual(rp.title, "Safe Import — 2026-05-24 19:03")
            self.assertEqual(rp.device_identity, _identity())
            # Round-trip via the store: load returns the same record.
            loaded = store.load(rp.id)
            self.assertEqual(loaded.id, rp.id)

    def test_fresh_read_with_all_failures_and_no_cache_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, store = _make_service(tmpdir=tmp)
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            self.assertIsNone(rp)
            valid, _ = store.list()
            self.assertEqual(valid, [])

    def test_fresh_read_with_partial_failures_uses_fresh_with_coverage_holes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            stub.deadzones = None  # one read fails
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            self.assertIsNotNone(rp)
            assert rp is not None
            self.assertEqual(rp.coverage.capture_source, CaptureSource.FRESH_READ)
            self.assertEqual(
                rp.coverage.fields["deadzones"].state,
                CoverageState.NOT_CAPTURED,
            )
            self.assertEqual(
                rp.coverage.fields["polling_rate"].state,
                CoverageState.CAPTURED,
            )

    def test_capture_default_title_when_none_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, _store = _make_service(
                tmpdir=tmp, settings_stub=_full_populated_stub()
            )
            rp = service.capture(_basic_trigger())
            assert rp is not None
            self.assertIn("Safe Import", rp.title)


class CaptureCachedFallbackTests(unittest.TestCase):
    def test_cached_snapshot_used_when_fresh_read_fails_and_cache_is_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, _store = _make_service(tmpdir=tmp, clock_now=1000.0)
            cached = ControllerSnapshot(
                polling_rate=PollingRate.HZ_2000,
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
            rp = service.capture(
                _basic_trigger(),
                device_identity=_identity(),
                cached_snapshot=cached,
                cached_snapshot_ts=985.0,   # 15s old
            )
            assert rp is not None
            self.assertEqual(rp.coverage.capture_source, CaptureSource.CACHED_SNAPSHOT)
            self.assertEqual(rp.snapshot.polling_rate, PollingRate.HZ_2000)
            self.assertEqual(
                rp.coverage.fields["polling_rate"].state,
                CoverageState.CAPTURED,
            )

    def test_cached_snapshot_ignored_when_too_old(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, _store = _make_service(tmpdir=tmp, clock_now=1000.0)
            cached = ControllerSnapshot(
                polling_rate=PollingRate.HZ_2000,
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
            rp = service.capture(
                _basic_trigger(),
                device_identity=_identity(),
                cached_snapshot=cached,
                cached_snapshot_ts=1000.0 - (FRESH_READ_MAX_AGE_S + 5),
            )
            self.assertIsNone(rp)


# ---------------------------------------------------------------------------
# Capture retention prune (prune_on_capture)
# ---------------------------------------------------------------------------


class _PruneRecordingStore(RestorePointStore):
    """Real store that counts prune() calls (optionally into a shared call log)."""

    def __init__(self, base_dir, call_log: Optional[list[str]] = None) -> None:
        super().__init__(base_dir)
        self.prune_calls = 0
        self._call_log = call_log

    def save(self, rp):
        if self._call_log is not None:
            self._call_log.append("save")
        return super().save(rp)

    def prune(self, **kwargs):
        self.prune_calls += 1
        if self._call_log is not None:
            self._call_log.append("prune")
        return super().prune(**kwargs)


class _LowCapStore(RestorePointStore):
    """Real store whose no-arg prune() enforces a tiny max_count, so the
    capture path's default-caps ``store.prune()`` call is testable without
    50 fixture files."""

    def __init__(self, base_dir, *, max_count: int) -> None:
        super().__init__(base_dir)
        self._test_max_count = max_count

    def prune(self, **kwargs):
        kwargs.setdefault("max_count", self._test_max_count)
        return super().prune(**kwargs)


class _PruneRaisingStore(RestorePointStore):
    """Real store whose prune() always raises (failure-isolation tests)."""

    def prune(self, **kwargs):
        raise RuntimeError("prune exploded")


class _LedgerCallLogRecorder:
    """Wraps a WearLedgerService so append() lands in a shared call log."""

    def __init__(self, ledger, call_log: list[str]) -> None:
        self._ledger = ledger
        self._call_log = call_log

    def append(self, *args, **kwargs):
        self._call_log.append("ledger")
        return self._ledger.append(*args, **kwargs)


def _make_prune_service(
    store: RestorePointStore,
    *,
    wear_ledger=None,
    prune_on_capture: bool = True,
) -> RestorePointService:
    """Like :func:`_make_service` but with a caller-supplied store + a ticking
    clock — prune orders by ``created_at``, so unlike the frozen-time shared
    helper, each capture here must get a distinct timestamp."""

    stub = _full_populated_stub()
    coordinator = SettingsApplyCoordinator(stub)  # type: ignore[arg-type]
    tick = {"n": 0}

    def _next_moment() -> datetime:
        tick["n"] += 1
        return datetime(2026, 6, 10, 12, 0, tick["n"], tzinfo=timezone.utc)

    return RestorePointService(
        store=store,
        settings_service=stub,  # type: ignore[arg-type]
        apply_coordinator=coordinator,
        app_version="2.0.0-test",
        app_build_commit="abc1234",
        clock=lambda: 1000.0,
        utc_now=_next_moment,
        wear_ledger=wear_ledger,
        prune_on_capture=prune_on_capture,
    )


class CapturePruneRetentionTests(unittest.TestCase):
    """capture() prunes the vault after save + ledger."""

    def test_capture_past_cap_prunes_oldest_auto_and_keeps_new_capture(self) -> None:
        # T6
        with tempfile.TemporaryDirectory() as tmp:
            store = _LowCapStore(tmp, max_count=2)
            service = _make_prune_service(store)
            rp1 = service.capture(_basic_trigger(), device_identity=_identity())
            rp2 = service.capture(_basic_trigger(), device_identity=_identity())
            assert rp1 is not None and rp2 is not None
            with self.assertLogs(
                "zd_app.services.restore_point_service", level="INFO"
            ) as logs:
                rp3 = service.capture(_basic_trigger(), device_identity=_identity())
            assert rp3 is not None
            valid, _ = store.list()
            ids = {rp.id for rp in valid}
            self.assertEqual(ids, {rp2.id, rp3.id})  # oldest auto RP pruned,
            self.assertNotIn(rp1.id, ids)            # the fresh capture survives
            self.assertTrue(any(rp1.id in line for line in logs.output))

    def test_prune_failure_never_fails_or_rolls_back_capture(self) -> None:
        # T7
        with tempfile.TemporaryDirectory() as tmp:
            store = _PruneRaisingStore(tmp)
            service = _make_prune_service(store)
            with self.assertLogs(
                "zd_app.services.restore_point_service", level="WARNING"
            ) as logs:
                rp = service.capture(_basic_trigger(), device_identity=_identity())
            self.assertIsNotNone(rp)
            assert rp is not None
            self.assertEqual(store.load(rp.id).id, rp.id)  # saved despite prune
            self.assertTrue(any("prune" in line.lower() for line in logs.output))

    def test_prune_on_capture_false_never_prunes(self) -> None:
        # T8
        with tempfile.TemporaryDirectory() as tmp:
            store = _PruneRecordingStore(tmp)
            service = _make_prune_service(store, prune_on_capture=False)
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            self.assertIsNotNone(rp)
            self.assertEqual(store.prune_calls, 0)

    def test_ledger_event_still_recorded_when_prune_fires(self) -> None:
        # Order is save -> ledger -> prune; the ledger append must not
        # be skipped or reordered by the prune step.
        from zd_app.services.wear_ledger import WearLedgerService

        with tempfile.TemporaryDirectory() as tmp:
            calls: list[str] = []
            store = _PruneRecordingStore(str(Path(tmp) / "rp"), call_log=calls)
            ledger = WearLedgerService(base_dir=Path(tmp) / "wear_ledger")
            service = _make_prune_service(
                store,
                wear_ledger=_LedgerCallLogRecorder(ledger, calls),
            )
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            self.assertIsNotNone(rp)
            assert rp is not None
            events = [e for e in ledger.read_events() if e.event_type == "rp_capture"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].details["rp_id"], rp.id)
            self.assertEqual(calls, ["save", "ledger", "prune"])
            self.assertEqual(store.prune_calls, 1)


class CoverageMapInferenceTests(unittest.TestCase):
    def test_motion_settings_unsupported_when_not_captured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            stub.motion = None
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            assert rp is not None
            self.assertEqual(
                rp.coverage.fields["motion_settings"].state,
                CoverageState.UNSUPPORTED,
            )
            self.assertFalse(rp.coverage.fields["motion_settings"].writable)

    def test_motion_settings_captured_when_read_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            assert rp is not None
            self.assertEqual(
                rp.coverage.fields["motion_settings"].state,
                CoverageState.CAPTURED,
            )
            self.assertFalse(rp.coverage.fields["motion_settings"].writable)

    def test_lighting_partial_when_subset_captured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            # Only HOME zone captured; LEFT_LIGHT / RIGHT_LIGHT empty.
            self.assertEqual(set(stub.lighting_zones.keys()), {LightingZone.HOME})
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            assert rp is not None
            coverage = rp.coverage.fields["lighting_zones"]
            self.assertEqual(coverage.state, CoverageState.PARTIAL)
            self.assertIsNotNone(coverage.note)

    def test_counts_exclude_unsupported_from_supported_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, _store = _make_service(
                tmpdir=tmp, settings_stub=_full_populated_stub()
            )
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            assert rp is not None
            # motion_settings is the only UNSUPPORTED scalar. With 11 scalar
            # fields (10 supported + 1 unsupported) plus 3 collection fields,
            # supported total is 13.
            self.assertEqual(rp.coverage.total_supported_count, 13)
            self.assertGreater(rp.coverage.captured_supported_count, 0)


class RestoreFlowTests(unittest.TestCase):
    def test_restore_creates_before_restore_snapshot_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            service, _stub, store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            result = service.restore(captured.id)
            valid, _ = store.list()
            ids = {rp.id for rp in valid}
            self.assertIsNotNone(result.before_restore_point_id)
            self.assertIn(result.before_restore_point_id, ids)
            before_rp = next(
                rp for rp in valid if rp.id == result.before_restore_point_id
            )
            self.assertEqual(before_rp.trigger.type, "before_restore")

    def test_restore_verified_when_all_writes_match_readback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            result = service.restore(captured.id)
            self.assertEqual(result.label, RestoreResultLabel.VERIFIED)
            self.assertEqual(result.write_failed, 0)
            self.assertEqual(result.mismatched, 0)
            self.assertGreater(result.attempted, 0)

    def test_restore_partial_when_one_field_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            stub.fail_labels = {"polling"}
            result = service.restore(captured.id)
            self.assertEqual(result.label, RestoreResultLabel.PARTIALLY_RESTORED)
            self.assertEqual(result.write_failed, 1)
            polling_outcome = next(
                f for f in result.fields if f.field_name == "polling_rate"
            )
            self.assertFalse(polling_outcome.write_succeeded)

    def test_restore_failed_when_every_field_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            stub.fail_labels = set()
            # Fail every label we know the apply pipeline will emit for this
            # snapshot.
            for slot in stub.button_bindings:
                stub.fail_labels.add(f"binding_{slot.name}")
            for zone in stub.lighting_zones:
                stub.fail_labels.add(f"lighting_{zone.name}")
            stub.fail_labels.update({
                "polling", "step_size", "vibration", "deadzones",
                "axis_inv_left", "axis_inv_right",
                "sens_left", "sens_right",
                "trigger_left", "trigger_right",
            })
            result = service.restore(captured.id)
            self.assertEqual(result.label, RestoreResultLabel.RESTORE_FAILED)
            self.assertEqual(result.write_failed, result.attempted)

    def test_restore_mismatch_when_readback_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            # Restore as normal but flip the polling rate after capture: the
            # stub's writes "succeed" (label='polling'), but the readback
            # returns the new value, which won't match the captured value.
            stub.polling_rate = PollingRate.HZ_8000
            result = service.restore(captured.id)
            self.assertEqual(result.label, RestoreResultLabel.MISMATCH_AFTER_RESTORE)
            self.assertEqual(result.write_failed, 0)
            self.assertGreater(result.mismatched, 0)

    def test_restore_warnings_when_readback_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            # Drop one field's read source so the verification can't run.
            stub.polling_rate = None
            result = service.restore(captured.id)
            self.assertEqual(result.label, RestoreResultLabel.RESTORED_WITH_WARNINGS)
            self.assertGreater(result.could_not_verify, 0)

    def test_mismatch_field_outcome_carries_expected_and_observed(self) -> None:
        """Restore-result enrichment: when a verify lands on mismatch,
        the per-field outcome carries ``expected_value`` + ``observed_value``
        as str() values so the result page can show the actual numbers
        inline. Avoids the problem local testing surfaced, where the user had to Read
        the controller separately to see what value the device actually had.
        """
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            # Capture pinned polling_rate at HZ_1000 (the _full_populated_stub
            # default). Flip the stub to HZ_8000 post-capture so the writes
            # report OK but the readback returns the new value.
            stub.polling_rate = PollingRate.HZ_8000
            result = service.restore(captured.id)
            polling_outcome = next(
                f for f in result.fields if f.field_name == "polling_rate"
            )
            self.assertIs(polling_outcome.verify_matched, False)
            self.assertIsNotNone(polling_outcome.expected_value)
            self.assertIsNotNone(polling_outcome.observed_value)
            self.assertIn("HZ_1000", polling_outcome.expected_value)
            self.assertIn("HZ_8000", polling_outcome.observed_value)

    def test_verified_field_outcome_leaves_expected_observed_none(self) -> None:
        """On verified fields, ``expected_value`` / ``observed_value`` stay
        ``None`` — the per-field record only carries diagnostic values when
        something actually went wrong, keeping the common-case JSON small.
        """
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            result = service.restore(captured.id)
            verified_outcomes = [
                f for f in result.fields if f.verify_matched is True
            ]
            self.assertGreater(len(verified_outcomes), 0)
            for outcome in verified_outcomes:
                self.assertIsNone(
                    outcome.expected_value,
                    f"{outcome.field_name}: expected_value should stay None on success",
                )
                self.assertIsNone(
                    outcome.observed_value,
                    f"{outcome.field_name}: observed_value should stay None on success",
                )

    def test_could_not_verify_carries_read_exception_text(self) -> None:
        """Restore-result enrichment: when the verify-read raises (e.g.
        HID timeout), ``verify_note`` must include the underlying exception
        text so the result page can show "verify-read failed: TimeoutError:
        HID read timed out after 967ms" instead of forcing the user to grep
        zd_wrapper.log. Reproduces the shape local testing surfaced, where
        polling_rate's verify timed out at 967ms inside the burst.
        """
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            # Make the polling-rate read raise on every call: capture()'s
            # before_restore snapshot will miss polling_rate (one of many
            # fields) and the verify-read will raise too. _safe_call swallows
            # both; the verify-read's exception text propagates onto the
            # per-field outcome.
            def _raise_polling():
                raise TimeoutError("HID read timed out after 967ms")
            stub.get_polling_rate = _raise_polling
            result = service.restore(captured.id)
            polling_outcome = next(
                f for f in result.fields if f.field_name == "polling_rate"
            )
            self.assertIsNone(polling_outcome.verify_matched)
            self.assertIsNotNone(polling_outcome.verify_note)
            self.assertIn("TimeoutError", polling_outcome.verify_note)
            self.assertIn("967ms", polling_outcome.verify_note)
            self.assertIn("verify-read failed", polling_outcome.verify_note)

    def test_could_not_verify_falls_back_to_generic_note_when_no_exception(self) -> None:
        """When a getter returns ``None`` without raising (e.g. the stub's
        ``polling_rate=None`` mode), no exception text is captured and the
        verify_note keeps the original generic copy. Pins that the
        exception-text branch only fires when there's actually an exception
        to attribute to the field.
        """
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            # Drop polling_rate to None — the getter returns None cleanly
            # (no exception raised). This must keep the bare-bones note.
            stub.polling_rate = None
            result = service.restore(captured.id)
            polling_outcome = next(
                f for f in result.fields if f.field_name == "polling_rate"
            )
            self.assertIsNone(polling_outcome.verify_matched)
            self.assertEqual(
                polling_outcome.verify_note,
                "read-back did not return a value",
            )

    def test_restore_skips_unwritable_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            stub.write_calls.clear()
            service.restore(captured.id)
            self.assertNotIn("motion", stub.write_calls)
            # The set_motion_settings setter isn't even defined on the stub
            # because the coordinator should never try to call it.

    def test_restore_persists_attempt_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            service, _stub, store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            result = service.restore(captured.id)
            reloaded = store.load(captured.id)
            self.assertIsNotNone(reloaded.last_restore_attempt)
            self.assertEqual(reloaded.last_restore_attempt.label, result.label)
            self.assertEqual(reloaded.last_restore_attempt.attempted, result.attempted)


class ListPassThroughTests(unittest.TestCase):
    def test_list_with_skipped_round_trips_via_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, _store = _make_service(
                tmpdir=tmp, settings_stub=_full_populated_stub()
            )
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            valid, skipped = service.list_with_skipped()
            self.assertEqual([rp.id for rp in valid], [captured.id])
            self.assertEqual(skipped, [])


class ReadCurrentStateWithProvenanceTests(unittest.TestCase):
    """The public passthrough the Device-vs-Profile screen reads through."""

    def test_returns_snapshot_success_and_error_triple(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, _store = _make_service(
                tmpdir=tmp, settings_stub=_full_populated_stub()
            )
            snapshot, read_success, read_errors = (
                service.read_current_state_with_provenance()
            )
            # Snapshot is a real fresh read; provenance maps carry the per-field
            # success signal the diff screen needs to tell unreadable from absent.
            self.assertEqual(snapshot.polling_rate, PollingRate.HZ_1000)
            self.assertIs(read_success["polling_rate"], True)
            self.assertIsInstance(read_success["button_bindings"], set)
            self.assertEqual(read_errors, {})

    def test_read_failure_surfaces_in_error_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()

            def _boom() -> None:
                raise TimeoutError("HID read timed out after 967ms")

            stub.get_polling_rate = _boom  # type: ignore[method-assign]
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            snapshot, read_success, read_errors = (
                service.read_current_state_with_provenance()
            )
            self.assertIsNone(snapshot.polling_rate)
            self.assertIs(read_success["polling_rate"], False)
            self.assertIn("TimeoutError", read_errors["polling_rate"])


# ---------------------------------------------------------------------------
# compute_restore_preview — pre-restore diff exposed to the CONFIRM modal.
# ---------------------------------------------------------------------------


class ComputeRestorePreviewTests(unittest.TestCase):
    """Coverage for :meth:`RestorePointService.compute_restore_preview`.

    The preview mirrors the field surface of :meth:`restore` (minus the
    write-only ``back_paddle_bindings``) and never raises — read failures
    become per-field notes. These tests pin the count fields, the per-field
    will_change semantics, and the unreadable-fallback wording so the
    CONFIRM modal's render logic stays trustworthy.
    """

    def _capture_and_drift(
        self,
        *,
        tmpdir: str,
        mutate: Optional[Callable[[_StubSettingsService], None]] = None,
    ):
        """Build a service + RP at one device state; then optionally drift
        the stub so the preview compares two different states.
        Returns ``(service, stub, captured_rp)``.
        """

        stub = _full_populated_stub()
        service, stub, _store = _make_service(tmpdir=tmpdir, settings_stub=stub)
        captured = service.capture(_basic_trigger(), device_identity=_identity())
        assert captured is not None
        if mutate is not None:
            mutate(stub)
        return service, stub, captured

    def test_preview_with_no_drift_reports_zero_changing(self) -> None:
        """When the live device matches the captured RP exactly, the
        preview reports zero changing fields and zero unreadable — every
        delta is the unchanged steady state. The CONFIRM modal uses this
        to render the "No changes detected" copy.
        """
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, captured = self._capture_and_drift(tmpdir=tmp)
            preview = service.compute_restore_preview(captured.id)
            self.assertEqual(preview.fields_changing, 0)
            self.assertEqual(preview.fields_unreadable, 0)
            self.assertGreater(preview.fields_unchanged, 0)
            for delta in preview.fields:
                self.assertFalse(
                    delta.will_change,
                    f"{delta.field_name}: should be unchanged",
                )
                self.assertIsNone(delta.note)
                self.assertIsNotNone(delta.current_value)
                self.assertIsNotNone(delta.target_value)

    def test_preview_with_all_drift_reports_every_field_changing(self) -> None:
        """When every readable field has drifted post-capture, the preview
        reports each one as changing with both current and target values
        populated.
        """
        def _drift(stub: _StubSettingsService) -> None:
            stub.polling_rate = PollingRate.HZ_8000
            stub.step_size = 999
            stub.vibration = VibrationSettings(
                99, 99, 99, 99, TriggerVibrationMode.NATIVE
            )
            stub.deadzones = StickDeadzones(99, 99, 99, 99)
            stub.axis_inversion = (
                AxisInversion(True, True), AxisInversion(True, True),
            )
            stub.sensitivity = (
                (
                    SensitivityAnchor(99, 99),
                    SensitivityAnchor(99, 99),
                    SensitivityAnchor(99, 99),
                ),
                (
                    SensitivityAnchor(99, 99),
                    SensitivityAnchor(99, 99),
                    SensitivityAnchor(99, 99),
                ),
            )
            stub.trigger = (
                TriggerSettings(99, 99, TriggerMode.LONG),
                TriggerSettings(99, 99, TriggerMode.LONG),
            )
            stub.button_bindings = {
                ButtonSlot.A: ButtonMapping.controller_button(
                    ControllerButtonTarget.X
                ),
            }
            stub.lighting_zones = {
                LightingZone.HOME: LightingSettings(
                    True, LightingMode.ALWAYS_ON, 50, RgbColor(99, 99, 99)
                ),
            }

        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, captured = self._capture_and_drift(
                tmpdir=tmp, mutate=_drift
            )
            preview = service.compute_restore_preview(captured.id)
            self.assertGreater(preview.fields_changing, 0)
            self.assertEqual(preview.fields_unchanged, 0)
            self.assertEqual(preview.fields_unreadable, 0)
            for delta in preview.fields:
                self.assertTrue(
                    delta.will_change,
                    f"{delta.field_name}: should be changing",
                )
                self.assertIsNotNone(delta.current_value)
                self.assertIsNotNone(delta.target_value)
                self.assertNotEqual(delta.current_value, delta.target_value)

    def test_preview_reports_unreadable_when_getter_raises(self) -> None:
        """When a fresh-read getter raises (HID timeout, OSError), the
        preview's matching delta has ``current_value=None`` and a note
        containing the exception text. The preview must NOT propagate.
        """
        with tempfile.TemporaryDirectory() as tmp:
            service, stub, captured = self._capture_and_drift(tmpdir=tmp)

            def _raise_polling():
                raise TimeoutError("HID read timed out after 967ms")

            stub.get_polling_rate = _raise_polling
            preview = service.compute_restore_preview(captured.id)
            polling = next(
                d for d in preview.fields if d.field_name == "polling_rate"
            )
            self.assertIsNone(polling.current_value)
            self.assertFalse(polling.will_change)
            self.assertIsNotNone(polling.note)
            self.assertIn("TimeoutError", polling.note)
            self.assertIn("967ms", polling.note)
            self.assertGreaterEqual(preview.fields_unreadable, 1)

    def test_preview_reports_unreadable_when_getter_returns_none(self) -> None:
        """When a getter returns ``None`` cleanly (no exception), the
        impacted delta still surfaces as unreadable with the
        "device not readable" fallback note — the user sees the same
        column for both unsupported-getter and exception cases.
        """
        with tempfile.TemporaryDirectory() as tmp:
            service, stub, captured = self._capture_and_drift(tmpdir=tmp)
            stub.polling_rate = None
            preview = service.compute_restore_preview(captured.id)
            polling = next(
                d for d in preview.fields if d.field_name == "polling_rate"
            )
            self.assertIsNone(polling.current_value)
            self.assertFalse(polling.will_change)
            self.assertEqual(polling.note, "device not readable")

    def test_preview_includes_every_writable_scalar_target_field(self) -> None:
        """The preview mirrors the restore() field surface — every
        writable scalar that the captured RP recorded shows up as one
        delta with ``target_value`` set.
        """
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, captured = self._capture_and_drift(tmpdir=tmp)
            preview = service.compute_restore_preview(captured.id)
            names = {d.field_name for d in preview.fields}
            for expected in (
                "polling_rate",
                "step_size",
                "vibration",
                "deadzones",
                "axis_inversion_left",
                "axis_inversion_right",
                "sensitivity_left",
                "sensitivity_right",
                "trigger_left",
                "trigger_right",
            ):
                self.assertIn(expected, names, f"missing scalar {expected}")

    def test_preview_breaks_collections_into_per_slot_entries(self) -> None:
        """``button_bindings`` and ``lighting_zones`` expand to one delta
        per captured slot/zone so the modal can call out exactly which
        binding or zone changed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, captured = self._capture_and_drift(tmpdir=tmp)
            preview = service.compute_restore_preview(captured.id)
            names = {d.field_name for d in preview.fields}
            # _full_populated_stub captures button_bindings[A] + lighting_zones[HOME].
            self.assertIn("button_bindings[A]", names)
            self.assertIn("lighting_zones[HOME]", names)

    def test_preview_excludes_back_paddle_bindings_even_if_captured(self) -> None:
        """back_paddle_bindings is a write-only device surface — there's
        no read path, so the preview would always render it as
        "unreadable". The spec excludes it explicitly to avoid noise.
        """
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            from zd_app.services.settings_service import (
                BackPaddleBinding,
                ControllerButtonTarget,
                MacroSlot,
            )
            stub.back_paddles = {
                MacroSlot.M1: BackPaddleBinding(
                    target=ControllerButtonTarget.A
                ),
            }
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            preview = service.compute_restore_preview(captured.id)
            names = {d.field_name for d in preview.fields}
            for name in names:
                self.assertFalse(
                    name.startswith("back_paddle_bindings"),
                    f"preview unexpectedly included {name}",
                )

    def test_preview_renders_scalar_str_values_inline(self) -> None:
        """Per-field ``target_value`` / ``current_value`` are ``str()``
        renderings (same convention the restore-result-enrichment work uses on
        :class:`RestoreFieldOutcome`). For ``PollingRate``, that means
        the enum's ``str()`` text contains the variant name.
        """
        def _drift(stub: _StubSettingsService) -> None:
            stub.polling_rate = PollingRate.HZ_8000

        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, captured = self._capture_and_drift(
                tmpdir=tmp, mutate=_drift
            )
            preview = service.compute_restore_preview(captured.id)
            polling = next(
                d for d in preview.fields if d.field_name == "polling_rate"
            )
            self.assertTrue(polling.will_change)
            self.assertIn("HZ_1000", polling.target_value)
            self.assertIn("HZ_8000", polling.current_value)

    def test_preview_renders_collection_values_compactly(self) -> None:
        """Per the compact-preview rendering: collection-type fields
        (vibration, deadzones, axis_inversion, trigger, sensitivity,
        button binding entries, lighting zone entries) render via the
        :mod:`zd_app.services.restore_field_formatting` helper — NOT
        ``str(dataclass)`` — so the modal does not leak class names
        like ``VibrationSettings(``.

        Drifts every collection-type field so each one populates both
        ``current_value`` and ``target_value``.
        """
        def _drift(stub: _StubSettingsService) -> None:
            stub.vibration = VibrationSettings(
                99, 99, 99, 99, TriggerVibrationMode.STEREO_RESONANCE
            )
            stub.deadzones = StickDeadzones(99, 99, 99, 99)
            stub.axis_inversion = (
                AxisInversion(True, True),
                AxisInversion(True, True),
            )
            stub.sensitivity = (
                (
                    SensitivityAnchor(11, 22),
                    SensitivityAnchor(33, 44),
                    SensitivityAnchor(55, 66),
                ),
                (
                    SensitivityAnchor(11, 22),
                    SensitivityAnchor(33, 44),
                    SensitivityAnchor(55, 66),
                ),
            )
            stub.trigger = (
                TriggerSettings(20, 80, TriggerMode.SHORT),
                TriggerSettings(20, 80, TriggerMode.SHORT),
            )
            stub.button_bindings = {
                ButtonSlot.A: ButtonMapping.controller_button(
                    ControllerButtonTarget.X
                ),
            }
            stub.lighting_zones = {
                LightingZone.HOME: LightingSettings(
                    True, LightingMode.BREATH, 50, RgbColor(99, 88, 77)
                ),
            }

        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, captured = self._capture_and_drift(
                tmpdir=tmp, mutate=_drift
            )
            preview = service.compute_restore_preview(captured.id)
            by_name = {d.field_name: d for d in preview.fields}

            for field in (
                "vibration",
                "deadzones",
                "axis_inversion_left",
                "axis_inversion_right",
                "sensitivity_left",
                "sensitivity_right",
                "trigger_left",
                "trigger_right",
                "button_bindings[A]",
                "lighting_zones[HOME]",
            ):
                delta = by_name[field]
                for rendered in (delta.current_value, delta.target_value):
                    self.assertIsNotNone(rendered, f"{field}: rendered None")
                    for fragment in (
                        "VibrationSettings(",
                        "StickDeadzones(",
                        "AxisInversion(",
                        "TriggerSettings(",
                        "ButtonMapping(",
                        "LightingSettings(",
                        "SensitivityAnchor(",
                        "RgbColor(",
                    ):
                        self.assertNotIn(
                            fragment,
                            rendered,
                            f"{field}: leaked class name {fragment!r} in {rendered!r}",
                        )

            # Spot-check that the compact info actually made it through —
            # vibration carries all four strength bytes plus mode name.
            vibration = by_name["vibration"]
            for fragment in ("10", "20", "30", "40", "NATIVE"):
                self.assertIn(fragment, vibration.target_value)
            for fragment in ("99", "STEREO_RESONANCE"):
                self.assertIn(fragment, vibration.current_value)

            # Deadzones carry all four values (captured 5/6/90/91).
            deadzones = by_name["deadzones"]
            for fragment in ("5", "6", "90", "91"):
                self.assertIn(fragment, deadzones.target_value)

            # Button mapping renders the target name (X in drift, B in capture).
            self.assertIn("X", by_name["button_bindings[A]"].current_value)
            self.assertIn("B", by_name["button_bindings[A]"].target_value)

    def test_preview_step_size_drift_picked_up(self) -> None:
        """Specific drift case from today's smoke: step_size is one of the
        burst-vulnerable fields that motivated this preview. The user
        drifts step_size via the wrapper, then chooses Restore — the
        preview must show the diff so they understand what's about to
        change.
        """
        def _drift(stub: _StubSettingsService) -> None:
            stub.step_size = 75   # drifted from captured 128

        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, captured = self._capture_and_drift(
                tmpdir=tmp, mutate=_drift
            )
            preview = service.compute_restore_preview(captured.id)
            step = next(d for d in preview.fields if d.field_name == "step_size")
            self.assertTrue(step.will_change)
            self.assertEqual(step.current_value, "75")
            self.assertEqual(step.target_value, "128")

    def test_preview_does_not_raise_when_every_getter_fails(self) -> None:
        """End-to-end honesty: even if every read fails (HID is
        completely unreachable), the preview returns a sensible
        all-unreadable result rather than raising. The CONFIRM modal
        then renders the unreadable count.
        """
        with tempfile.TemporaryDirectory() as tmp:
            service, stub, captured = self._capture_and_drift(tmpdir=tmp)

            def _raise(*_a, **_kw):
                raise TimeoutError("HID read timed out")

            stub.get_polling_rate = _raise
            stub.get_vibration = _raise
            stub.get_deadzones = _raise
            stub.get_axis_inversion = _raise
            stub.get_sensitivity_curves = _raise
            stub.get_trigger_settings = _raise
            stub.get_motion_settings = _raise
            stub.get_step_size = _raise
            stub.get_button_binding = lambda _slot: _raise()
            stub.get_zone_lighting = lambda _zone: _raise()
            stub.get_all_back_paddle_bindings = _raise

            preview = service.compute_restore_preview(captured.id)
            self.assertEqual(preview.fields_changing, 0)
            self.assertEqual(preview.fields_unchanged, 0)
            self.assertGreater(preview.fields_unreadable, 0)
            for delta in preview.fields:
                self.assertIsNone(delta.current_value)
                self.assertIsNotNone(delta.note)


class DeleteTests(unittest.TestCase):
    """service.delete() — thin pass-through to :meth:`RestorePointStore.delete`."""

    def test_delete_passes_through_to_store_and_removes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, store = _make_service(
                tmpdir=tmp, settings_stub=_full_populated_stub()
            )
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            assert rp is not None
            self.assertTrue(service.delete(rp.id))
            with self.assertRaises(FileNotFoundError):
                store.load(rp.id)

    def test_delete_returns_false_for_unknown_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, _store = _make_service(
                tmpdir=tmp, settings_stub=_full_populated_stub()
            )
            self.assertFalse(service.delete("rp_does_not_exist"))

    def test_delete_without_ledger_does_not_crash(self) -> None:
        # Default _make_service has wear_ledger=None — delete must work
        # without firing any event (mirrors the capture/restore contract).
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, store = _make_service(
                tmpdir=tmp, settings_stub=_full_populated_stub()
            )
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            assert rp is not None
            self.assertTrue(service.delete(rp.id))
            valid, _ = store.list()
            self.assertEqual(valid, [])


class WearLedgerIntegrationTests(unittest.TestCase):
    """RestorePointService writes wear-ledger events on capture + restore +
    delete."""

    def _service_with_ledger(self, tmp: str) -> tuple[
        RestorePointService,
        _StubSettingsService,
        "WearLedgerService",
    ]:
        from zd_app.services.wear_ledger import WearLedgerService

        stub = _full_populated_stub()
        store = RestorePointStore(tmp + "/restore_points")
        coordinator = SettingsApplyCoordinator(stub)  # type: ignore[arg-type]
        ledger = WearLedgerService(base_dir=Path(tmp) / "wear_ledger")
        service = RestorePointService(
            store=store,
            settings_service=stub,  # type: ignore[arg-type]
            apply_coordinator=coordinator,
            app_version="2.0.0-test",
            app_build_commit="abc1234",
            clock=lambda: 1000.0,
            utc_now=lambda: datetime(2026, 5, 24, 19, 3, 55, tzinfo=timezone.utc),
            wear_ledger=ledger,
        )
        return service, stub, ledger

    def test_capture_emits_rp_capture_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, ledger = self._service_with_ledger(tmp)
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            self.assertIsNotNone(rp)
            assert rp is not None
            events = ledger.read_events()
            capture_events = [e for e in events if e.event_type == "rp_capture"]
            self.assertEqual(len(capture_events), 1)
            self.assertEqual(capture_events[0].details["rp_id"], rp.id)
            self.assertEqual(capture_events[0].details["title"], rp.title)
            self.assertIn(rp.title, capture_events[0].summary)

    def test_restore_emits_rp_restore_event_with_result_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, ledger = self._service_with_ledger(tmp)
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            assert rp is not None
            result = service.restore(rp.id)
            restore_events = [
                e for e in ledger.read_events() if e.event_type == "rp_restore"
            ]
            self.assertEqual(len(restore_events), 1)
            self.assertEqual(restore_events[0].details["rp_id"], rp.id)
            self.assertEqual(
                restore_events[0].details["result_label"], result.label.value
            )
            self.assertIn(rp.title, restore_events[0].summary)

    def test_capture_without_ledger_does_not_crash(self) -> None:
        # Default _make_service has wear_ledger=None — capture/restore must
        # continue to work without firing any event.
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, _store = _make_service(
                tmpdir=tmp, settings_stub=_full_populated_stub()
            )
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            self.assertIsNotNone(rp)

    def test_delete_emits_rp_delete_event_with_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, ledger = self._service_with_ledger(tmp)
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            assert rp is not None
            self.assertTrue(service.delete(rp.id))
            delete_events = [
                e for e in ledger.read_events() if e.event_type == "rp_delete"
            ]
            self.assertEqual(len(delete_events), 1)
            self.assertEqual(delete_events[0].details["rp_id"], rp.id)
            self.assertEqual(delete_events[0].details["title"], rp.title)
            self.assertIn(rp.title, delete_events[0].summary)

    def test_delete_miss_emits_no_event(self) -> None:
        # A no-op delete (id not in the vault) must not pollute the
        # maintenance log with a "Deleted" line for something that never
        # went away.
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, ledger = self._service_with_ledger(tmp)
            self.assertFalse(service.delete("rp_does_not_exist"))
            self.assertEqual(
                [e for e in ledger.read_events() if e.event_type == "rp_delete"],
                [],
            )


class Restore8PointSensitivityTests(unittest.TestCase):
    """The 1.2.9 / fw-1.24 8-point (cat 0x86) curve survives capture → store →
    restore and is written + verified in place of the 3-point curve on a capable
    device, while a legacy controller is byte-for-byte unaffected (fold design,
    see ``_SENSITIVITY_8POINT_RIDERS``).
    """

    # -- capable device ------------------------------------------------------

    def test_capture_records_8point_curve_on_capable_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, store = _make_service(
                tmpdir=tmp, settings_stub=_full_populated_stub_8point()
            )
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            # In-memory snapshot carries it...
            self.assertEqual(captured.snapshot.sensitivity_left_8point, _LEFT_8POINT)
            self.assertEqual(captured.snapshot.sensitivity_right_8point, _RIGHT_8POINT)
            # ...and so does the snapshot after a codec round-trip through the
            # store (the dependency this work builds on top of).
            reloaded = store.load(captured.id)
            self.assertEqual(reloaded.snapshot.sensitivity_left_8point, _LEFT_8POINT)
            self.assertEqual(reloaded.snapshot.sensitivity_right_8point, _RIGHT_8POINT)

    def test_restore_writes_and_verifies_8point_on_capable_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub_8point()
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            result = service.restore(captured.id)

            self.assertEqual(result.label, RestoreResultLabel.VERIFIED)
            self.assertEqual(result.write_failed, 0)
            self.assertEqual(result.mismatched, 0)
            # The 8-point write actually went to the device via the 0x86 setters.
            self.assertIn(
                "sens_left",
                stub.write_calls,
            )
            # The verify set carries the 8-point fields, not the 3-point hosts.
            names = {f.field_name for f in result.fields}
            self.assertIn("sensitivity_left_8point", names)
            self.assertIn("sensitivity_right_8point", names)
            self.assertNotIn("sensitivity_left", names)
            self.assertNotIn("sensitivity_right", names)
            left = next(
                f for f in result.fields if f.field_name == "sensitivity_left_8point"
            )
            self.assertTrue(left.write_succeeded)
            self.assertIs(left.verify_matched, True)

    def test_restore_8point_uses_0x86_setter_not_3point(self) -> None:
        # The coordinator must reach for the 8-point setter on a capable device;
        # the 3-point setter must not fire for a stick that has an 8-point curve.
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub_8point()

            calls: dict[str, object] = {}
            orig_8 = stub.set_left_stick_sensitivity_curve_8point
            orig_3 = stub.set_left_stick_sensitivity_curve

            def _rec_8(anchors):
                calls["8point"] = anchors
                return orig_8(anchors)

            def _rec_3(anchors):
                calls["3point"] = anchors
                return orig_3(anchors)

            stub.set_left_stick_sensitivity_curve_8point = _rec_8  # type: ignore[assignment]
            stub.set_left_stick_sensitivity_curve = _rec_3  # type: ignore[assignment]

            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            service.restore(captured.id)

            self.assertEqual(calls.get("8point"), _LEFT_8POINT)
            self.assertNotIn("3point", calls)

    def test_restore_partial_when_8point_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub_8point()
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            # The 8-point writer emits the shared ``sens_left`` label; failing it
            # must flip the 8-point field outcome.
            stub.fail_labels = {"sens_left"}
            result = service.restore(captured.id)

            self.assertEqual(result.label, RestoreResultLabel.PARTIALLY_RESTORED)
            left = next(
                f for f in result.fields if f.field_name == "sensitivity_left_8point"
            )
            self.assertFalse(left.write_succeeded)

    def test_restore_mismatch_when_8point_readback_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub_8point()
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            # Drift the live 8-point curve after capture: writes report OK but
            # the read-back differs from the captured curve.
            drifted = tuple(SensitivityAnchor(a.x, a.y + 1) for a in _LEFT_8POINT)
            stub.sensitivity_8point = (drifted, _RIGHT_8POINT)
            result = service.restore(captured.id)

            self.assertEqual(result.label, RestoreResultLabel.MISMATCH_AFTER_RESTORE)
            left = next(
                f for f in result.fields if f.field_name == "sensitivity_left_8point"
            )
            self.assertIs(left.verify_matched, False)
            self.assertIsNotNone(left.expected_value)
            self.assertIsNotNone(left.observed_value)

    def test_mixed_one_stick_8point_other_3point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub_8point()
            # Only LEFT carries an 8-point curve; RIGHT falls back to 3-point.
            stub.sensitivity_8point = (_LEFT_8POINT, None)
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            result = service.restore(captured.id)

            names = {f.field_name for f in result.fields}
            self.assertIn("sensitivity_left_8point", names)
            self.assertIn("sensitivity_right", names)
            self.assertNotIn("sensitivity_left", names)
            self.assertNotIn("sensitivity_right_8point", names)
            self.assertEqual(result.label, RestoreResultLabel.VERIFIED)

    def test_excluding_host_stick_drops_the_8point_rider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub_8point()
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            result = service.restore(
                captured.id, excluded_fields={"sensitivity_left"}
            )
            names = {f.field_name for f in result.fields}
            # Excluding the host drops its rider too — neither left field is
            # attempted; the right stick still rides 8-point.
            self.assertNotIn("sensitivity_left", names)
            self.assertNotIn("sensitivity_left_8point", names)
            self.assertIn("sensitivity_right_8point", names)

    def test_coverage_folds_8point_into_sensitivity_no_extra_entries(self) -> None:
        # The fold guarantee: a capable device produces NO per-stick 8-point
        # coverage row and the supported total is unchanged from a legacy device.
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, _store = _make_service(
                tmpdir=tmp, settings_stub=_full_populated_stub_8point()
            )
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            assert rp is not None
            self.assertNotIn("sensitivity_left_8point", rp.coverage.fields)
            self.assertNotIn("sensitivity_right_8point", rp.coverage.fields)
            self.assertEqual(rp.coverage.total_supported_count, 13)

    def test_preview_shows_8point_delta_on_capable_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub_8point()
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            preview = service.compute_restore_preview(captured.id)
            names = {d.field_name for d in preview.fields}
            self.assertIn("sensitivity_left_8point", names)
            self.assertIn("sensitivity_right_8point", names)
            self.assertNotIn("sensitivity_left", names)
            self.assertNotIn("sensitivity_right", names)
            left = next(
                d for d in preview.fields
                if d.field_name == "sensitivity_left_8point"
            )
            # All 8 anchor pairs render (no truncation in the diff helper).
            self.assertIn("(0,0)", left.target_value)
            self.assertIn("(70,70)", left.target_value)

    # -- legacy device (regression guard) ------------------------------------

    def test_legacy_device_restore_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()  # not capable
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            # No 8-point captured; the probe answered not-capable.
            self.assertIsNone(captured.snapshot.sensitivity_left_8point)
            self.assertIsNone(captured.snapshot.sensitivity_right_8point)

            result = service.restore(captured.id)
            names = {f.field_name for f in result.fields}
            # Still verifies the 3-point hosts; no 8-point field appears anywhere.
            self.assertIn("sensitivity_left", names)
            self.assertIn("sensitivity_right", names)
            self.assertNotIn("sensitivity_left_8point", names)
            self.assertNotIn("sensitivity_right_8point", names)
            self.assertEqual(result.label, RestoreResultLabel.VERIFIED)

    def test_legacy_device_never_probes_per_stick_8point(self) -> None:
        # A not-capable device must answer the capability probe False and never
        # issue the per-stick 0x86 reads (mirrors get_all_settings).
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            calls = {"per_stick": 0}
            orig = stub.get_sensitivity_curve_8point

            def _count(stick):
                calls["per_stick"] += 1
                return orig(stick)

            stub.get_sensitivity_curve_8point = _count  # type: ignore[assignment]
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            service.capture(_basic_trigger(), device_identity=_identity())
            self.assertEqual(calls["per_stick"], 0)


def _exhausting_clock(*ticks: float, then: float = 10_000.0) -> Callable[[], float]:
    """Scripted clock: yields ``ticks`` in order, then sticks at ``then``."""

    remaining = list(ticks)

    def _clock() -> float:
        if remaining:
            return remaining.pop(0)
        return then

    return _clock


_BUDGET_SKIP = "skipped: batch read budget exhausted"


class BatchReadBudgetTests(unittest.TestCase):
    """B4/B5: ``_do_fresh_read`` under an exhausted batch budget.

    Skipped fields must be recorded as UNREADABLE (``read_errors`` carries the
    "skipped:" marker) — never as legitimately absent — and ``capture()`` must
    still succeed with honestly-reduced coverage counts.
    """

    # Every snapshot field downstream of the first getter (polling_rate). The
    # 8-point riders are included: the capability probe was budget-skipped, so
    # capability is unknown and the riders are unread, unlike a not-capable
    # device where they are legitimately absent.
    _EXPECTED_SKIPPED_FIELDS = frozenset(
        {
            "vibration",
            "deadzones",
            "axis_inversion_left",
            "axis_inversion_right",
            "sensitivity_left",
            "sensitivity_right",
            "sensitivity_left_8point",
            "sensitivity_right_8point",
            "trigger_left",
            "trigger_right",
            "motion_settings",
            "step_size",
            "button_bindings",
            "lighting_zones",
            "back_paddle_bindings",
        }
    )

    def _budget_service(
        self, tmpdir: str
    ) -> tuple[RestorePointService, _StubSettingsService, RestorePointStore]:
        # Deadline is computed at tick 0.0 (-> 8.0); the first getter's guard
        # sees 1.0 (issued), and every guard after that sees 10_000
        # (exhausted), so exactly one getter — polling_rate — runs.
        stub = _full_populated_stub()
        store = RestorePointStore(tmpdir)
        service = RestorePointService(
            store=store,
            settings_service=stub,  # type: ignore[arg-type]
            apply_coordinator=SettingsApplyCoordinator(stub),  # type: ignore[arg-type]
            app_version="2.0.0-test",
            app_build_commit="abc1234",
            clock=_exhausting_clock(0.0, 1.0),
            utc_now=lambda: datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc),
        )
        return service, stub, store

    def test_fresh_read_budget_skip_provenance(self) -> None:
        # B4 (read side): skipped scalars land in read_errors with the
        # "skipped:" prefix; collection read_success sets stay empty.
        with tempfile.TemporaryDirectory() as tmp:
            service, stub, _store = self._budget_service(tmp)

            with self.assertLogs(
                "zd_app.services.restore_point_service", level="WARNING"
            ) as cm:
                snapshot, read_success, read_errors = (
                    service.read_current_state_with_provenance()
                )

            # The first getter completed before the deadline...
            self.assertEqual(snapshot.polling_rate, stub.polling_rate)
            self.assertTrue(read_success["polling_rate"])
            self.assertNotIn("polling_rate", read_errors)
            # ...and every later field is marked unreadable, not absent.
            self.assertEqual(
                read_errors,
                {name: _BUDGET_SKIP for name in self._EXPECTED_SKIPPED_FIELDS},
            )
            for name in self._EXPECTED_SKIPPED_FIELDS:
                self.assertTrue(read_errors[name].startswith("skipped:"), name)
            self.assertIsNone(snapshot.vibration)
            self.assertIsNone(snapshot.deadzones)
            self.assertIsNone(snapshot.step_size)
            self.assertIsNone(snapshot.sensitivity_left_8point)
            self.assertEqual(snapshot.button_bindings, {})
            # Collection read_success sets stay partial/empty rather than lying.
            self.assertEqual(read_success["button_bindings"], set())
            self.assertEqual(read_success["lighting_zones"], set())
            self.assertEqual(read_success["back_paddle_bindings"], set())
            self.assertFalse(read_success["vibration"])
            # Exactly one budget warning for the whole fresh read.
            self.assertEqual(len(cm.records), 1)
            self.assertIn("batch read budget", cm.output[0])

    def test_capture_succeeds_with_not_captured_coverage_under_exhaustion(
        self,
    ) -> None:
        # B4 (capture side): a budget-truncated fresh read still counts as
        # FRESH_READ and saves; the skipped fields surface as NOT_CAPTURED.
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, store = self._budget_service(tmp)

            rp = service.capture(_basic_trigger(), device_identity=_identity())

            self.assertIsNotNone(rp)
            assert rp is not None
            self.assertEqual(rp.coverage.capture_source, CaptureSource.FRESH_READ)
            self.assertEqual(
                rp.coverage.fields["polling_rate"].state, CoverageState.CAPTURED
            )
            for name in (
                "vibration",
                "deadzones",
                "sensitivity_left",
                "trigger_right",
                "step_size",
                "button_bindings",
                "lighting_zones",
            ):
                self.assertEqual(
                    rp.coverage.fields[name].state, CoverageState.NOT_CAPTURED, name
                )
            # Saved to the vault despite the truncated read.
            points, skipped_files = store.list()
            self.assertEqual([p.id for p in points], [rp.id])
            self.assertEqual(skipped_files, [])

    def test_capture_coverage_counts_reflect_budget_skips(self) -> None:
        # B5: the headline coverage counts admit the truncation — only
        # polling_rate (supported, DEVICE category) was captured.
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, _store = self._budget_service(tmp)

            rp = service.capture(_basic_trigger(), device_identity=_identity())

            self.assertIsNotNone(rp)
            assert rp is not None
            self.assertEqual(rp.coverage.captured_supported_count, 1)
            self.assertLess(
                rp.coverage.captured_supported_count,
                rp.coverage.total_supported_count,
            )

    def test_budget_zero_disables_fresh_read_cap(self) -> None:
        # Mirror of the SettingsService B3: budget 0 = disabled, so even a
        # clock already sitting far past any window reads every field.
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            service = RestorePointService(
                store=RestorePointStore(tmp),
                settings_service=stub,  # type: ignore[arg-type]
                apply_coordinator=SettingsApplyCoordinator(stub),  # type: ignore[arg-type]
                app_version="2.0.0-test",
                clock=_exhausting_clock(),  # 10_000.0 from the first call
                batch_read_budget_s=0,
            )

            snapshot, read_success, read_errors = (
                service.read_current_state_with_provenance()
            )

            self.assertEqual(read_errors, {})
            self.assertEqual(snapshot.polling_rate, stub.polling_rate)
            self.assertEqual(snapshot.vibration, stub.vibration)
            self.assertEqual(snapshot.step_size, stub.step_size)
            self.assertEqual(read_success["button_bindings"], {ButtonSlot.A})
            self.assertEqual(read_success["lighting_zones"], {LightingZone.HOME})


class _ReadRecordingStub(_StubSettingsService):
    """Stub that records ``get_polling_rate`` calls into a shared event list.

    One recorded getter is enough to anchor the verify-read's position in the
    event sequence — ``_do_fresh_read`` always calls it first.
    """

    def __init__(self, events: list) -> None:
        super().__init__()
        self._events = events

    def get_polling_rate(self):
        self._events.append("read:polling_rate")
        return super().get_polling_rate()


class _RecordingCoordinator(SettingsApplyCoordinator):
    """Real coordinator that records ``apply_snapshot`` into the event list."""

    def __init__(self, stub, events: list) -> None:
        super().__init__(stub)  # type: ignore[arg-type]
        self._events = events

    def apply_snapshot(self, snapshot, **kwargs):
        self._events.append("apply")
        return super().apply_snapshot(snapshot, **kwargs)


class RestorePostApplySettleTests(unittest.TestCase):
    """restore() settles between the apply burst and the verify fresh-read.

    Mitigation for the transient first-read-after-burst HID timeout observed
    twice on hardware 2026-06-10: without a quiet interval the first verify
    read can time out and pollute the result with a spurious
    could-not-verify. The sleep seam is injectable (mirroring
    SettingsService) so these tests assert ordering without real delay.
    """

    def _make_settle_service(
        self, tmpdir: str, events: list, **service_kwargs
    ) -> RestorePointService:
        stub = _ReadRecordingStub(events)
        stub.polling_rate = PollingRate.HZ_1000
        id_counter = {"n": 0}

        def _next_id(_moment) -> str:
            n = id_counter["n"]
            id_counter["n"] += 1
            return f"rp_20260610_120000_{n:06x}"

        return RestorePointService(
            store=RestorePointStore(tmpdir),
            settings_service=stub,  # type: ignore[arg-type]
            apply_coordinator=_RecordingCoordinator(stub, events),
            app_version="2.0.0-test",
            app_build_commit="abc1234",
            clock=lambda: 1000.0,
            utc_now=lambda: datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc),
            id_factory=_next_id,
            sleep=lambda seconds: events.append(("sleep", seconds)),
            **service_kwargs,
        )

    def test_restore_sleeps_default_settle_between_apply_and_verify_read(self) -> None:
        # R5a: the default settle (0.25 s) fires exactly once, strictly
        # between apply_snapshot and the verify-read getters. The leading
        # read event is the before_restore capture's fresh read.
        events: list = []
        with tempfile.TemporaryDirectory() as tmp:
            service = self._make_settle_service(tmp, events)
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            assert rp is not None
            events.clear()
            service.restore(rp.id)
        self.assertEqual(
            events,
            ["read:polling_rate", "apply", ("sleep", 0.25), "read:polling_rate"],
        )

    def test_restore_settle_zero_disables_sleep(self) -> None:
        # R5b: post_apply_read_settle_s=0 disables the settle entirely.
        events: list = []
        with tempfile.TemporaryDirectory() as tmp:
            service = self._make_settle_service(
                tmp, events, post_apply_read_settle_s=0
            )
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            assert rp is not None
            events.clear()
            service.restore(rp.id)
        self.assertEqual(
            events,
            ["read:polling_rate", "apply", "read:polling_rate"],
        )


# ---------------------------------------------------------------------------
# verify_applied_snapshot (module-level read-back comparison)
# ---------------------------------------------------------------------------


def _verify_snapshot(**overrides: Any) -> ControllerSnapshot:
    """An all-``None`` / all-empty snapshot for verify_applied_snapshot tests."""

    payload: dict[str, Any] = dict(
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
    payload.update(overrides)
    return ControllerSnapshot(**payload)


_HOST_CURVE_A = (
    SensitivityAnchor(x=0, y=0),
    SensitivityAnchor(x=50, y=50),
    SensitivityAnchor(x=100, y=100),
)
_HOST_CURVE_B = (
    SensitivityAnchor(x=0, y=10),
    SensitivityAnchor(x=50, y=60),
    SensitivityAnchor(x=100, y=100),
)
_RIDER_CURVE_8 = tuple(
    SensitivityAnchor(x=x, y=y)
    for x, y in (
        (0, 5), (14, 20), (29, 35), (43, 50), (57, 65), (71, 80), (86, 90), (100, 100),
    )
)


class VerifyAppliedSnapshotTests(unittest.TestCase):
    """The read-back comparison Safe Import's "verified" claim rides on.

    Write ACKs alone must never produce "verified" (the in-burst firmware
    rejection family) — this helper walks exactly the applied fields,
    dispatches 8-point riders like the apply coordinator, and refuses to
    call write-only surfaces verified.
    """

    def test_all_match_returns_empty_lists(self) -> None:
        kwargs = dict(
            polling_rate=PollingRate.HZ_2000,
            deadzones=StickDeadzones(5, 5, 95, 95),
            button_bindings={ButtonSlot.A: ButtonMapping(0, 0, 0)},
        )
        self.assertEqual(
            verify_applied_snapshot(_verify_snapshot(**kwargs), _verify_snapshot(**kwargs)),
            ([], []),
        )

    def test_scalar_mismatch_is_named(self) -> None:
        applied = _verify_snapshot(deadzones=StickDeadzones(5, 5, 95, 95))
        readback = _verify_snapshot(deadzones=StickDeadzones(9, 9, 90, 90))
        self.assertEqual(
            verify_applied_snapshot(applied, readback), (["deadzones"], [])
        )

    def test_readback_none_scalar_is_unverifiable_not_mismatched(self) -> None:
        applied = _verify_snapshot(polling_rate=PollingRate.HZ_2000)
        self.assertEqual(
            verify_applied_snapshot(applied, _verify_snapshot()),
            ([], ["polling_rate"]),
        )

    def test_rider_present_skips_host_compare(self) -> None:
        # The stick was written via cat 0x86, so the rider is what gets
        # compared; the intentionally-different 3-point host values must NOT
        # surface as a mismatch.
        applied = _verify_snapshot(
            sensitivity_left=_HOST_CURVE_A,
            sensitivity_left_8point=_RIDER_CURVE_8,
        )
        readback = _verify_snapshot(
            sensitivity_left=_HOST_CURVE_B,
            sensitivity_left_8point=_RIDER_CURVE_8,
        )
        self.assertEqual(verify_applied_snapshot(applied, readback), ([], []))

    def test_rider_readback_none_is_unverifiable_under_rider_name(self) -> None:
        applied = _verify_snapshot(
            sensitivity_left=_HOST_CURVE_A,
            sensitivity_left_8point=_RIDER_CURVE_8,
        )
        readback = _verify_snapshot(sensitivity_left=_HOST_CURVE_A)
        self.assertEqual(
            verify_applied_snapshot(applied, readback),
            ([], ["sensitivity_left_8point"]),
        )

    def test_host_without_rider_still_compared(self) -> None:
        # No rider applied for the stick -> the plain 3-point compare runs.
        applied = _verify_snapshot(sensitivity_left=_HOST_CURVE_A)
        readback = _verify_snapshot(sensitivity_left=_HOST_CURVE_B)
        self.assertEqual(
            verify_applied_snapshot(applied, readback), (["sensitivity_left"], [])
        )

    def test_back_paddle_applied_is_unverifiable(self) -> None:
        # Write-only HID surface: the device cannot echo paddles back, so an
        # applied paddle can never flip to "mismatched" — only unverifiable.
        applied = _verify_snapshot(
            back_paddle_bindings={
                MacroSlot.M1: BackPaddleBinding(target=ControllerButtonTarget.A)
            }
        )
        self.assertEqual(
            verify_applied_snapshot(applied, _verify_snapshot()),
            ([], ["back_paddle_bindings[M1]"]),
        )

    def test_readable_collection_missing_key_is_mismatched(self) -> None:
        # Buttons/lighting have a real read path that enumerates every slot —
        # an applied slot absent from the read-back means the write didn't
        # leave a value, which is a mismatch (not an honest read gap).
        applied = _verify_snapshot(
            button_bindings={ButtonSlot.A: ButtonMapping(0, 0, 0)}
        )
        self.assertEqual(
            verify_applied_snapshot(applied, _verify_snapshot()),
            (["button_bindings[A]"], []),
        )

    def test_readable_collection_value_differs_is_mismatched(self) -> None:
        applied = _verify_snapshot(
            button_bindings={ButtonSlot.A: ButtonMapping(0, 0, 0)}
        )
        readback = _verify_snapshot(
            button_bindings={ButtonSlot.A: ButtonMapping(1, 0, 0)}
        )
        self.assertEqual(
            verify_applied_snapshot(applied, readback),
            (["button_bindings[A]"], []),
        )

    def test_motion_settings_never_compared(self) -> None:
        # motion_settings has no write path: applied carries it for record,
        # the read-back lacking it must not count as unverifiable.
        applied = _verify_snapshot(
            motion_settings=MotionSettings(
                MotionMappingTarget.LEFT_JOYSTICK,
                0x06,
                MotionMappingMode.CONTINUOUS,
                50,
            )
        )
        self.assertEqual(
            verify_applied_snapshot(applied, _verify_snapshot()), ([], [])
        )

    def test_none_applied_fields_are_not_walked(self) -> None:
        # Nothing applied -> nothing to verify, regardless of read-back state.
        readback = _verify_snapshot(polling_rate=PollingRate.HZ_2000)
        self.assertEqual(
            verify_applied_snapshot(_verify_snapshot(), readback), ([], [])
        )


class PartialCaptureCollectionVerifyTests(unittest.TestCase):
    """Restore verify compares collection fields per APPLIED
    entry (the verify_applied_snapshot core), not by whole-dict equality.

    A restore point whose capture recorded only some button bindings (per-
    slot read failures, or the batch read budget expiring mid-loop) applies
    those N entries and then reads back the device's FULL slot set — under
    whole-dict equality that reported "read-back value differs from
    expected" even when every applied entry took perfectly.
    """

    _BIND_A = ButtonMapping.controller_button(ControllerButtonTarget.B)
    _BIND_B = ButtonMapping.controller_button(ControllerButtonTarget.A)

    def _full_readback(self) -> dict:
        # The two applied entries, correct, among a wider device-enumerated set.
        return {
            ButtonSlot.A: self._BIND_A,
            ButtonSlot.B: self._BIND_B,
            ButtonSlot.X: ButtonMapping.controller_button(ControllerButtonTarget.Y),
            ButtonSlot.Y: ButtonMapping.controller_button(ControllerButtonTarget.X),
        }

    def test_restore_of_partial_binding_capture_verifies_matched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            stub.button_bindings = {
                ButtonSlot.A: self._BIND_A,
                ButtonSlot.B: self._BIND_B,
            }
            service, _stub, _store = _make_service(tmpdir=tmp, settings_stub=stub)
            captured = service.capture(_basic_trigger(), device_identity=_identity())
            assert captured is not None
            self.assertEqual(
                captured.coverage.fields["button_bindings"].state,
                CoverageState.PARTIAL,
            )

            # Post-capture the device answers for MORE slots than the capture
            # recorded (the stub's setters don't mutate stub state, so this
            # is also the post-restore readback).
            stub.button_bindings = self._full_readback()
            result = service.restore(captured.id)

            outcome = next(
                f for f in result.fields if f.field_name == "button_bindings"
            )
            self.assertIs(outcome.verify_matched, True)
            self.assertIsNone(outcome.verify_note)
            self.assertEqual(result.mismatched, 0)
            self.assertEqual(result.label, RestoreResultLabel.VERIFIED)

    def test_field_outcomes_render_only_differing_entries(self) -> None:
        applied = _verify_snapshot(
            button_bindings={ButtonSlot.A: self._BIND_A, ButtonSlot.B: self._BIND_B}
        )
        readback_entries = self._full_readback()
        readback_entries[ButtonSlot.B] = ButtonMapping.controller_button(
            ControllerButtonTarget.X
        )
        readback = _verify_snapshot(button_bindings=readback_entries)

        outcomes = _build_field_outcomes(
            ["button_bindings"],
            applied,
            readback,
            ApplyResult(total_attempted=2),
            {},
        )

        outcome = outcomes[0]
        self.assertIs(outcome.verify_matched, False)
        self.assertEqual(
            outcome.verify_note, "read-back value differs from expected"
        )
        # Only the differing entry is rendered — not slot A, not the
        # readback-only extras.
        self.assertTrue(outcome.expected_value.startswith("B="))
        self.assertTrue(outcome.observed_value.startswith("B="))
        self.assertNotIn("A=", outcome.expected_value)
        self.assertNotIn("X=", outcome.expected_value)
        self.assertNotIn("A=", outcome.observed_value)
        self.assertNotIn("X=", outcome.observed_value)

    def test_field_outcomes_applied_entry_absent_from_readback_is_mismatch(self) -> None:
        applied = _verify_snapshot(
            button_bindings={ButtonSlot.A: self._BIND_A, ButtonSlot.B: self._BIND_B}
        )
        readback_entries = self._full_readback()
        del readback_entries[ButtonSlot.B]  # readable collection, B missing
        readback = _verify_snapshot(button_bindings=readback_entries)

        outcomes = _build_field_outcomes(
            ["button_bindings"],
            applied,
            readback,
            ApplyResult(total_attempted=2),
            {},
        )

        outcome = outcomes[0]
        self.assertIs(outcome.verify_matched, False)
        self.assertEqual(outcome.observed_value, "B=None")
        self.assertTrue(outcome.expected_value.startswith("B="))

    def test_field_outcomes_back_paddles_stay_could_not_verify(self) -> None:
        applied = _verify_snapshot(
            back_paddle_bindings={
                MacroSlot.M1: BackPaddleBinding(target=ControllerButtonTarget.A)
            }
        )
        readback = _verify_snapshot()  # write-only surface: nothing to read

        outcomes = _build_field_outcomes(
            ["back_paddle_bindings"],
            applied,
            readback,
            ApplyResult(total_attempted=1),
            {},
        )

        outcome = outcomes[0]
        self.assertIsNone(outcome.verify_matched)
        self.assertEqual(outcome.verify_note, "read-back did not return a value")
        self.assertIsNone(outcome.expected_value)
        self.assertIsNone(outcome.observed_value)

    def test_field_outcomes_empty_readable_readback_keeps_read_error_note(self) -> None:
        # A READABLE collection whose readback is entirely empty is a failed
        # verify-read, not a mismatch — and it surfaces the read_errors text.
        applied = _verify_snapshot(
            button_bindings={ButtonSlot.A: self._BIND_A}
        )
        readback = _verify_snapshot()

        outcomes = _build_field_outcomes(
            ["button_bindings"],
            applied,
            readback,
            ApplyResult(total_attempted=1),
            {"button_bindings": "TimeoutError: HID read timed out after 967ms"},
        )

        outcome = outcomes[0]
        self.assertIsNone(outcome.verify_matched)
        self.assertEqual(
            outcome.verify_note,
            "verify-read failed: TimeoutError: HID read timed out after 967ms",
        )


class RestorePostApplyPersistTests(unittest.TestCase):
    """A6/A7: a committed apply's result must survive a post-apply persist
    failure, and a restore with no captured safety snapshot must surface that
    condition (without aborting)."""

    def test_restore_returns_result_when_post_apply_persist_fails(self) -> None:
        # A6: an OSError from the post-apply _store.save must NOT propagate and
        # discard the RestoreResult (the device change already happened).
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, store = _make_service(
                tmpdir=tmp, settings_stub=_full_populated_stub()
            )
            target = service.capture(_basic_trigger(), device_identity=_identity())
            self.assertIsNotNone(target)

            real_save = store.save

            def failing_save(rp):
                # Only the post-apply persist (the rp carrying a restore
                # attempt) fails; the before-restore capture save still works.
                if rp.last_restore_attempt is not None:
                    raise OSError("simulated disk-full on post-apply persist")
                return real_save(rp)

            store.save = failing_save  # type: ignore[assignment]

            result = service.restore(target.id)  # must not raise

            self.assertIsInstance(result, RestoreResult)
            self.assertIsNotNone(result.before_restore_point_id)

    def test_restore_with_no_safety_snapshot_warns_and_exposes_condition(self) -> None:
        # A7: when capture() returns None (flaky controller, no fresh read /
        # cached snapshot), restore() proceeds but must surface the "no rollback
        # point" condition: a pre-apply WARNING + before_restore_point_id None.
        with tempfile.TemporaryDirectory() as tmp:
            service, _stub, _store = _make_service(
                tmpdir=tmp, settings_stub=_full_populated_stub()
            )
            target = service.capture(_basic_trigger(), device_identity=_identity())
            self.assertIsNotNone(target)

            # Simulate the before-restore safety capture failing.
            service.capture = lambda *a, **k: None  # type: ignore[assignment]

            with self.assertLogs(
                "zd_app.services.restore_point_service", level="WARNING"
            ) as cm:
                result = service.restore(target.id)

            self.assertIsInstance(result, RestoreResult)
            self.assertIsNone(result.before_restore_point_id)
            joined = "\n".join(cm.output).lower()
            self.assertIn("without a rollback point", joined)


if __name__ == "__main__":
    unittest.main()
