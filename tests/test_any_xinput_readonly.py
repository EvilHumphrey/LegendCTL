"""Any-XInput read-only widening + ZD-only write gating.

The read-only XInput surfaces (the Live Verify tester: per-stick axes +
circularity, live buttons/triggers) work for ANY connected XInput controller,
while EVERY HID write/settings surface stays allowlisted to the verified ZD
Ultimate Legend. These tests prove the three required properties:

* (a) a non-ZD XInput device enables read-only / live-verify but gates + labels
  the write/settings surfaces;
* (b) a ZD device keeps full functionality;
* (c) no write path fires for a non-allowlisted device.

The honesty posture is load-bearing: the app must never claim or attempt a
settings write on an untested controller. The HID transport is itself
ZD-scoped, but these gates make the refusal explicit (no write is *attempted*)
and honestly labeled, which is what the diff has to prove.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from tests.test_live_verify import _FakeXInputService, _live_snap
from zd_app import i18n
from zd_app.models import DeviceState
from zd_app.services.device_service import (
    ZD_ULTIMATE_LEGEND_DEVICE_IDS,
    DeviceService,
    instance_id_is_allowlisted_zd,
)
from zd_app.services.settings_service import StickDeadzones
from zd_app.ui.screens import controller as controller_screen
from zd_app.ui.screens import live_verify
from zd_app.ui.screens import restore_points as rp_screen


_ZD_INSTANCE_ID = "USB\\VID_413D&PID_2104\\ABC123"
_XBOX_INSTANCE_ID = "USB\\VID_045E&PID_028E\\XYZ789"


class _RecordingSettingsService:
    """Settings-service stub: every read/write records its name and reports OK.

    The honesty assertions check that ``calls`` stays empty (no read OR write
    reached the service) on a non-allowlisted controller.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _ok(self, name: str):
        self.calls.append(name)
        return SimpleNamespace(
            outcome=SimpleNamespace(name="OK", value="ok"), error_code=None
        )

    def set_polling_rate(self, rate):
        return self._ok("set_polling_rate")

    def set_all_deadzones(self, deadzones):
        return self._ok("set_all_deadzones")

    def get_deadzones(self):
        self.calls.append("get_deadzones")
        return StickDeadzones(0, 0, 0, 0)


def _shell_with_class(device_class: str, *, settings_service=None, **kw):
    """A real AppShell whose device state carries ``device_class``.

    ``make_shell`` builds the device_service as a MagicMock with a REAL
    ``DeviceState`` — so setting ``device_class`` flips the genuine
    ``write_supported`` / ``live_verify_supported`` properties the gates read.
    """

    shell = make_shell(settings_service=settings_service, **kw)
    shell.device_service.state.device_class = device_class
    shell.refresh_shell = lambda: None
    shell.refresh_current_screen = lambda: None
    shell.rebuild_current_screen = lambda: None
    return shell


# ---------------------------------------------------------------------------
# Detection: allowlist + capability classification
# ---------------------------------------------------------------------------


