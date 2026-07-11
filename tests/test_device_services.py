"""Hardware-free tests for device polling and summary bridge behavior."""

from __future__ import annotations

import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from zd_app import i18n
from zd_app.models import DeviceState
from zd_app.services import device_service as device_service_module
from zd_app.services.device_service import DeviceService
from zd_app.services.diagnostics_service import DiagnosticsService
from zd_app.services.model_fingerprint import InterfaceInventory, ModelFingerprint
from zd_app.services.official_app_summary_service import OfficialAppSummary, OfficialAppSummaryService
from zd_app.services.trust_matrix import (
    INFERRED,
    VERIFIED,
    TrustMatrixSignals,
    build_trust_matrix,
)


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


class _ImmediateThread:
    def __init__(self, *, target, args=(), kwargs=None, name=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.name = name
        self.daemon = daemon
        self._alive = False

    def start(self) -> None:
        self._alive = True
        try:
            self._target(*self._args, **self._kwargs)
        finally:
            self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    def join(self, timeout=None) -> None:
        return None


class _CapturedThread:
    instances = []

    def __init__(self, *, target, args=(), kwargs=None, name=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.name = name
        self.daemon = daemon
        self.started = False
        _CapturedThread.instances.append(self)

    def start(self) -> None:
        self.started = True

    def run(self) -> None:
        self._target(*self._args, **self._kwargs)

    def is_alive(self) -> bool:
        return False

    def join(self, timeout=None) -> None:
        return None


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

    def test_summary_freshness_clears_on_disconnect_until_a_new_live_source_arrives(self) -> None:
        # Audit26 HO-F3: retained summary values survive reconnect, but their
        # current-connection qualifier cannot. A fresh scrape/read restores it.
        service = DeviceService(clock=lambda: 0.0)
        self._refresh_with_zd_entry(service)
        service._apply_official_app_summary(
            OfficialAppSummary(firmware_version="1.24", active_onboard_profile=2)
        )
        self.assertTrue(service.summary_field_refreshed_this_connection("firmware"))
        self.assertTrue(
            service.summary_field_refreshed_this_connection("active_profile")
        )

        self._refresh_with_no_device(service)
        self.assertFalse(service.summary_field_refreshed_this_connection("firmware"))
        self.assertFalse(
            service.summary_field_refreshed_this_connection("active_profile")
        )

        self._refresh_with_zd_entry(service)  # same unit, no fresh source yet
        self.assertFalse(service.summary_field_refreshed_this_connection("firmware"))
        self.assertFalse(
            service.summary_field_refreshed_this_connection("active_profile")
        )

        service._apply_official_app_summary(
            OfficialAppSummary(firmware_version="1.25", active_onboard_profile=3)
        )
        self.assertTrue(service.summary_field_refreshed_this_connection("firmware"))
        self.assertTrue(
            service.summary_field_refreshed_this_connection("active_profile")
        )

    def test_identity_change_clears_summary_freshness(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        self._refresh_with_zd_entry(service, "USB\\VID_413D&PID_2104\\OLD")
        service._apply_official_app_summary(
            OfficialAppSummary(firmware_version="1.24", active_onboard_profile=2)
        )

        self._refresh_with_zd_entry(service, "USB\\VID_413D&PID_2104\\NEW")

        self.assertFalse(service.summary_field_refreshed_this_connection("firmware"))
        self.assertFalse(
            service.summary_field_refreshed_this_connection("active_profile")
        )

    def test_official_summary_while_disconnected_does_not_claim_connection_freshness(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        service.state.connection_state = "no_device"

        service._apply_official_app_summary(
            OfficialAppSummary(firmware_version="1.24", active_onboard_profile=2)
        )

        self.assertFalse(service.summary_field_refreshed_this_connection("firmware"))
        self.assertFalse(
            service.summary_field_refreshed_this_connection("active_profile")
        )

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

    def test_model_fingerprint_runs_after_recognition_and_logs_i18n_info(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        fingerprint = ModelFingerprint(
            vid=0x413D,
            pid=0x2104,
            version_number=0x0124,
            product_string="ZD Ultimate Legend",
            manufacturer_string="ZD",
            usage_page=0xFF00,
            usage=0x0001,
            input_report_len=64,
            output_report_len=65,
            feature_report_len=17,
            button_caps_count=10,
            value_caps_count=6,
            interface_inventory=InterfaceInventory(count=2, mi_indices=(0, 2)),
        )
        entries = [
            {
                "instance_id": r"USB\VID_413D&PID_2104&MI_02\ABC123",
                "description": "ZD Ultimate Legend",
                "status": "Present",
            }
        ]

        with patch.object(
            service,
            "_find_zd_entries",
            return_value=entries,
        ), patch(
            "zd_app.services.device_service.get_connected_controllers",
            return_value=[],
        ), patch(
            "zd_app.services.device_service.collect_model_fingerprint",
            return_value=fingerprint,
        ) as collect_mock, patch(
            "zd_app.services.device_service.threading.Thread",
            _ImmediateThread,
        ), self.assertLogs("zd_app.services.device_service", level="INFO") as logs:
            state = service.refresh_state()

        self.assertEqual(state.connection_state, "connected")
        self.assertTrue(state.write_supported)
        self.assertIs(state.model_fingerprint, fingerprint)
        collect_mock.assert_called_once_with(pnp_entries=entries)
        events = service.recent_events(3)
        self.assertIn(fingerprint.short_digest or "", events[0])
        self.assertIn("product string: ZD Ultimate Legend", events[0])
        self.assertEqual(len(logs.records), 1)
        self.assertIn(fingerprint.short_digest or "", logs.output[0])

    def test_model_fingerprint_failure_never_blocks_connected_state(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        entries = [
            {
                "instance_id": r"USB\VID_413D&PID_2104&MI_02\ABC123",
                "description": "ZD Ultimate Legend",
                "status": "Present",
            }
        ]

        with patch.object(
            service,
            "_find_zd_entries",
            return_value=entries,
        ), patch(
            "zd_app.services.device_service.get_connected_controllers",
            return_value=[],
        ), patch(
            "zd_app.services.device_service.collect_model_fingerprint",
            side_effect=RuntimeError("boom"),
        ), patch(
            "zd_app.services.device_service.threading.Thread",
            _ImmediateThread,
        ):
            state = service.refresh_state()

        self.assertEqual(state.connection_state, "connected")
        self.assertTrue(state.write_supported)
        self.assertIsNone(getattr(state, "model_fingerprint", None))

    def test_stale_model_fingerprint_worker_cannot_overwrite_newer_request(self) -> None:
        service = DeviceService(clock=lambda: 0.0)
        stable_identifier = r"USB\VID_413D&PID_2104&MI_02\SAME"
        old_entries = [
            {
                "instance_id": stable_identifier,
                "description": "ZD Ultimate Legend",
                "status": "Present",
            },
            {
                "instance_id": r"HID\VID_413D&PID_2104&MI_00\OLD",
                "description": "ZD Ultimate Legend",
                "status": "Present",
            },
        ]
        new_entries = [
            {
                "instance_id": stable_identifier,
                "description": "ZD Ultimate Legend",
                "status": "Present",
            },
            {
                "instance_id": r"HID\VID_413D&PID_2104&MI_03\NEW",
                "description": "ZD Ultimate Legend",
                "status": "Present",
            },
        ]
        old_fingerprint = ModelFingerprint(
            product_string="old",
            interface_inventory=InterfaceInventory(count=2, mi_indices=(0, 2)),
        )
        new_fingerprint = ModelFingerprint(
            product_string="new",
            interface_inventory=InterfaceInventory(count=2, mi_indices=(2, 3)),
        )

        def fake_collect(*, pnp_entries):
            ids = "\n".join(entry["instance_id"] for entry in pnp_entries)
            return old_fingerprint if "OLD" in ids else new_fingerprint

        service.state.stable_identifier = stable_identifier
        service.state.device_class = "zd_ultimate_legend"
        _CapturedThread.instances = []
        with patch(
            "zd_app.services.device_service.collect_model_fingerprint",
            side_effect=fake_collect,
        ), patch(
            "zd_app.services.device_service.threading.Thread",
            _CapturedThread,
        ):
            service._schedule_model_fingerprint_collection(stable_identifier, old_entries)
            service._schedule_model_fingerprint_collection(stable_identifier, new_entries)

            self.assertEqual(len(_CapturedThread.instances), 2)
            self.assertTrue(all(thread.daemon for thread in _CapturedThread.instances))
            self.assertTrue(all(thread.name == "zd-model-fingerprint" for thread in _CapturedThread.instances))

            _CapturedThread.instances[1].run()
            self.assertIs(service.state.model_fingerprint, new_fingerprint)

            _CapturedThread.instances[0].run()
            self.assertIs(service.state.model_fingerprint, new_fingerprint)

    def test_inventory_only_fingerprint_recollects_then_short_circuits_on_complete(self) -> None:
        # Fix A: an inventory-only result (vid None — first-connect HID-interface
        # lag, or F2's ambiguous-path abstain) must NOT be cached as final. It
        # re-collects on the next refresh; only a COMPLETE result (vid populated)
        # short-circuits. A repeated same-digest collection must not re-log.
        service = DeviceService(clock=lambda: 0.0)
        stable_identifier = r"USB\VID_413D&PID_2104&MI_02\SAME"
        entries = [
            {
                "instance_id": stable_identifier,
                "description": "ZD Ultimate Legend",
                "status": "Present",
            },
            {
                "instance_id": r"HID\VID_413D&PID_2104&MI_00\SAME",
                "description": "ZD Ultimate Legend",
                "status": "Present",
            },
        ]
        inventory_only = ModelFingerprint(
            interface_inventory=InterfaceInventory(count=2, mi_indices=(0, 2)),
        )
        complete = ModelFingerprint(
            vid=0x413D,
            pid=0x2104,
            interface_inventory=InterfaceInventory(count=2, mi_indices=(0, 2)),
        )
        results = [inventory_only, inventory_only, complete]

        def fake_collect(*, pnp_entries):
            return results.pop(0)

        service.state.stable_identifier = stable_identifier
        service.state.device_class = "zd_ultimate_legend"
        _CapturedThread.instances = []
        with patch(
            "zd_app.services.device_service.collect_model_fingerprint",
            side_effect=fake_collect,
        ), patch(
            "zd_app.services.device_service.threading.Thread",
            _CapturedThread,
        ), self.assertLogs("zd_app.services.device_service", level="INFO") as logs:
            # 1st collection caches an inventory-only fingerprint (vid None).
            service._schedule_model_fingerprint_collection(stable_identifier, entries)
            self.assertEqual(len(_CapturedThread.instances), 1)
            _CapturedThread.instances[0].run()
            self.assertIs(service.state.model_fingerprint, inventory_only)
            self.assertIsNone(service.state.model_fingerprint.vid)

            # Same request key but incomplete -> MUST re-collect (not short-circuit).
            service._schedule_model_fingerprint_collection(stable_identifier, entries)
            self.assertEqual(len(_CapturedThread.instances), 2)
            _CapturedThread.instances[1].run()
            self.assertIs(service.state.model_fingerprint, inventory_only)

            # Third collection returns a COMPLETE fingerprint.
            service._schedule_model_fingerprint_collection(stable_identifier, entries)
            self.assertEqual(len(_CapturedThread.instances), 3)
            _CapturedThread.instances[2].run()
            self.assertIs(service.state.model_fingerprint, complete)
            self.assertEqual(service.state.model_fingerprint.vid, 0x413D)

            # Complete + same request key -> short-circuit (no new collection).
            service._schedule_model_fingerprint_collection(stable_identifier, entries)
            self.assertEqual(len(_CapturedThread.instances), 3)

        # Log dedup: the repeated same-digest inventory-only collection logged
        # once; the complete fingerprint logged once. Two records total.
        self.assertEqual(len(logs.records), 2)

    def test_refresh_state_dedups_fingerprint_collection_per_identity(self) -> None:
        # G1: two identical refresh_state ticks collect once (same request key +
        # complete result short-circuits); an identity change re-collects.
        service = DeviceService(clock=lambda: 0.0)
        entries_a = [
            {
                "instance_id": r"USB\VID_413D&PID_2104&MI_02\UNIT_A",
                "description": "ZD Ultimate Legend",
                "status": "Present",
            }
        ]
        entries_b = [
            {
                "instance_id": r"USB\VID_413D&PID_2104&MI_02\UNIT_B",
                "description": "ZD Ultimate Legend",
                "status": "Present",
            }
        ]
        fingerprint_a = ModelFingerprint(
            vid=0x413D,
            pid=0x2104,
            interface_inventory=InterfaceInventory(count=1, mi_indices=(2,)),
        )
        fingerprint_b = ModelFingerprint(
            vid=0x413D,
            pid=0x2104,
            version_number=0x0125,
            interface_inventory=InterfaceInventory(count=1, mi_indices=(2,)),
        )

        def fake_collect(*, pnp_entries):
            ids = "".join(entry["instance_id"] for entry in pnp_entries)
            return fingerprint_b if "UNIT_B" in ids else fingerprint_a

        with patch.object(
            service,
            "_find_zd_entries",
            side_effect=[entries_a, entries_a, entries_b],
        ), patch(
            "zd_app.services.device_service.get_connected_controllers",
            return_value=[],
        ), patch(
            "zd_app.services.device_service.collect_model_fingerprint",
            side_effect=fake_collect,
        ) as collect_mock, patch(
            "zd_app.services.device_service.threading.Thread",
            _ImmediateThread,
        ):
            service.refresh_state()  # unit A -> collect #1
            self.assertIs(service.state.model_fingerprint, fingerprint_a)
            service.refresh_state()  # identical -> short-circuit, no collect
            self.assertEqual(collect_mock.call_count, 1)
            service.refresh_state()  # identity change -> collect #2
            self.assertEqual(collect_mock.call_count, 2)
            self.assertIs(service.state.model_fingerprint, fingerprint_b)


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


class _RaisingSummaryService(OfficialAppSummaryService):
    """A summary service whose scrape blows up — models a subprocess/UIA
    failure. ``read_official_summary_into_state`` must swallow it whole."""

    def __init__(self) -> None:
        super().__init__(timeout_seconds=0.1)
        self.calls = 0

    def read_summary(self, force_refresh: bool = False):
        self.calls += 1
        raise RuntimeError("probe boom")


class DeviceServiceOfficialSummaryReadTests(unittest.TestCase):
    """The best-effort official-app enrichment folded into the main Read
    (``read_official_summary_into_state``). Fakes ENFORCE the read_summary
    contract (parse the payload / return None / raise) — never echo."""

    def setUp(self) -> None:
        self.service = DeviceService(clock=lambda: 0.0)

    @staticmethod
    def _summary_payload() -> dict:
        return {
            "window_title": "Controller Settings",
            "names": [
                "Battery: 100%",
                "Version: 1.18",
                "Config: 2",
                "Sleep: Never Sleep",
            ],
        }

    def test_populates_firmware_and_profile_from_official_app(self) -> None:
        self.service.official_app_summary_service = FakeSummaryProbeService(
            [self._summary_payload()], clock=lambda: 0.0
        )

        result = self.service.read_official_summary_into_state()

        self.assertTrue(result.applied)
        self.assertFalse(result.retained_protocol_active_profile)
        self.assertEqual(self.service.state.firmware_version, "1.18")
        self.assertEqual(
            self.service.state.summary_sources["firmware"], "official_app_ui"
        )
        self.assertEqual(self.service.state.active_onboard_profile, 2)
        self.assertEqual(
            self.service.state.summary_sources["active_profile"], "official_app_ui"
        )
        # A populating read logs the official-summary event (mirrors
        # read_device_state's connected branch).
        self.assertIn(
            "Official App UI summary", self.service.recent_events(1)[0]
        )

    def test_no_window_leaves_firmware_and_profile_unknown_without_error(self) -> None:
        # ``None`` payload == "Controller Settings" window not found: the scrape
        # returns None and the read must succeed with firmware/profile Unknown.
        self.service.official_app_summary_service = FakeSummaryProbeService(
            [None], clock=lambda: 0.0
        )

        result = self.service.read_official_summary_into_state()

        self.assertFalse(result.applied)
        self.assertFalse(result.retained_protocol_active_profile)
        self.assertEqual(self.service.state.firmware_version, "Unknown")
        self.assertEqual(self.service.state.summary_sources["firmware"], "unknown")
        self.assertEqual(
            self.service.state.summary_sources["active_profile"], "unknown"
        )
        # No official-summary log line was emitted (nothing was applied).
        self.assertEqual(len(self.service.recent_events(5)), 0)

    def test_scrape_exception_degrades_silently_and_state_untouched(self) -> None:
        raising = _RaisingSummaryService()
        self.service.official_app_summary_service = raising

        # Must NOT raise, and state stays at defaults.
        result = self.service.read_official_summary_into_state()

        self.assertEqual(raising.calls, 1)
        self.assertFalse(result.applied)
        self.assertFalse(result.retained_protocol_active_profile)
        self.assertEqual(self.service.state.firmware_version, "Unknown")
        self.assertEqual(self.service.state.summary_sources["firmware"], "unknown")
        self.assertEqual(
            self.service.state.summary_sources["active_profile"], "unknown"
        )

    def test_protocol_owned_active_profile_is_retained(self) -> None:
        # A verified protocol switch owns the active profile; a lagging official
        # scrape reporting a different config must NOT overwrite it, but firmware
        # (which has no protocol owner) still populates.
        self.service.record_protocol_active_profile(3)
        self.service.official_app_summary_service = FakeSummaryProbeService(
            [
                {
                    "window_title": "Controller Settings",
                    "names": ["Version: 1.18", "Config: 1"],
                }
            ],
            clock=lambda: 0.0,
        )

        result = self.service.read_official_summary_into_state()

        self.assertTrue(result.applied)
        self.assertTrue(result.retained_protocol_active_profile)
        self.assertEqual(self.service.state.active_onboard_profile, 3)
        self.assertEqual(
            self.service.state.summary_sources["active_profile"], "protocol"
        )
        self.assertEqual(self.service.state.firmware_version, "1.18")
        self.assertEqual(
            self.service.state.summary_sources["firmware"], "official_app_ui"
        )
        self.assertIn(
            "Protocol-owned active config was retained",
            self.service.recent_events(1)[0],
        )


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


@unittest.skipUnless(sys.platform == "win32", "real Win32 DLL binding")
class DeviceServiceSetupApiRealDllBindingTests(unittest.TestCase):
    """Bind the REAL setupapi.dll (no device I/O) so a wrong library name or a
    renamed SetupDi export fails here. Mirrors ModelFingerprintRealDllBindingTests:
    the strict _FakeSetupApi patches _Win32.setupapi, so it can never catch a bad
    raw binding (C1's finding — the strict fake shouldn't be the only raw-binding
    contract for the SetupDi surface)."""

    def test_setupapi_exports_bind_from_real_dll(self) -> None:
        setupapi = device_service_module._Win32.setupapi()
        for name in (
            "SetupDiGetClassDevsW",
            "SetupDiEnumDeviceInfo",
            "SetupDiGetDeviceInstanceIdW",
            "SetupDiGetDeviceRegistryPropertyW",
            "SetupDiDestroyDeviceInfoList",
        ):
            with self.subTest(export=name):
                self.assertTrue(callable(getattr(setupapi, name)))


class DeviceServiceProtocolProfileVerificationTests(unittest.TestCase):
    """End-to-end guard for the sticky-``protocol`` active-profile honesty defect.

    The active-profile ``summary_sources`` value is sticky across a disconnect
    (cleared only on an identity change), so the source alone could re-green a
    STALE retained slot to "Verified from device" on a same-unit reconnect —
    the cardinal sin (a retained value shown as a live device read). The
    ``active_profile_protocol_verified_this_connection`` flag is the fix: it is
    set by a genuine protocol switch and cleared the moment the controller
    disconnects, and the trust-matrix verified branch requires it.
    """

    ID_A = "USB\\VID_413D&PID_2104\\ABC123"
    ID_B = "USB\\VID_413D&PID_2104\\ZZZ999"

    def _refresh_zd(self, service: DeviceService, instance_id: str = ID_A):
        with patch.object(
            service, "_find_zd_entries", return_value=[{"instance_id": instance_id}]
        ), patch(
            "zd_app.services.device_service.get_connected_controllers",
            return_value=[],
        ), patch(
            "zd_app.services.device_service.describe_battery_level",
            return_value="Wired",
        ):
            return service.refresh_state()

    def _refresh_none(self, service: DeviceService):
        with patch.object(service, "_find_zd_entries", return_value=[]), patch(
            "zd_app.services.device_service.get_connected_controllers",
            return_value=[],
        ):
            return service.refresh_state()

    def _profile_row(self, state):
        # Mirror screens.diagnostics._trust_matrix_signals for the profile-
        # relevant fields, so this asserts the ACTUAL rendered provenance.
        sources = state.summary_sources
        firmware = str(getattr(state, "firmware_version", "") or "").strip()
        signals = TrustMatrixSignals(
            connection_state=state.connection_state,
            data_freshness=state.data_freshness,
            firmware_known=bool(firmware and firmware != "Unknown"),
            active_profile_known=sources.get("active_profile", "unknown") != "unknown",
            firmware_source=sources.get("firmware", "unknown"),
            profile_source=sources.get("active_profile", "unknown"),
            active_profile_protocol_verified_this_connection=(
                state.active_profile_protocol_verified_this_connection
            ),
            settings_read_current=False,
            has_retained_settings=False,
            settings_skipped_fields=0,
            fingerprint_present=False,
            fingerprint_complete=False,
            fingerprint_short_digest=None,
        )
        return build_trust_matrix(signals)[2]  # PROFILE row

    def test_protocol_switch_then_disconnect_reconnect_does_not_regreen(self) -> None:
        service = DeviceService(clock=lambda: 0.0)

        # 1) Connected + a genuine in-app protocol profile switch (0xd0 0x0f
        #    readback). Row -> "Verified from device".
        self._refresh_zd(service)
        service.record_protocol_active_profile(2)
        self.assertTrue(service.state.active_profile_protocol_verified_this_connection)
        self.assertEqual(service.state.summary_sources["active_profile"], "protocol")
        self.assertEqual(self._profile_row(service.state).provenance, VERIFIED)

        # 2) Unplug. Disconnect ends the readback's validity: flag clears, but the
        #    retained slot + protocol source are intentionally kept visible.
        self._refresh_none(service)
        self.assertFalse(service.state.active_profile_protocol_verified_this_connection)
        self.assertEqual(service.state.summary_sources["active_profile"], "protocol")
        self.assertEqual(service.state.active_onboard_profile, 2)  # value retained
        row = self._profile_row(service.state)
        self.assertEqual(row.provenance, INFERRED)
        self.assertNotEqual(row.provenance, VERIFIED)

        # 3) User changes the onboard profile on the controller while unplugged
        #    (START+D-Pad) — invisible to us — then replugs the SAME unit. The
        #    reconnect re-reads only Wrapper Settings, NO fresh protocol read.
        #    The row MUST stay inferred/retained, never re-green while showing a
        #    possibly-stale slot.
        self._refresh_zd(service)  # same ID_A
        self.assertEqual(service.state.connection_state, "connected")
        self.assertFalse(service.state.active_profile_protocol_verified_this_connection)
        self.assertEqual(service.state.summary_sources["active_profile"], "protocol")
        reconnect_row = self._profile_row(service.state)
        self.assertEqual(reconnect_row.provenance, INFERRED)
        self.assertNotEqual(reconnect_row.provenance, VERIFIED)

    def test_official_app_scrape_can_correct_stale_protocol_after_disconnect(self) -> None:
        # Secondary defect: the protocol retention guard in
        # _apply_official_app_summary must NOT refuse a correcting scrape after a
        # disconnect (flag cleared), or the row stays green-stale.
        service = DeviceService(clock=lambda: 0.0)
        self._refresh_zd(service)
        service.record_protocol_active_profile(2)
        self._refresh_none(service)  # disconnect clears the flag

        # A later official-app scrape reports a different (corrected) slot.
        summary = OfficialAppSummary(active_onboard_profile=3)
        retained = service._apply_official_app_summary(summary)
        self.assertFalse(retained)  # NOT refused as protocol-retained
        self.assertEqual(service.state.summary_sources["active_profile"], "official_app_ui")
        self.assertEqual(service.state.active_onboard_profile, 3)

    def test_still_connected_protocol_verification_stays_verified(self) -> None:
        # No false-negative regression: while still connected, a genuine protocol
        # verification keeps the "Verified from device" chip across steady refreshes.
        service = DeviceService(clock=lambda: 0.0)
        self._refresh_zd(service)
        service.record_protocol_active_profile(2)
        self._refresh_zd(service)  # steady connected tick, same unit
        self.assertTrue(service.state.active_profile_protocol_verified_this_connection)
        self.assertEqual(self._profile_row(service.state).provenance, VERIFIED)

    def test_identity_change_clears_protocol_verification(self) -> None:
        # The pre-existing identity-change clear path must still zero the flag and
        # source (a different physical unit's slot is never the retained one's).
        service = DeviceService(clock=lambda: 0.0)
        self._refresh_zd(service, self.ID_A)
        service.record_protocol_active_profile(2)
        self._refresh_zd(service, self.ID_B)  # different unit, connected->connected
        self.assertFalse(service.state.active_profile_protocol_verified_this_connection)
        self.assertEqual(service.state.summary_sources["active_profile"], "unknown")


if __name__ == "__main__":
    unittest.main()
