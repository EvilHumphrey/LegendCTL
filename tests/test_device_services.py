"""Hardware-free tests for device polling and summary bridge behavior."""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from zd_app import i18n
from zd_app.models import DeviceState
from zd_app.services import device_service as device_service_module
from zd_app.services.device_service import DeviceService
from zd_app.services.diagnostics_service import DiagnosticsService
from zd_app.services.official_app_summary_service import OfficialAppSummary, OfficialAppSummaryService


PUBLIC_DEVICE_ENTRY = {
    "instance_id": "USB\\VID_413D&PID_2104\\ABC123",
    "description": "ZD Ultimate Legend",
    "status": "Present",
}
PUBLIC_DEVICE_ENTRIES = [PUBLIC_DEVICE_ENTRY]

LOCALIZED_PNPUTIL_OUTPUTS = {
    "ru": """ИД экземпляра устройства: USB\\VID_413D&PID_2104\\ABC123
Описание устройства: ZD Ultimate Legend
Состояние: Запущено
""",
    "de": """Instanz-ID: USB\\VID_413D&PID_2104\\ABC123
Gerätebeschreibung: ZD Ultimate Legend
Status: Gestartet
""",
    "zh": """实例 ID: USB\\VID_413D&PID_2104\\ABC123
设备描述: ZD Ultimate Legend
状态: 已启动
""",
}


def _legacy_english_label_parser_matches_zd(output: str) -> bool:
    """Mirror the removed bug: only the English label key can identify a device."""

    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            current[key.strip().lower()] = value.strip()
    if current:
        entries.append(current)
    return any(
        device_service_module.instance_id_is_allowlisted_zd(entry.get("instance id", ""))
        for entry in entries
    )


class _FakeKernel32:
    def __init__(self) -> None:
        self.last_error = 0

    def GetLastError(self) -> int:
        return self.last_error


class _FakeSetupApi:
    def __init__(
        self,
        kernel32: _FakeKernel32,
        devices_by_enumerator: dict[str | None, list[dict[str, str]]],
    ) -> None:
        self.kernel32 = kernel32
        self.devices_by_enumerator = devices_by_enumerator
        self.enumerators: list[str | None] = []
        self.destroyed_handles: list[str] = []
        self._handles: dict[str, list[dict[str, str]]] = {}

    def SetupDiGetClassDevsW(self, class_guid, enumerator, _hwnd, flags):
        # Enforce the REAL API contract the mocked layer previously hid: a NULL
        # ClassGuid requires DIGCF_ALLCLASSES, else the call fails with
        # ERROR_INVALID_PARAMETER (87) and enumeration sees zero devices. The
        # shipped 2026-07-02 recognition regression (operator's controller
        # unrecognized on EN-Windows) passed the old permissive fake exactly
        # because it ignored flags — keep this fake strict.
        if class_guid is None and not (flags & device_service_module.DIGCF_ALLCLASSES):
            self.kernel32.last_error = 87  # ERROR_INVALID_PARAMETER
            return None  # _setupdi_handle_failed treats None as a failed handle
        self.enumerators.append(enumerator)
        handle = f"info-set-{len(self.enumerators)}"
        self._handles[handle] = list(self.devices_by_enumerator.get(enumerator, []))
        return handle

    def SetupDiEnumDeviceInfo(self, info_set, index, device_info_ptr):
        devices = self._handles[info_set]
        if index >= len(devices):
            self.kernel32.last_error = device_service_module.ERROR_NO_MORE_ITEMS
            return False
        self.kernel32.last_error = 0
        device_info_ptr._obj.DevInst = index
        return True

    def SetupDiGetDeviceInstanceIdW(
        self,
        info_set,
        device_info_ptr,
        instance_id_buffer,
        instance_id_buffer_chars,
        required_size_ptr,
    ):
        instance_id = self._handles[info_set][device_info_ptr._obj.DevInst]["instance_id"]
        required_size_ptr._obj.value = len(instance_id) + 1
        if instance_id_buffer_chars <= len(instance_id):
            return False
        instance_id_buffer.value = instance_id
        return True

    def SetupDiGetDeviceRegistryPropertyW(
        self,
        info_set,
        device_info_ptr,
        _property_id,
        _property_type_ptr,
        property_buffer,
        property_buffer_bytes,
        required_size_ptr,
    ):
        description = self._handles[info_set][device_info_ptr._obj.DevInst].get(
            "description",
            "Unknown device",
        )
        required_bytes = (len(description) + 1) * 2
        required_size_ptr._obj.value = required_bytes
        if property_buffer_bytes < required_bytes:
            return False
        property_buffer.value = description
        return True

    def SetupDiDestroyDeviceInfoList(self, info_set) -> bool:
        self.destroyed_handles.append(info_set)
        return True


