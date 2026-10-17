"""Controller page transitions and narrow Apply readback, without hardware."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import empty_snapshot, make_shell
from zd_app.i18n import set_locale, t
from zd_app.services.settings_service import (
    PollingRate,
    SensitivityAnchor,
    SetDeadzoneOutcome,
    SetPollingRateOutcome,
    SetStepSizeOutcome,
    StickDeadzones,
)
from zd_app.ui.screens import controller


class ControllerReadbackStateTests(unittest.TestCase):
    def setUp(self) -> None:
        set_locale("en")
        dpg.create_context()
        self.addCleanup(dpg.destroy_context)
        self.service = MagicMock()
        self.service.set_all_deadzones.return_value = SimpleNamespace(
            outcome=SetDeadzoneOutcome.OK, error_code=None,
        )
        self.shell = make_shell(settings_service=self.service)
        self.shell.device_service.state.connection_state = "connected"
        self.shell.device_service.state.device_class = "zd_ultimate_legend"
        self.shell.device_service.state.stable_identifier = "fixture-controller"
        self.shell.current_screen = "controller"
        self.shell.refresh_shell = lambda: None
        self.shell._dpg_context_ready = True
        self.shell._diag_deadzone_hydrated = True
        self.shell.last_controller_snapshot = empty_snapshot(
            deadzones=StickDeadzones(0, 0, 0, 0),
            step_size=146, polling_rate=PollingRate.HZ_4000,
        )
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass

    def build(self) -> None:
        self.shell.rebuild_current_screen()

    def settle(self) -> None:
        now = max(self.shell._slider_throttle._last_write_ts.values(), default=0) + 1
        with patch("zd_app.ui.app_shell.time.monotonic", return_value=now):
            self.shell._flush_slider_throttle()

    def test_step_number_entry_commits_only_on_apply(self) -> None:
        self.build()
        config = dpg.get_item_configuration("step_size_slider")
        self.assertIsNone(config["callback"], "Partial typed numbers must not invoke the writer")
        self.assertFalse(config["on_enter"], "A draft must survive clicking Save As")
        self.assertEqual(config["step"], 1)
        self.assertEqual(config["step_fast"], 1)
        self.service.set_step_size.return_value = SimpleNamespace(outcome=SetStepSizeOutcome.OK)
        self.service.get_step_size.return_value = 147
        for partial in (14, 1, 147):
            dpg.set_value("step_size_slider", partial)
            self.settle()
            self.service.set_step_size.assert_not_called()
        dpg.get_item_configuration("controller_apply_step_size")["callback"]()
        self.settle()
        self.service.set_step_size.assert_called_once_with(147)
        self.assertEqual(self.shell.last_controller_snapshot.step_size, 147)

    def test_save_refuses_unsubmitted_step_number(self) -> None:
        self.build()
        dpg.set_value("step_size_slider", 14)
        self.shell.save_current_as_named_wrapper_profile("Unfinished", include_device=True)
        self.shell.wrapper_profile_store.save.assert_not_called()
        self.service.set_step_size.assert_not_called()
        self.assertEqual(self.shell.last_controller_snapshot.step_size, 146)
        self.assertEqual(dpg.get_value("step_size_slider"), 14)

    def test_save_as_reports_unapplied_step_before_opening_modal(self) -> None:
        self.build()
        dpg.set_value("step_size_slider", 147)
        self.shell._open_save_as_modal()
        self.assertFalse(dpg.does_item_exist("wrapper_profile_save_as_modal"))
        self.assertEqual(dpg.get_value("step_size_slider"), 147)
        self.shell.wrapper_profile_store.save.assert_not_called()
        self.service.set_step_size.assert_not_called()

    def test_step_apply_is_disabled_until_read_and_while_busy(self) -> None:
        self.build()
        self.assertTrue(dpg.get_item_configuration("controller_apply_step_size")["enabled"])
        self.shell._hid_job_in_flight = True
        controller.refresh_state(self.shell)
        self.assertFalse(dpg.get_item_configuration("controller_apply_step_size")["enabled"])
        self.shell._hid_job_in_flight = False
        self.shell.last_controller_snapshot = empty_snapshot(step_size=None)
        controller.refresh_state(self.shell)
        self.assertFalse(dpg.get_item_configuration("controller_apply_step_size")["enabled"])

    def test_save_after_step_apply_uses_confirmed_value(self) -> None:
        self.build()
        self.service.set_step_size.return_value = SimpleNamespace(outcome=SetStepSizeOutcome.OK)
        self.service.get_step_size.return_value = 147
        dpg.set_value("step_size_slider", 147)
        dpg.get_item_configuration("controller_apply_step_size")["callback"]()
        self.shell.save_current_as_named_wrapper_profile("Pending", include_device=True)
        self.shell.wrapper_profile_store.save.assert_not_called()
        self.settle()
        self.shell.save_current_as_named_wrapper_profile("Confirmed", include_device=True)
        self.assertEqual(self.shell.wrapper_profile_store.save.call_args.args[0].snapshot.step_size, 147)

    def test_unchanged_step_number_does_not_block_profile_save(self) -> None:
        self.build()
        dpg.set_value("step_size_slider", 14)
        dpg.set_value("step_size_slider", 146)
        self.shell.save_current_as_named_wrapper_profile("Unchanged", include_device=True)
        self.shell.wrapper_profile_store.save.assert_called_once()
        self.service.set_step_size.assert_not_called()

    def test_pending_deadzone_banner_does_not_claim_failure_or_success(self) -> None:
        self.shell._hid_executor = lambda _job, _deliver: None
        self.build()
        dpg.set_value("deadzone_left_center_slider", 1)
        self.shell.apply_deadzone_settings()
        for locale in ("en", "zh-CN", "ko"):
            set_locale(locale)
            self.shell._render_active_apply_status()
            self.assertEqual(self.shell._apply_status_text, t("apply.deadzone.verifying"))

    def test_step_size_readback_replaces_cache_after_drag_settles(self) -> None:
        self.build()
        self.service.set_step_size.return_value = SimpleNamespace(outcome=SetStepSizeOutcome.OK)
        self.service.get_step_size.return_value = 147
        self.shell.apply_step_size(147)
        self.assertIsNone(self.shell.last_controller_snapshot.step_size)
        self.service.get_step_size.assert_not_called()
        self.settle()
        self.assertEqual(self.shell.last_controller_snapshot.step_size, 147)
        self.build()
        self.assertEqual(dpg.get_value("step_size_slider"), 147)
        self.service.get_step_size.assert_called_once()

    def test_polling_rate_confirmation_updates_cache_for_every_rate(self) -> None:
        self.service.set_polling_rate.return_value = SimpleNamespace(outcome=SetPollingRateOutcome.OK)
        for rate, label in ((PollingRate.HZ_1000, "1000Hz"), (PollingRate.HZ_4000, "4000Hz"), (PollingRate.HZ_8000, "8000Hz")):
            with self.subTest(rate=rate):
                self.build()
                self.service.get_polling_rate.reset_mock()
                self.service.get_polling_rate.return_value = rate
                self.shell._slider_throttle.clear_all()
                self.shell.apply_polling_rate(label)
                self.settle()
                self.assertEqual(self.shell.last_controller_snapshot.polling_rate, rate)
                self.build()
                self.assertEqual(dpg.get_value("usb_polling_rate_combo"), label)
                self.service.get_polling_rate.assert_called_once()

    def test_8k_mismatch_keeps_confirmed_rate_after_rebuild(self) -> None:
        self.build()
        self.service.set_polling_rate.return_value = SimpleNamespace(outcome=SetPollingRateOutcome.OK)
        self.service.get_polling_rate.return_value = PollingRate.HZ_1000
        self.shell.apply_polling_rate("8000Hz")
        self.settle()
        self.assertEqual(self.shell.last_controller_snapshot.polling_rate, PollingRate.HZ_1000)
        self.build()
        self.assertEqual(dpg.get_value("usb_polling_rate_combo"), "1000Hz")

    def test_8k_unreadable_confirmation_disables_stale_combo(self) -> None:
        self.build()
        self.service.set_polling_rate.return_value = SimpleNamespace(outcome=SetPollingRateOutcome.OK)
        self.service.get_polling_rate.return_value = None
        self.shell.apply_polling_rate("8000Hz")
        self.settle()
        self.assertIsNone(self.shell.last_controller_snapshot.polling_rate)
        self.assertFalse(self.shell._polling_rate_hydrated)
        self.assertFalse(dpg.get_item_configuration("usb_polling_rate_combo")["enabled"])

    def test_step_size_drag_reads_only_settled_final_value(self) -> None:
        self.build()
        self.service.set_step_size.return_value = SimpleNamespace(outcome=SetStepSizeOutcome.OK)
        self.service.get_step_size.return_value = 148
        with patch("zd_app.ui.app_shell.time.monotonic", return_value=10):
            self.shell.apply_step_size(147)
            self.shell.apply_step_size(148)
        self.settle()  # trailing write; confirmation waits for its quiet interval
        self.settle()
        self.assertEqual(self.service.set_step_size.call_count, 2)
        self.service.get_step_size.assert_called_once()
        self.assertEqual(self.shell.last_controller_snapshot.step_size, 148)

    def test_step_size_unreadable_confirmation_does_not_revive_baseline(self) -> None:
        self.build()
        self.service.set_step_size.return_value = SimpleNamespace(outcome=SetStepSizeOutcome.OK)
        self.service.get_step_size.side_effect = TimeoutError("fixture timeout")
        self.shell.apply_step_size(147)
        self.settle()
        self.assertIsNone(self.shell.last_controller_snapshot.step_size)
        self.assertFalse(self.shell._step_size_hydrated)
        self.build()
        self.assertFalse(dpg.get_item_configuration("step_size_slider")["enabled"])

    def test_save_profile_uses_confirmed_step_and_rate_without_manual_read(self) -> None:
        self.build()
        self.service.set_step_size.return_value = SimpleNamespace(outcome=SetStepSizeOutcome.OK)
        self.service.get_step_size.return_value = 147
        self.service.set_polling_rate.return_value = SimpleNamespace(outcome=SetPollingRateOutcome.OK)
        self.service.get_polling_rate.return_value = PollingRate.HZ_1000
        self.shell.apply_step_size(147)
        self.shell.apply_polling_rate("1000Hz")
        self.shell.save_current_as_named_wrapper_profile("Updated", include_device=True)
        self.shell.wrapper_profile_store.save.assert_not_called()
        self.settle()
        self.shell.save_current_as_named_wrapper_profile("Updated", include_device=True)
        saved = self.shell.wrapper_profile_store.save.call_args.args[0]
        self.assertEqual(saved.snapshot.step_size, 147)
        self.assertEqual(saved.snapshot.polling_rate, PollingRate.HZ_1000)
        self.service.get_all_settings.assert_not_called()

    def test_save_profile_refuses_unconfirmed_value_instead_of_saving_old_baseline(self) -> None:
        self.build()
        self.service.set_step_size.return_value = SimpleNamespace(outcome=SetStepSizeOutcome.OK)
        self.service.get_step_size.return_value = None
        self.shell.apply_step_size(147)
        self.settle()
        self.shell.save_current_as_named_wrapper_profile("Updated", include_device=True)
        self.shell.wrapper_profile_store.save.assert_not_called()
        self.assertIsNone(self.shell.last_controller_snapshot.step_size)

    def test_read_during_drag_cannot_save_before_trailing_write(self) -> None:
        self.build()
        self.service.set_step_size.return_value = SimpleNamespace(outcome=SetStepSizeOutcome.OK)
        self.service.get_step_size.return_value = 148
        with patch("zd_app.ui.app_shell.time.monotonic", return_value=10):
            self.shell.apply_step_size(147)
            self.shell.apply_step_size(148)
        self.shell._refresh_read_on_done((empty_snapshot(step_size=147), None), include_device=True)
        self.shell.save_current_as_named_wrapper_profile("Updated", include_device=True)
        self.shell.wrapper_profile_store.save.assert_not_called()
        self.settle()
        self.settle()
        self.shell.save_current_as_named_wrapper_profile("Updated", include_device=True)
        saved = self.shell.wrapper_profile_store.save.call_args.args[0]
        self.assertEqual(saved.snapshot.step_size, 148)

    def test_step_save_nudge_appears_after_confirmation_before_saving(self) -> None:
        self.shell._active_wrapper_profile_name = "Existing"
        self.shell.wrapper_profile_store.load.return_value = SimpleNamespace(
            snapshot=empty_snapshot(step_size=146),
        )
        self.build()
        self.service.set_step_size.return_value = SimpleNamespace(outcome=SetStepSizeOutcome.OK)
        self.service.get_step_size.return_value = 147
        self.shell.apply_step_size(147)
        self.assertFalse(self.shell.step_size_save_nudge_offered())
        self.settle()
        self.assertTrue(self.shell.step_size_save_nudge_offered())
        self.assertEqual(self.shell._pending_step_size_save, ("Existing", 147))
        self.shell.wrapper_profile_store.save.assert_not_called()

    def test_changed_controller_during_checkpoint_refuses_step_and_rate_write(self) -> None:
        for method, arg, setter in (
            ("apply_step_size", 147, self.service.set_step_size),
            ("apply_polling_rate", "1000Hz", self.service.set_polling_rate),
        ):
            with self.subTest(method=method):
                self.shell.device_service.state.stable_identifier = "fixture-controller"
                self.shell.last_controller_snapshot = empty_snapshot(step_size=146, polling_rate=PollingRate.HZ_4000)
                self.build()
                def checkpoint(**_kwargs):
                    self.shell.device_service.state.stable_identifier = "replacement-controller"
                    return True, False
                with patch.object(self.shell, "_manual_device_write_gate", side_effect=checkpoint):
                    getattr(self.shell, method)(arg)
                setter.assert_not_called()
                self.assertIsNone(self.shell.last_controller_snapshot)

    def test_pending_step_confirmation_drops_swapped_result(self) -> None:
        jobs = []
        self.shell._hid_executor = lambda job, deliver: jobs.append((job, deliver))
        self.build()
        self.service.set_step_size.return_value = SimpleNamespace(outcome=SetStepSizeOutcome.OK)
        self.shell.apply_step_size(147)
        self.settle()
        self.assertTrue(self.shell.hid_busy)
        self.shell.device_service.state.stable_identifier = "replacement-controller"
        job, deliver = jobs.pop()
        with patch("zd_app.ui.app_shell.time.sleep"):
            deliver(job())
        self.shell._drain_hid_job_completions()
        self.service.get_step_size.assert_not_called()
        self.assertIsNone(self.shell.last_controller_snapshot)

    def test_apply_readback_survives_leaving_and_reopening_page(self) -> None:
        self.build()
        target = StickDeadzones(1, 0, 0, 0)
        self.service.get_deadzones.return_value = target
        dpg.set_value("deadzone_left_center_slider", 1)
        self.shell.apply_deadzone_settings()
        self.assertEqual(self.shell.last_controller_snapshot.deadzones, target)
        self.service.set_all_deadzones.assert_called_once_with(target)
        self.service.get_deadzones.assert_called_once_with()
        self.build()
        self.assertEqual(dpg.get_value("deadzone_left_center_slider"), 1)
        self.assertIn(t("apply.deadzone.verified"), dpg.get_value("controller_deadzone_status"))

    def test_live_deadzone_blocks_save_until_single_write_is_confirmed(self) -> None:
        self.build()
        target = StickDeadzones(1, 2, 3, 4)
        self.service.get_deadzones.return_value = target
        self.shell._controller_deadzone_status_key = "verified"
        self.shell._controller_deadzone_written = StickDeadzones(0, 0, 0, 0)
        self.shell._controller_deadzone_readback = StickDeadzones(0, 0, 0, 0)
        self.shell.apply_diagnostics_deadzone(target)
        self.assertIsNone(self.shell.last_controller_snapshot.deadzones)
        self.assertEqual(self.shell._controller_deadzone_written, target)
        self.assertIsNone(self.shell._controller_deadzone_readback)
        self.assertEqual(self.shell._controller_deadzone_status_key, "verifying")
        self.shell.save_current_as_named_wrapper_profile("Pending", include_device=True)
        self.shell.wrapper_profile_store.save.assert_not_called()
        self.service.get_deadzones.assert_not_called()
        self.settle()
        self.assertEqual(self.shell.last_controller_snapshot.deadzones, target)
        self.build()
        self.assertEqual(dpg.get_value("deadzone_left_center_slider"), 1)
        self.assertIn(t("apply.deadzone.verified"), dpg.get_value("controller_deadzone_status"))
        self.shell.save_current_as_named_wrapper_profile("Confirmed", include_device=True)
        self.assertEqual(self.shell.wrapper_profile_store.save.call_args.args[0].snapshot.deadzones, target)
        self.service.set_all_deadzones.assert_called_once_with(target)
        self.service.get_deadzones.assert_called_once_with()
        self.service.get_all_settings.assert_not_called()

    def test_live_deadzone_mismatch_publishes_actual_values_without_retry(self) -> None:
        self.build()
        actual = StickDeadzones(1, 2, 3, 4)
        self.service.get_deadzones.return_value = actual
        requested = StickDeadzones(9, 8, 7, 6)
        self.shell.apply_diagnostics_deadzone(requested)
        self.settle()
        self.assertEqual(self.shell.last_controller_snapshot.deadzones, actual)
        self.assertEqual(self.shell._diag_deadzone_status_key, "mismatch")
        self.assertEqual(dpg.get_value("deadzone_left_center_slider"), 1)
        self.build()
        self.assertIn(t("apply.deadzone.mismatch"), dpg.get_value("controller_deadzone_status"))
        self.shell.save_current_as_named_wrapper_profile("Actual", include_device=True)
        self.assertEqual(self.shell.wrapper_profile_store.save.call_args.args[0].snapshot.deadzones, actual)
        self.service.set_all_deadzones.assert_called_once_with(requested)
        self.service.get_deadzones.assert_called_once_with()

    def test_live_deadzone_unreadable_or_failed_read_keeps_save_blocked(self) -> None:
        for readback in (None, TimeoutError("fixture timeout")):
            with self.subTest(readback=readback):
                self.shell._slider_throttle.clear_all()
                self.shell.last_controller_snapshot = empty_snapshot(deadzones=StickDeadzones(0, 0, 0, 0))
                self.build()
                self.service.set_all_deadzones.reset_mock()
                self.service.get_deadzones.reset_mock()
                self.service.get_deadzones.side_effect = readback if isinstance(readback, Exception) else None
                self.service.get_deadzones.return_value = readback
                self.shell.apply_diagnostics_deadzone(StickDeadzones(1, 0, 0, 0))
                self.settle()
                self.assertIsNone(self.shell.last_controller_snapshot.deadzones)
                self.assertEqual(self.shell._diag_deadzone_status_key, "sent_unverified")
                self.build()
                self.assertFalse(dpg.get_item_configuration("controller_deadzone_editor")["show"])
                self.assertIn(t("apply.deadzone.unverified"), dpg.get_value("controller_deadzone_status"))
                self.shell.save_current_as_named_wrapper_profile("Unreadable", include_device=True)
                self.shell.wrapper_profile_store.save.assert_not_called()
                self.service.set_all_deadzones.assert_called_once()
                self.service.get_deadzones.assert_called_once_with()

    def test_live_deadzone_failed_write_retires_baseline_without_read_or_retry(self) -> None:
        self.build()
        self.service.set_all_deadzones.return_value = SimpleNamespace(
            outcome=SetDeadzoneOutcome.WRITE_FAILED, error_code=None,
        )
        self.shell.apply_diagnostics_deadzone(StickDeadzones(1, 0, 0, 0))
        self.settle()
        self.assertIsNone(self.shell.last_controller_snapshot.deadzones)
        self.assertEqual(self.shell._diag_deadzone_status_key, "failed")
        self.assertEqual(self.shell._controller_deadzone_status_key, "write_failed")
        self.shell.save_current_as_named_wrapper_profile("Failed", include_device=True)
        self.shell.wrapper_profile_store.save.assert_not_called()
        self.service.set_all_deadzones.assert_called_once()
        self.service.get_deadzones.assert_not_called()

    def test_live_deadzone_drag_saves_only_confirmed_trailing_value(self) -> None:
        self.build()
        leading, trailing = StickDeadzones(1, 0, 0, 0), StickDeadzones(2, 3, 4, 5)
        self.service.get_deadzones.return_value = trailing
        with patch("zd_app.ui.app_shell.time.monotonic", return_value=10):
            self.shell.apply_diagnostics_deadzone(leading)
            self.shell.apply_diagnostics_deadzone(trailing)
        self.shell.save_current_as_named_wrapper_profile("Dragging", include_device=True)
        self.shell.wrapper_profile_store.save.assert_not_called()
        self.settle()
        self.settle()
        self.assertEqual([call.args[0] for call in self.service.set_all_deadzones.call_args_list], [leading, trailing])
        self.service.get_deadzones.assert_called_once_with()
        self.build()
        self.assertEqual(dpg.get_value("deadzone_right_outer_slider"), 5)
        self.shell.save_current_as_named_wrapper_profile("Settled", include_device=True)
        self.assertEqual(self.shell.wrapper_profile_store.save.call_args.args[0].snapshot.deadzones, trailing)

    def test_live_deadzone_read_during_settlement_does_not_enable_save(self) -> None:
        self.build()
        self.shell.apply_diagnostics_deadzone(StickDeadzones(1, 0, 0, 0))
        self.shell._refresh_read_on_done((empty_snapshot(deadzones=StickDeadzones(0, 0, 0, 0)), None), include_device=True)
        self.shell.save_current_as_named_wrapper_profile("Pending", include_device=True)
        self.shell.wrapper_profile_store.save.assert_not_called()

    def test_live_deadzone_checkpoint_change_refuses_old_controller_write(self) -> None:
        self.build()
        def checkpoint(**_kwargs):
            self.shell.device_service.state.stable_identifier = "replacement-controller"
            return True, False
        with patch.object(self.shell, "_manual_device_write_gate", side_effect=checkpoint):
            self.shell.apply_diagnostics_deadzone(StickDeadzones(1, 0, 0, 0))
        self.service.set_all_deadzones.assert_not_called()
        self.assertIsNone(self.shell.last_controller_snapshot)

    def test_live_deadzone_checkpoint_reconnect_refuses_same_identifier_new_generation(self) -> None:
        self.build()
        self.shell._observe_controller_presence()
        def checkpoint(**_kwargs):
            self.shell.device_service.state.connection_state = "no_device"
            self.shell._observe_controller_presence()
            self.shell.device_service.state.connection_state = "connected"
            self.shell._observe_controller_presence()
            return True, False
        with patch.object(self.shell, "_manual_device_write_gate", side_effect=checkpoint):
            self.shell.apply_diagnostics_deadzone(StickDeadzones(1, 0, 0, 0))
        self.service.set_all_deadzones.assert_not_called()
        self.assertIsNone(self.shell.last_controller_snapshot)

    def test_live_deadzone_swap_before_quiet_window_never_reads_replacement(self) -> None:
        self.build()
        self.shell.apply_diagnostics_deadzone(StickDeadzones(1, 0, 0, 0))
        self.shell.device_service.state.stable_identifier = "replacement-controller"
        self.settle()
        self.service.set_all_deadzones.assert_called_once()
        self.service.get_deadzones.assert_not_called()
        self.assertIsNone(self.shell.last_controller_snapshot)

    def test_live_deadzone_reconnect_during_read_rejects_same_identifier_result(self) -> None:
        self.build()
        self.shell._observe_controller_presence()
        target = StickDeadzones(1, 0, 0, 0)
        def read():
            self.shell.device_service.state.connection_state = "no_device"
            self.shell._observe_controller_presence()
            self.shell.device_service.state.connection_state = "connected"
            self.shell._observe_controller_presence()
            return target
        self.service.get_deadzones.side_effect = read
        self.shell.apply_diagnostics_deadzone(target)
        self.settle()
        self.service.get_deadzones.assert_called_once_with()
        self.assertIsNone(self.shell.last_controller_snapshot)
        self.assertIsNone(self.shell._controller_deadzone_readback)

    def test_live_deadzone_swap_during_write_discards_old_controller_state(self) -> None:
        self.build()
        result = self.service.set_all_deadzones.return_value
        def write(_value):
            self.shell.device_service.state.stable_identifier = "replacement-controller"
            return result
        self.service.set_all_deadzones.side_effect = write
        self.shell.apply_diagnostics_deadzone(StickDeadzones(1, 0, 0, 0))
        self.assertIsNone(self.shell.last_controller_snapshot)
        self.assertIsNone(self.shell._deadzone_pending_verify)
        self.service.get_deadzones.assert_not_called()

    def test_live_deadzone_stale_read_or_completion_never_publishes_to_replacement(self) -> None:
        for swap_before_read in (True, False):
            with self.subTest(swap_before_read=swap_before_read):
                self.shell.device_service.state.stable_identifier = "fixture-controller"
                self.shell.last_controller_snapshot = empty_snapshot(deadzones=StickDeadzones(0, 0, 0, 0))
                self.shell._diag_deadzone_hydrated = True
                self.shell._slider_throttle.clear_all()
                self.build()
                jobs = []
                self.shell._hid_executor = lambda job, deliver: jobs.append((job, deliver))
                target = StickDeadzones(1, 0, 0, 0)
                self.service.get_deadzones.reset_mock()
                self.service.get_deadzones.return_value = target
                self.shell.apply_diagnostics_deadzone(target)
                self.settle()
                self.assertTrue(self.shell.hid_busy)
                self.shell.save_current_as_named_wrapper_profile("Pending", include_device=True)
                self.shell.wrapper_profile_store.save.assert_not_called()
                job, deliver = jobs.pop()
                if swap_before_read:
                    self.shell.device_service.state.stable_identifier = "replacement-controller"
                with patch("zd_app.ui.app_shell.time.sleep"):
                    readback = job()
                self.shell.device_service.state.stable_identifier = "replacement-controller"
                deliver(readback)
                self.shell._drain_hid_job_completions()
                self.assertIsNone(self.shell.last_controller_snapshot)
                self.assertIsNone(self.shell._controller_deadzone_readback)
                if swap_before_read:
                    self.service.get_deadzones.assert_not_called()

    def test_mismatch_displays_read_value_and_never_retries_write(self) -> None:
        self.build()
        actual = StickDeadzones(1, 2, 3, 4)
        self.service.get_deadzones.return_value = actual
        dpg.set_value("deadzone_left_center_slider", 9)
        self.shell.apply_deadzone_settings()
        self.assertEqual(self.shell.last_controller_snapshot.deadzones, actual)
        self.assertEqual(dpg.get_value("deadzone_left_center_slider"), 1)
        self.assertIn(t("apply.deadzone.mismatch"), dpg.get_value("controller_deadzone_status"))
        self.service.set_all_deadzones.assert_called_once()
        self.service.get_deadzones.assert_called_once()

    def test_unreadable_confirmation_retires_prewrite_value(self) -> None:
        self.build()
        self.service.get_deadzones.return_value = None
        dpg.set_value("deadzone_left_center_slider", 1)
        self.shell.apply_deadzone_settings()
        self.assertIsNone(self.shell.last_controller_snapshot.deadzones)
        self.build()
        self.assertFalse(dpg.get_item_configuration("controller_deadzone_editor")["show"])
        self.assertFalse(dpg.get_item_configuration("controller_apply_deadzones")["enabled"])
        self.assertIn(t("apply.deadzone.unverified"), dpg.get_value("controller_deadzone_status"))

    def test_disconnect_hides_defaults_and_does_not_claim_foreign_device(self) -> None:
        self.shell.last_controller_snapshot = None
        self.shell.device_service.state.connection_state = "no_device"
        self.shell.device_service.state.device_class = "none"
        self.build()
        self.assertFalse(dpg.get_item_configuration("controller_unverified_device_banner")["show"])
        self.assertFalse(dpg.get_item_configuration("controller_settings_editor")["show"])
        self.assertEqual(dpg.get_value("controller_state_hint"), t("controller.disconnected"))
        self.assertFalse(dpg.get_item_configuration("controller_apply_deadzones")["enabled"])
        self.shell.apply_deadzone_settings()
        self.service.set_all_deadzones.assert_not_called()

    def test_async_first_read_replaces_loading_and_legacy_curve_layout(self) -> None:
        self.shell.last_controller_snapshot = None
        self.build()
        anchors = tuple(SensitivityAnchor(n, n) for n in (0, 14, 28, 42, 57, 71, 85, 100))
        snapshot = empty_snapshot(
            deadzones=StickDeadzones(1, 0, 0, 0),
            sensitivity_left_8point=anchors, sensitivity_right_8point=anchors,
        )
        self.shell._refresh_read_on_done((snapshot, None), include_device=True)
        self.assertTrue(dpg.get_item_configuration("controller_settings_editor")["show"])
        self.assertFalse(dpg.get_item_configuration("controller_state_hint")["show"])
        self.assertTrue(dpg.does_item_exist("sensitivity_left_a8x_slider_8point"))
        self.assertFalse(dpg.does_item_exist("sensitivity_left_a1x_slider"))
        self.assertEqual(dpg.get_value("deadzone_left_center_slider"), 1)

    def test_failed_first_read_is_not_presented_as_still_loading(self) -> None:
        self.shell.last_controller_snapshot = None
        self.build()
        self.shell._refresh_read_on_done(TimeoutError("fixture timeout"), include_device=True)
        controller.refresh_state(self.shell)
        self.assertEqual(dpg.get_value("controller_state_hint"), self.shell.last_snapshot_status)
        self.assertNotEqual(dpg.get_value("controller_state_hint"), t("controller.loading"))

    def test_pending_confirmation_blocks_second_write_and_drops_swapped_result(self) -> None:
        jobs = []
        self.shell._hid_executor = lambda job, deliver: jobs.append((job, deliver))
        self.build()
        dpg.set_value("deadzone_left_center_slider", 1)
        self.shell.apply_deadzone_settings()
        self.assertTrue(self.shell.hid_busy)
        self.assertFalse(dpg.get_item_configuration("controller_apply_deadzones")["enabled"])
        self.shell.apply_deadzone_settings()
        self.service.set_all_deadzones.assert_called_once()
        self.shell.device_service.state.stable_identifier = "replacement-controller"
        job, deliver = jobs.pop()
        with patch("zd_app.ui.app_shell.time.sleep"):
            deliver(job())
        self.shell._drain_hid_job_completions()
        self.service.get_deadzones.assert_not_called()
        self.assertIsNone(self.shell.last_controller_snapshot)


if __name__ == "__main__":
    unittest.main()
