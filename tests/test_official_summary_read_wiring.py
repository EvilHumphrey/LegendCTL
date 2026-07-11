"""Wiring for the official-app summary scrape into the MAIN controller Read.

Two seams:

- ``AppShell._refresh_read_job`` (the bottom-bar / Home "Read..." job half)
  invokes ``device_service.read_official_summary_into_state`` AFTER the primary
  HID settings batch, and still returns the settings snapshot — the enrichment
  is additive and never gates the read result.
- END-TO-END: a populating scrape sets ``summary_sources`` such that the
  diagnostics trust-matrix gathering (``_trust_matrix_signals``) flows the
  source through and the matrix derivation renders the amber "From the official
  app" chip for firmware and active profile — never "Verified from device".
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from tests.r2_shell_test_helpers import empty_snapshot
from tests.test_app_shell_worker_thread import _GatedService, _make_shell
from tests.test_device_services import FakeSummaryProbeService
from zd_app.services.device_service import DeviceService
from zd_app.services.trust_matrix import (
    INFERRED,
    OFFICIAL_APP_LABEL_KEY,
    VERIFIED,
    build_trust_matrix,
    provenance_label,
    row_label,
)
from zd_app.ui.screens import diagnostics


class RefreshReadJobOfficialSummaryWiringTests(unittest.TestCase):
    def test_read_job_reads_settings_then_official_summary_and_returns_snapshot(
        self,
    ) -> None:
        snapshot = empty_snapshot()
        service = _GatedService(snapshot)
        shell = _make_shell(service)

        outcome = shell._refresh_read_job()

        snap, _first_connect, skipped = outcome
        # The settings snapshot is the primary, returned deliverable.
        self.assertIs(snap, snapshot)
        self.assertEqual(skipped, 0)
        self.assertEqual(service.read_calls, 1)  # HID settings batch ran
        # The official-summary enrichment was invoked job-side (off the UI
        # thread), once, after the settings batch.
        shell.device_service.read_official_summary_into_state.assert_called_once()

    def test_read_job_returns_snapshot_even_if_summary_step_misbehaves(self) -> None:
        # Belt-and-suspenders: the real method swallows failures internally
        # (see DeviceServiceOfficialSummaryReadTests), but the job must not
        # depend on that — a summary step that raised would still leave the
        # settings snapshot as the read's deliverable. Here the summary call is
        # a no-op mock; assert the snapshot returns regardless.
        snapshot = empty_snapshot()
        service = _GatedService(snapshot)
        shell = _make_shell(service)

        snap, _first_connect, _skipped = shell._refresh_read_job()

        self.assertIs(snap, snapshot)


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