class FakeSummaryProbeService(OfficialAppSummaryService):
    def __init__(self, payloads: list[dict | None], clock, cache_window_seconds: float = 2.0):
        super().__init__(timeout_seconds=0.1, cache_window_seconds=cache_window_seconds, clock=clock)
        self.payloads = list(payloads)
        self.probe_calls = 0

    def _run_probe(self) -> dict | None:
        self.probe_calls += 1
        if self.payloads:
            return self.payloads.pop(0)
        return None


class DeviceServicePresenceCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.service = DeviceService(
            clock=lambda: self.now,
            presence_cache_ttl_connected_seconds=10.0,
            presence_cache_ttl_disconnected_seconds=1.0,
        )

    def test_connected_presence_probe_uses_cache_within_ttl(self) -> None:
        with patch(
            "zd_app.services.device_service._enumerate_present_device_entries",
            return_value=PUBLIC_DEVICE_ENTRIES,
        ) as enumerate_mock:
            first = self.service._find_zd_entries()
            self.now = 1.0
            second = self.service._find_zd_entries()

        self.assertEqual(enumerate_mock.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["instance_id"], "USB\\VID_413D&PID_2104\\ABC123")

    def test_force_refresh_bypasses_presence_cache(self) -> None:
        with patch(
            "zd_app.services.device_service._enumerate_present_device_entries",
            return_value=PUBLIC_DEVICE_ENTRIES,
        ) as enumerate_mock:
            self.service._find_zd_entries()
            self.now = 1.0
            self.service._find_zd_entries(force_refresh=True)

        self.assertEqual(enumerate_mock.call_count, 2)

    def test_allow_probe_false_skips_presence_probe_on_cold_cache(self) -> None:
        # The non-blocking UI-tick path must never run the SetupDi probe.
        with patch("zd_app.services.device_service._enumerate_present_device_entries") as enumerate_mock:
            entries = self.service._find_zd_entries(allow_probe=False)

        self.assertEqual(entries, [])
        enumerate_mock.assert_not_called()

    def test_allow_probe_false_returns_stale_cache_without_presence_probe(self) -> None:
        with patch(
            "zd_app.services.device_service._enumerate_present_device_entries",
            return_value=PUBLIC_DEVICE_ENTRIES,
        ) as enumerate_mock:
            warmed = self.service._find_zd_entries()  # warms the cache (1 probe)
            self.now = 100.0  # well past the 10s connected TTL -> stale
            stale = self.service._find_zd_entries(allow_probe=False)

        self.assertEqual(enumerate_mock.call_count, 1)
        self.assertEqual(stale, warmed)
        self.assertEqual(stale[0]["instance_id"], "USB\\VID_413D&PID_2104\\ABC123")

    def test_allow_probe_false_serves_fresh_cache_without_presence_probe(self) -> None:
        with patch(
            "zd_app.services.device_service._enumerate_present_device_entries",
            return_value=PUBLIC_DEVICE_ENTRIES,
        ) as enumerate_mock:
            self.service._find_zd_entries()  # warms the cache (1 probe)
            self.now = 1.0  # within the connected TTL
            fresh = self.service._find_zd_entries(allow_probe=False)

        self.assertEqual(enumerate_mock.call_count, 1)
        self.assertEqual(fresh[0]["instance_id"], "USB\\VID_413D&PID_2104\\ABC123")

    def test_force_probe_overrides_allow_probe_false(self) -> None:
        with patch(
            "zd_app.services.device_service._enumerate_present_device_entries",
            return_value=PUBLIC_DEVICE_ENTRIES,
        ) as enumerate_mock:
            self.service._find_zd_entries()
            self.now = 100.0
            self.service._find_zd_entries(force_refresh=True, allow_probe=False)

        self.assertEqual(enumerate_mock.call_count, 2)