class DeviceClassificationAndCapabilityTests(unittest.TestCase):
    def _refresh(self, pnp_entries, xinput_slots) -> DeviceState:
        service = DeviceService(clock=lambda: 0.0)
        with patch.object(service, "_find_zd_entries", return_value=pnp_entries), patch(
            "zd_app.services.device_service.get_connected_controllers",
            return_value=xinput_slots,
        ), patch(
            "zd_app.services.device_service.describe_battery_level",
            return_value="Unknown",
        ):
            return service.refresh_state()

    def test_allowlist_matcher_accepts_zd_rejects_others(self) -> None:
        self.assertTrue(instance_id_is_allowlisted_zd(_ZD_INSTANCE_ID))
        self.assertTrue(instance_id_is_allowlisted_zd(_ZD_INSTANCE_ID.lower()))
        self.assertFalse(instance_id_is_allowlisted_zd(_XBOX_INSTANCE_ID))
        self.assertFalse(instance_id_is_allowlisted_zd("USB\\VID_413D&PID_9999\\X"))
        self.assertIn("vid_413d&pid_2104", ZD_ULTIMATE_LEGEND_DEVICE_IDS)

    def test_capability_map_per_device_class(self) -> None:
        cases = {
            "zd_ultimate_legend": (True, True),
            "generic_xinput": (False, True),
            "none": (False, False),
        }
        for cls, (write, live) in cases.items():
            with self.subTest(device_class=cls):
                state = DeviceState(device_class=cls)
                self.assertEqual(state.write_supported, write)
                self.assertEqual(state.live_verify_supported, live)

    def test_default_state_is_write_capable(self) -> None:
        # Preserves pre-widening behaviour: a freshly-constructed state (before
        # detection runs / in tests) defaults to the write-capable ZD class.
        self.assertEqual(DeviceState().device_class, "zd_ultimate_legend")
        self.assertTrue(DeviceState().write_supported)

    def test_zd_device_classified_full(self) -> None:
        state = self._refresh([{"instance_id": _ZD_INSTANCE_ID}], [0])
        self.assertEqual(state.device_class, "zd_ultimate_legend")
        self.assertTrue(state.write_supported)
        self.assertTrue(state.live_verify_supported)
        self.assertEqual(state.product_name, "ZD Ultimate Legend")
        self.assertEqual(state.connection_state, "connected")

    def test_generic_xinput_classified_read_only(self) -> None:
        # No ZD via pnputil, but an XInput pad is present.
        state = self._refresh([], [0])
        self.assertEqual(state.device_class, "generic_xinput")
        self.assertFalse(state.write_supported)
        self.assertTrue(state.live_verify_supported)
        self.assertEqual(state.product_name, "Xbox-compatible controller")
        self.assertEqual(state.connection_state, "connected")

    def test_no_controller_classified_none(self) -> None:
        state = self._refresh([], [])
        self.assertEqual(state.device_class, "none")
        self.assertFalse(state.write_supported)
        self.assertFalse(state.live_verify_supported)
        self.assertEqual(state.connection_state, "no_device")


# ---------------------------------------------------------------------------
# (c) No write path fires for a non-allowlisted device
# ---------------------------------------------------------------------------


class WriteGateRefusesOnNonZdTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_hid_available_or_refuse_false_on_generic_xinput(self) -> None:
        shell = _shell_with_class("generic_xinput")
        self.assertFalse(shell._hid_available_or_refuse())

    def test_hid_available_or_refuse_false_on_no_device(self) -> None:
        shell = _shell_with_class("none")
        self.assertFalse(shell._hid_available_or_refuse())

    def test_per_tab_apply_writes_nothing_on_generic_xinput(self) -> None:
        service = _RecordingSettingsService()
        shell = _shell_with_class("generic_xinput", settings_service=service)
        shell._polling_rate_hydrated = True  # would otherwise gate the write
        with patch("zd_app.ui.app_shell.dpg.get_value") as get_value:
            result = shell.apply_polling_rate("1000Hz")
        self.assertIsNone(result)
        # Refused before reading any widget or touching the service.
        get_value.assert_not_called()
        self.assertEqual(service.calls, [])

    def test_diagnostics_deadzone_writes_nothing_on_generic_xinput(self) -> None:
        service = _RecordingSettingsService()
        shell = _shell_with_class("generic_xinput", settings_service=service)
        shell._diag_deadzone_hydrated = True  # even pretending hydration happened
        result = shell.apply_diagnostics_deadzone(StickDeadzones(1, 2, 3, 4))
        self.assertIsNone(result)
        self.assertEqual(service.calls, [])

    def test_profile_apply_refuses_before_loading_on_generic_xinput(self) -> None:
        service = _RecordingSettingsService()
        shell = _shell_with_class("generic_xinput", settings_service=service)
        shell._apply_snapshot_to_controller = MagicMock()
        shell.apply_named_wrapper_profile("Some Profile")
        # Refused at the entry: the store was never read and no burst started.
        shell.wrapper_profile_store.load.assert_not_called()
        shell._apply_snapshot_to_controller.assert_not_called()
        self.assertEqual(service.calls, [])

    def test_profile_snapshot_burst_refuses_on_generic_xinput(self) -> None:
        # Defense-in-depth: the actual write-burst chokepoint refuses too, so a
        # caller that skipped the entry gate still cannot write to a non-ZD pad.
        service = _RecordingSettingsService()
        shell = _shell_with_class("generic_xinput", settings_service=service)
        shell._apply_snapshot_to_controller = MagicMock()
        shell._apply_wrapper_profile_snapshot("Some Profile", SimpleNamespace())
        shell._apply_snapshot_to_controller.assert_not_called()
        self.assertEqual(service.calls, [])

    def test_safe_import_apply_refuses_whole_action_on_generic_xinput(self) -> None:
        # Safe Import's "Save + Apply" is a named profile-write surface: it must
        # refuse as a whole on a non-ZD pad — nothing saved, nothing applied.
        service = _RecordingSettingsService()
        shell = _shell_with_class("generic_xinput", settings_service=service)
        shell._apply_snapshot_to_controller = MagicMock()
        shell._safe_import_result = SimpleNamespace(
            profile=SimpleNamespace(snapshot=SimpleNamespace()),
            generated_name="Imported Profile",
            audit=SimpleNamespace(),
            categories={},
        )
        shell._safe_import_selected = set()
        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=False), patch(
            "zd_app.ui.app_shell.dpg.get_value"
        ) as get_value:
            shell.safe_import_apply(apply_to_controller=True)
        get_value.assert_not_called()
        shell.wrapper_profile_store.save.assert_not_called()
        shell._apply_snapshot_to_controller.assert_not_called()
        self.assertEqual(service.calls, [])

    def test_refusal_surfaces_unverified_device_status(self) -> None:
        shell = _shell_with_class("generic_xinput")
        shell._dpg_context_ready = True
        captured: dict[str, str] = {}
        with patch(
            "zd_app.ui.app_shell.dpg.does_item_exist", return_value=True
        ), patch(
            "zd_app.ui.app_shell.dpg.set_value",
            side_effect=lambda tag, value: captured.__setitem__(tag, value),
        ):
            self.assertFalse(shell._hid_available_or_refuse())
        # The honest, localized "read-only on this device" message lands in both
        # status strips — never the busy banner.
        message = i18n.t("apply.device_unverified")
        self.assertIn(message, captured.get("footer_status_text", ""))
        self.assertIn(message, captured.get("settings_v2_status_text", ""))
        self.assertNotIn(i18n.t("apply.busy"), captured.get("footer_status_text", ""))


# ---------------------------------------------------------------------------
# (b) A ZD device keeps full functionality
# ---------------------------------------------------------------------------


class WriteGateAllowsOnZdTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_hid_available_or_refuse_true_on_zd(self) -> None:
        shell = _shell_with_class("zd_ultimate_legend")
        self.assertTrue(shell._hid_available_or_refuse())

    def test_zd_still_busy_gates(self) -> None:
        # The device gate is additive: a ZD that is mid-job still refuses (busy),
        # so the existing busy contract is untouched.
        shell = _shell_with_class("zd_ultimate_legend")
        shell._hid_job_in_flight = True
        self.assertFalse(shell._hid_available_or_refuse())

    def test_per_tab_apply_writes_through_on_zd(self) -> None:
        service = _RecordingSettingsService()
        shell = _shell_with_class("zd_ultimate_legend", settings_service=service)
        shell._polling_rate_hydrated = True
        shell._maybe_capture_before_manual_device_write = lambda **kw: None
        with patch(
            "zd_app.ui.app_shell.dpg.does_item_exist", return_value=False
        ), patch("zd_app.ui.app_shell.dpg.set_value"):
            shell.apply_polling_rate("1000Hz")
        self.assertIn("set_polling_rate", service.calls)

    def test_profile_apply_proceeds_to_load_on_zd(self) -> None:
        service = _RecordingSettingsService()
        shell = _shell_with_class("zd_ultimate_legend", settings_service=service)
        # No device settings on the snapshot -> applies directly (no modal).
        shell.wrapper_profile_store.load.return_value = SimpleNamespace(
            snapshot=SimpleNamespace()
        )
        with patch(
            "zd_app.ui.app_shell.has_device_settings", return_value=False
        ), patch.object(
            type(shell), "_apply_wrapper_profile_snapshot", autospec=True
        ) as snap:
            shell.apply_named_wrapper_profile("Some Profile")
        shell.wrapper_profile_store.load.assert_called_once_with("Some Profile")
        snap.assert_called_once()


# ---------------------------------------------------------------------------
# Restore Points: the restore (a write burst) is gated too
# ---------------------------------------------------------------------------


class RestoreExecutionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def _shell(self, device_class: str):
        rp_service = MagicMock()
        rp_service.restore.return_value = SimpleNamespace(id="rp1")
        shell = _shell_with_class(
            device_class,
            settings_service=MagicMock(),
            restore_point_service=rp_service,
        )
        shell.rebuild_current_screen = MagicMock()
        shell.restore_points_screen_state = rp_screen.RestorePointsScreenState(
            view=rp_screen.VIEW_IN_PROGRESS, selected_rp_id="rp1"
        )
        return shell, rp_service

    def test_restore_refused_on_generic_xinput(self) -> None:
        shell, rp_service = self._shell("generic_xinput")
        rp_screen._execute_restore(shell)
        rp_service.restore.assert_not_called()
        state = shell.restore_points_screen_state
        self.assertEqual(state.view, rp_screen.VIEW_CONFIRM)
        self.assertEqual(state.status_text, i18n.t("apply.device_unverified"))
        self.assertEqual(state.status_kind, "warn")

    def test_restore_executes_on_zd(self) -> None:
        shell, rp_service = self._shell("zd_ultimate_legend")
        rp_screen._execute_restore(shell)
        rp_service.restore.assert_called_once_with("rp1")
        self.assertEqual(shell.restore_points_screen_state.view, rp_screen.VIEW_RESULT)


# ---------------------------------------------------------------------------
# Live Verify: read-only tester runs for any pad; deadzone WRITE card is gated
# ---------------------------------------------------------------------------


class LiveVerifyDeadzoneCardCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def _build(self, device_class: str, service):
        shell = make_shell(settings_service=service)
        shell.device_service.state.device_class = device_class
        shell._xinput_poll_service = _FakeXInputService(_live_snap())
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass
        live_verify.build(shell, "content_region")
        return shell

    def test_generic_xinput_deadzone_card_is_read_only_and_reads_nothing(self) -> None:
        service = _RecordingSettingsService()
        dpg.create_context()
        try:
            shell = self._build("generic_xinput", service)
            # The read-only live tester still built (any XInput pad): its core
            # tags exist regardless of device class.
            self.assertTrue(dpg.does_item_exist(live_verify._test_button_tag("left")))
            self.assertTrue(
                dpg.does_item_exist(
                    live_verify._button_chip_tag(
                        live_verify._BUTTON_CHIP_ORDER[0]
                    )
                )
            )
            # The firmware deadzone read was NEVER attempted on a non-ZD pad.
            self.assertNotIn("get_deadzones", service.calls)
            self.assertFalse(shell._diag_deadzone_hydrated)
            # Status labels the card read-only; sliders are disabled.
            self.assertEqual(
                dpg.get_value(live_verify.DEADZONE_STATUS_TAG),
                i18n.t("diagnostics.live_verify.deadzone.status.unverified_device"),
            )
            self.assertEqual(shell._diag_deadzone_status_key, "unverified_device")
            for side, kind in (("left", "center"), ("right", "outer")):
                cfg = dpg.get_item_configuration(
                    live_verify._deadzone_slider_tag(side, kind)
                )
                self.assertFalse(cfg.get("enabled"))
        finally:
            dpg.destroy_context()

    def test_generic_xinput_deadzone_write_callback_is_inert(self) -> None:
        service = _RecordingSettingsService()
        dpg.create_context()
        try:
            shell = self._build("generic_xinput", service)
            service.calls.clear()
            # A stray slider callback must never write on a non-ZD pad.
            with patch(
                "zd_app.ui.app_shell.dpg.does_item_exist", return_value=True
            ), patch("zd_app.ui.app_shell.dpg.set_value"), patch(
                "zd_app.ui.app_shell.dpg.configure_item"
            ):
                shell.apply_diagnostics_deadzone(StickDeadzones(5, 5, 5, 5))
            self.assertEqual(service.calls, [])
        finally:
            dpg.destroy_context()

    def test_zd_deadzone_card_reads_and_enables(self) -> None:
        service = _RecordingSettingsService()
        dpg.create_context()
        try:
            shell = self._build("zd_ultimate_legend", service)
            # ZD: the card hydrates from a real firmware read and enables.
            self.assertIn("get_deadzones", service.calls)
            self.assertTrue(shell._diag_deadzone_hydrated)
            cfg = dpg.get_item_configuration(
                live_verify._deadzone_slider_tag("left", "center")
            )
            self.assertTrue(cfg.get("enabled"))
        finally:
            dpg.destroy_context()


# ---------------------------------------------------------------------------
# Controller screen: the read-only banner shows only on a non-ZD pad
# ---------------------------------------------------------------------------


class ControllerUnverifiedBannerTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def _banner_show(self, device_class: str) -> bool:
        shell = _shell_with_class("none", settings_service=MagicMock())
        shell.device_service.state.device_class = device_class
        dpg.create_context()
        try:
            with dpg.window():
                controller_screen._render_unverified_device_banner(shell)
            tag = "controller_unverified_device_banner"
            self.assertTrue(dpg.does_item_exist(tag))
            self.assertEqual(
                dpg.get_value(tag),
                i18n.t("controller.unverified_device.banner"),
            )
            return bool(dpg.get_item_configuration(tag).get("show"))
        finally:
            dpg.destroy_context()

    def test_banner_shown_on_generic_xinput(self) -> None:
        self.assertTrue(self._banner_show("generic_xinput"))

    def test_banner_shown_on_no_device(self) -> None:
        self.assertTrue(self._banner_show("none"))

    def test_banner_hidden_on_zd(self) -> None:
        self.assertFalse(self._banner_show("zd_ultimate_legend"))


# ---------------------------------------------------------------------------
# i18n: the new user-facing strings exist (non-empty) in both locales
# ---------------------------------------------------------------------------


class UnverifiedDeviceI18nTests(unittest.TestCase):
    def test_new_keys_present_and_nonempty_in_both_locales(self) -> None:
        locale_dir = Path("zd_app/i18n/locales")
        en = json.loads((locale_dir / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((locale_dir / "zh-CN.json").read_text(encoding="utf-8"))
        for key in (
            "apply.device_unverified",
            "controller.unverified_device.banner",
            "diagnostics.live_verify.deadzone.status.unverified_device",
            "diagnostics.live_verify.deadzone.note_unverified",
        ):
            with self.subTest(key=key):
                self.assertIn(key, en)
                self.assertIn(key, zh)
                self.assertTrue(en[key])
                self.assertTrue(zh[key])


if __name__ == "__main__":
    unittest.main()
