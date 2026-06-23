"""AppShell wear-ledger wiring tests.

The session_start / session_end emit helpers and the profile-apply +
slider-write hook points are pure functions w.r.t. the wear-ledger
collaborator. These tests construct an AppShell via the shared
``make_shell`` helper, inject a real :class:`WearLedgerService` backed by a
tempdir, and assert the events appear on the underlying ``read_events()``.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from zd_app.services.wear_ledger import WearLedgerService

from tests.r2_shell_test_helpers import make_shell


class AppShellWearLedgerEmitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = WearLedgerService(base_dir=Path(self._tmp.name))
        self.shell = make_shell()
        # The helper builds AppShell with wear_ledger_service=None. Inject
        # the live ledger + reset the emit-flags so the helpers will run.
        self.shell.wear_ledger_service = self.ledger
        self.shell._session_start_emitted = False
        self.shell._session_end_emitted = False

    def test_emit_session_start_appends_event(self) -> None:
        self.shell._emit_session_start_event()
        events = self.ledger.read_events()
        starts = [e for e in events if e.event_type == "session_start"]
        self.assertEqual(len(starts), 1)
        self.assertIn("Wrapper session started", starts[0].summary)
        self.assertIn("app_version", starts[0].details)

    def test_emit_session_start_is_idempotent(self) -> None:
        self.shell._emit_session_start_event()
        self.shell._emit_session_start_event()
        self.shell._emit_session_start_event()
        starts = [e for e in self.ledger.read_events() if e.event_type == "session_start"]
        self.assertEqual(len(starts), 1)

    def test_emit_session_end_appends_event(self) -> None:
        self.shell._emit_session_end_event()
        ends = [e for e in self.ledger.read_events() if e.event_type == "session_end"]
        self.assertEqual(len(ends), 1)
        self.assertIn("Wrapper session ended", ends[0].summary)

    def test_emit_session_end_is_idempotent(self) -> None:
        self.shell._emit_session_end_event()
        self.shell._emit_session_end_event()
        ends = [e for e in self.ledger.read_events() if e.event_type == "session_end"]
        self.assertEqual(len(ends), 1)

    def test_record_wear_event_with_none_service_is_noop(self) -> None:
        self.shell.wear_ledger_service = None
        self.shell._record_wear_event(
            "session_start",
            summary="should be ignored",
            details={"k": "v"},
        )
        # No events written to the live ledger because the shell no longer
        # holds a reference. (The instance we have still works directly.)
        self.assertEqual(self.ledger.count_events(), 0)

    def test_record_wear_event_writes_through_to_ledger(self) -> None:
        self.shell._record_wear_event(
            "service_note",
            summary="hand-written note",
            details={"note": "test"},
        )
        events = self.ledger.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "service_note")
        self.assertEqual(events[0].details["note"], "test")


if __name__ == "__main__":  # pragma: no cover - manual driver
    unittest.main()
