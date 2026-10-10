"""Adversarial consent/evidence probes, also runnable against the pre-fix tree.

Compatibility selection below is test-only: it exercises the old production
entry on the baseline rather than counting a missing new API as the red proof.
"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_restore_point_service import (
    _basic_trigger, _full_populated_stub, _identity, _make_service,
)
from tests.test_restore_points_screen import _FakeService, _PatchedScreen, _rp, _shell_with
from tests.test_settings_apply_coordinator import _empty_snapshot, _polling_result
from zd_app.services.settings_apply_coordinator import ApplyFailure, ApplyResult, SettingsApplyCoordinator
from zd_app.services.settings_service import (
    BackPaddleBinding, ButtonMapping, ButtonSlot, ControllerButtonTarget,
    MacroSlot, PollingRate, SensitivityAnchor, SetPollingRateOutcome,
)
from zd_app.services.write_verification import build_field_outcomes
from zd_app.storage.restore_point_models import CoverageCategory, CoverageState, FieldCoverage
from zd_app.ui.screens import restore_points as screen


def _prepare(service, rp_id):
    if hasattr(service, "prepare_restore"):
        return service.prepare_restore(rp_id)
    service.compute_restore_preview(rp_id)
    return rp_id


class ConsentRaceTests(unittest.TestCase):
    def test_same_id_replacement_never_changes_approved_controller_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, stub, store = _make_service(tmpdir=tmp, settings_stub=_full_populated_stub())
            a = service.capture(_basic_trigger(), device_identity=_identity())
            prepared = _prepare(service, a.id)
            b = replace(a, snapshot=replace(a.snapshot, polling_rate=PollingRate.HZ_8000))
            store.save(b)
            with patch.object(service._apply_coordinator, "apply_snapshot", wraps=service._apply_coordinator.apply_snapshot) as apply:
                result = service.restore(prepared)
            self.assertEqual(apply.call_args.args[0].polling_rate, a.snapshot.polling_rate)
            self.assertEqual(store.load(a.id).snapshot.polling_rate, b.snapshot.polling_rate)
            self.assertIsNone(store.load(a.id).last_restore_attempt)
            self.assertIsNotNone(result.before_restore_point_id)

    def test_replacement_during_attempt_recording_is_not_overwritten_or_marked_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, stub, store = _make_service(tmpdir=tmp, settings_stub=_full_populated_stub())
            a = service.capture(_basic_trigger(), device_identity=_identity())
            prepared = _prepare(service, a.id)
            b = replace(a, title="Replacement", snapshot=replace(a.snapshot, polling_rate=PollingRate.HZ_8000))
            save = store.save
            if hasattr(store, "record_restore_attempt"):
                record = store.record_restore_attempt
                def racing_record(source, attempt):
                    save(b)
                    record(source, attempt)
                store.record_restore_attempt = racing_record
            else:
                def racing_save(source):
                    if source.last_restore_attempt is not None:
                        save(b)
                    return save(source)
                store.save = racing_save
            service.restore(prepared)
            loaded = store.load(a.id)
            self.assertEqual(loaded.title, "Replacement")
            self.assertIsNone(loaded.last_restore_attempt)

    def test_nonliteral_ack_refused_before_capture_or_write(self):
        for ack in (False, None, "true", "false", 1, 0, [], object()):
            with self.subTest(ack=repr(ack)), tempfile.TemporaryDirectory() as tmp:
                stub = _full_populated_stub()
                stub.back_paddles = {MacroSlot.M1: BackPaddleBinding(ControllerButtonTarget.A)}
                service, stub, store = _make_service(tmpdir=tmp, settings_stub=stub)
                a = service.capture(_basic_trigger(), device_identity=_identity())
                prepared = _prepare(service, a.id)
                stub.write_calls.clear()
                kwargs = {"acknowledge_write_only_paddles": ack} if hasattr(service, "prepare_restore") else {}
                with patch.object(service, "capture", wraps=service.capture) as capture:
                    refused = False
                    try:
                        service.restore(prepared, **kwargs)
                    except PermissionError:
                        refused = True
                    self.assertTrue(refused, "restore accepted an undisclosed write-only operation")
                    capture.assert_not_called()
                self.assertEqual(stub.write_calls, [])

    def test_persisted_coverage_is_not_a_write_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, stub, store = _make_service(tmpdir=tmp, settings_stub=_full_populated_stub())
            a = service.capture(_basic_trigger(), device_identity=_identity())
            path = next(Path(tmp).glob("*.json"))
            payload = json.loads(path.read_text("utf-8"))
            payload["coverage"]["fields"]["motion_settings"]["writable"] = True
            payload["coverage"]["fields"]["polling_rate"]["writable"] = False
            path.write_text(json.dumps(payload), "utf-8")
            result = service.restore(_prepare(service, a.id))
            names = {f.field_name for f in result.fields}
            self.assertNotIn("motion_settings", names)
            self.assertIn("polling_rate", names)


class WriterReceiptTests(unittest.TestCase):
    def test_fallback_receipts_name_the_actual_three_point_setter(self):
        service = Mock()
        service.supports_8point_sensitivity.return_value = False
        service.set_left_stick_sensitivity_curve.return_value = _polling_result(SetPollingRateOutcome.OK)
        host = tuple(SensitivityAnchor(x, x) for x in (0, 50, 100))
        rider = tuple(SensitivityAnchor(x, x) for x in (0, 14, 28, 42, 57, 71, 85, 100))
        result = SettingsApplyCoordinator(service, field_trailer_delay_s=0).apply_snapshot(
            _empty_snapshot(sensitivity_left=host, sensitivity_left_8point=rider))
        service.set_left_stick_sensitivity_curve.assert_called_once_with(host)
        service.set_left_stick_sensitivity_curve_8point.assert_not_called()
        self.assertEqual(result.sensitivity_downgrades, ("sens_left",))
        self.assertEqual(getattr(result, "attempted_labels", ()), ("sens_left",))
        self.assertEqual(getattr(result, "succeeded_labels", ()), ("sens_left",))

    def test_matching_readback_does_not_supply_missing_receipt(self):
        expected = _empty_snapshot(polling_rate=PollingRate.HZ_1000)
        for attempted, succeeded in (((), ()), (("polling",), ()), ((), ("polling",))):
            with self.subTest(attempted=attempted, succeeded=succeeded):
                result = ApplyResult(total_attempted=1, succeeded=1)
                result.attempted_labels, result.succeeded_labels = attempted, succeeded
                outcome = build_field_outcomes(["polling_rate"], expected, expected, result, {})[0]
                self.assertFalse(outcome.write_succeeded)
                self.assertIsNone(outcome.verify_matched)

    def test_partial_collection_receipt_cannot_verify_collection(self):
        expected = _empty_snapshot(button_bindings={ButtonSlot.A: ButtonMapping(), ButtonSlot.B: ButtonMapping()})
        result = ApplyResult(total_attempted=2, succeeded=2)
        result.attempted_labels = ("binding_A", "binding_B")
        result.succeeded_labels = ("binding_A",)
        outcome = build_field_outcomes(["button_bindings"], expected, expected, result, {})[0]
        self.assertFalse(outcome.write_succeeded)
        self.assertIsNone(outcome.verify_matched)

    def test_abort_only_receipts_actual_setter_dispatch(self):
        service = Mock()
        service.set_polling_rate.return_value = _polling_result(SetPollingRateOutcome.WRITE_FAILED, error_code=1167)
        snapshot = _empty_snapshot(polling_rate=PollingRate.HZ_1000, button_bindings={ButtonSlot.A: ButtonMapping()})
        result = SettingsApplyCoordinator(service, field_trailer_delay_s=0).apply_snapshot(snapshot)
        service.set_button_binding.assert_not_called()
        self.assertEqual(getattr(result, "attempted_labels", ()), ("polling",))
        self.assertEqual(getattr(result, "succeeded_labels", ()), ())

    def test_retry_receipt_requires_retry_invocation_and_success(self):
        retry = Mock(return_value=_polling_result(SetPollingRateOutcome.OK))
        result = SettingsApplyCoordinator(Mock()).retry_failures([
            ApplyFailure("polling", "failed", True, retry_fn=retry),
            ApplyFailure("binding_A", "not retryable", False),
        ])
        retry.assert_called_once()
        self.assertEqual(getattr(result, "attempted_labels", ()), ("polling",))
        self.assertEqual(getattr(result, "succeeded_labels", ()), ("polling",))


class ConfirmationUiTests(unittest.TestCase):
    def test_preview_failure_disables_restore(self):
        rp = _rp()
        service = _FakeService(valid=[rp], preview_raises=ValueError("bad preview"))
        state = screen.RestorePointsScreenState(view=screen.VIEW_CONFIRM, selected_rp_id=rp.id)
        shell = _shell_with(service, screen_state=state)
        with _PatchedScreen(shell) as ui:
            ui.build()
        button = next(b for b in ui.buttons if b.get("tag") == screen.TAG_CONFIRM_RESTORE_BUTTON)
        self.assertFalse(button.get("enabled", True))
        screen._on_confirm_restore(shell)
        self.assertNotEqual(state.view, screen.VIEW_IN_PROGRESS)

    def test_paddle_only_preview_never_claims_no_op(self):
        rp = _rp()
        rp = replace(rp, snapshot=_empty_snapshot(back_paddle_bindings={MacroSlot.M2: BackPaddleBinding(ControllerButtonTarget.B)}),
                     coverage=replace(rp.coverage, fields={"back_paddle_bindings": FieldCoverage(CoverageState.CAPTURED, False, True, CoverageCategory.LAYOUT)}))
        service = _FakeService(valid=[rp])
        state = screen.RestorePointsScreenState(view=screen.VIEW_CONFIRM, selected_rp_id=rp.id)
        shell = _shell_with(service, screen_state=state)
        with _PatchedScreen(shell) as ui:
            ui.build()
        text = " ".join(ui.text_strings())
        self.assertNotIn("No changes detected", text)
        self.assertIn("M2: →B", text)
        self.assertIn("cannot read these bindings back", text)


class AttemptSidecarTests(unittest.TestCase):
    def test_corrupt_oversized_and_refused_sidecars_leave_source_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _, store = _make_service(tmpdir=tmp, settings_stub=_full_populated_stub())
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            result = service.restore(service.prepare_restore(rp.id))
            self.assertEqual(store.load(rp.id).last_restore_attempt.label, result.label)
            sidecar = store._attempt_path(rp.id)
            for contents in ("{", "[]", " " * (1024 * 1024 + 1), '{"source_sha256": "wrong"}'):
                sidecar.write_text(contents, "utf-8")
                self.assertIsNone(store.load(rp.id).last_restore_attempt)
                valid, skipped = store.list()
                self.assertIn(rp.id, {item.id for item in valid})
                self.assertFalse(skipped)
            from zd_app.storage import restore_point_store as module
            guarded = module.read_guarded_json
            def refuse_sidecar(path):
                if Path(path).suffix == ".attempt":
                    raise ValueError("reparse file refused")
                return guarded(path)
            with patch.object(module, "read_guarded_json", side_effect=refuse_sidecar):
                self.assertIsNone(store.load(rp.id).last_restore_attempt)
                self.assertFalse(store.list()[1])

    def test_delete_removes_only_its_own_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _, store = _make_service(tmpdir=tmp, settings_stub=_full_populated_stub())
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            service.restore(service.prepare_restore(rp.id))
            unrelated = Path(tmp) / "unrelated.attempt"
            unrelated.write_text("preserve", "utf-8")
            sidecar = store._attempt_path(rp.id)
            self.assertTrue(sidecar.exists())
            self.assertTrue(store.delete(rp.id))
            self.assertFalse(sidecar.exists())
            self.assertEqual(unrelated.read_text("utf-8"), "preserve")


class PreparedPayloadTests(unittest.TestCase):
    def test_exclusions_and_materialized_mutation_cannot_reintroduce_paddles(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            stub.back_paddles = {MacroSlot.M1: BackPaddleBinding(ControllerButtonTarget.A)}
            service, stub, store = _make_service(tmpdir=tmp, settings_stub=stub)
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            exclusions = {"back_paddle_bindings", "polling_rate"}
            prepared = service.prepare_restore(rp.id, excluded_fields=exclusions)
            exclusions.clear()
            prepared.materialize().snapshot.back_paddle_bindings.clear()
            self.assertEqual(prepared.write_only_paddle_slots, ())
            self.assertNotIn("polling_rate", {f.field_name for f in prepared.preview.fields})
            stub.write_calls.clear()
            service.restore(prepared)
            self.assertNotIn("polling", stub.write_calls)
            self.assertNotIn("back_paddle_M1", stub.write_calls)

    def test_disclosure_mismatch_is_rejected_before_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _full_populated_stub()
            stub.back_paddles = {MacroSlot.M1: BackPaddleBinding(ControllerButtonTarget.A)}
            service, stub, store = _make_service(tmpdir=tmp, settings_stub=stub)
            rp = service.capture(_basic_trigger(), device_identity=_identity())
            prepared = replace(service.prepare_restore(rp.id), write_only_paddle_slots=())
            with patch.object(service, "capture") as capture:
                with self.assertRaisesRegex(ValueError, "disclosure"):
                    service.restore(prepared, acknowledge_write_only_paddles=True)
                capture.assert_not_called()

    def test_admission_runner_refusal_has_no_writer_receipts(self):
        class RefusingService:
            def admit_write_operation(self):
                return object()

            def session_admission_is_current(self, token):
                return True

            def run_write_operation(self, token, callback):
                return _polling_result(SetPollingRateOutcome.DEVICE_NOT_FOUND)

            def set_polling_rate(self, rate):
                raise AssertionError("refused writer must not be called")

        coordinator = SettingsApplyCoordinator(RefusingService())
        result = coordinator.apply_snapshot(_empty_snapshot(polling_rate=PollingRate.HZ_1000))
        self.assertEqual(result.attempted_labels, ())
        self.assertEqual(result.succeeded_labels, ())
        retry = coordinator.retry_failures(result.failed)
        self.assertEqual(retry.attempted_labels, ())
        self.assertEqual(retry.succeeded_labels, ())