class DeviceServiceLocalizedRecognitionTests(unittest.TestCase):
    def test_legacy_english_label_parser_misses_localized_pnputil_output(self) -> None:
        for locale, output in LOCALIZED_PNPUTIL_OUTPUTS.items():
            with self.subTest(locale=locale):
                self.assertFalse(_legacy_english_label_parser_matches_zd(output))

    def test_find_zd_entries_uses_setupdi_instance_id_not_localized_labels(self) -> None:
        for locale in LOCALIZED_PNPUTIL_OUTPUTS:
            with self.subTest(locale=locale):
                service = DeviceService(clock=lambda: 0.0)
                with patch(
                    "zd_app.services.device_service._enumerate_present_device_entries",
                    return_value=PUBLIC_DEVICE_ENTRIES,
                ):
                    entries = service._find_zd_entries(force_refresh=True)
                self.assertEqual(entries[0]["instance_id"], "USB\\VID_413D&PID_2104\\ABC123")

    def test_find_zd_entries_filters_allowlist_and_sorts_usb_first(self) -> None:
        entries = [
            {
                "instance_id": "BTHENUM\\DEV_VID_413D&PID_2104\\BT123",
                "description": "ZD Ultimate Legend",
                "status": "Present",
            },
            {
                "instance_id": "USB\\VID_045E&PID_028E\\XBOX",
                "description": "Xbox-compatible controller",
                "status": "Present",
            },
            {
                "instance_id": "USB\\VID_413D&PID_2104\\USB123",
                "description": "ZD Ultimate Legend",
                "status": "Present",
            },
        ]
        service = DeviceService(clock=lambda: 0.0)

        with patch(
            "zd_app.services.device_service._enumerate_present_device_entries",
            return_value=entries,
        ):
            matches = service._find_zd_entries(force_refresh=True)

        self.assertEqual(
            [entry["instance_id"] for entry in matches],
            [
                "USB\\VID_413D&PID_2104\\USB123",
                "BTHENUM\\DEV_VID_413D&PID_2104\\BT123",
            ],
        )


class DeviceServiceSetupDiEnumerationTests(unittest.TestCase):
    def _enumerate(
        self,
        devices_by_enumerator: dict[str | None, list[dict[str, str]]],
    ) -> tuple[list[dict[str, str]], _FakeSetupApi]:
        kernel32 = _FakeKernel32()
        setupapi = _FakeSetupApi(kernel32, devices_by_enumerator)
        with patch.object(
            device_service_module._Win32,
            "setupapi",
            return_value=setupapi,
        ), patch.object(
            device_service_module._Win32,
            "kernel32",
            return_value=kernel32,
        ):
            entries = device_service_module._enumerate_present_device_entries()
        return entries, setupapi

    def test_setupdi_usb_enumerator_returns_present_instance_entries(self) -> None:
        entries, setupapi = self._enumerate({"USB": PUBLIC_DEVICE_ENTRIES})

        self.assertEqual(setupapi.enumerators, ["USB"])
        self.assertEqual(setupapi.destroyed_handles, ["info-set-1"])
        self.assertEqual(entries, PUBLIC_DEVICE_ENTRIES)

    def test_setupdi_falls_back_to_all_present_when_usb_scope_misses_zd(self) -> None:
        entries, setupapi = self._enumerate(
            {
                "USB": [
                    {
                        "instance_id": "USB\\VID_045E&PID_028E\\XBOX",
                        "description": "Xbox-compatible controller",
                        "status": "Present",
                    }
                ],
                None: PUBLIC_DEVICE_ENTRIES,
            }
        )

        self.assertEqual(setupapi.enumerators, ["USB", None])
        self.assertEqual(
            [entry["instance_id"] for entry in entries],
            [
                "USB\\VID_045E&PID_028E\\XBOX",
                "USB\\VID_413D&PID_2104\\ABC123",
            ],
        )


class DeviceServicePresencePrimerTests(unittest.TestCase):
    def _warm_predicate(self, service: DeviceService) -> bool:
        return service._has_cached_zd_entries

    def test_start_presence_primer_warms_cache_then_stops(self) -> None:
        service = DeviceService()
        with patch(
            "zd_app.services.device_service._enumerate_present_device_entries",
            return_value=PUBLIC_DEVICE_ENTRIES,
        ):
            service.start_presence_primer(interval_seconds=0.05)
            try:
                deadline = time.monotonic() + 3.0
                while not service._has_cached_zd_entries and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(service._has_cached_zd_entries)
                self.assertEqual(
                    service._cached_zd_entries[0]["instance_id"],
                    "USB\\VID_413D&PID_2104\\ABC123",
                )
            finally:
                service.stop_presence_primer()

        self.assertIsNone(service._presence_primer_thread)

    def test_start_presence_primer_is_idempotent(self) -> None:
        service = DeviceService()
        with patch(
            "zd_app.services.device_service._enumerate_present_device_entries",
            return_value=[],
        ):
            service.start_presence_primer(interval_seconds=0.5)
            try:
                first = service._presence_primer_thread
                service.start_presence_primer(interval_seconds=0.5)
                self.assertIs(service._presence_primer_thread, first)
            finally:
                service.stop_presence_primer()

    def test_stop_presence_primer_is_safe_when_never_started(self) -> None:
        service = DeviceService()
        service.stop_presence_primer()  # must not raise
        self.assertIsNone(service._presence_primer_thread)

    def test_refresh_presence_cache_never_raises(self) -> None:
        service = DeviceService()
        with patch(
            "zd_app.services.device_service._enumerate_present_device_entries",
            side_effect=RuntimeError("boom"),
        ):
            service.refresh_presence_cache()  # swallows the failure


