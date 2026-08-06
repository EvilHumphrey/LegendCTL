"""Adversarial controller-identity and lifecycle continuity tests."""

from __future__ import annotations

import threading
import unittest

from tests.test_settings_service import (
    _FAKE_PATH,
    _HANDLE,
    _READ_WRITE_HANDLE,
    _Recorder,
    _make_service,
)
from zd_app.services.settings_service import (
    HID_FEATURE_REPORT_SIZE,
    LightingMode,
    LightingSettings,
    LightingZone,
    PollingRate,
    RgbColor,
    SetPollingRateOutcome,
    SettingsService,
    SettingsServiceError,
    StrandedReadTimeout,
    WriteOutcome,
)
from zd_app.services.write_verification import (
    CONTROLLER_IDENTITY_CHANGED_ERROR,
    fresh_read,
)


_SECOND_PATH = (
    r"\\?\hid#vid_413d&pid_2104&mi_02#7&second"
    r"#{4d1e55b2-f16f-11cf-88cb-001111000030}"
)
_SECOND_HANDLE = 0x7272


class TestIoAdmissionAndLeases(unittest.TestCase):
    def test_stale_write_admission_refuses_first_io_after_handle_reuse(self) -> None:
        rec = _Recorder(paths=[_FAKE_PATH], open_result=(_HANDLE, 0))
        service = _make_service(rec)
        entered = threading.Event()
        release = threading.Event()
        original = service._write_payload_with_retry

        def blocked(admission, payload, *, context):
            entered.set()
            if not release.wait(timeout=1.0):
                raise AssertionError("test failed to release admitted write")
            return original(admission, payload, context=context)

        service._write_payload_with_retry = blocked  # type: ignore[method-assign]
        results = []
        worker = threading.Thread(
            target=lambda: results.append(service.set_polling_rate(PollingRate.HZ_8000))
        )
        worker.start()
        self.addCleanup(worker.join, 1.0)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(timeout=1.0))

        service.stop()
        rec.paths = [_SECOND_PATH]
        rec.open_result = (_HANDLE, 0)  # raw value reused by the new session
        self.assertEqual(service.start(), SetPollingRateOutcome.OK)
        release.set()
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0].outcome, WriteOutcome.WRITE_FAILED)
        self.assertEqual([e for e in rec.events if e[0] == "write_file"], [])

    def test_active_lease_defers_close_and_reports_stale_success_as_failure(self) -> None:
        rec = _Recorder(paths=[_FAKE_PATH], open_result=(_HANDLE, 0))
        entered = threading.Event()
        release = threading.Event()

        def blocked_write(handle: int, payload: bytes):
            rec.events.append(("write_file", handle, payload))
            entered.set()
            if not release.wait(timeout=1.0):
                raise AssertionError("test failed to release WriteFile")
            return True, 0, len(payload)

        rec.write_file = blocked_write  # type: ignore[method-assign]
        service = _make_service(rec)
        results = []
        worker = threading.Thread(
            target=lambda: results.append(service.set_polling_rate(PollingRate.HZ_8000))
        )
        worker.start()
        self.addCleanup(worker.join, 1.0)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(timeout=1.0))

        service.stop()
        self.assertNotIn(("close_handle", _HANDLE), rec.events)
        rec.paths = [_SECOND_PATH]
        rec.open_result = (_HANDLE, 0)
        self.assertEqual(service.start(), SetPollingRateOutcome.OPEN_FAILED)

        release.set()
        worker.join(timeout=1.0)
        self.assertEqual(results[0].outcome, WriteOutcome.WRITE_FAILED)
        self.assertIn(("close_handle", _HANDLE), rec.events)
        self.assertEqual(service.start(), SetPollingRateOutcome.OK)
        self.assertEqual(len([e for e in rec.events if e[0] == "write_file"]), 1)

    def test_non_disconnect_failure_after_stop_never_retries_on_b(self) -> None:
        rec = _Recorder(paths=[_FAKE_PATH], open_result=(_HANDLE, 0))
        entered = threading.Event()
        release = threading.Event()

        def blocked_write(handle: int, payload: bytes):
            rec.events.append(("write_file", handle, payload))
            entered.set()
            if not release.wait(timeout=1.0):
                raise AssertionError("test failed to release WriteFile")
            return False, 5, 0

        rec.write_file = blocked_write  # type: ignore[method-assign]
        service = _make_service(rec)
        results = []
        worker = threading.Thread(
            target=lambda: results.append(service.set_polling_rate(PollingRate.HZ_8000))
        )
        worker.start()
        self.addCleanup(worker.join, 1.0)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(timeout=1.0))
        service.stop()
        rec.paths = [_SECOND_PATH]
        rec.open_result = (_SECOND_HANDLE, 0)
        release.set()
        worker.join(timeout=1.0)

        self.assertEqual(results[0].outcome, WriteOutcome.WRITE_FAILED)
        self.assertNotIn(("open_write_handle", _SECOND_PATH), rec.events)
        self.assertEqual(len([e for e in rec.events if e[0] == "write_file"]), 1)

    def test_stale_disconnect_completion_does_not_relatch_after_stop(self) -> None:
        rec = _Recorder(paths=[_FAKE_PATH], open_result=(_HANDLE, 0))
        entered = threading.Event()
        release = threading.Event()

        def blocked_write(handle: int, payload: bytes):
            rec.events.append(("write_file", handle, payload))
            entered.set()
            if not release.wait(timeout=1.0):
                raise AssertionError("test failed to release WriteFile")
            return False, 1167, 0

        rec.write_file = blocked_write  # type: ignore[method-assign]
        service = _make_service(rec)
        results = []
        worker = threading.Thread(
            target=lambda: results.append(service.set_polling_rate(PollingRate.HZ_8000))
        )
        worker.start()
        self.addCleanup(worker.join, 1.0)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(timeout=1.0))
        service.stop()
        rec.paths = [_SECOND_PATH]
        rec.open_result = (_SECOND_HANDLE, 0)
        release.set()
        worker.join(timeout=1.0)

        self.assertEqual(results[0].outcome, WriteOutcome.WRITE_FAILED)
        self.assertEqual(service.start(), SetPollingRateOutcome.OK)

    def test_stale_read_query_admission_sends_no_query_to_reused_handle(self) -> None:
        rec = _Recorder(paths=[_FAKE_PATH], open_read_write_result=(_READ_WRITE_HANDLE, 0))
        service = _make_service(rec)
        entered = threading.Event()
        release = threading.Event()
        original = service._write_payload

        def blocked(admission, payload, *, context="payload"):
            entered.set()
            if not release.wait(timeout=1.0):
                raise AssertionError("test failed to release read query")
            return original(admission, payload, context=context)

        service._write_payload = blocked  # type: ignore[method-assign]
        results = []
        worker = threading.Thread(target=lambda: results.append(service.get_polling_rate()))
        worker.start()
        self.addCleanup(worker.join, 1.0)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(timeout=1.0))

        service.stop()
        rec.paths = [_SECOND_PATH]
        rec.open_read_write_result = (_READ_WRITE_HANDLE, 0)
        self.assertEqual(service._ensure_read_write_handle(), _READ_WRITE_HANDLE)
        release.set()
        worker.join(timeout=1.0)

        self.assertEqual(results, [None])
        self.assertEqual([e for e in rec.events if e[0] == "write_file"], [])

    def test_stale_stranded_read_cannot_poison_reused_b_handle(self) -> None:
        stranded = StrandedReadTimeout(
            "stranded A",
            cancel_succeeded=False,
            cancel_error=1168,
            reader=threading.Thread(),
        )
        rec = _Recorder(read_results=[stranded])
        service = _make_service(rec)
        entered = threading.Event()
        release = threading.Event()
        original = service._poison_read_write_handle

        def blocked(admission, *, cancel_error):
            entered.set()
            if not release.wait(timeout=1.0):
                raise AssertionError("test failed to release poison")
            return original(admission, cancel_error=cancel_error)

        service._poison_read_write_handle = blocked  # type: ignore[method-assign]
        errors = []

        def read_a():
            try:
                service.get_polling_rate()
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=read_a)
        worker.start()
        self.addCleanup(worker.join, 1.0)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(timeout=1.0))
        service.stop()
        rec.paths = [_SECOND_PATH]
        rec.open_read_write_result = (_READ_WRITE_HANDLE, 0)
        self.assertEqual(service._ensure_read_write_handle(), _READ_WRITE_HANDLE)
        release.set()
        worker.join(timeout=1.0)

        self.assertEqual(len(errors), 1)
        self.assertEqual(service.target_path, _SECOND_PATH)
        self.assertEqual(service._read_write_handle, _READ_WRITE_HANDLE)

    def test_stale_read_disconnect_does_not_relatch_b(self) -> None:
        rec = _Recorder(paths=[_FAKE_PATH])
        entered = threading.Event()
        release = threading.Event()

        def blocked_read(handle: int, length: int, timeout_ms: int):
            rec.events.append(("read_file", handle, length, timeout_ms))
            entered.set()
            if not release.wait(timeout=1.0):
                raise AssertionError("test failed to release ReadFile")
            raise SettingsServiceError("A disconnected", win32_error=1167)

        rec.read_file = blocked_read  # type: ignore[method-assign]
        service = _make_service(rec)
        worker = threading.Thread(target=service.get_polling_rate)
        worker.start()
        self.addCleanup(worker.join, 1.0)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(timeout=1.0))
        service.stop()
        rec.paths = [_SECOND_PATH]
        rec.open_read_write_result = (_SECOND_HANDLE, 0)
        self.assertEqual(service._ensure_read_write_handle(), _SECOND_HANDLE)
        release.set()
        worker.join(timeout=1.0)

        self.assertEqual(service.target_path, _SECOND_PATH)
        self.assertEqual(service._read_write_handle, _SECOND_HANDLE)


