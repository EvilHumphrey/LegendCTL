"""Apply-pipeline tests for the Last-Applied record (Device-vs-Profile Phase 2).

Pins the recording contract end to end through the real shell pipeline (sync
mode — ``hid_executor=None`` runs each job inline):

- the named-profile apply writes a record (right name / include_device /
  failed-label list / as-applied snapshot / fresh UTC timestamp);
- Safe Import's Save + Apply writes a record under the saved profile name;
- a storage failure NEVER affects the apply result (best-effort contract);
- a successful retry removes exactly the recovered labels from the stored
  record and leaves everything else (timestamp included) alone;
- per-field writes and shells without a store wired never record.

Harness shape follows tests/test_app_shell_hid_busy_guard.py (whose
``_RecordingService`` / ``_capture_widget_state`` are imported — same
precedent as that file importing the restore-points screen helpers).
Timestamp assertions are now-relative (time-rot rule).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.r2_shell_test_helpers import empty_snapshot
from tests.test_app_shell_hid_busy_guard import (
    _OK_OUTCOME,
    _RecordingService,
    _capture_widget_state,
)
from zd_app.models import AppSettings, DeviceState
from zd_app.services.settings_apply_coordinator import ApplyFailure
from zd_app.services.settings_service import (
    PollingRate,
    TriggerVibrationMode,
    VibrationSettings,
)
from zd_app.storage.last_applied_store import (
    LastAppliedRecord,
    LastAppliedStore,
    utc_now_iso_z,
)
from zd_app.ui.app_shell import AppShell
from zd_app.ui.screens import safe_import as safe_import_screen


def _make_shell(settings_service=None, *, last_applied_store=None) -> AppShell:
    """Sync-mode shell (mirrors the busy-guard helper + the Phase-2 store)."""

    settings_store = MagicMock()
    settings_store.load.return_value = AppSettings()
    device_service = MagicMock()
    device_service.state = DeviceState()
    device_service.recent_events.return_value = []
    device_service.summary_source_summary.return_value = "Not verified"
    device_service.last_read_duration_ms = None
    device_service.last_write_duration_ms = None
    profile_service = MagicMock()
    profile_service.pending_changes_count.return_value = 0
    wrapper_profile_store = MagicMock()
    wrapper_profile_store.list_profiles.return_value = []
    shell = AppShell(
        device_service=device_service,
        profile_service=profile_service,
        diagnostics_service=MagicMock(),
        settings_store=settings_store,
        preflight_service=MagicMock(),
        settings_service=settings_service,
        wrapper_profile_store=wrapper_profile_store,
        restore_point_service=None,
        last_applied_store=last_applied_store,
    )
    # Same suppression as the busy-guard helper: no default-built RP service,
    # so apply paths never fresh-read or write RPs on disk.
    shell.restore_point_service = None
    shell.refresh_shell = lambda: None
    shell.rebuild_current_screen = lambda: None
    return shell


class _FailingVibrationService(_RecordingService):
    """Every setter ACKs except vibration, which reports write_failed."""

    def set_vibration(self, settings):
        self.write_calls["set_vibration"] += 1
        return SimpleNamespace(
            outcome=SimpleNamespace(name="WRITE_FAILED", value="write_failed"),
            error_code=None,
        )


class _ExplodingStore:
    """A store whose save always raises — the best-effort contract's foil."""

    def __init__(self) -> None:
        self.save_attempts = 0

    def save(self, record) -> None:
        self.save_attempts += 1
        raise OSError("disk full")

    def load(self):
        return None


def _assert_fresh_utc(testcase: unittest.TestCase, stamp: str) -> None:
    parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    age = datetime.now(timezone.utc) - parsed
    testcase.assertGreaterEqual(age, timedelta(seconds=-5))
    testcase.assertLess(age, timedelta(minutes=2))


_APPLY_SNAPSHOT = empty_snapshot(
    polling_rate=PollingRate.HZ_8000,
    step_size=146,
    vibration=VibrationSettings(10, 20, 30, 40, TriggerVibrationMode.NATIVE),
)


