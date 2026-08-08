"""Wiring for the official-app summary scrape into the MAIN controller Read.

Two seams:

- ``AppShell._refresh_read_job`` (the bottom-bar / Home "Read..." job half)
  probes the official-app summary AFTER the primary HID settings batch, then
  the token-checked completion folds it into DeviceState — the enrichment is
  additive and never gates the read result or crosses controller identities.
- END-TO-END: a populating scrape sets ``summary_sources`` such that the
  diagnostics trust-matrix gathering (``_trust_matrix_signals``) flows the
  source through and the matrix derivation renders the amber "From the official
  app" chip for firmware and active profile — never "Verified from device".
"""

from __future__ import annotations

import unittest
import threading
from types import SimpleNamespace

from tests.r2_shell_test_helpers import empty_snapshot
from tests.test_app_shell_worker_thread import (
    _GATE_TIMEOUT_S,
    _StoppableGatedService,
    _capture_widget_state,
    _drain_queued_completions,
    _make_shell,
    _set_connected_identity,
)
from tests.test_device_services import FakeSummaryProbeService
from zd_app.services.device_service import DeviceService
from zd_app.services.official_app_summary_service import OfficialAppSummary
from zd_app.services.trust_matrix import (
    INFERRED,
    OFFICIAL_APP_LABEL_KEY,
    VERIFIED,
    build_trust_matrix,
    provenance_label,
    row_label,
)
from zd_app.ui.screens import diagnostics
from zd_app.ui.app_shell import threaded_hid_executor


class RefreshReadJobOfficialSummaryWiringTests(unittest.TestCase):
    def test_read_job_reads_settings_then_official_summary_and_returns_snapshot(
        self,
    ) -> None:
        snapshot = empty_snapshot()
        service = _StoppableGatedService(snapshot)
        shell = _make_shell(service)
        summary = OfficialAppSummary(firmware_version="1.18")
        shell.device_service.official_app_summary_service.read_summary.return_value = (
            summary
        )

        outcome = shell._refresh_read_job()

        snap, _first_connect, skipped = outcome
        # The settings snapshot is the primary, returned deliverable.
        self.assertIs(snap, snapshot)
        self.assertEqual(skipped, 0)
        self.assertEqual(service.read_calls, 1)  # HID settings batch ran
        # The probe was invoked job-side after the settings batch, but the
        # mutable DeviceState fold waits for token-checked on_done.
        shell.device_service.official_app_summary_service.read_summary.assert_called_once_with(
            force_refresh=True
        )
        shell.device_service._apply_official_app_summary.assert_not_called()

        shell.device_service._apply_official_app_summary.return_value = False
        _capture_widget_state(
            lambda: shell._refresh_read_on_done(outcome, include_device=True)
        )
        shell.device_service._apply_official_app_summary.assert_called_once_with(summary)

    def test_read_job_returns_snapshot_even_if_summary_step_misbehaves(self) -> None:
        # Belt-and-suspenders: the real method swallows failures internally
        # (see DeviceServiceOfficialSummaryReadTests), but the job must not
        # depend on that — a summary step that raised would still leave the
        # settings snapshot as the read's deliverable. Here the summary call is
        # a no-op mock; assert the snapshot returns regardless.
        snapshot = empty_snapshot()
        service = _StoppableGatedService(snapshot)
        shell = _make_shell(service)

        snap, _first_connect, _skipped = shell._refresh_read_job()

        self.assertIs(snap, snapshot)

    def test_unit_a_probe_result_does_not_mutate_unit_b_state(self) -> None:
        class GatedProbe:
            def __init__(self) -> None:
                self.started = threading.Event()
                self.release = threading.Event()

            def read_summary(self, *, force_refresh=False):
                self.started.set()
                if not self.release.wait(timeout=_GATE_TIMEOUT_S):
                    raise AssertionError("summary probe gate never released")
                return OfficialAppSummary(firmware_version="A-firmware")

        service = _StoppableGatedService(empty_snapshot())
        shell = _make_shell(service, hid_executor=threaded_hid_executor)
        shell._dpg_context_ready = True
        _set_connected_identity(shell, "zd-unit-a")
        probe = GatedProbe()
        shell.device_service.official_app_summary_service = probe

        _capture_widget_state(shell.refresh_from_controller)
        self.assertTrue(probe.started.wait(_GATE_TIMEOUT_S))
        shell.device_service.state.stable_identifier = "zd-unit-b"
        shell.device_service.state.firmware_version = "B-existing"
        shell._observe_controller_presence()
        shell._invalidate_cached_controller_settings()
        shell._restart_settings_service_for_current_presence()
        probe.release.set()

        _drain_queued_completions(shell, 1)
        _capture_widget_state(shell._drain_hid_job_completions)

        self.assertEqual(shell.device_service.state.firmware_version, "B-existing")
        shell.device_service._apply_official_app_summary.assert_not_called()


class OfficialSummaryReadTrustMatrixEndToEndTests(unittest.TestCase):
    def _connected_service_after_scrape(self, names: list[str]) -> DeviceService:
        service = DeviceService(clock=lambda: 0.0)
        service.state.connection_state = "connected"
        service.state.stable_identifier = "zd-unit-1"
        service.official_app_summary_service = FakeSummaryProbeService(
            [{"window_title": "Controller Settings", "names": names}],
            clock=lambda: 0.0,
        )
        applied = service.read_official_summary_into_state()
        self.assertTrue(applied.applied)
        return service

    def _matrix_rows(self, service: DeviceService) -> dict:
        shell = SimpleNamespace(
            device_service=service,
            last_controller_snapshot=None,
            last_snapshot_ts=None,
            last_snapshot_identity=None,
            settings_service=None,
        )
        signals = diagnostics._trust_matrix_signals(shell)
        return {row.key: row for row in build_trust_matrix(signals)}

    def test_populating_read_yields_official_app_chip_for_firmware_and_profile(
        self,
    ) -> None:
        service = self._connected_service_after_scrape(
            ["Version: 1.18", "Config: 2"]
        )

        rows = self._matrix_rows(service)

        for key in ("firmware", "profile"):
            row = rows[key]
            # Amber (INFERRED color), NOT verified-from-device.
            self.assertEqual(row.provenance, INFERRED, key)
            self.assertEqual(row.label_key, OFFICIAL_APP_LABEL_KEY, key)
            self.assertEqual(row_label(row), "From the official app", key)
            self.assertNotEqual(
                row_label(row), provenance_label(VERIFIED), key
            )

    def test_official_app_value_degrades_to_retained_inferred_on_disconnect(
        self,
    ) -> None:
        # A scraped value must not read as "From the official app" (a live claim)
        # once the controller disconnects — it degrades to the retained
        # "(last read)" inferred class, exactly as the matrix already handles.
        service = self._connected_service_after_scrape(
            ["Version: 1.18", "Config: 2"]
        )
        service.state.connection_state = "no_device"
        service.state.data_freshness = "stale"

        rows = self._matrix_rows(service)

        for key in ("firmware", "profile"):
            row = rows[key]
            self.assertEqual(row.provenance, INFERRED, key)
            # No live official-app override while disconnected.
            self.assertIsNone(row.label_key, key)
            self.assertNotEqual(
                row_label(row), provenance_label(VERIFIED), key
            )


if __name__ == "__main__":
    unittest.main()
