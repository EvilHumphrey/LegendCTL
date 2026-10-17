"""Restore readback replaces the shell cache without a second device read."""

from __future__ import annotations

import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.r2_shell_test_helpers import empty_snapshot, make_shell
from tests.test_restore_points_screen import _rp
from zd_app.i18n import set_locale, t
from zd_app.services._log_entry import LogEntry, render_log_message
from zd_app.services.device_service import DeviceService
from zd_app.services.restore_point_service import _build_restore_preview
from zd_app.services.settings_service import (
    BackPaddleBinding,
    MacroSlot,
    PollingRate,
)
from zd_app.storage.restore_point_models import PreparedRestore, RestoreResult, RestoreResultLabel
from zd_app.ui.app_shell import AppShell, threaded_hid_executor
from zd_app.ui.screens import restore_points


class RestoreShellSnapshotHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        set_locale("en")
        # Exercise real shell hydration/persistence with no native DPG calls,
        # filesystem writes, live device access, or official-app probes.
        for name in ("does_item_exist", "set_value", "configure_item", "delete_item"):
            replacement = patch(f"dearpygui.dearpygui.{name}", return_value=False)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.old = empty_snapshot(step_size=10, polling_rate=PollingRate.HZ_1000)
        self.new = empty_snapshot(step_size=180, polling_rate=PollingRate.HZ_2000)

    def make_shell(self, readback=None):
        source = _rp()
        source = replace(
            source,
            snapshot=self.new,
            coverage=replace(
                source.coverage,
                captured_supported_count=2,
                total_supported_count=2,
                fields={name: source.coverage.fields[name] for name in ("polling_rate", "step_size")},
            ),
        )
        self.prepared = PreparedRestore.from_restore_point(
            source, preview=_build_restore_preview(source, self.old, {}),
        )
        service = SimpleNamespace(restore=MagicMock(return_value=self.restore_result(readback)))

        def restore(prepared, *, acknowledge_write_only_paddles, presence_guard):
            self.assert_prepared_call(prepared, acknowledge_write_only_paddles, presence_guard)
            return service.restore.return_value

        service.restore.side_effect = restore
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
            selected_rp_id=source.id,
            prepared_restore=self.prepared,
            presence_key=shell._controller_presence_key(),
            presence_generation=shell._controller_presence_generation,
        )
        return shell, service

    def restore_result(self, readback, **changes):
        result = RestoreResult(
            label=RestoreResultLabel.VERIFIED if readback is not None else RestoreResultLabel.RESTORED_WITH_WARNINGS,
            attempted=2, wrote_succeeded=2, write_failed=0,
            verified_matched=2 if readback is not None else 0,
            could_not_verify=0 if readback is not None else 2, mismatched=0,
            fields=(), before_restore_point_id=None, completed_at="2026-10-17T00:00:00Z",
            readback_snapshot=readback,
        )
        return replace(result, **changes)

    def footer_shell(self, *, context_ready=True):
        self.old = empty_snapshot(step_size=147, polling_rate=PollingRate.HZ_2000)
        self.new = empty_snapshot(step_size=146, polling_rate=PollingRate.HZ_2000)
        shell, service = self.make_shell(self.new)
        values = {}
        shell._dpg_context_ready = context_ready
        shell._viewport_client_width = lambda: 1280
        shell._set_if_exists = lambda tag, value: values.__setitem__(tag, value)
        # Use the actual non-I/O receipt writer: a no-op Mock would conceal the
        # canonical service fallback which the real refresh tick repaints.
        shell.device_service.record_apply_result.side_effect = lambda success, message: DeviceService.record_apply_result(
            shell.device_service, success, message,
        )
        shell.refresh_shell = lambda: AppShell.refresh_shell(shell)
        exists = patch("dearpygui.dearpygui.does_item_exist", side_effect=lambda tag: tag in {
            "footer_status_text", "settings_v2_status_text",
        })
        exists.start()
        self.addCleanup(exists.stop)
        shell._record_settings_apply_result(True, LogEntry("00:00:00", "apply.step_size.success", {"value": 147}))
        shell.device_service.record_apply_result.reset_mock()
        return shell, service, values

    def refresh_after_banner_expires(self, shell):
        with patch("zd_app.ui.app_shell.time.time", return_value=10**12):
            AppShell.refresh_shell(shell)

    def assert_prepared_call(self, prepared, acknowledgement, presence_guard):
        self.assertIs(prepared, self.prepared)
        self.assertEqual(prepared.materialize().snapshot, self.new)
        self.assertIs(acknowledgement, False)
        self.assertTrue(presence_guard())

    def test_restore_then_controller_hydrates_actual_readback(self) -> None:
        shell, service = self.make_shell(self.new)
        restore_points._execute_restore(shell)
        service.restore.assert_called_once()

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
        shell, service = self.make_shell(self.new)
        restore_points._execute_restore(shell)
        service.restore.assert_called_once()
        shell.save_current_as_named_wrapper_profile("After restore", include_device=True)

        saved = shell.wrapper_profile_store.save.call_args.args[0]
        self.assertEqual(saved.snapshot.step_size, 180)
        self.assertEqual(saved.snapshot.polling_rate, PollingRate.HZ_2000)

    def test_partial_write_and_read_publish_observed_fields_only(self) -> None:
        partial = empty_snapshot(step_size=None, polling_rate=PollingRate.HZ_2000)
        shell, service = self.make_shell(partial)
        service.restore.return_value = self.restore_result(
            partial, label=RestoreResultLabel.PARTIALLY_RESTORED,
            wrote_succeeded=1, write_failed=1, verified_matched=0, could_not_verify=1,
        )
        restore_points._execute_restore(shell)
        service.restore.assert_called_once()
        shell.save_current_as_named_wrapper_profile("Partial restore", include_device=True)

        saved = shell.wrapper_profile_store.save.call_args.args[0]
        self.assertIsNone(saved.snapshot.step_size)
        self.assertEqual(saved.snapshot.polling_rate, PollingRate.HZ_2000)
        self.assertFalse(shell._step_size_hydrated)
        self.assertIn("step_size", shell.last_snapshot_status)

    def test_unreadable_restore_blocks_save_current(self) -> None:
        shell, service = self.make_shell()
        restore_points._execute_restore(shell)
        service.restore.assert_called_once()
        shell.save_current_as_named_wrapper_profile("Unreadable", include_device=True)

        self.assertIsNone(shell.last_controller_snapshot)
        self.assertIsNone(shell.last_snapshot_ts)
        self.assertIsNone(shell.last_snapshot_identity)
        shell.wrapper_profile_store.save.assert_not_called()
        self.assertFalse(shell._step_size_hydrated)

    def test_exception_after_possible_write_does_not_restore_old_cache(self) -> None:
        shell, service = self.make_shell(self.new)

        def fail_after_write(prepared, *, acknowledge_write_only_paddles, presence_guard):
            self.assert_prepared_call(prepared, acknowledge_write_only_paddles, presence_guard)
            self.assertIsNone(shell.last_controller_snapshot)
            raise RuntimeError("fixture failure after partial write")

        service.restore.side_effect = fail_after_write
        restore_points._execute_restore(shell)
        service.restore.assert_called_once()
        shell.save_current_as_named_wrapper_profile("Failed", include_device=True)

        self.assertIsNone(shell.last_controller_snapshot)
        self.assertIsNone(shell.last_snapshot_ts)
        self.assertIsNone(shell.last_snapshot_identity)
        self.assertEqual(shell.restore_points_screen_state.view, restore_points.VIEW_LIST)
        self.assertIn("fixture failure after partial write", shell.restore_points_screen_state.status_text)
        shell.wrapper_profile_store.save.assert_not_called()

    def test_busy_refusal_preserves_cache_without_starting_restore(self) -> None:
        shell, service = self.make_shell(self.new)
        shell._hid_executor = MagicMock()
        shell._hid_job_in_flight = True
        restore_points._execute_restore(shell)

        self.assertIs(shell.last_controller_snapshot, self.old)
        self.assertEqual(shell.last_snapshot_ts, 123.0)
        self.assertEqual(shell.restore_points_screen_state.view, restore_points.VIEW_CONFIRM)
        self.assertEqual(shell.restore_points_screen_state.status_text, t("apply.busy"))
        service.restore.assert_not_called()
        shell._hid_executor.assert_not_called()

    def test_consent_refusal_preserves_cache_without_starting_restore(self) -> None:
        shell, service = self.make_shell(self.new)
        shell._consent_pending_write_allowed_or_refuse = lambda: False
        restore_points._execute_restore(shell)

        self.assertIs(shell.last_controller_snapshot, self.old)
        self.assertEqual(shell.last_snapshot_ts, 123.0)
        self.assertEqual(shell.restore_points_screen_state.status_text, t("first_run.pending_write_blocked"))
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

        def restore(prepared, *, acknowledge_write_only_paddles, presence_guard):
            self.assert_prepared_call(prepared, acknowledge_write_only_paddles, presence_guard)
            observed_threads.append(("restore", threading.get_ident()))
            self.assertIsNone(shell.last_controller_snapshot)
            return self.restore_result(self.new)

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

        service.restore.assert_called_once()
        self.assertIs(shell.last_controller_snapshot, self.new)
        self.assertEqual(observed_threads[0], ("prepare", render_thread))
        self.assertNotEqual(observed_threads[1][1], render_thread)
        self.assertEqual(observed_threads[2], ("publish", render_thread))

    def test_identity_change_before_completion_discards_readback(self) -> None:
        shell, service = self.make_shell(self.new)
        pending = []
        shell._hid_executor = lambda job, done: pending.append((job, done))
        restore_points._execute_restore(shell)
        job, done = pending.pop()
        result = job()
        service.restore.assert_called_once()
        shell.device_service.state.stable_identifier = "controller-b"
        done(result)
        shell._drain_hid_job_completions()

        self.assertIsNone(shell.last_controller_snapshot)
        self.assertIsNone(shell.last_snapshot_identity)
        self.assertIsNone(shell.restore_points_screen_state.result)
        self.assertEqual(shell.restore_points_screen_state.view, restore_points.VIEW_CONFIRM)

    def test_same_controller_write_only_paddles_survive_cache_retirement(self) -> None:
        shell, service = self.make_shell(self.new)
        binding = BackPaddleBinding(None)
        shell._remember_back_paddle_binding(MacroSlot.M1, binding)
        restore_points._execute_restore(shell)
        service.restore.assert_called_once()

        self.assertEqual(shell.last_controller_snapshot.back_paddle_bindings[MacroSlot.M1], binding)
        self.assertEqual(shell.last_controller_snapshot.step_size, 180)

    def test_accepted_restore_replaces_old_footer_before_worker_starts(self) -> None:
        for context_ready in (True, False):
            with self.subTest(context_ready=context_ready):
                shell, service, values = self.footer_shell(context_ready=context_ready)
                jobs = []
                shell._hid_executor = lambda job, done: jobs.append((job, done))
                restore_points._execute_restore(shell)
                self.assertEqual(len(jobs), 1)
                service.restore.assert_not_called()
                shell.device_service.record_apply_result.assert_not_called()
                self.assertEqual(render_log_message(shell.device_service.last_apply_result), t("restore_points.in_progress.title"))
                self.refresh_after_banner_expires(shell)
                self.assertEqual(values["footer_status_text"], t("restore_points.in_progress.title"))
                self.assertNotIn("147", values["bottom_apply_result"])
                if not context_ready:
                    self.assertIsNone(shell._apply_status_message)
                    self.assertFalse(shell._apply_status_success)

    def test_restore_footer_survives_real_refresh_navigation_and_read(self) -> None:
        shell, service, values = self.footer_shell()
        restore_points._execute_restore(shell)
        expected = t("restore_points.result.footer", status=t("restore_points.result.header.verified"), matched=2, attempted=2)
        self.assertEqual(values["footer_status_text"], expected)
        self.refresh_after_banner_expires(shell)
        self.assertEqual(values["footer_status_text"], expected)
        self.assertEqual(values["bottom_apply_result"], expected)
        shell.current_screen = "controller"
        shell._set_widget = lambda tag, value: values.__setitem__(tag, value)
        with patch.dict(AppShell.SCREEN_BUILDERS, {"controller": lambda *_: None}):
            AppShell.rebuild_current_screen(shell)
        self.assertEqual(values["step_size_slider"], 146)
        shell._refresh_read_on_done((self.new, None), include_device=True)
        self.refresh_after_banner_expires(shell)
        self.assertEqual(values["footer_status_text"], expected)
        self.assertNotIn("147", values["footer_status_text"])
        service.restore.assert_called_once()
        shell.settings_service.get_all_settings.assert_not_called()

    def test_restore_footer_names_each_actual_outcome_after_banner_expires(self) -> None:
        cases = (
            (RestoreResultLabel.VERIFIED, 2, 0, 2, 0, 0, True),
            (RestoreResultLabel.RESTORED_WITH_WARNINGS, 2, 0, 1, 1, 0, True),
            (RestoreResultLabel.PARTIALLY_RESTORED, 1, 1, 1, 0, 0, False),
            (RestoreResultLabel.MISMATCH_AFTER_RESTORE, 2, 0, 1, 0, 1, False),
            (RestoreResultLabel.RESTORE_FAILED, 0, 2, 0, 0, 0, False),
        )
        for label, wrote, failed, matched, unread, mismatch, success in cases:
            with self.subTest(label=label):
                shell, service, values = self.footer_shell()
                service.restore.return_value = self.restore_result(
                    self.new, label=label, wrote_succeeded=wrote, write_failed=failed,
                    verified_matched=matched, could_not_verify=unread, mismatched=mismatch,
                )
                restore_points._execute_restore(shell)
                expected = t("restore_points.result.footer", status=t(f"restore_points.result.header.{label.value}"), matched=matched, attempted=2)
                self.assertEqual(values["footer_status_text"], expected)
                self.refresh_after_banner_expires(shell)
                self.assertEqual(values["footer_status_text"], expected)
                self.assertEqual(values["bottom_apply_result"], expected)
                shell.device_service.record_apply_result.assert_called_once()
                self.assertIs(shell.device_service.record_apply_result.call_args.args[0], success)

    def test_restore_exception_replaces_canonical_footer_with_failure(self) -> None:
        shell, service, values = self.footer_shell()
        service.restore.side_effect = RuntimeError("fixture partial-write exception")
        restore_points._execute_restore(shell)
        self.refresh_after_banner_expires(shell)
        self.assertEqual(values["footer_status_text"], t("restore_points.result.header.restore_failed"))
        self.assertEqual(values["bottom_apply_result"], t("restore_points.result.header.restore_failed"))
        self.assertIs(shell.device_service.record_apply_result.call_args.args[0], False)

    def test_restore_preflight_or_cancel_preserves_previous_action(self) -> None:
        for action in ("cancel", "busy", "consent", "unprepared"):
            with self.subTest(action=action):
                shell, service, values = self.footer_shell()
                old_receipt = shell.device_service.last_apply_result
                if action == "cancel":
                    restore_points._on_back_to_detail(shell)
                else:
                    if action == "busy":
                        shell._hid_executor = MagicMock()
                        shell._hid_job_in_flight = True
                    elif action == "consent":
                        shell._consent_pending_write_allowed_or_refuse = lambda: False
                    else:
                        shell.restore_points_screen_state.prepared_restore = None
                    restore_points._execute_restore(shell)
                self.refresh_after_banner_expires(shell)
                self.assertEqual(shell.device_service.last_apply_result, old_receipt)
                self.assertIn("147", values["footer_status_text"])
                service.restore.assert_not_called()
                shell.device_service.record_apply_result.assert_not_called()

    def test_stale_restore_result_or_exception_never_overwrites_device_changed_receipt(self) -> None:
        for raises in (False, True):
            with self.subTest(raises=raises):
                shell, service, values = self.footer_shell()
                jobs = []
                shell._hid_executor = lambda job, done: jobs.append((job, done))
                if raises:
                    service.restore.side_effect = RuntimeError("fixture failure on old controller")
                restore_points._execute_restore(shell)
                job, done = jobs.pop()
                outcome = shell._call_hid_job(job)
                shell.device_service.state.stable_identifier = "controller-b"
                done(outcome)
                shell._drain_hid_job_completions()
                self.refresh_after_banner_expires(shell)
                self.assertEqual(values["footer_status_text"], t("apply.device_changed_after_write"))
                self.assertIsNone(shell.restore_points_screen_state.result)
                self.assertNotIn("147", values["footer_status_text"])

    def test_presence_change_during_readback_publication_cannot_publish_restore_receipt(self) -> None:
        shell, _service, values = self.footer_shell()
        publish = shell._publish_restore_readback
        def changed_publish(*args):
            publish(*args)
            shell.device_service.state.stable_identifier = "controller-b"
        shell._publish_restore_readback = changed_publish
        restore_points._execute_restore(shell)
        self.refresh_after_banner_expires(shell)
        self.assertEqual(values["footer_status_text"], t("apply.device_changed_after_write"))
        self.assertIsNone(shell.restore_points_screen_state.result)


if __name__ == "__main__":
    unittest.main()
