"""Regression coverage for the public standalone hidden-write boundary."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from zd_app.services.hidden_write_policy import HIDDEN_WRITE_DISABLED_MESSAGE
from zd_app.services.standalone_trigger_service import (
    EnsureDualModeStatus,
    HidKeepaliveIo,
    KeepaliveLoop,
    StandaloneStartStatus,
    StandaloneTriggerService,
    TriggerOutcome,
    _default_write_hid,
    ensure_dual_mode,
    fire_trigger_sequence,
)


ROOT = Path(__file__).resolve().parents[1]


class HiddenWriteContainmentTests(unittest.TestCase):
    def test_direct_trigger_and_retry_wrapper_send_no_packets(self):
        enumerate_paths = MagicMock(side_effect=AssertionError("enumeration must not run"))
        device_io = MagicMock(side_effect=AssertionError("IOCTL must not run"))

        fired = fire_trigger_sequence(
            enumerate_xusb_paths=enumerate_paths,
            device_io_control=device_io,
        )

        self.assertEqual(fired.outcome, TriggerOutcome.DISABLED)
        self.assertEqual(fired.error_message, HIDDEN_WRITE_DISABLED_MESSAGE)
        enumerate_paths.assert_not_called()
        device_io.assert_not_called()

        injected_fire = MagicMock(side_effect=AssertionError("trigger must not run"))
        ensured = ensure_dual_mode(fire_trigger=injected_fire)
        self.assertEqual(ensured.status, EnsureDualModeStatus.DISABLED)
        self.assertEqual(ensured.error_message, HIDDEN_WRITE_DISABLED_MESSAGE)
        injected_fire.assert_not_called()

    def test_keepalive_entry_direct_cycle_and_final_helper_refuse(self):
        writer = MagicMock(side_effect=AssertionError("hidden writer must not run"))
        loop = KeepaliveLoop(io=HidKeepaliveIo(write=writer), sleep=lambda _seconds: None)

        loop.start(11, 22)
        loop._run_one_cycle()

        writer.assert_not_called()
        snapshot = loop.health_snapshot()
        self.assertFalse(snapshot.is_running)
        self.assertEqual(snapshot.cycle_count, 0)
        self.assertEqual(snapshot.last_error, HIDDEN_WRITE_DISABLED_MESSAGE)
        loop.stop()
        loop.stop()
        self.assertFalse(loop.health_snapshot().is_running)

        with patch(
            "zd_app.services.standalone_trigger_service._Win32.kernel32",
            side_effect=AssertionError("WriteFile binding must not be reached"),
        ):
            self.assertEqual(_default_write_hid(123, b"x" * 64), (False, 0, 0))

    def test_service_start_refuses_before_trigger_open_or_thread(self):
        ensure = MagicMock(side_effect=AssertionError("trigger must not run"))
        open_session = MagicMock(side_effect=AssertionError("handles must not open"))
        loop_factory = MagicMock(side_effect=AssertionError("thread must not start"))
        service = StandaloneTriggerService(
            ensure_dual_mode_fn=ensure,
            open_hid_session_fn=open_session,
            keepalive_loop_factory=loop_factory,
            enable_singleton_mutex=False,
        )

        result = service.start()

        self.assertEqual(result.status, StandaloneStartStatus.HIDDEN_WRITES_DISABLED)
        self.assertEqual(result.error_message, HIDDEN_WRITE_DISABLED_MESSAGE)
        ensure.assert_not_called()
        open_session.assert_not_called()
        loop_factory.assert_not_called()
        service.stop()
        service.stop()
        snapshot = service.health_snapshot()
        self.assertFalse(snapshot.is_started)
        self.assertFalse(snapshot.is_keepalive_running)

    def test_standalone_module_has_no_writefile_call_or_binding(self):
        relative = "zd_app/services/standalone_trigger_service.py"
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else (
                    node.func.attr if isinstance(node.func, ast.Attribute) else None
                )
                self.assertNotEqual(name, "WriteFile")
            if isinstance(node, ast.Assign):
                self.assertFalse(
                    any(isinstance(target, ast.Name) and target.id == "WriteFile" for target in node.targets)
                )

        settings_source = (ROOT / "zd_app/services/settings_service.py").read_text(encoding="utf-8")
        self.assertIn("kernel32.WriteFile(", settings_source)


if __name__ == "__main__":
    unittest.main()