class NamedApplyRecordingTests(unittest.TestCase):
    def _apply(self, shell, name="race day", include_device=True) -> None:
        with patch("zd_app.ui.app_shell.time.sleep"):
            _capture_widget_state(
                lambda: shell._apply_wrapper_profile_snapshot(
                    name, _APPLY_SNAPSHOT, include_device=include_device
                )
            )

    def test_apply_writes_record_with_name_and_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            shell = _make_shell(
                _RecordingService(empty_snapshot()), last_applied_store=store
            )
            self._apply(shell)
            record = store.load()
        self.assertIsNotNone(record)
        self.assertEqual(record.profile_name, "race day")
        self.assertTrue(record.include_device)
        self.assertEqual(record.failed_fields, ())
        # The record holds exactly what the coordinator received, codec
        # round-tripped losslessly.
        self.assertEqual(record.snapshot, _APPLY_SNAPSHOT)
        _assert_fresh_utc(self, record.applied_at)
        # The apply itself succeeded as usual.
        recorded = shell.device_service.record_apply_result.call_args
        self.assertIs(recorded.args[0], True)

    def test_apply_records_include_device_false(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            shell = _make_shell(
                _RecordingService(empty_snapshot()), last_applied_store=store
            )
            self._apply(shell, include_device=False)
            record = store.load()
        self.assertFalse(record.include_device)

    def test_apply_records_failed_setting_labels(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            shell = _make_shell(
                _FailingVibrationService(empty_snapshot()), last_applied_store=store
            )
            self._apply(shell)
            record = store.load()
        self.assertEqual(record.failed_fields, ("vibration",))
        # The partial-failure banner still fired exactly as before.
        recorded = shell.device_service.record_apply_result.call_args
        self.assertIs(recorded.args[0], False)

    def test_storage_failure_never_breaks_the_apply(self) -> None:
        store = _ExplodingStore()
        shell = _make_shell(
            _RecordingService(empty_snapshot()), last_applied_store=store
        )
        with self.assertLogs("zd_app.ui.app_shell", level="ERROR") as logs:
            self._apply(shell)
        self.assertEqual(store.save_attempts, 1)
        self.assertTrue(any("Last-applied" in line for line in logs.output))
        # The apply result reported success exactly as without the store.
        recorded = shell.device_service.record_apply_result.call_args
        self.assertIs(recorded.args[0], True)

    def test_no_store_wired_applies_clean_and_records_nothing(self) -> None:
        shell = _make_shell(_RecordingService(empty_snapshot()))
        self._apply(shell)  # must not raise
        recorded = shell.device_service.record_apply_result.call_args
        self.assertIs(recorded.args[0], True)

    def test_no_service_sentinel_records_nothing(self) -> None:
        # No settings service → the coordinator attempts nothing and returns
        # its sentinel failure. There was no apply, so nothing may be
        # recorded (a record would overclaim what reached the device).
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            shell = _make_shell(None, last_applied_store=store)
            self._apply(shell)
            self.assertIsNone(store.load())

    def test_per_field_write_does_not_record(self) -> None:
        # Manual per-field writes are deliberately outside the record's claim.
        # (apply_polling_rate writes immediately — no slider throttle.)
        from zd_app.ui.app_shell import POLLING_RATE_BY_LABEL

        polling_label = next(iter(POLLING_RATE_BY_LABEL))
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            service = _RecordingService(empty_snapshot())
            shell = _make_shell(service, last_applied_store=store)
            shell._polling_rate_hydrated = True  # past the stale-combo guard
            _capture_widget_state(lambda: shell.apply_polling_rate(polling_label))
            self.assertEqual(service.write_calls["set_polling_rate"], 1)
            self.assertIsNone(store.load())


class SafeImportRecordingTests(unittest.TestCase):
    def test_save_apply_records_under_saved_name(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            shell = _make_shell(
                _RecordingService(empty_snapshot()), last_applied_store=store
            )
            shell._safe_import_result = SimpleNamespace(
                profile=SimpleNamespace(snapshot=empty_snapshot(step_size=99)),
                generated_name="Imported Profile",
                audit=SimpleNamespace(),
                categories={},
            )
            shell._safe_import_selected = set()
            failure = SimpleNamespace(setting_label="deadzones", error="write failed")
            shell._apply_snapshot_to_controller = MagicMock(
                return_value=SimpleNamespace(failed=[failure], total_attempted=1)
            )
            with patch(
                "zd_app.ui.app_shell.dpg.get_value",
                side_effect={
                    safe_import_screen.NAME_INPUT: "Imported Profile"
                }.__getitem__,
            ), patch("zd_app.ui.app_shell.safe_import.open_result"), patch(
                "zd_app.ui.app_shell.time.sleep"
            ), patch(
                "zd_app.ui.app_shell.dpg.delete_item"
            ):
                _capture_widget_state(
                    lambda: shell.safe_import_apply(apply_to_controller=True)
                )
            record = store.load()
        self.assertIsNotNone(record)
        self.assertEqual(record.profile_name, "Imported Profile")
        self.assertEqual(record.failed_fields, ("deadzones",))
        # filtered_snapshot() with an empty category selection carries no
        # device-global fields → include_device derives to False.
        self.assertFalse(record.include_device)
        _assert_fresh_utc(self, record.applied_at)

    def test_store_only_save_records_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            shell = _make_shell(
                _RecordingService(empty_snapshot()), last_applied_store=store
            )
            shell._safe_import_result = SimpleNamespace(
                profile=SimpleNamespace(snapshot=empty_snapshot()),
                generated_name="Imported Profile",
                audit=SimpleNamespace(),
                categories={},
            )
            shell._safe_import_selected = set()
            shell._apply_snapshot_to_controller = MagicMock()
            with patch(
                "zd_app.ui.app_shell.dpg.get_value",
                side_effect={
                    safe_import_screen.NAME_INPUT: "Imported Profile"
                }.__getitem__,
            ), patch("zd_app.ui.app_shell.safe_import.open_result"):
                _capture_widget_state(
                    lambda: shell.safe_import_apply(apply_to_controller=False)
                )
            self.assertIsNone(store.load())
        shell._apply_snapshot_to_controller.assert_not_called()


class RetryRecordUpdateTests(unittest.TestCase):
    def _seed(self, store: LastAppliedStore, failed=("vibration", "deadzones")):
        seeded = LastAppliedRecord(
            profile_name="race day",
            applied_at=utc_now_iso_z(),
            include_device=True,
            failed_fields=tuple(failed),
            snapshot=_APPLY_SNAPSHOT,
        )
        store.save(seeded)
        return seeded

    @staticmethod
    def _failures():
        ok = SimpleNamespace(outcome=_OK_OUTCOME, error_code=None)
        bad = SimpleNamespace(
            outcome=SimpleNamespace(name="WRITE_FAILED", value="write_failed"),
            error_code=None,
        )
        return [
            ApplyFailure(
                setting_label="vibration",
                error="write failed",
                is_transient=True,
                retry_fn=lambda: ok,
            ),
            ApplyFailure(
                setting_label="deadzones",
                error="write failed",
                is_transient=True,
                retry_fn=lambda: bad,
            ),
        ]

    def test_retry_removes_only_recovered_labels(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            seeded = self._seed(store)
            shell = _make_shell(
                _RecordingService(empty_snapshot()), last_applied_store=store
            )
            with patch("zd_app.ui.app_shell.time.sleep"):
                _capture_widget_state(
                    lambda: shell._retry_failed_settings(self._failures())
                )
            record = store.load()
        # vibration recovered → removed; deadzones still failing → kept.
        self.assertEqual(record.failed_fields, ("deadzones",))
        # The retry amends the SAME apply: name / timestamp / snapshot stay.
        self.assertEqual(record.profile_name, seeded.profile_name)
        self.assertEqual(record.applied_at, seeded.applied_at)
        self.assertEqual(record.snapshot, seeded.snapshot)

    def test_fully_recovered_retry_clears_failed_fields(self) -> None:
        ok = SimpleNamespace(outcome=_OK_OUTCOME, error_code=None)
        failures = [
            ApplyFailure(
                setting_label="vibration",
                error="write failed",
                is_transient=True,
                retry_fn=lambda: ok,
            )
        ]
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            self._seed(store, failed=("vibration",))
            shell = _make_shell(
                _RecordingService(empty_snapshot()), last_applied_store=store
            )
            with patch("zd_app.ui.app_shell.time.sleep"):
                _capture_widget_state(lambda: shell._retry_failed_settings(failures))
            record = store.load()
        self.assertEqual(record.failed_fields, ())

    def test_retry_without_record_is_a_quiet_no_op(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LastAppliedStore(base_dir=tmp)
            shell = _make_shell(
                _RecordingService(empty_snapshot()), last_applied_store=store
            )
            with patch("zd_app.ui.app_shell.time.sleep"):
                _capture_widget_state(
                    lambda: shell._retry_failed_settings(self._failures())
                )
            self.assertIsNone(store.load())  # nothing invented

    def test_retry_storage_failure_never_breaks_the_retry(self) -> None:
        class _ExplodingLoadStore(_ExplodingStore):
            def load(self):
                raise OSError("unreadable")

        store = _ExplodingLoadStore()
        shell = _make_shell(
            _RecordingService(empty_snapshot()), last_applied_store=store
        )
        with self.assertLogs("zd_app.ui.app_shell", level="ERROR") as logs:
            with patch("zd_app.ui.app_shell.time.sleep"):
                _capture_widget_state(
                    lambda: shell._retry_failed_settings(self._failures())
                )
        self.assertTrue(any("retry update failed" in line for line in logs.output))
        # The retry outcome still landed (partial: deadzones failed again).
        recorded = shell.device_service.record_apply_result.call_args
        self.assertIs(recorded.args[0], False)


if __name__ == "__main__":  # pragma: no cover - manual driver
    unittest.main()