class TestAtomicTargetCommit(unittest.TestCase):
    def test_write_first_rejects_concurrent_rw_target_b(self) -> None:
        rw_opened = threading.Event()
        release_rw = threading.Event()
        closed = []

        def enumerate_paths():
            return [_SECOND_PATH] if threading.current_thread().name == "rw-open" else [_FAKE_PATH]

        def open_rw(path):
            rw_opened.set()
            if not release_rw.wait(timeout=1.0):
                raise AssertionError("test failed to release RW opener")
            return _SECOND_HANDLE, 0

        service = SettingsService(
            enumerate_paths=enumerate_paths,
            open_write_handle=lambda path: (_HANDLE, 0),
            open_read_write_handle=open_rw,
            close_handle=lambda handle: closed.append(handle) or True,
        )
        rw_results = []
        worker = threading.Thread(
            target=lambda: rw_results.append(service._ensure_read_write_handle()),
            name="rw-open",
        )
        worker.start()
        self.addCleanup(worker.join, 1.0)
        self.addCleanup(release_rw.set)
        self.assertTrue(rw_opened.wait(timeout=1.0))
        self.assertEqual(service.start(), SetPollingRateOutcome.OK)
        release_rw.set()
        worker.join(timeout=1.0)

        self.assertEqual(rw_results, [None])
        self.assertEqual(service.target_path, _FAKE_PATH)
        self.assertEqual(service._write_handle, _HANDLE)
        self.assertIsNone(service._read_write_handle)
        self.assertEqual(closed, [_SECOND_HANDLE])

    def test_rw_first_rejects_concurrent_write_target_a(self) -> None:
        write_opened = threading.Event()
        release_write = threading.Event()
        closed = []

        def enumerate_paths():
            return [_FAKE_PATH] if threading.current_thread().name == "write-open" else [_SECOND_PATH]

        def open_write(path):
            write_opened.set()
            if not release_write.wait(timeout=1.0):
                raise AssertionError("test failed to release write opener")
            return _HANDLE, 0

        service = SettingsService(
            enumerate_paths=enumerate_paths,
            open_write_handle=open_write,
            open_read_write_handle=lambda path: (_SECOND_HANDLE, 0),
            close_handle=lambda handle: closed.append(handle) or True,
        )
        write_results = []
        worker = threading.Thread(
            target=lambda: write_results.append(service._ensure_handle()),
            name="write-open",
        )
        worker.start()
        self.addCleanup(worker.join, 1.0)
        self.addCleanup(release_write.set)
        self.assertTrue(write_opened.wait(timeout=1.0))
        self.assertEqual(service._ensure_read_write_handle(), _SECOND_HANDLE)
        release_write.set()
        worker.join(timeout=1.0)

        self.assertEqual(write_results[0].outcome, SetPollingRateOutcome.OPEN_FAILED)
        self.assertEqual(service.target_path, _SECOND_PATH)
        self.assertIsNone(service._write_handle)
        self.assertEqual(service._read_write_handle, _SECOND_HANDLE)
        self.assertEqual(closed, [_HANDLE])

    def test_stale_missing_target_opener_does_not_latch_same_a_winner(self) -> None:
        rec = _Recorder(paths=[_FAKE_PATH])
        service = _make_service(rec)
        self.assertEqual(service.get_polling_rate(), PollingRate.HZ_8000)
        stale_entered = threading.Event()
        release_stale = threading.Event()

        def enumerate_paths():
            if threading.current_thread().name == "stale-open":
                stale_entered.set()
                if not release_stale.wait(timeout=1.0):
                    raise AssertionError("test failed to release stale opener")
                return [_SECOND_PATH]
            return [_FAKE_PATH]

        service._enumerate_paths = enumerate_paths
        stale_results = []
        worker = threading.Thread(
            target=lambda: stale_results.append(service._ensure_handle()),
            name="stale-open",
        )
        worker.start()
        self.addCleanup(worker.join, 1.0)
        self.addCleanup(release_stale.set)
        self.assertTrue(stale_entered.wait(timeout=1.0))
        self.assertEqual(service.start(), SetPollingRateOutcome.OK)
        release_stale.set()
        worker.join(timeout=1.0)

        self.assertEqual(stale_results[0].outcome, SetPollingRateOutcome.DEVICE_NOT_FOUND)
        self.assertEqual(service.target_path, _FAKE_PATH)
        self.assertEqual(service._write_handle, _HANDLE)
        self.assertFalse(service._identity_change_latched)


