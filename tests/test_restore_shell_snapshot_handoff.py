"""Restore readback replaces the shell cache without a second device read."""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.r2_shell_test_helpers import empty_snapshot, make_shell
from zd_app.services.settings_service import (
    BackPaddleBinding,
    MacroSlot,
    PollingRate,
)
from zd_app.ui.app_shell import AppShell, threaded_hid_executor
from zd_app.ui.screens import restore_points


class RestoreShellSnapshotHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        # Exercise real shell hydration/persistence with no native DPG calls,
        # filesystem writes, device service calls, or official-app probes.
        for name in ("does_item_exist", "set_value", "configure_item", "delete_item"):
            replacement = patch(f"dearpygui.dearpygui.{name}", return_value=False)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.old = empty_snapshot(step_size=10, polling_rate=PollingRate.HZ_1000)
        self.new = empty_snapshot(step_size=180, polling_rate=PollingRate.HZ_2000)

    def make_shell(self, readback=None):
        service = SimpleNamespace(
            restore=MagicMock(return_value=SimpleNamespace(readback_snapshot=readback))
        )
        shell = make_shell(settings_service=MagicMock(), restore_point_service=service)
        state = shell.device_service.state
        state.connection_state = "connected"
        state.device_class = "zd_ultimate_legend"
        state.stable_identifier = "controller-a"
        shell._observed_controller_presence_key = shell._controller_presence_key()
        shell._set_last_controller_snapshot(self.old)
        shell.last_snapshot_ts = 123.0
        shell.last_snapshot_status = "old read"
        shell._step_size_hydrated = True
        shell._polling_rate_hydrated = True
        shell._diag_deadzone_hydrated = True
        shell.refresh_shell = MagicMock()
        shell.rebuild_current_screen = MagicMock()
        shell.refresh_current_screen = MagicMock()
        shell._request_geometry_log = MagicMock()
        shell.restore_points_screen_state = restore_points.RestorePointsScreenState(
            view=restore_points.VIEW_IN_PROGRESS,
            selected_rp_id="rp_fixture",
            presence_key=shell._controller_presence_key(),
            presence_generation=shell._controller_presence_generation,
        )
        return shell, service

    def test_restore_then_controller_hydrates_actual_readback(self) -> None:
        shell, _service = self.make_shell(self.new)
        restore_points._execute_restore(shell)

        values = {}
        shell._set_widget = lambda tag, value: values.__setitem__(tag, value)
        shell.current_screen = "controller"
        with patch.dict(AppShell.SCREEN_BUILDERS, {"controller": lambda *_: None}):
            AppShell.rebuild_current_screen(shell)

        self.assertEqual(values["step_size_slider"], 180)
        self.assertEqual(values["usb_polling_rate_combo"], "2000Hz")
        self.assertIs(shell.last_controller_snapshot, self.new)
        self.assertEqual(shell.last_snapshot_identity, "controller-a")
        shell.settings_service.get_all_settings.assert_not_called()
        shell.device_service.official_app_summary_service.read_summary.assert_not_called()

    def test_restore_then_save_current_persists_readback_not_old_cache(self) -> None:
        shell, _service = self.make_shell(self.new)
        restore_points._execute_restore(shell)
        shell.save_current_as_named_wrapper_profile("After restore", include_device=True)

        saved = shell.wrapper_profile_store.save.call_args.args[0]
        self.assertEqual(saved.snapshot.step_size, 180)
        self.assertEqual(saved.snapshot.polling_rate, PollingRate.HZ_2000)

    def test_partial_write_and_read_publish_observed_fields_only(self) -> None:
        partial = empty_snapshot(step_size=None, polling_rate=PollingRate.HZ_2000)
        shell, service = self.make_shell(partial)
        service.restore.return_value.write_failed = 1
        service.restore.return_value.mismatched = 1
        restore_points._execute_restore(shell)
        shell.save_current_as_named_wrapper_profile("Partial restore", include_device=True)

        saved = shell.wrapper_profile_store.save.call_args.args[0]
        self.assertIsNone(saved.snapshot.step_size)
        self.assertEqual(saved.snapshot.polling_rate, PollingRate.HZ_2000)
        self.assertFalse(shell._step_size_hydrated)
        self.assertIn("step_size", shell.last_snapshot_status)

    def test_unreadable_restore_blocks_save_current(self) -> None:
        shell, _service = self.make_shell()
        restore_points._execute_restore(shell)
        shell.save_current_as_named_wrapper_profile("Unreadable", include_device=True)

        self.assertIsNone(shell.last_controller_snapshot)
        self.assertIsNone(shell.last_snapshot_ts)
        self.assertIsNone(shell.last_snapshot_identity)
        shell.wrapper_profile_store.save.assert_not_called()
        self.assertFalse(shell._step_size_hydrated)

    def test_exception_after_possible_write_does_not_restore_old_cache(self) -> None:
        shell, service = self.make_shell(self.new)

        def fail_after_write(_rp_id):
            self.assertIsNone(shell.last_controller_snapshot)
            raise RuntimeError("fixture failure after partial write")

        service.restore.side_effect = fail_after_write
        restore_points._execute_restore(shell)
        shell.save_current_as_named_wrapper_profile("Failed", include_device=True)

        self.assertIsNone(shell.last_controller_snapshot)
        self.assertIsNone(shell.last_snapshot_ts)
        self.assertIsNone(shell.last_snapshot_identity)
        self.assertEqual(shell.restore_points_screen_state.view, restore_points.VIEW_LIST)
        shell.wrapper_profile_store.save.assert_not_called()

    def test_busy_refusal_preserves_cache_without_starting_restore(self) -> None:
        shell, service = self.make_shell(self.new)
        shell._hid_executor = MagicMock()
        shell._hid_job_in_flight = True
        restore_points._execute_restore(shell)

        self.assertIs(shell.last_controller_snapshot, self.old)
        self.assertEqual(shell.last_snapshot_ts, 123.0)
        self.assertEqual(shell.restore_points_screen_state.view, restore_points.VIEW_CONFIRM)
        service.restore.assert_not_called()
        shell._hid_executor.assert_not_called()

    def test_consent_refusal_preserves_cache_without_starting_restore(self) -> None:
        shell, service = self.make_shell(self.new)
        shell._consent_pending_write_allowed_or_refuse = lambda: False
        restore_points._execute_restore(shell)

        self.assertIs(shell.last_controller_snapshot, self.old)
        self.assertEqual(shell.last_snapshot_ts, 123.0)
        service.restore.assert_not_called()

    def test_threaded_restore_invalidates_before_worker_and_publishes_on_render(self) -> None:
        shell, service = self.make_shell(self.new)
        shell._hid_executor = threaded_hid_executor
        render_thread = threading.get_ident()
        observed_threads = []
        prepare = shell._prepare_controller_snapshot_for_restore
        publish = shell._publish_restore_readback

        def record_prepare():
            observed_threads.append(("prepare", threading.get_ident()))
            prepare()

        def record_publish(*args):
            observed_threads.append(("publish", threading.get_ident()))
            publish(*args)

        def restore(_rp_id):
            observed_threads.append(("restore", threading.get_ident()))
            self.assertIsNone(shell.last_controller_snapshot)
            return SimpleNamespace(readback_snapshot=self.new)

        shell._prepare_controller_snapshot_for_restore = record_prepare
        shell._publish_restore_readback = record_publish
        service.restore.side_effect = restore
        restore_points._execute_restore(shell)
        completion = shell._hid_job_completions.get(timeout=5)
        self.assertIsNone(shell.last_controller_snapshot)
        shell.save_current_as_named_wrapper_profile("While busy", include_device=True)
        shell.wrapper_profile_store.save.assert_not_called()
        shell._hid_job_completions.put(completion)
        shell._drain_hid_job_completions()

        self.assertIs(shell.last_controller_snapshot, self.new)
        self.assertEqual(observed_threads[0], ("prepare", render_thread))
        self.assertNotEqual(observed_threads[1][1], render_thread)
        self.assertEqual(observed_threads[2], ("publish", render_thread))

    def test_identity_change_before_completion_discards_readback(self) -> None:
        shell, _service = self.make_shell(self.new)
        pending = []
        shell._hid_executor = lambda job, done: pending.append((job, done))
        restore_points._execute_restore(shell)
        job, done = pending.pop()
        result = job()
        shell.device_service.state.stable_identifier = "controller-b"
        done(result)
        shell._drain_hid_job_completions()

        self.assertIsNone(shell.last_controller_snapshot)
        self.assertIsNone(shell.last_snapshot_identity)
        self.assertIsNone(shell.restore_points_screen_state.result)
        self.assertEqual(shell.restore_points_screen_state.view, restore_points.VIEW_CONFIRM)

    def test_same_controller_write_only_paddles_survive_cache_retirement(self) -> None:
        shell, _service = self.make_shell(self.new)
        binding = BackPaddleBinding(None)
        shell._remember_back_paddle_binding(MacroSlot.M1, binding)
        restore_points._execute_restore(shell)

        self.assertEqual(shell.last_controller_snapshot.back_paddle_bindings[MacroSlot.M1], binding)
        self.assertEqual(shell.last_controller_snapshot.step_size, 180)


if __name__ == "__main__":
    unittest.main()
