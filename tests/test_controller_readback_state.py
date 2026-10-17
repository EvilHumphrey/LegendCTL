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