class TestMultiCallContinuity(unittest.TestCase):
    def _transition_on_settle(self, service, rec, replacement_path):
        transitioned = False

        def settle(_seconds):
            nonlocal transitioned
            if transitioned:
                return
            transitioned = True
            service.stop()
            rec.paths = [replacement_path]
            rec.open_result = (_SECOND_HANDLE, 0)
            service.start()

        service._sleep = settle

    def test_step_verify_never_accepts_or_rewrites_replacement(self) -> None:
        for replacement_path in (_SECOND_PATH, _FAKE_PATH):
            with self.subTest(replacement_path=replacement_path):
                rec = _Recorder(paths=[_FAKE_PATH])
                service = _make_service(rec)
                self._transition_on_settle(service, rec, replacement_path)
                result = service.set_step_size_verified(73, attempts=3, settle_s=0.1)
                self.assertEqual(result.outcome, WriteOutcome.OK)
                self.assertTrue(result.verify_inconclusive)
                self.assertEqual(len([e for e in rec.events if e[0] == "write_file"]), 1)

    def test_lighting_verify_never_accepts_or_rewrites_replacement(self) -> None:
        settings = LightingSettings(
            light_on=True,
            mode=LightingMode.ALWAYS_ON,
            brightness_byte=50,
            color=RgbColor(10, 20, 30),
        )
        for replacement_path in (_SECOND_PATH, _FAKE_PATH):
            with self.subTest(replacement_path=replacement_path):
                rec = _Recorder(paths=[_FAKE_PATH])
                service = _make_service(rec)
                self._transition_on_settle(service, rec, replacement_path)
                result = service.set_zone_lighting_verified(
                    LightingZone.HOME,
                    settings,
                    attempts=3,
                    settle_s=0.1,
                )
                self.assertEqual(result.outcome, WriteOutcome.OK)
                self.assertTrue(result.verify_inconclusive)
                self.assertEqual(len([e for e in rec.events if e[0] == "write_file"]), 1)

    def test_stale_capability_probe_never_publishes_into_b(self) -> None:
        rec = _Recorder(paths=[_FAKE_PATH])
        service = _make_service(rec)
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def probe(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                if not release.wait(timeout=1.0):
                    raise AssertionError("test failed to release capability probe")
                return object()
            return None

        service.get_sensitivity_curve_8point = probe  # type: ignore[method-assign]
        results = []
        worker = threading.Thread(target=lambda: results.append(service.supports_8point_sensitivity()))
        worker.start()
        self.addCleanup(worker.join, 1.0)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(timeout=1.0))
        service.stop()
        rec.paths = [_SECOND_PATH]
        rec.open_result = (_SECOND_HANDLE, 0)
        rec.open_read_write_result = (_SECOND_HANDLE + 1, 0)
        self.assertEqual(service.start(), SetPollingRateOutcome.OK)
        release.set()
        worker.join(timeout=1.0)

        self.assertEqual(results, [False])
        self.assertIsNone(service._supports_8point)
        self.assertFalse(service.supports_8point_sensitivity())
        self.assertEqual(calls, 3)

    def test_get_all_settings_discards_transitioned_batch(self) -> None:
        rec = _Recorder(paths=[_FAKE_PATH])
        service = _make_service(rec)

        def polling_from_a_then_transition():
            service.stop()
            rec.paths = [_SECOND_PATH]
            rec.open_result = (_SECOND_HANDLE, 0)
            service.start()
            return PollingRate.HZ_4000

        service.get_polling_rate = polling_from_a_then_transition  # type: ignore[method-assign]
        snapshot = service.get_all_settings()
        self.assertIsNone(snapshot.polling_rate)
        self.assertIsNone(snapshot.vibration)
        self.assertEqual(snapshot.button_bindings, {})

    def test_fresh_read_discards_transitioned_batch_and_provenance(self) -> None:
        rec = _Recorder(paths=[_FAKE_PATH])
        service = _make_service(rec)

        def polling_from_a_then_transition():
            service.stop()
            rec.paths = [_SECOND_PATH]
            rec.open_result = (_SECOND_HANDLE, 0)
            service.start()
            return PollingRate.HZ_4000

        service.get_polling_rate = polling_from_a_then_transition  # type: ignore[method-assign]
        snapshot, success, errors = fresh_read(service)
        self.assertIsNone(snapshot.polling_rate)
        self.assertFalse(success["polling_rate"])
        self.assertEqual(errors["polling_rate"], CONTROLLER_IDENTITY_CHANGED_ERROR)

    def test_button_retry_never_switches_to_b(self) -> None:
        rec = _Recorder(paths=[_FAKE_PATH])
        service = _make_service(rec)
        calls = 0

        def failed_a_read(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            service.stop()
            rec.paths = [_SECOND_PATH]
            rec.open_result = (_SECOND_HANDLE, 0)
            service.start()
            raise SettingsServiceError("A disappeared", win32_error=1167)

        service._read_response = failed_a_read  # type: ignore[method-assign]
        from zd_app.services.settings_service import ButtonSlot

        self.assertIsNone(service.get_button_binding(ButtonSlot.A))
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