class DeviceServiceRefreshStateTests(unittest.TestCase):
    def _refresh_with_zd_entry(
        self,
        service: DeviceService,
        instance_id: str = "USB\\VID_413D&PID_2104\\ABC123",
    ):
        with patch.object(
            service,
            "_find_zd_entries",
            return_value=[{"instance_id": instance_id}],
        ), patch(
            "zd_app.services.device_service.get_connected_controllers",
            return_value=[],
        ):
            return service.refresh_state()

    def _refresh_with_zd_entry_and_slots(
        self,
        service: DeviceService,
        instance_id: str = "USB\\VID_413D&PID_2104\\ABC123",
        slots: list[int] | None = None,
        **refresh_kwargs,
    ):
        slots = list(slots or [])
        with patch.object(
            service,
            "_find_zd_entries",
            return_value=[{"instance_id": instance_id}],
        ), patch(
            "zd_app.services.device_service.get_connected_controllers",
            return_value=slots,
        ), patch(
            "zd_app.services.device_service.describe_battery_level",
            return_value="Wired",
        ):
            return service.refresh_state(**refresh_kwargs)

    def _refresh_with_no_device(self, service: DeviceService):
        with patch.object(service, "_find_zd_entries", return_value=[]), patch(
            "zd_app.services.device_service.get_connected_controllers",
            return_value=[],
        ):
            return service.refresh_state()

    def test_refresh_state_resets_sync_status_on_reconnect_after_previous_read(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        service.state.connection_state = "no_device"
        service.state.last_read_time = "2026-05-04T16:25:26+00:00"
        service.state.sync_status = "Disconnected"

        state = self._refresh_with_zd_entry(service)

        self.assertEqual(state.connection_state, "connected")
        self.assertEqual(state.sync_status, "Connected")

    def test_refresh_state_preserves_sync_status_during_steady_connected(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        service.state.connection_state = "connected"
        service.state.last_read_time = "2026-05-04T16:25:26+00:00"
        service.state.sync_status = "Ready"

        state = self._refresh_with_zd_entry(service)

        self.assertEqual(state.connection_state, "connected")
        self.assertEqual(state.sync_status, "Ready")

    def test_refresh_state_preserves_reading_state_during_steady_connected(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        service.state.connection_state = "connected"
        service.state.last_read_time = "2026-05-04T16:25:26+00:00"
        service.state.sync_status = "Reading"

        state = self._refresh_with_zd_entry(service)

        self.assertEqual(state.connection_state, "connected")
        self.assertEqual(state.sync_status, "Reading")

    def test_disconnect_keeps_retained_read_values_but_marks_stale(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        service.state.connection_state = "connected"
        service.state.stable_identifier = "USB\\VID_413D&PID_2104\\ABC123"
        service._last_connected_identifier = service.state.stable_identifier
        service.state.firmware_version = "1.24"
        service.state.active_onboard_profile = 3
        service.state.last_read_time = "2026-05-04T16:25:26+00:00"
        service.state.data_freshness = "fresh"
        service.state.summary_sources["firmware"] = "official_app_ui"
        service.state.summary_sources["active_profile"] = "protocol"

        state = self._refresh_with_no_device(service)

        self.assertEqual(state.connection_state, "no_device")
        self.assertEqual(state.firmware_version, "1.24")
        self.assertEqual(state.active_onboard_profile, 3)
        self.assertEqual(state.last_read_time, "2026-05-04T16:25:26+00:00")
        self.assertEqual(state.data_freshness, "stale")
        self.assertEqual(state.summary_sources["firmware"], "official_app_ui")
        self.assertEqual(state.summary_sources["active_profile"], "protocol")

    def test_slot_drop_while_same_identity_connected_does_not_log_detected(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        instance_id = "USB\\VID_413D&PID_2104\\ABC123"
        self._refresh_with_zd_entry_and_slots(service, instance_id, [0])
        service.event_log.clear()

        self._refresh_with_zd_entry_and_slots(
            service,
            instance_id,
            [],
            background=True,
            allow_probe=False,
        )

        self.assertEqual(service.recent_events(5), [])

    def test_replug_transition_logs_detected_once(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        instance_id = "USB\\VID_413D&PID_2104\\ABC123"
        self._refresh_with_zd_entry_and_slots(service, instance_id, [0])
        self._refresh_with_no_device(service)
        service.event_log.clear()

        self._refresh_with_zd_entry_and_slots(service, instance_id, [0])

        events = service.recent_events(5)
        self.assertEqual(len(events), 1)
        self.assertIn("Detected ZD Ultimate Legend on USB.", events[0])

    def test_connected_identity_change_logs_detected_once(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        self._refresh_with_zd_entry_and_slots(
            service,
            "USB\\VID_413D&PID_2104\\OLD",
            [0],
        )
        service.event_log.clear()

        self._refresh_with_zd_entry_and_slots(
            service,
            "USB\\VID_413D&PID_2104\\NEW",
            [0],
        )

        events = service.recent_events(5)
        self.assertEqual(len(events), 1)
        self.assertIn("Detected ZD Ultimate Legend on USB.", events[0])

    def test_identity_change_clears_retained_read_state(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        service.state.connection_state = "connected"
        service.state.stable_identifier = "USB\\VID_413D&PID_2104\\OLD"
        service._last_connected_identifier = service.state.stable_identifier
        service.state.firmware_version = "1.24"
        service.state.battery_level = "Full"
        service.state.sleep_setting = "10m"
        service.state.active_onboard_profile = 4
        service.state.last_read_time = "2026-05-04T16:25:26+00:00"
        service.state.data_freshness = "fresh"
        service.state.sync_status = "Ready"
        service.state.summary_sources.update(
            {
                "battery": "official_app_ui",
                "firmware": "official_app_ui",
                "active_profile": "protocol",
                "sleep": "official_app_ui",
            }
        )

        state = self._refresh_with_zd_entry(
            service,
            "USB\\VID_413D&PID_2104\\NEW",
        )

        self.assertEqual(state.stable_identifier, "USB\\VID_413D&PID_2104\\NEW")
        self.assertEqual(state.firmware_version, "Unknown")
        self.assertEqual(state.battery_level, "Unknown")
        self.assertEqual(state.sleep_setting, "Unknown")
        self.assertEqual(state.active_onboard_profile, 1)
        self.assertIsNone(state.last_read_time)
        self.assertEqual(state.data_freshness, "never_read")
        self.assertEqual(state.sync_status, "Connected")
        for field_name in ("battery", "firmware", "active_profile", "sleep"):
            self.assertEqual(state.summary_sources[field_name], "unknown")

    def test_reconnect_same_identity_keeps_retained_read_state(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        service.state.connection_state = "no_device"
        service.state.stable_identifier = "unknown"
        service._last_connected_identifier = "USB\\VID_413D&PID_2104\\ABC123"
        service.state.firmware_version = "1.24"
        service.state.active_onboard_profile = 2
        service.state.last_read_time = "2026-05-04T16:25:26+00:00"
        service.state.data_freshness = "stale"
        service.state.summary_sources["firmware"] = "official_app_ui"
        service.state.summary_sources["active_profile"] = "protocol"

        state = self._refresh_with_zd_entry(
            service,
            "USB\\VID_413D&PID_2104\\ABC123",
        )

        self.assertEqual(state.connection_state, "connected")
        self.assertEqual(state.firmware_version, "1.24")
        self.assertEqual(state.active_onboard_profile, 2)
        self.assertEqual(state.last_read_time, "2026-05-04T16:25:26+00:00")
        self.assertEqual(state.data_freshness, "stale")
        self.assertEqual(state.summary_sources["firmware"], "official_app_ui")
        self.assertEqual(state.summary_sources["active_profile"], "protocol")


class DeviceServiceEventLogTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def tearDown(self) -> None:
        i18n.set_locale("en")

    def test_i18n_log_entry_re_renders_on_locale_change(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        service.log_i18n_event(
            "log.setting.updated",
            label_key="setting.label.language",
        )

        en_event = service.recent_events(1)[0]
        i18n.set_locale("zh-CN")
        zh_event = service.recent_events(1)[0]

        self.assertIn("Updated language setting.", en_event)
        self.assertIn("语言", zh_event)
        self.assertNotEqual(en_event, zh_event)

    def test_raw_string_log_entry_renders_unchanged(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        service.log_event("Legacy raw event.")

        i18n.set_locale("zh-CN")

        self.assertTrue(service.recent_events(1)[0].endswith("Legacy raw event."))

    def test_recent_events_scrubs_paths_before_display_and_clipboard_sinks(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        service.log_i18n_event(
            "log.diagnostics.exported_bundle",
            path=r"C:\Users\Jane Doe\diagnostics\bundle.json",
        )

        event = service.recent_events(1)[0]

        self.assertNotIn("Jane", event)
        self.assertNotIn("Doe", event)
        self.assertNotIn(r"C:\Users", event)
        self.assertIn("bundle.json", event)

    def test_i18n_log_entry_preserves_format_args(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        service.log_i18n_event(
            "log.controller.detected",
            product_name="ZD Ultimate Legend",
            connection_mode="USB",
        )

        i18n.set_locale("zh-CN")
        event = service.recent_events(1)[0]

        self.assertIn("ZD Ultimate Legend", event)
        self.assertIn("USB", event)
        self.assertIn("检测到", event)

    def test_i18n_log_entry_retranslates_label_key_args(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        service.log_i18n_event(
            "log.setting.updated",
            label_key="setting.label.developer_panels_visible",
        )

        en_event = service.recent_events(1)[0]
        i18n.set_locale("zh-CN")
        zh_event = service.recent_events(1)[0]

        self.assertIn("developer panels visibility", en_event)
        self.assertIn("开发者面板可见性", zh_event)
        self.assertNotEqual(en_event, zh_event)


class DiagnosticsServiceEventLogTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def tearDown(self) -> None:
        i18n.set_locale("en")

    def test_diagnostics_divider_retranslates(self) -> None:
        service = DiagnosticsService()
        service.log_i18n_event("log.diagnostics.divider")

        en_event = service.build_snapshot(DeviceState(), None, None).event_log[0]
        i18n.set_locale("zh-CN")
        zh_event = service.build_snapshot(DeviceState(), None, None).event_log[0]

        self.assertIn("-- Live Diagnostics --", en_event)
        self.assertIn("-- 实时诊断 --", zh_event)

    def test_diagnostics_legacy_raw_string_entries_unchanged(self) -> None:
        service = DiagnosticsService()
        service.log_event("Legacy diagnostics event.")

        i18n.set_locale("zh-CN")

        self.assertTrue(
            service.build_snapshot(DeviceState(), None, None).event_log[0].endswith(
                "Legacy diagnostics event."
            )
        )


class DeviceServiceSummaryPrecedenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = DeviceService(clock=lambda: 0.0)

    def test_unknown_summary_source_label_falls_back_to_unknown_and_logs_warning(self) -> None:
        self.service.state.summary_sources["active_profile"] = "bluetooth_bridge"

        with self.assertLogs("zd_app.services.device_service", level="WARNING") as captured:
            label = self.service.summary_source_label_for("active_profile")

        self.assertEqual(label, "Unknown")
        self.assertIn("Unmapped summary source label requested: bluetooth_bridge", captured.output[0])

    def test_summary_source_summary_uses_unknown_fallback_for_unmapped_source(self) -> None:
        self.service.state.summary_sources["active_profile"] = "bluetooth_bridge"

        with self.assertLogs("zd_app.services.device_service", level="WARNING") as captured:
            summary = self.service.summary_source_summary()

        self.assertEqual(summary, "Unknown (source for: Config)")
        self.assertIn("Unmapped summary source label requested: bluetooth_bridge", captured.output[0])

    def test_summary_source_summary_disambiguates_single_source_partial_fields(self) -> None:
        self.service.state.summary_sources["battery"] = "xinput"

        summary = self.service.summary_source_summary()

        self.assertEqual(summary, "XInput (source for: Battery)")
        self.assertNotEqual(summary, "XInput (Battery)")

    def test_summary_source_label_localizes_in_zh_cn(self) -> None:
        try:
            i18n.set_locale("zh-CN")
            expected = {
                "unknown": "未验证",
                "xinput": "XInput",
                "official_app_ui": "官方应用界面",
                "protocol": "控制器协议",
            }

            for source, label in expected.items():
                with self.subTest(source=source):
                    self.service.state.summary_sources["active_profile"] = source
                    self.assertEqual(self.service.summary_source_label_for("active_profile"), label)
        finally:
            i18n.set_locale("en")

    def test_summary_field_label_localizes_in_zh_cn(self) -> None:
        try:
            i18n.set_locale("zh-CN")
            self.service.state.summary_sources.update(
                {
                    "battery": "official_app_ui",
                    "firmware": "official_app_ui",
                    "active_profile": "official_app_ui",
                    "sleep": "official_app_ui",
                }
            )

            self.assertEqual(
                self.service.summary_fields_from_source("official_app_ui"),
                ["电池", "固件", "配置", "休眠"],
            )
        finally:
            i18n.set_locale("en")

    def test_official_summary_sets_active_profile_when_no_protocol_owner_exists(self) -> None:
        retained = self.service._apply_official_app_summary(
            OfficialAppSummary(
                battery_level="100%",
                firmware_version="1.13",
                active_onboard_profile=2,
                sleep_setting="Never Sleep",
                window_title="Controller Settings",
            )
        )

        self.assertFalse(retained)
        self.assertEqual(self.service.state.active_onboard_profile, 2)
        self.assertEqual(self.service.state.summary_sources["active_profile"], "official_app_ui")

    def test_official_summary_does_not_demote_protocol_owned_active_profile(self) -> None:
        self.service.record_protocol_active_profile(3)

        retained = self.service._apply_official_app_summary(
            OfficialAppSummary(
                battery_level="100%",
                firmware_version="1.13",
                active_onboard_profile=1,
                sleep_setting="Never Sleep",
                window_title="Controller Settings",
            )
        )

        self.assertTrue(retained)
        self.assertEqual(self.service.state.active_onboard_profile, 3)
        self.assertEqual(self.service.state.summary_sources["active_profile"], "protocol")
        self.assertEqual(self.service.state.firmware_version, "1.13")
        self.assertEqual(self.service.state.summary_sources["firmware"], "official_app_ui")
        self.assertEqual(self.service.state.sleep_setting, "Never Sleep")
        self.assertEqual(self.service.state.summary_sources["sleep"], "official_app_ui")

    def test_read_device_state_keeps_protocol_owned_active_profile_during_summary_bridge_refresh(self) -> None:
        self.service.record_protocol_active_profile(3)
        self.service.official_app_summary_service = FakeSummaryProbeService(
            [
                {
                    "window_title": "Controller Settings",
                    "names": [
                        "Battery: 100%",
                        "Version: 1.13",
                        "Config: 1",
                        "Sleep: Never Sleep",
                    ],
                }
            ],
            clock=lambda: 0.0,
        )

        with patch(
            "zd_app.services.device_service._enumerate_present_device_entries",
            return_value=PUBLIC_DEVICE_ENTRIES,
        ), patch(
            "zd_app.services.device_service.get_connected_controllers",
            return_value=[0],
        ), patch(
            "zd_app.services.device_service.describe_battery_level",
            return_value="Wired",
        ):
            state = self.service.read_device_state()

        self.assertEqual(state.active_onboard_profile, 3)
        self.assertEqual(state.summary_sources["active_profile"], "protocol")
        self.assertEqual(state.battery_level, "100%")
        self.assertEqual(state.summary_sources["battery"], "official_app_ui")
        self.assertIn("Protocol-owned active config was retained.", self.service.recent_events(1)[0])

    def test_find_zd_entries_uses_setupdi_enumeration(self) -> None:
        with patch(
            "zd_app.services.device_service._enumerate_present_device_entries",
            return_value=PUBLIC_DEVICE_ENTRIES,
        ) as enumerate_mock:
            entries = self.service._find_zd_entries(force_refresh=True)

        self.assertEqual(entries[0]["instance_id"], "USB\\VID_413D&PID_2104\\ABC123")
        enumerate_mock.assert_called_once()


class DeviceValueLocalizationTests(unittest.TestCase):
    """Coverage for the firmware/battery value-string formatters.
    Canonical values stay Latin in DeviceState; the
    formatter methods route them through locale-aware keys for display."""

    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def tearDown(self) -> None:
        i18n.set_locale("en")

    def test_format_firmware_unknown_localizes_in_zh_cn(self) -> None:
        service = DeviceService()
        service.state.firmware_version = "Unknown"
        i18n.set_locale("zh-CN")
        self.assertEqual(service.format_firmware_version(), "未知")

    def test_format_firmware_known_passes_through(self) -> None:
        service = DeviceService()
        service.state.firmware_version = "1.18"
        i18n.set_locale("zh-CN")
        # Numeric/identifier versions are not localized — only the sentinel
        # "Unknown" routes through the locale.
        self.assertEqual(service.format_firmware_version(), "1.18")

    def test_format_firmware_empty_falls_back_to_unknown(self) -> None:
        service = DeviceService()
        service.state.firmware_version = ""
        i18n.set_locale("en")
        self.assertEqual(service.format_firmware_version(), "Unknown")

    def test_format_battery_wired_localizes_in_zh_cn(self) -> None:
        service = DeviceService()
        service.state.battery_level = "Wired"
        i18n.set_locale("zh-CN")
        self.assertEqual(service.format_battery_level(), "有线")

    def test_format_battery_unknown_localizes_in_zh_cn(self) -> None:
        service = DeviceService()
        service.state.battery_level = "Unknown"
        i18n.set_locale("zh-CN")
        self.assertEqual(service.format_battery_level(), "未知")

    def test_format_battery_levels_localize_in_zh_cn(self) -> None:
        service = DeviceService()
        i18n.set_locale("zh-CN")
        for canonical, expected_zh in (
            ("Empty", "无电量"),
            ("Low", "低"),
            ("Medium", "中"),
            ("Full", "满"),
        ):
            with self.subTest(canonical=canonical):
                service.state.battery_level = canonical
                self.assertEqual(service.format_battery_level(), expected_zh)

    def test_format_battery_levels_in_en(self) -> None:
        service = DeviceService()
        for canonical in ("Unknown", "Wired", "Empty", "Low", "Medium", "Full"):
            with self.subTest(canonical=canonical):
                service.state.battery_level = canonical
                self.assertEqual(service.format_battery_level(), canonical)

    def test_format_battery_unmapped_passes_through(self) -> None:
        # Forward-compat: a future XInput SDK could add a state we haven't
        # mapped yet. Pass through the canonical value verbatim instead of
        # rendering as a missing-key tombstone.
        service = DeviceService()
        service.state.battery_level = "Charging"  # not in BATTERY_LEVEL_KEY_FOR
        self.assertEqual(service.format_battery_level(), "Charging")

    def test_state_battery_level_stays_canonical_after_format(self) -> None:
        # The formatter must not mutate state.battery_level — discriminator
        # paths (summary_sources branching, etc.) read the canonical value.
        service = DeviceService()
        service.state.battery_level = "Wired"
        i18n.set_locale("zh-CN")
        _ = service.format_battery_level()
        self.assertEqual(service.state.battery_level, "Wired")


class LogI18nEventRetranslationTests(unittest.TestCase):
    """The ~15 high-frequency emit sites use the
    LogEntry pattern. Tests assert the migrated entries retranslate when
    the locale switches; covering 2-3 representative sites is enough to
    trust the LogEntry machinery for the rest (it's the same ``t()`` call
    underneath)."""

    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def tearDown(self) -> None:
        i18n.set_locale("en")

    def test_draft_saved_locally_retranslates_on_locale_switch(self) -> None:
        service = DeviceService()
        service.log_i18n_event("log.draft.saved_locally", name="Apex Tuned")

        i18n.set_locale("en")
        en_msgs = service.recent_events(1)
        self.assertIn("Apex Tuned", en_msgs[0])
        self.assertIn("Saved config draft locally", en_msgs[0])

        i18n.set_locale("zh-CN")
        zh_msgs = service.recent_events(1)
        self.assertIn("Apex Tuned", zh_msgs[0])
        self.assertIn("已将配置草稿保存到本地", zh_msgs[0])

    def test_config_imported_retranslates_on_locale_switch(self) -> None:
        service = DeviceService()
        service.log_i18n_event("log.config.imported", name="From Disk Config")

        i18n.set_locale("en")
        self.assertIn("Imported saved PC config", service.recent_events(1)[0])

        i18n.set_locale("zh-CN")
        self.assertIn("已导入保存的 PC 配置", service.recent_events(1)[0])

    def test_no_selection_blocked_retranslates_on_locale_switch(self) -> None:
        service = DeviceService()
        service.log_i18n_event("log.config.no_selection_blocked")

        i18n.set_locale("en")
        self.assertIn("Select a saved PC config first", service.recent_events(1)[0])

        i18n.set_locale("zh-CN")
        self.assertIn("请先选择一个已保存的 PC 配置", service.recent_events(1)[0])

    def test_settings_restored_defaults_retranslates_on_locale_switch(self) -> None:
        service = DeviceService()
        service.log_i18n_event("log.settings.restored_defaults")

        i18n.set_locale("en")
        self.assertIn("Restored app defaults", service.recent_events(1)[0])

        i18n.set_locale("zh-CN")
        self.assertIn("已恢复应用默认值", service.recent_events(1)[0])


class OfficialAppSummaryServiceCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.payload = {
            "window_title": "Controller Settings",
            "names": [
                "Battery: 100%",
                "Version: 1.13",
                "Config: 2",
                "Sleep: Never Sleep",
            ],
        }

    def test_recent_probe_result_is_reused_within_cache_window(self) -> None:
        service = FakeSummaryProbeService([self.payload], clock=lambda: self.now, cache_window_seconds=2.0)

        first = service.read_summary()
        self.now = 1.0
        second = service.read_summary()

        self.assertEqual(service.probe_calls, 1)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.active_onboard_profile, 2)
        self.assertEqual(second.active_onboard_profile, 2)

    def test_force_refresh_bypasses_summary_cache(self) -> None:
        service = FakeSummaryProbeService([self.payload, self.payload], clock=lambda: self.now, cache_window_seconds=2.0)

        service.read_summary()
        self.now = 1.0
        service.read_summary(force_refresh=True)

        self.assertEqual(service.probe_calls, 2)

    def test_run_probe_uses_silent_subprocess(self) -> None:
        service = OfficialAppSummaryService(timeout_seconds=0.1)
        result = SimpleNamespace(stdout='{"window_title":"Controller Settings","names":["Battery: 100%"]}')

        with patch("zd_app.services.official_app_summary_service.silent_run", return_value=result) as run_mock:
            payload = service._run_probe()

        self.assertEqual(payload["window_title"], "Controller Settings")
        run_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
