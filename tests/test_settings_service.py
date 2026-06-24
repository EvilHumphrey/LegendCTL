"""Hardware-free tests for SettingsService polling-rate writes."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from unittest import mock

from zd_app.services.settings_service import (
    BUTTON_BINDING_READ_ATTEMPTS,
    BUTTON_BINDING_READ_RETRY_DELAY_S,
    BUTTON_BINDING_SUBCOMMAND,
    BACK_PADDLE_DEFAULT_CYCLE_MS,
    BACK_PADDLE_DEFAULT_DURATION_MS,
    BACK_PADDLE_EVENT_INDEX_BINDING,
    BACK_PADDLE_EVENT_INDEX_TERMINATOR,
    BACK_PADDLE_SUBCOMMAND,
    BACK_PADDLE_UNBOUND_FLAG,
    CAPTURED_A_TO_B_MAPPING,
    CATEGORY_BACK_PADDLE_BINDING,
    CATEGORY_BUTTON_BINDING,
    CATEGORY_MOTION,
    CATEGORY_POLLING_RATE,
    CATEGORY_STICK_DEADZONE,
    CATEGORY_STICK_INVERSION,
    CATEGORY_STICK_SENSITIVITY,
    CATEGORY_STICK_SENSITIVITY_8POINT,
    CATEGORY_TRIGGER_SETTINGS,
    CATEGORY_VIBRATION,
    CATEGORY_LIGHTING,
    DEADZONE_SUBCOMMAND,
    DEADZONE_VALUE_MAX,
    DEADZONE_VALUE_MIN,
    HID_FEATURE_REPORT_SIZE,
    INVERSION_STICK_LEFT,
    INVERSION_STICK_RIGHT,
    INVERSION_SUBCOMMAND,
    LIGHTING_BRIGHTNESS_BYTE_MAX,
    LIGHTING_BRIGHTNESS_BYTE_MIN,
    LIGHTING_SUBCOMMAND,
    MAGIC_READ_QUERY_PREFIX,
    MAGIC_READ_RESPONSE_PREFIX,
    MAGIC_WRITE_PREFIX,
    MAGIC_WRITE_ACK_PREFIX,
    MI02_DEVICE_INTERFACE_GUID,
    MOTION_SUBCOMMAND,
    MI02_READ_WRITE_OPEN_DESIRED_ACCESS,
    MI02_READ_WRITE_OPEN_FLAGS_AND_ATTRIBUTES,
    MI02_READ_WRITE_OPEN_SHARE_MODE,
    MI02_WRITE_OPEN_DESIRED_ACCESS,
    MI02_WRITE_OPEN_FLAGS_AND_ATTRIBUTES,
    MI02_WRITE_OPEN_SHARE_MODE,
    POLLING_RATE_SUBCOMMAND,
    POLLING_RATE_SUFFIX,
    PUBLIC_PRODUCT_ID,
    PUBLIC_VENDOR_ID,
    CATEGORY_STEP_SIZE,
    STEP_SIZE_SUBCOMMAND,
    STEP_SIZE_VALUE_DEFAULT,
    STEP_SIZE_VALUE_MAX,
    STEP_SIZE_VALUE_MIN,
    SUPPORTED_POLLING_RATES,
    TRIGGER_RANGE_MAX_VALUE,
    TRIGGER_RANGE_MIN_VALUE,
    TRIGGER_SELECTOR_LEFT,
    TRIGGER_SELECTOR_RIGHT,
    TRIGGER_SUBCOMMAND,
    VIBRATION_STRENGTH_MAX,
    VIBRATION_STRENGTH_MIN,
    VIBRATION_SUBCOMMAND,
    WRITE_RETRY_DELAY_S,
    AxisInversion,
    BackPaddleBinding,
    ButtonMapping,
    ButtonSlot,
    ControllerButtonTarget,
    ControllerSnapshot,
    LightingMode,
    LightingSettings,
    LightingZone,
    MacroSlot,
    MotionMappingMode,
    MotionMappingTarget,
    MotionSettings,
    POLLING_RATE_HZ,
    PollingRate,
    RgbColor,
    SENSITIVITY_8POINT_PROBE_TIMEOUT_MS,
    SENSITIVITY_ANCHOR_COUNT,
    SENSITIVITY_ANCHOR_COUNT_8POINT,
    SENSITIVITY_STICK_LEFT,
    SENSITIVITY_STICK_RIGHT,
    SENSITIVITY_SUBCOMMAND,
    SENSITIVITY_VALUE_MAX,
    SENSITIVITY_VALUE_MIN,
    SensitivityAnchor,
    SetAxisInversionOutcome,
    SetBackPaddleBindingOutcome,
    SetButtonBindingOutcome,
    SetDeadzoneOutcome,
    SetPollingRateOutcome,
    SetSensitivityCurveOutcome,
    SetStepSizeOutcome,
    SetTriggerSettingsOutcome,
    SetVibrationOutcome,
    SetLightingOutcome,
    SettingsService,
    SettingsServiceError,
    StickDeadzones,
    TriggerMode,
    TriggerVibrationMode,
    TriggerSettings,
    VibrationSettings,
    _choose_mi02_path,
    _decode_sensitivity_curve_8point,
    build_all_deadzones_payload,
    build_axis_inversion_payload,
    build_back_paddle_binding_payloads,
    build_button_binding_payload,
    build_left_stick_sensitivity_curve_payload,
    build_left_stick_sensitivity_curve_payload_8point,
    build_polling_rate_payload,
    build_read_query_payload,
    build_right_stick_sensitivity_curve_payload,
    build_right_stick_sensitivity_curve_payload_8point,
    build_sensitivity_curve_payload,
    build_sensitivity_curve_payload_8point,
    build_lighting_payload,
    build_step_size_payload,
    build_trigger_settings_payload,
    build_vibration_payload,
)


_FAKE_PATH = (
    r"\\?\hid#vid_413d&pid_2104&mi_02#7&fake"
    r"#{4d1e55b2-f16f-11cf-88cb-001111000030}"
)
_OTHER_PATH = (
    r"\\?\hid#vid_413d&pid_2104&mi_01#7&fake"
    r"#{4d1e55b2-f16f-11cf-88cb-001111000030}"
)
_HANDLE = 0x5151
_READ_WRITE_HANDLE = 0x6161


class _Recorder:
    def __init__(
        self,
        *,
        paths: list[str] | None = None,
        open_result: tuple[int | None, int] = (_HANDLE, 0),
        open_read_write_result: tuple[int | None, int] = (_READ_WRITE_HANDLE, 0),
        write_results: list[tuple[bool, int, int]] | None = None,
        read_results: list[bytes | BaseException] | None = None,
        clock_ticks: list[float] | None = None,
    ):
        self.paths = [_FAKE_PATH] if paths is None else list(paths)
        self.open_result = open_result
        self.open_read_write_result = open_read_write_result
        self.write_results = list(write_results or [])
        self.read_results = list(read_results or [])
        self.clock_ticks = list(clock_ticks or [])
        self.events: list[tuple] = []
        self._clock_value = 0.0

    def enumerate_paths(self) -> list[str]:
        self.events.append(("enumerate_paths",))
        return list(self.paths)

    def open_write_handle(self, path: str) -> tuple[int | None, int]:
        self.events.append(("open_write_handle", path))
        return self.open_result

    def open_read_write_handle(self, path: str) -> tuple[int | None, int]:
        self.events.append(("open_read_write_handle", path))
        return self.open_read_write_result

    def write_file(self, handle: int, payload: bytes) -> tuple[bool, int, int]:
        self.events.append(("write_file", handle, payload))
        if self.write_results:
            return self.write_results.pop(0)
        return True, 0, len(payload)

    def read_file(self, handle: int, length: int, timeout_ms: int) -> bytes:
        self.events.append(("read_file", handle, length, timeout_ms))
        if self.read_results:
            result = self.read_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        response = bytearray(HID_FEATURE_REPORT_SIZE)
        response[0] = 0x00
        response[1:5] = MAGIC_READ_RESPONSE_PREFIX
        response[5] = CATEGORY_POLLING_RATE
        response[7] = PollingRate.HZ_8000.value
        return bytes(response)

    def close_handle(self, handle: int) -> bool:
        self.events.append(("close_handle", handle))
        return True

    def sleep(self, seconds: float) -> None:
        self.events.append(("sleep", seconds))

    def clock(self) -> float:
        if self.clock_ticks:
            return self.clock_ticks.pop(0)
        self._clock_value += 0.001
        return self._clock_value


def _make_service(rec: _Recorder) -> SettingsService:
    return SettingsService(
        enumerate_paths=rec.enumerate_paths,
        open_write_handle=rec.open_write_handle,
        open_read_write_handle=rec.open_read_write_handle,
        write_file=rec.write_file,
        read_file=rec.read_file,
        close_handle=rec.close_handle,
        clock=rec.clock,
        sleep=rec.sleep,
    )


class _ScriptedReadResponse:
    """Drop-in for ``SettingsService._read_response`` with a scripted sequence.

    Each call pops the next item: a ``BaseException`` is raised, ``bytes`` are
    returned. This drives ``get_button_binding``'s retry loop precisely --
    including the decode-level slot-mismatch branch the real ``_read_response``
    would otherwise filter out before returning -- and records the call count so
    attempt-budget assertions don't depend on the ``_Recorder`` default-response
    fallback firing on exhaustion.
    """

    def __init__(self, items: list[bytes | BaseException]):
        self._items = list(items)
        self.calls = 0

    def __call__(
        self,
        query_payload: bytes,
        *,
        expected_cat: int,
        expected_selector: int | None = None,
        **_kwargs: object,
    ) -> bytes:
        self.calls += 1
        if not self._items:
            raise AssertionError("scripted _read_response exhausted")
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _make_read_response(
    *,
    category: int = CATEGORY_POLLING_RATE,
    value: int = 0x06,
    payload: bytes | None = None,
) -> bytes:
    response = bytearray(HID_FEATURE_REPORT_SIZE)
    response[0] = 0x00
    response[1:5] = MAGIC_READ_RESPONSE_PREFIX
    response[5] = category
    response[6] = 0x00
    response_payload = bytes([value]) if payload is None else payload
    response[7 : 7 + len(response_payload)] = response_payload
    return bytes(response)


def _make_write_ack_response(
    *,
    category: int = CATEGORY_POLLING_RATE,
    payload: bytes | None = None,
) -> bytes:
    response = bytearray(HID_FEATURE_REPORT_SIZE)
    response[0] = 0x00
    response[1:5] = MAGIC_WRITE_ACK_PREFIX
    response[5] = category
    response[6] = 0x00
    response_payload = b"" if payload is None else payload
    response[7 : 7 + len(response_payload)] = response_payload
    return bytes(response)


# Verified 1.2.9 / fw-1.24 8-point curves from the 2026-05-29 capture: the LEFT
# stick's custom curve was read back from the device by a fresh process; the
# RIGHT stick's linear default. Both monotonic non-decreasing in X and Y.
_VERIFIED_LEFT_8POINT = (
    SensitivityAnchor(0, 0),
    SensitivityAnchor(12, 17),
    SensitivityAnchor(24, 31),
    SensitivityAnchor(36, 47),
    SensitivityAnchor(54, 73),
    SensitivityAnchor(70, 86),
    SensitivityAnchor(84, 94),
    SensitivityAnchor(100, 100),
)
_DEFAULT_RIGHT_8POINT = (
    SensitivityAnchor(0, 0),
    SensitivityAnchor(14, 14),
    SensitivityAnchor(28, 28),
    SensitivityAnchor(42, 42),
    SensitivityAnchor(57, 57),
    SensitivityAnchor(71, 71),
    SensitivityAnchor(85, 85),
    SensitivityAnchor(100, 100),
)


def _make_8point_read_response(stick: int, anchors) -> bytes:
    """Build a ``_read_response``-shaped cat-0x86 buffer.

    Layout: report-id@0 + read magic@[1:5] + cat 0x86@[5] + sub@[6] + stick
    selector@[7] + the eight ``(X, Y)`` pairs@[8..23] — i.e. the same shape the
    0x86 write emits, with Windows' report-id byte prepended.
    """

    pairs = bytearray()
    for anchor in anchors:
        pairs.append(anchor.x)
        pairs.append(anchor.y)
    return _make_read_response(
        category=CATEGORY_STICK_SENSITIVITY_8POINT,
        payload=bytes([stick]) + bytes(pairs),
    )


class TestPollingRatePayload(unittest.TestCase):
    def test_250hz_payload_exact_bytes(self) -> None:
        payload = build_polling_rate_payload(PollingRate.HZ_250)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(
            payload.hex(),
            "001055aa511100010202" + ("00" * 55),
        )

    def test_500hz_payload_exact_bytes(self) -> None:
        payload = build_polling_rate_payload(PollingRate.HZ_500)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(
            payload.hex(),
            "001055aa511100020202" + ("00" * 55),
        )

    def test_1000hz_payload_exact_bytes(self) -> None:
        payload = build_polling_rate_payload(PollingRate.HZ_1000)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(
            payload.hex(),
            "001055aa511100030202" + ("00" * 55),
        )

    def test_2000hz_payload_exact_bytes(self) -> None:
        payload = build_polling_rate_payload(PollingRate.HZ_2000)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(
            payload.hex(),
            "001055aa511100040202" + ("00" * 55),
        )

    def test_4000hz_payload_exact_bytes(self) -> None:
        payload = build_polling_rate_payload(PollingRate.HZ_4000)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(
            payload.hex(),
            "001055aa511100050202" + ("00" * 55),
        )

    def test_8000hz_payload_exact_bytes(self) -> None:
        payload = build_polling_rate_payload(PollingRate.HZ_8000)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(
            payload.hex(),
            "001055aa511100060202" + ("00" * 55),
        )

    def test_protocol_constants_match_capture(self) -> None:
        self.assertEqual(PUBLIC_VENDOR_ID, 0x413D)
        self.assertEqual(PUBLIC_PRODUCT_ID, 0x2104)
        self.assertEqual(MI02_DEVICE_INTERFACE_GUID, "{4d1e55b2-f16f-11cf-88cb-001111000030}")
        self.assertEqual(MI02_WRITE_OPEN_DESIRED_ACCESS, 0x40000000)
        self.assertEqual(MI02_WRITE_OPEN_SHARE_MODE, 0x3)


class TestPollingRateHzLookup(unittest.TestCase):
    """The selector byte (1..6) is wire-format only; the Hz value is what the
    operator sees and what downstream code (Health Report device context,
    sidebar Hz label) actually wants. The lookup table is the single source of
    truth so a new selector can't accidentally leak its byte into an Hz field.
    """

    def test_every_member_maps_to_integer_hz(self) -> None:
        expected = {
            PollingRate.HZ_250: 250,
            PollingRate.HZ_500: 500,
            PollingRate.HZ_1000: 1000,
            PollingRate.HZ_2000: 2000,
            PollingRate.HZ_4000: 4000,
            PollingRate.HZ_8000: 8000,
        }

        self.assertEqual(POLLING_RATE_HZ, expected)
        for rate, hz in expected.items():
            self.assertIsInstance(POLLING_RATE_HZ[rate], int)
            self.assertEqual(POLLING_RATE_HZ[rate], hz)

    def test_table_covers_every_supported_rate(self) -> None:
        self.assertEqual(set(POLLING_RATE_HZ), set(PollingRate))
        self.assertEqual(MI02_WRITE_OPEN_FLAGS_AND_ATTRIBUTES, 0x0)
        self.assertEqual(MI02_READ_WRITE_OPEN_DESIRED_ACCESS, 0xC0000000)
        self.assertEqual(MI02_READ_WRITE_OPEN_SHARE_MODE, 0x3)
        self.assertEqual(MI02_READ_WRITE_OPEN_FLAGS_AND_ATTRIBUTES, 0x0)
        self.assertEqual(MAGIC_WRITE_PREFIX, bytes.fromhex("1055aa51"))
        self.assertEqual(MAGIC_READ_QUERY_PREFIX, bytes.fromhex("1055aa50"))
        self.assertEqual(MAGIC_READ_RESPONSE_PREFIX, bytes.fromhex("3055aad0"))
        self.assertEqual(MAGIC_WRITE_ACK_PREFIX, bytes.fromhex("3055aad1"))
        self.assertEqual(CATEGORY_POLLING_RATE, 0x11)
        self.assertEqual(CATEGORY_MOTION, 0x0B)
        self.assertEqual(POLLING_RATE_SUFFIX, bytes.fromhex("0202"))

    def test_build_read_query_payload_polling_rate(self) -> None:
        payload = build_read_query_payload(CATEGORY_POLLING_RATE)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload.hex(), "001055aa50110000" + ("00" * 57))

    def test_build_read_query_payload_with_selector(self) -> None:
        payload = build_read_query_payload(CATEGORY_BUTTON_BINDING, selector=ButtonSlot.A.value)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload[5], CATEGORY_BUTTON_BINDING)
        self.assertEqual(payload[6], BUTTON_BINDING_SUBCOMMAND)
        self.assertEqual(payload[7], ButtonSlot.A.value)
        self.assertEqual(payload.hex(), "001055aa50020005" + ("00" * 57))

    def test_build_read_query_payload_vibration(self) -> None:
        payload = build_read_query_payload(CATEGORY_VIBRATION, VIBRATION_SUBCOMMAND)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload.hex(), "001055aa500c0000" + ("00" * 57))

    def test_build_read_query_payload_motion(self) -> None:
        payload = build_read_query_payload(CATEGORY_MOTION, MOTION_SUBCOMMAND)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload.hex(), "001055aa500b0000" + ("00" * 57))

    def test_build_read_query_payload_lighting_zone(self) -> None:
        payload = build_read_query_payload(CATEGORY_LIGHTING, selector=LightingZone.LEFT_LIGHT.value)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload[5], CATEGORY_LIGHTING)
        self.assertEqual(payload[6], LIGHTING_SUBCOMMAND)
        self.assertEqual(payload[7], LightingZone.LEFT_LIGHT.value)
        self.assertEqual(payload.hex(), "001055aa50100001" + ("00" * 57))

    def test_build_read_query_payload_deadzones(self) -> None:
        payload = build_read_query_payload(CATEGORY_STICK_DEADZONE, DEADZONE_SUBCOMMAND)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload.hex(), "001055aa50090000" + ("00" * 57))

    def test_build_read_query_payload_axis_inversion(self) -> None:
        payload = build_read_query_payload(CATEGORY_STICK_INVERSION, INVERSION_SUBCOMMAND)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload.hex(), "001055aa50070000" + ("00" * 57))

    def test_build_read_query_payload_trigger_settings(self) -> None:
        payload = build_read_query_payload(CATEGORY_TRIGGER_SETTINGS, TRIGGER_SUBCOMMAND)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload.hex(), "001055aa500a0000" + ("00" * 57))

    def test_build_read_query_payload_sensitivity_curves(self) -> None:
        payload = build_read_query_payload(CATEGORY_STICK_SENSITIVITY, SENSITIVITY_SUBCOMMAND)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload.hex(), "001055aa50060000" + ("00" * 57))

    def test_supported_polling_rates_include_full_capture_enum(self) -> None:
        self.assertEqual(
            SUPPORTED_POLLING_RATES,
            {
                PollingRate.HZ_250,
                PollingRate.HZ_500,
                PollingRate.HZ_1000,
                PollingRate.HZ_2000,
                PollingRate.HZ_4000,
                PollingRate.HZ_8000,
            },
        )

    def test_unsupported_int_rate_raises_before_io(self) -> None:
        with self.assertRaises(ValueError):
            build_polling_rate_payload(0x05)  # type: ignore[arg-type]

    def test_set_polling_rate_rejects_invalid_rate_before_win32(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        with self.assertRaises(ValueError):
            service.set_polling_rate(0x06)  # type: ignore[arg-type]

        self.assertEqual(rec.events, [])


class TestStepSizePayload(unittest.TestCase):
    """Confirmed cat 0x0D step-size write frame.

    Empirical capture: byte 7 holds the value (1-255), bytes 30-31 hold an
    LE 16-bit echo.
    """

    def _expected_hex(self, value: int) -> str:
        body = bytearray(HID_FEATURE_REPORT_SIZE)
        body[0] = 0x00
        body[1:5] = MAGIC_WRITE_PREFIX
        body[5] = CATEGORY_STEP_SIZE
        body[6] = STEP_SIZE_SUBCOMMAND
        body[7] = value
        body[30:32] = value.to_bytes(2, "little")
        return bytes(body).hex()

    def test_default_73_payload_exact_bytes(self) -> None:
        payload = build_step_size_payload(STEP_SIZE_VALUE_DEFAULT)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload.hex(), self._expected_hex(73))
        self.assertEqual(payload[7], 0x49)
        self.assertEqual(payload[30:32], b"\x49\x00")

    def test_min_1_payload_exact_bytes(self) -> None:
        payload = build_step_size_payload(STEP_SIZE_VALUE_MIN)

        self.assertEqual(payload.hex(), self._expected_hex(1))
        self.assertEqual(payload[7], 0x01)
        self.assertEqual(payload[30:32], b"\x01\x00")

    def test_max_255_payload_exact_bytes(self) -> None:
        payload = build_step_size_payload(STEP_SIZE_VALUE_MAX)

        self.assertEqual(payload.hex(), self._expected_hex(255))
        self.assertEqual(payload[7], 0xFF)
        self.assertEqual(payload[30:32], b"\xff\x00")

    def test_payload_rejects_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            build_step_size_payload(0)
        with self.assertRaises(ValueError):
            build_step_size_payload(256)

    def test_payload_rejects_non_int(self) -> None:
        with self.assertRaises(ValueError):
            build_step_size_payload(1.5)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            build_step_size_payload("100")  # type: ignore[arg-type]
        # bool is a subclass of int; reject defensively
        with self.assertRaises(ValueError):
            build_step_size_payload(True)  # type: ignore[arg-type]


class TestButtonBindingPayload(unittest.TestCase):
    def test_all_confirmed_slot_target_payloads_exact_bytes(self) -> None:
        for slot in ButtonSlot:
            for target in ControllerButtonTarget:
                with self.subTest(slot=slot.name, target=target.name):
                    payload = build_button_binding_payload(
                        slot,
                        ButtonMapping.controller_button(target),
                    )

                    self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
                    self.assertEqual(
                        payload.hex(),
                        (
                            "001055aa510200"
                            f"{slot.value:02x}"
                            "0100"
                            f"{target.value:02x}"
                            "00"
                            + ("00" * 53)
                        ),
                    )

    def test_a_to_b_payload_exact_bytes(self) -> None:
        payload = build_button_binding_payload(
            ButtonSlot.A,
            ButtonMapping.controller_button(ControllerButtonTarget.B),
        )

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(
            payload.hex(),
            "001055aa5102000501001000" + ("00" * 53),
        )

    def test_button_slot_constants_match_capture(self) -> None:
        self.assertEqual(CATEGORY_BUTTON_BINDING, 0x02)
        self.assertEqual(BUTTON_BINDING_SUBCOMMAND, 0x00)
        self.assertEqual(ButtonSlot.UP.value, 0x01)
        self.assertEqual(ButtonSlot.RIGHT.value, 0x02)
        self.assertEqual(ButtonSlot.DOWN.value, 0x03)
        self.assertEqual(ButtonSlot.LEFT.value, 0x04)
        self.assertEqual(ButtonSlot.A.value, 0x05)
        self.assertEqual(ButtonSlot.B.value, 0x06)
        self.assertEqual(ButtonSlot.X.value, 0x07)
        self.assertEqual(ButtonSlot.Y.value, 0x08)
        self.assertEqual(ButtonSlot.LB.value, 0x09)
        self.assertEqual(ButtonSlot.RB.value, 0x0A)
        self.assertEqual(ButtonSlot.LT.value, 0x0B)
        self.assertEqual(ButtonSlot.RT.value, 0x0C)
        self.assertEqual(ButtonSlot.BACK.value, 0x0D)
        self.assertEqual(ButtonSlot.START.value, 0x0E)
        self.assertEqual(ButtonSlot.LS.value, 0x0F)
        self.assertEqual(ButtonSlot.RS.value, 0x10)

    def test_controller_button_target_constants_match_capture(self) -> None:
        self.assertEqual(ControllerButtonTarget.LS.value, 0x09)
        self.assertEqual(ControllerButtonTarget.RS.value, 0x0A)
        self.assertEqual(ControllerButtonTarget.UP.value, 0x0B)
        self.assertEqual(ControllerButtonTarget.RIGHT.value, 0x0C)
        self.assertEqual(ControllerButtonTarget.DOWN.value, 0x0D)
        self.assertEqual(ControllerButtonTarget.LEFT.value, 0x0E)
        self.assertEqual(ControllerButtonTarget.A.value, 0x0F)
        self.assertEqual(ControllerButtonTarget.B.value, 0x10)
        self.assertEqual(ControllerButtonTarget.X.value, 0x11)
        self.assertEqual(ControllerButtonTarget.Y.value, 0x12)
        self.assertEqual(ControllerButtonTarget.LB.value, 0x13)
        self.assertEqual(ControllerButtonTarget.RB.value, 0x14)
        self.assertEqual(ControllerButtonTarget.LT.value, 0x15)
        self.assertEqual(ControllerButtonTarget.RT.value, 0x16)
        self.assertEqual(ControllerButtonTarget.BACK.value, 0x17)
        self.assertEqual(ControllerButtonTarget.START.value, 0x18)
        self.assertEqual(CAPTURED_A_TO_B_MAPPING.target_kind, 0x01)
        self.assertEqual(CAPTURED_A_TO_B_MAPPING.target_low, 0x00)
        self.assertEqual(CAPTURED_A_TO_B_MAPPING.target_value, 0x10)

    def test_controller_button_factory_builds_confirmed_target_mapping(self) -> None:
        self.assertEqual(
            ButtonMapping.controller_button(ControllerButtonTarget.LB),
            ButtonMapping(target_kind=0x01, target_low=0x00, target_value=0x13),
        )

    def test_unknown_slot_raises_not_implemented_before_io(self) -> None:
        with self.assertRaises(NotImplementedError):
            build_button_binding_payload(0x11, ButtonMapping())  # type: ignore[arg-type]

    def test_unknown_target_value_raises_not_implemented_before_io(self) -> None:
        with self.assertRaises(NotImplementedError):
            build_button_binding_payload(
                ButtonSlot.A,
                ButtonMapping(target_kind=0x01, target_low=0x00, target_value=0x19),
            )

    def test_unknown_target_kind_raises_not_implemented_before_io(self) -> None:
        with self.assertRaises(NotImplementedError):
            build_button_binding_payload(
                ButtonSlot.A,
                ButtonMapping(target_kind=0x02, target_low=0x00, target_value=0x10),
            )

    def test_unknown_target_low_byte_raises_not_implemented_before_io(self) -> None:
        with self.assertRaises(NotImplementedError):
            build_button_binding_payload(
                ButtonSlot.A,
                ButtonMapping(target_kind=0x01, target_low=0x01, target_value=0x10),
            )

    def test_set_button_binding_rejects_unknown_mapping_before_win32(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        with self.assertRaises(NotImplementedError):
            service.set_button_binding(
                ButtonSlot.A,
                ButtonMapping(target_kind=0x01, target_low=0x00, target_value=0x19),
            )

        self.assertEqual(rec.events, [])


class TestDeadzonePayload(unittest.TestCase):
    def test_deadzone_payload_exact_bytes_for_all_zero(self) -> None:
        payload = build_all_deadzones_payload(StickDeadzones(0, 0, 0, 0))

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload.hex(), "001055aa51090000000000" + ("00" * 54))

    def test_deadzone_payload_exact_bytes_for_original_left_center_five(self) -> None:
        payload = build_all_deadzones_payload(StickDeadzones(5, 0, 0, 0))

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload.hex(), "001055aa51090005000000" + ("00" * 54))

    def test_deadzone_payload_exact_bytes_for_asymmetric_capture(self) -> None:
        payload = build_all_deadzones_payload(StickDeadzones(0, 11, 7, 13))

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload.hex(), "001055aa510900000b070d" + ("00" * 54))

    def test_deadzone_constants_match_capture(self) -> None:
        self.assertEqual(CATEGORY_STICK_DEADZONE, 0x09)
        self.assertEqual(DEADZONE_SUBCOMMAND, 0x00)
        self.assertEqual(DEADZONE_VALUE_MIN, 0)
        self.assertEqual(DEADZONE_VALUE_MAX, 100)

    def test_deadzone_rejects_out_of_range_values(self) -> None:
        cases = [
            StickDeadzones(-1, 0, 0, 0),
            StickDeadzones(0, 101, 0, 0),
            StickDeadzones(0, 0, 1000, 0),
            StickDeadzones(0, 0, 0, -1),
        ]
        for deadzones in cases:
            with self.subTest(deadzones=deadzones):
                with self.assertRaises(ValueError):
                    build_all_deadzones_payload(deadzones)

    def test_deadzone_rejects_non_plain_int_values(self) -> None:
        cases = [
            StickDeadzones(True, 0, 0, 0),
            StickDeadzones(0, 5.0, 0, 0),
            StickDeadzones(0, 0, "5", 0),
        ]
        for deadzones in cases:
            with self.subTest(deadzones=deadzones):
                with self.assertRaises(TypeError):
                    build_all_deadzones_payload(deadzones)  # type: ignore[arg-type]

    def test_deadzone_rejects_non_stick_deadzones_object(self) -> None:
        with self.assertRaises(TypeError):
            build_all_deadzones_payload((0, 0, 0, 0))  # type: ignore[arg-type]

    def test_set_deadzone_rejects_invalid_value_before_win32(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        with self.assertRaises(ValueError):
            service.set_all_deadzones(StickDeadzones(0, 101, 0, 0))

        self.assertEqual(rec.events, [])


class TestSensitivityPayload(unittest.TestCase):
    def test_custom_linear_payload_exact_bytes(self) -> None:
        anchors = (
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
        )

        payload = build_left_stick_sensitivity_curve_payload(anchors)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(
            payload.hex(),
            "001055aa51060000000032326464" + ("00" * 51),
        )

    def test_high_performance_payload_exact_bytes(self) -> None:
        anchors = (
            SensitivityAnchor(24, 24),
            SensitivityAnchor(54, 80),
            SensitivityAnchor(100, 100),
        )

        payload = build_left_stick_sensitivity_curve_payload(anchors)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(
            payload.hex(),
            "001055aa51060000181836506464" + ("00" * 51),
        )

    def test_sensitivity_constants_match_capture(self) -> None:
        self.assertEqual(CATEGORY_STICK_SENSITIVITY, 0x06)
        self.assertEqual(SENSITIVITY_SUBCOMMAND, 0x00)
        self.assertEqual(SENSITIVITY_STICK_LEFT, 0x00)
        self.assertEqual(SENSITIVITY_STICK_RIGHT, 0x01)
        self.assertEqual(SENSITIVITY_VALUE_MIN, 0)
        self.assertEqual(SENSITIVITY_VALUE_MAX, 100)
        self.assertEqual(SENSITIVITY_ANCHOR_COUNT, 3)

    def test_right_stick_linear_payload_exact_bytes(self) -> None:
        payload = build_right_stick_sensitivity_curve_payload((
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
        ))

        self.assertEqual(payload.hex(), "001055aa51060001000032326464" + ("00" * 51))

    def test_right_stick_high_performance_payload_exact_bytes(self) -> None:
        payload = build_right_stick_sensitivity_curve_payload((
            SensitivityAnchor(24, 24),
            SensitivityAnchor(54, 80),
            SensitivityAnchor(100, 100),
        ))

        self.assertEqual(payload.hex(), "001055aa51060001181836506464" + ("00" * 51))

    def test_sensitivity_rejects_unknown_stick_selector(self) -> None:
        anchors = (
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
        )
        with self.assertRaises(NotImplementedError):
            build_sensitivity_curve_payload(2, anchors)
        with self.assertRaises(TypeError):
            build_sensitivity_curve_payload(True, anchors)

    def test_sensitivity_allows_non_monotonic_x_coords(self) -> None:
        payload = build_left_stick_sensitivity_curve_payload((
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
            SensitivityAnchor(0, 0),
        ))

        self.assertEqual(payload.hex(), "001055aa51060000323264640000" + ("00" * 51))

    def test_sensitivity_rejects_out_of_range_values(self) -> None:
        cases = [
            (SensitivityAnchor(-1, 0), SensitivityAnchor(50, 50), SensitivityAnchor(100, 100)),
            (SensitivityAnchor(0, 0), SensitivityAnchor(50, 101), SensitivityAnchor(100, 100)),
            (SensitivityAnchor(0, 0), SensitivityAnchor(50, 50), SensitivityAnchor(1000, 100)),
        ]
        for anchors in cases:
            with self.subTest(anchors=anchors):
                with self.assertRaises(ValueError):
                    build_left_stick_sensitivity_curve_payload(anchors)

    def test_sensitivity_rejects_non_plain_int_anchor_values(self) -> None:
        cases = [
            (SensitivityAnchor(True, 0), SensitivityAnchor(50, 50), SensitivityAnchor(100, 100)),
            (SensitivityAnchor(0, 0), SensitivityAnchor(50.0, 50), SensitivityAnchor(100, 100)),
            (SensitivityAnchor(0, 0), SensitivityAnchor(50, "50"), SensitivityAnchor(100, 100)),
        ]
        for anchors in cases:
            with self.subTest(anchors=anchors):
                with self.assertRaises(TypeError):
                    build_left_stick_sensitivity_curve_payload(anchors)  # type: ignore[arg-type]

    def test_sensitivity_rejects_wrong_length_tuple(self) -> None:
        with self.assertRaises(TypeError):
            build_left_stick_sensitivity_curve_payload((
                SensitivityAnchor(0, 0),
                SensitivityAnchor(100, 100),
            ))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            build_left_stick_sensitivity_curve_payload((
                SensitivityAnchor(0, 0),
                SensitivityAnchor(50, 50),
                SensitivityAnchor(75, 75),
                SensitivityAnchor(100, 100),
            ))  # type: ignore[arg-type]

    def test_sensitivity_rejects_non_tuple_or_non_anchor_items(self) -> None:
        with self.assertRaises(TypeError):
            build_left_stick_sensitivity_curve_payload([
                SensitivityAnchor(0, 0),
                SensitivityAnchor(50, 50),
                SensitivityAnchor(100, 100),
            ])  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            build_left_stick_sensitivity_curve_payload((
                SensitivityAnchor(0, 0),
                (50, 50),
                SensitivityAnchor(100, 100),
            ))  # type: ignore[arg-type]

    def test_set_sensitivity_allows_non_monotonic_anchors_before_win32(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        result = service.set_left_stick_sensitivity_curve((
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
            SensitivityAnchor(0, 0),
        ))

        self.assertEqual(result.outcome, SetSensitivityCurveOutcome.OK)
        self.assertEqual(
            result.payload_hex,
            "001055aa51060000323264640000" + ("00" * 51),
        )
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file"],
        )

    # --- Dormant 8-point (category 0x86) builder, gated off / not wired ------

    def test_8point_sensitivity_constants_match_capture(self) -> None:
        self.assertEqual(CATEGORY_STICK_SENSITIVITY_8POINT, 0x86)
        # 0x86 is the legacy 0x06 category with the high bit set.
        self.assertEqual(CATEGORY_STICK_SENSITIVITY_8POINT, CATEGORY_STICK_SENSITIVITY | 0x80)
        self.assertEqual(SENSITIVITY_ANCHOR_COUNT_8POINT, 8)

    def test_left_8point_high_performance_payload_byte_layout(self) -> None:
        # Captured 1.2.9 left-stick "High Performance" 8-point curve.
        payload = build_left_stick_sensitivity_curve_payload_8point((
            SensitivityAnchor(0, 0),
            SensitivityAnchor(12, 12),
            SensitivityAnchor(24, 24),
            SensitivityAnchor(36, 46),
            SensitivityAnchor(54, 80),
            SensitivityAnchor(70, 87),
            SensitivityAnchor(84, 93),
            SensitivityAnchor(100, 100),
        ))

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload[1:5].hex(), "1055aa51")
        self.assertEqual(payload[5], CATEGORY_STICK_SENSITIVITY_8POINT)
        self.assertEqual(payload[6], SENSITIVITY_SUBCOMMAND)
        self.assertEqual(payload[7], SENSITIVITY_STICK_LEFT)
        self.assertEqual(
            payload[8:24],
            bytes([0, 0, 12, 12, 24, 24, 36, 46, 54, 80, 70, 87, 84, 93, 100, 100]),
        )
        # Tail past the 8 pairs is zero-filled.
        self.assertEqual(payload[24:], bytes(HID_FEATURE_REPORT_SIZE - 24))

    def test_right_8point_payload_known_capture_vector(self) -> None:
        # Captured 1.2.9 right-stick (edited) 8-point curve.
        payload = build_right_stick_sensitivity_curve_payload_8point((
            SensitivityAnchor(5, 5),
            SensitivityAnchor(15, 18),
            SensitivityAnchor(28, 35),
            SensitivityAnchor(42, 50),
            SensitivityAnchor(56, 65),
            SensitivityAnchor(70, 78),
            SensitivityAnchor(85, 90),
            SensitivityAnchor(100, 100),
        ))

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload[5], CATEGORY_STICK_SENSITIVITY_8POINT)
        self.assertEqual(payload[7], SENSITIVITY_STICK_RIGHT)
        self.assertEqual(payload[8:24].hex(), "05050f121c232a323841464e555a6464")
        self.assertEqual(payload[24:], bytes(HID_FEATURE_REPORT_SIZE - 24))
        self.assertEqual(
            payload.hex(),
            "001055aa51860001" + "05050f121c232a323841464e555a6464" + ("00" * 41),
        )

    def test_8point_sensitivity_rejects_unknown_stick_selector(self) -> None:
        anchors = tuple(SensitivityAnchor(i, i) for i in range(8))
        with self.assertRaises(NotImplementedError):
            build_sensitivity_curve_payload_8point(2, anchors)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            build_sensitivity_curve_payload_8point(True, anchors)  # type: ignore[arg-type]

    def test_8point_sensitivity_rejects_wrong_length_tuple(self) -> None:
        with self.assertRaises(TypeError):
            build_left_stick_sensitivity_curve_payload_8point(
                tuple(SensitivityAnchor(i, i) for i in range(7))
            )  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            build_left_stick_sensitivity_curve_payload_8point(
                tuple(SensitivityAnchor(i, i) for i in range(9))
            )  # type: ignore[arg-type]

    def test_8point_sensitivity_rejects_non_tuple_or_non_anchor_items(self) -> None:
        with self.assertRaises(TypeError):
            build_left_stick_sensitivity_curve_payload_8point(
                [SensitivityAnchor(i, i) for i in range(8)]
            )  # type: ignore[arg-type]
        anchors = [SensitivityAnchor(i, i) for i in range(8)]
        anchors[3] = (30, 30)  # type: ignore[call-overload]
        with self.assertRaises(TypeError):
            build_left_stick_sensitivity_curve_payload_8point(tuple(anchors))  # type: ignore[arg-type]

    def test_8point_sensitivity_rejects_out_of_range_values(self) -> None:
        cases = [
            (0, SensitivityAnchor(-1, 0)),
            (4, SensitivityAnchor(40, 101)),
            (7, SensitivityAnchor(1000, 100)),
        ]
        for index, bad_anchor in cases:
            anchors = [SensitivityAnchor(i * 10, i * 10) for i in range(8)]
            anchors[index] = bad_anchor
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    build_left_stick_sensitivity_curve_payload_8point(tuple(anchors))  # type: ignore[arg-type]

    def test_8point_sensitivity_rejects_non_monotonic_x(self) -> None:
        anchors = [SensitivityAnchor(i * 10, i * 10) for i in range(8)]
        anchors[3] = SensitivityAnchor(19, 30)  # x dips below anchors[2].x == 20
        with self.assertRaises(ValueError):
            build_left_stick_sensitivity_curve_payload_8point(tuple(anchors))  # type: ignore[arg-type]

    def test_8point_sensitivity_rejects_non_monotonic_y(self) -> None:
        anchors = [SensitivityAnchor(i * 10, i * 10) for i in range(8)]
        anchors[5] = SensitivityAnchor(50, 39)  # y dips below anchors[4].y == 40
        with self.assertRaises(ValueError):
            build_left_stick_sensitivity_curve_payload_8point(tuple(anchors))  # type: ignore[arg-type]

    def test_8point_sensitivity_allows_equal_adjacent_values(self) -> None:
        # Monotonic rule is non-decreasing (>=), so flat segments are valid.
        payload = build_right_stick_sensitivity_curve_payload_8point((
            SensitivityAnchor(0, 0),
            SensitivityAnchor(0, 0),
            SensitivityAnchor(10, 10),
            SensitivityAnchor(10, 50),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
            SensitivityAnchor(100, 100),
        ))

        self.assertEqual(payload[5], CATEGORY_STICK_SENSITIVITY_8POINT)
        self.assertEqual(
            payload[8:24],
            bytes([0, 0, 0, 0, 10, 10, 10, 50, 50, 50, 50, 50, 100, 100, 100, 100]),
        )


class TestAxisInversionPayload(unittest.TestCase):
    def _assert_payload(self, stick_selector: int, inversion: AxisInversion, frame_bytes_7_9: str) -> None:
        payload = build_axis_inversion_payload(stick_selector, inversion)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload.hex(), "001055aa510700" + frame_bytes_7_9 + ("00" * 55))

    def test_axis_inversion_constants_match_capture(self) -> None:
        self.assertEqual(CATEGORY_STICK_INVERSION, 0x07)
        self.assertEqual(INVERSION_SUBCOMMAND, 0x00)
        self.assertEqual(INVERSION_STICK_LEFT, 0x00)
        self.assertEqual(INVERSION_STICK_RIGHT, 0x01)

    def test_left_axis_inversion_off_off_payload_exact_bytes(self) -> None:
        self._assert_payload(INVERSION_STICK_LEFT, AxisInversion(False, False), "000000")

    def test_left_axis_inversion_x_off_y_on_payload_exact_bytes(self) -> None:
        self._assert_payload(INVERSION_STICK_LEFT, AxisInversion(False, True), "000001")

    def test_left_axis_inversion_x_on_y_off_payload_exact_bytes(self) -> None:
        self._assert_payload(INVERSION_STICK_LEFT, AxisInversion(True, False), "000100")

    def test_left_axis_inversion_x_on_y_on_payload_exact_bytes(self) -> None:
        self._assert_payload(INVERSION_STICK_LEFT, AxisInversion(True, True), "000101")

    def test_right_axis_inversion_off_off_payload_exact_bytes(self) -> None:
        self._assert_payload(INVERSION_STICK_RIGHT, AxisInversion(False, False), "010000")

    def test_right_axis_inversion_x_off_y_on_payload_exact_bytes(self) -> None:
        self._assert_payload(INVERSION_STICK_RIGHT, AxisInversion(False, True), "010001")

    def test_right_axis_inversion_x_on_y_off_payload_exact_bytes(self) -> None:
        self._assert_payload(INVERSION_STICK_RIGHT, AxisInversion(True, False), "010100")

    def test_right_axis_inversion_x_on_y_on_payload_exact_bytes(self) -> None:
        self._assert_payload(INVERSION_STICK_RIGHT, AxisInversion(True, True), "010101")

    def test_axis_inversion_rejects_non_bool_flags(self) -> None:
        cases = [
            (1, False),
            (False, 0),
            ("true", False),
        ]
        for x_inverted, y_inverted in cases:
            with self.subTest(x_inverted=x_inverted, y_inverted=y_inverted):
                with self.assertRaises(TypeError):
                    AxisInversion(x_inverted, y_inverted)  # type: ignore[arg-type]

    def test_axis_inversion_rejects_unknown_stick_selector(self) -> None:
        inversion = AxisInversion(False, False)
        with self.assertRaises(NotImplementedError):
            build_axis_inversion_payload(2, inversion)
        with self.assertRaises(TypeError):
            build_axis_inversion_payload(True, inversion)

    def test_axis_inversion_rejects_non_axis_inversion_object(self) -> None:
        with self.assertRaises(TypeError):
            build_axis_inversion_payload(INVERSION_STICK_LEFT, (False, False))  # type: ignore[arg-type]

    def test_set_axis_inversion_rejects_invalid_inversion_before_win32(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        with self.assertRaises(TypeError):
            service.set_left_stick_inversion((False, False))  # type: ignore[arg-type]

        self.assertEqual(rec.events, [])


class TestTriggerSettingsPayload(unittest.TestCase):
    def _assert_payload(
        self,
        trigger_selector: int,
        settings: TriggerSettings,
        frame_bytes_7_10: str,
    ) -> None:
        payload = build_trigger_settings_payload(trigger_selector, settings)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload.hex(), "001055aa510a00" + frame_bytes_7_10 + ("00" * 54))

    def test_trigger_settings_constants_match_capture(self) -> None:
        self.assertEqual(CATEGORY_TRIGGER_SETTINGS, 0x0A)
        self.assertEqual(TRIGGER_SUBCOMMAND, 0x00)
        self.assertEqual(TRIGGER_SELECTOR_LEFT, 0x00)
        self.assertEqual(TRIGGER_SELECTOR_RIGHT, 0x01)
        self.assertEqual(TRIGGER_RANGE_MIN_VALUE, 0)
        self.assertEqual(TRIGGER_RANGE_MAX_VALUE, 100)
        self.assertEqual(TriggerMode.LONG.value, 0x00)
        self.assertEqual(TriggerMode.SHORT.value, 0x01)

    def test_left_trigger_short_default_payload_exact_bytes(self) -> None:
        self._assert_payload(
            TRIGGER_SELECTOR_LEFT,
            TriggerSettings(0, 100, TriggerMode.SHORT),
            "00006401",
        )

    def test_left_trigger_long_default_payload_exact_bytes(self) -> None:
        self._assert_payload(
            TRIGGER_SELECTOR_LEFT,
            TriggerSettings(0, 100, TriggerMode.LONG),
            "00006400",
        )

    def test_left_trigger_long_min_ten_payload_exact_bytes(self) -> None:
        self._assert_payload(
            TRIGGER_SELECTOR_LEFT,
            TriggerSettings(10, 100, TriggerMode.LONG),
            "000a6400",
        )

    def test_right_trigger_short_default_payload_exact_bytes(self) -> None:
        self._assert_payload(
            TRIGGER_SELECTOR_RIGHT,
            TriggerSettings(0, 100, TriggerMode.SHORT),
            "01006401",
        )

    def test_right_trigger_long_default_payload_exact_bytes(self) -> None:
        self._assert_payload(
            TRIGGER_SELECTOR_RIGHT,
            TriggerSettings(0, 100, TriggerMode.LONG),
            "01006400",
        )

    def test_trigger_settings_rejects_min_greater_than_max(self) -> None:
        with self.assertRaises(ValueError):
            build_trigger_settings_payload(
                TRIGGER_SELECTOR_LEFT,
                TriggerSettings(80, 20, TriggerMode.LONG),
            )

    def test_trigger_settings_rejects_out_of_range_values(self) -> None:
        cases = [
            TriggerSettings(-1, 100, TriggerMode.SHORT),
            TriggerSettings(0, 101, TriggerMode.SHORT),
        ]
        for settings in cases:
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    build_trigger_settings_payload(TRIGGER_SELECTOR_LEFT, settings)

    def test_trigger_settings_rejects_non_plain_int_ranges(self) -> None:
        cases = [
            TriggerSettings(True, 100, TriggerMode.SHORT),
            TriggerSettings(0, 100.0, TriggerMode.SHORT),
            TriggerSettings("0", 100, TriggerMode.SHORT),
        ]
        for settings in cases:
            with self.subTest(settings=settings):
                with self.assertRaises(TypeError):
                    build_trigger_settings_payload(TRIGGER_SELECTOR_LEFT, settings)  # type: ignore[arg-type]

    def test_trigger_settings_rejects_non_trigger_mode(self) -> None:
        with self.assertRaises(TypeError):
            build_trigger_settings_payload(
                TRIGGER_SELECTOR_LEFT,
                TriggerSettings(0, 100, 0),  # type: ignore[arg-type]
            )

    def test_trigger_settings_rejects_unknown_trigger_selector(self) -> None:
        settings = TriggerSettings(0, 100, TriggerMode.SHORT)
        with self.assertRaises(NotImplementedError):
            build_trigger_settings_payload(2, settings)
        with self.assertRaises(TypeError):
            build_trigger_settings_payload(True, settings)

    def test_trigger_settings_rejects_non_trigger_settings_object(self) -> None:
        with self.assertRaises(TypeError):
            build_trigger_settings_payload(TRIGGER_SELECTOR_LEFT, (0, 100, TriggerMode.SHORT))  # type: ignore[arg-type]

    def test_set_trigger_settings_rejects_invalid_settings_before_win32(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        with self.assertRaises(ValueError):
            service.set_left_trigger_settings(TriggerSettings(80, 20, TriggerMode.LONG))

        self.assertEqual(rec.events, [])


class TestVibrationPayload(unittest.TestCase):
    def _assert_payload(self, settings: VibrationSettings, frame_bytes_7_11: str) -> None:
        payload = build_vibration_payload(settings)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload.hex(), "001055aa510c00" + frame_bytes_7_11 + ("00" * 53))

    def test_vibration_constants_match_capture(self) -> None:
        self.assertEqual(CATEGORY_VIBRATION, 0x0C)
        self.assertEqual(VIBRATION_SUBCOMMAND, 0x00)
        self.assertEqual(VIBRATION_STRENGTH_MIN, 0)
        self.assertEqual(VIBRATION_STRENGTH_MAX, 100)
        self.assertEqual(TriggerVibrationMode.NATIVE.value, 0x00)
        self.assertEqual(TriggerVibrationMode.STEREO_RESONANCE.value, 0x01)
        self.assertEqual(TriggerVibrationMode.TRIGGER_VIBRATION.value, 0x02)

    def test_vibration_payload_exact_bytes_for_initial_native_capture(self) -> None:
        self._assert_payload(
            VibrationSettings(30, 15, 15, 15, TriggerVibrationMode.NATIVE),
            "1e0f0f0f00",
        )

    def test_vibration_payload_exact_bytes_for_right_grip_change(self) -> None:
        self._assert_payload(
            VibrationSettings(30, 50, 15, 15, TriggerVibrationMode.NATIVE),
            "1e320f0f00",
        )

    def test_vibration_payload_exact_bytes_for_left_trigger_motor_change(self) -> None:
        self._assert_payload(
            VibrationSettings(30, 50, 70, 15, TriggerVibrationMode.NATIVE),
            "1e32460f00",
        )

    def test_vibration_payload_exact_bytes_for_right_trigger_motor_change(self) -> None:
        self._assert_payload(
            VibrationSettings(30, 50, 70, 90, TriggerVibrationMode.NATIVE),
            "1e32465a00",
        )

    def test_vibration_payload_exact_bytes_for_stereo_mode(self) -> None:
        self._assert_payload(
            VibrationSettings(30, 50, 70, 90, TriggerVibrationMode.STEREO_RESONANCE),
            "1e32465a01",
        )

    def test_vibration_payload_exact_bytes_for_trigger_vibration_mode(self) -> None:
        self._assert_payload(
            VibrationSettings(30, 50, 70, 90, TriggerVibrationMode.TRIGGER_VIBRATION),
            "1e32465a02",
        )

    def test_vibration_rejects_out_of_range_strengths(self) -> None:
        cases = [
            VibrationSettings(-1, 15, 15, 15, TriggerVibrationMode.NATIVE),
            VibrationSettings(30, 101, 15, 15, TriggerVibrationMode.NATIVE),
            VibrationSettings(30, 15, -1, 15, TriggerVibrationMode.NATIVE),
            VibrationSettings(30, 15, 15, 101, TriggerVibrationMode.NATIVE),
        ]
        for settings in cases:
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    build_vibration_payload(settings)

    def test_vibration_rejects_non_plain_int_strengths(self) -> None:
        cases = [
            VibrationSettings(True, 15, 15, 15, TriggerVibrationMode.NATIVE),
            VibrationSettings(30, 15.0, 15, 15, TriggerVibrationMode.NATIVE),
            VibrationSettings(30, 15, "15", 15, TriggerVibrationMode.NATIVE),
            VibrationSettings(30, 15, 15, True, TriggerVibrationMode.NATIVE),
        ]
        for settings in cases:
            with self.subTest(settings=settings):
                with self.assertRaises(TypeError):
                    build_vibration_payload(settings)  # type: ignore[arg-type]

    def test_vibration_rejects_non_trigger_vibration_mode(self) -> None:
        with self.assertRaises(TypeError):
            build_vibration_payload(
                VibrationSettings(30, 15, 15, 15, 0),  # type: ignore[arg-type]
            )

    def test_vibration_rejects_non_vibration_settings_object(self) -> None:
        with self.assertRaises(TypeError):
            build_vibration_payload((30, 15, 15, 15, TriggerVibrationMode.NATIVE))  # type: ignore[arg-type]

    def test_set_vibration_rejects_invalid_settings_before_win32(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        with self.assertRaises(TypeError):
            service.set_vibration((30, 15, 15, 15, TriggerVibrationMode.NATIVE))  # type: ignore[arg-type]

        self.assertEqual(rec.events, [])


class TestLightingPayload(unittest.TestCase):
    def _assert_payload(
        self,
        zone: LightingZone,
        settings: LightingSettings,
        frame_bytes_6_13: str,
    ) -> None:
        payload = build_lighting_payload(zone, settings)

        self.assertEqual(len(payload), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(payload.hex(), "001055aa5110" + frame_bytes_6_13 + ("00" * 51))

    def test_lighting_constants_match_capture(self) -> None:
        self.assertEqual(CATEGORY_LIGHTING, 0x10)
        self.assertEqual(LIGHTING_SUBCOMMAND, 0x00)
        self.assertEqual(LIGHTING_BRIGHTNESS_BYTE_MIN, 0)
        self.assertEqual(LIGHTING_BRIGHTNESS_BYTE_MAX, 255)
        self.assertEqual(LightingZone.HOME.value, 0x00)
        self.assertEqual(LightingZone.LEFT_LIGHT.value, 0x01)
        self.assertEqual(LightingZone.RIGHT_LIGHT.value, 0x02)
        self.assertEqual(LightingMode.OFF.value, 0x00)
        self.assertEqual(LightingMode.ALWAYS_ON.value, 0x01)
        self.assertEqual(LightingMode.BREATH.value, 0x02)
        self.assertEqual(LightingMode.FADE.value, 0x03)
        self.assertEqual(LightingMode.FLOW.value, 0x04)

    def test_lighting_payload_exact_bytes_for_home_fade_blue(self) -> None:
        self._assert_payload(
            LightingZone.HOME,
            LightingSettings(True, LightingMode.FADE, 0x66, RgbColor(0, 0, 255)),
            "00000103660000ff",
        )

    def test_lighting_payload_exact_bytes_for_home_flow_blue(self) -> None:
        self._assert_payload(
            LightingZone.HOME,
            LightingSettings(True, LightingMode.FLOW, 0xCC, RgbColor(0, 0, 255)),
            "00000104cc0000ff",
        )

    def test_lighting_payload_exact_bytes_for_home_always_on_white(self) -> None:
        self._assert_payload(
            LightingZone.HOME,
            LightingSettings(True, LightingMode.ALWAYS_ON, 0x66, RgbColor(255, 255, 255)),
            "0000010166ffffff",
        )

    def test_lighting_payload_exact_bytes_for_left_flow_red(self) -> None:
        self._assert_payload(
            LightingZone.LEFT_LIGHT,
            LightingSettings(True, LightingMode.FLOW, 0x66, RgbColor(255, 0, 0)),
            "0001010466ff0000",
        )

    def test_lighting_payload_exact_bytes_for_right_flow_green(self) -> None:
        self._assert_payload(
            LightingZone.RIGHT_LIGHT,
            LightingSettings(True, LightingMode.FLOW, 0x66, RgbColor(0, 255, 0)),
            "000201046600ff00",
        )

    def test_lighting_payload_exact_bytes_for_light_off(self) -> None:
        self._assert_payload(
            LightingZone.HOME,
            LightingSettings(False, LightingMode.OFF, 0, RgbColor(0, 0, 0)),
            "0000000000000000",
        )

    def test_lighting_rejects_out_of_range_byte_values(self) -> None:
        cases = [
            LightingSettings(True, LightingMode.FLOW, -1, RgbColor(0, 0, 255)),
            LightingSettings(True, LightingMode.FLOW, 256, RgbColor(0, 0, 255)),
            LightingSettings(True, LightingMode.FLOW, 0x66, RgbColor(-1, 0, 255)),
            LightingSettings(True, LightingMode.FLOW, 0x66, RgbColor(0, 256, 255)),
            LightingSettings(True, LightingMode.FLOW, 0x66, RgbColor(0, 0, 256)),
        ]
        for settings in cases:
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    build_lighting_payload(LightingZone.HOME, settings)

    def test_lighting_rejects_non_plain_int_byte_values(self) -> None:
        cases = [
            LightingSettings(True, LightingMode.FLOW, True, RgbColor(0, 0, 255)),
            LightingSettings(True, LightingMode.FLOW, 0x66, RgbColor(True, 0, 255)),
            LightingSettings(True, LightingMode.FLOW, 0x66, RgbColor(0, 0.0, 255)),
            LightingSettings(True, LightingMode.FLOW, 0x66, RgbColor(0, 0, "255")),
        ]
        for settings in cases:
            with self.subTest(settings=settings):
                with self.assertRaises(TypeError):
                    build_lighting_payload(LightingZone.HOME, settings)  # type: ignore[arg-type]

    def test_lighting_rejects_non_bool_light_on(self) -> None:
        with self.assertRaises(TypeError):
            build_lighting_payload(
                LightingZone.HOME,
                LightingSettings(1, LightingMode.FLOW, 0x66, RgbColor(0, 0, 255)),  # type: ignore[arg-type]
            )

    def test_lighting_rejects_non_lighting_mode(self) -> None:
        with self.assertRaises(TypeError):
            build_lighting_payload(
                LightingZone.HOME,
                LightingSettings(True, 0x04, 0x66, RgbColor(0, 0, 255)),  # type: ignore[arg-type]
            )

    def test_lighting_rejects_non_rgb_color(self) -> None:
        with self.assertRaises(TypeError):
            build_lighting_payload(
                LightingZone.HOME,
                LightingSettings(True, LightingMode.FLOW, 0x66, (0, 0, 255)),  # type: ignore[arg-type]
            )

    def test_lighting_rejects_non_lighting_zone(self) -> None:
        with self.assertRaises(TypeError):
            build_lighting_payload(
                0,  # type: ignore[arg-type]
                LightingSettings(True, LightingMode.FLOW, 0x66, RgbColor(0, 0, 255)),
            )

    def test_lighting_rejects_non_lighting_settings_object(self) -> None:
        with self.assertRaises(TypeError):
            build_lighting_payload(
                LightingZone.HOME,
                (True, LightingMode.FLOW, 0x66, RgbColor(0, 0, 255)),  # type: ignore[arg-type]
            )

    def test_set_lighting_rejects_invalid_inputs_before_win32(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        with self.assertRaises(TypeError):
            service.set_zone_lighting(
                0,  # type: ignore[arg-type]
                LightingSettings(True, LightingMode.FLOW, 0x66, RgbColor(0, 0, 255)),
            )

        with self.assertRaises(TypeError):
            service.set_zone_lighting(
                LightingZone.HOME,
                LightingSettings(True, LightingMode.FLOW, 0x66, (0, 0, 255)),  # type: ignore[arg-type]
            )

        self.assertEqual(rec.events, [])


class TestMi02PathSelection(unittest.TestCase):
    def test_choose_mi02_path_prefers_hid_prefix(self) -> None:
        self.assertEqual(
            _choose_mi02_path([r"\\?\foo#x", _FAKE_PATH]),
            _FAKE_PATH,
        )

    def test_choose_mi02_path_returns_first_non_hid_fallback(self) -> None:
        self.assertEqual(_choose_mi02_path([r"\\?\foo#x"]), r"\\?\foo#x")

    def test_choose_mi02_path_returns_none_for_empty(self) -> None:
        self.assertIsNone(_choose_mi02_path([]))


class TestSettingsServiceLifecycle(unittest.TestCase):
    def test_start_opens_handle_once_and_caches_it(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        self.assertEqual(service.start(), SetPollingRateOutcome.OK)
        self.assertTrue(service.is_started)
        self.assertEqual(service.target_path, _FAKE_PATH)
        self.assertEqual(service.start(), SetPollingRateOutcome.OK)

        self.assertEqual(
            rec.events,
            [
                ("enumerate_paths",),
                ("open_write_handle", _FAKE_PATH),
            ],
        )

    def test_stop_closes_cached_handle(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        service.start()
        service.stop()

        self.assertFalse(service.is_started)
        self.assertIsNone(service.target_path)
        self.assertIn(("close_handle", _HANDLE), rec.events)

    def test_stop_without_start_is_noop(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        service.stop()
        service.stop()

        self.assertEqual(rec.events, [])

    def test_start_no_device_returns_device_not_found(self) -> None:
        rec = _Recorder(paths=[])
        service = _make_service(rec)

        self.assertEqual(service.start(), SetPollingRateOutcome.DEVICE_NOT_FOUND)
        self.assertFalse(service.is_started)
        self.assertEqual(rec.events, [("enumerate_paths",)])

    def test_start_open_failure_returns_open_failed(self) -> None:
        rec = _Recorder(open_result=(None, 5))
        service = _make_service(rec)

        self.assertEqual(service.start(), SetPollingRateOutcome.OPEN_FAILED)
        self.assertFalse(service.is_started)
        self.assertEqual(service.target_path, _FAKE_PATH)


class TestSetPollingRate(unittest.TestCase):
    def test_set_before_start_auto_opens_and_writes_8000hz(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        result = service.set_polling_rate(PollingRate.HZ_8000)

        self.assertEqual(result.outcome, SetPollingRateOutcome.OK)
        self.assertEqual(result.rate, PollingRate.HZ_8000)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE)
        self.assertEqual(result.payload_hex, "001055aa511100060202" + ("00" * 55))
        self.assertTrue(service.is_started)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file"],
        )

    def test_set_reuses_cached_handle_for_multiple_writes(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        first = service.set_polling_rate(PollingRate.HZ_2000)
        second = service.set_polling_rate(PollingRate.HZ_8000)

        self.assertEqual(first.outcome, SetPollingRateOutcome.OK)
        self.assertEqual(second.outcome, SetPollingRateOutcome.OK)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file", "write_file"],
        )
        self.assertEqual(rec.events[2][1], _HANDLE)
        self.assertEqual(rec.events[3][1], _HANDLE)

    def test_set_when_no_device_returns_device_not_found_and_does_not_write(self) -> None:
        rec = _Recorder(paths=[])
        service = _make_service(rec)

        result = service.set_polling_rate(PollingRate.HZ_2000)

        self.assertEqual(result.outcome, SetPollingRateOutcome.DEVICE_NOT_FOUND)
        self.assertEqual(result.rate, PollingRate.HZ_2000)
        self.assertIsNone(result.error_code)
        self.assertIsNone(result.bytes_written)
        self.assertEqual(result.payload_hex, "001055aa511100040202" + ("00" * 55))
        self.assertEqual(rec.events, [("enumerate_paths",)])

    def test_set_when_open_fails_surfaces_open_error(self) -> None:
        rec = _Recorder(open_result=(None, 32))
        service = _make_service(rec)

        result = service.set_polling_rate(PollingRate.HZ_8000)

        self.assertEqual(result.outcome, SetPollingRateOutcome.OPEN_FAILED)
        self.assertEqual(result.error_code, 32)
        self.assertIsNone(result.bytes_written)
        self.assertFalse(service.is_started)
        self.assertEqual(
            rec.events,
            [
                ("enumerate_paths",),
                ("open_write_handle", _FAKE_PATH),
            ],
        )

    def test_write_failure_returns_write_failed_with_error_code(self) -> None:
        rec = _Recorder(write_results=[(False, 995, 0), (False, 995, 0)])
        service = _make_service(rec)

        result = service.set_polling_rate(PollingRate.HZ_8000)

        self.assertEqual(result.outcome, SetPollingRateOutcome.WRITE_FAILED)
        self.assertEqual(result.error_code, 995)
        self.assertEqual(result.bytes_written, 0)
        self.assertTrue(service.is_started)

    def test_set_polling_rate_retries_once_on_write_failed(self) -> None:
        rec = _Recorder(write_results=[(False, 995, 0), (True, 0, HID_FEATURE_REPORT_SIZE)])
        service = _make_service(rec)

        result = service.set_polling_rate(PollingRate.HZ_8000)

        self.assertEqual(result.outcome, SetPollingRateOutcome.OK_WITH_RETRY)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file", "sleep", "write_file"],
        )
        self.assertIn(("sleep", WRITE_RETRY_DELAY_S), rec.events)

    def test_short_write_returns_write_failed(self) -> None:
        rec = _Recorder(
            write_results=[
                (True, 0, HID_FEATURE_REPORT_SIZE - 1),
                (True, 0, HID_FEATURE_REPORT_SIZE - 1),
            ]
        )
        service = _make_service(rec)

        result = service.set_polling_rate(PollingRate.HZ_8000)

        self.assertEqual(result.outcome, SetPollingRateOutcome.WRITE_FAILED)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE - 1)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file", "sleep", "write_file"],
        )

    def test_retry_bytes_written_reports_successful_attempt_only(self) -> None:
        rec = _Recorder(
            write_results=[
                (True, 0, HID_FEATURE_REPORT_SIZE - 1),
                (True, 0, HID_FEATURE_REPORT_SIZE),
            ]
        )
        service = _make_service(rec)

        result = service.set_polling_rate(PollingRate.HZ_8000)

        self.assertEqual(result.outcome, SetPollingRateOutcome.OK_WITH_RETRY)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE)

    def test_disconnect_write_error_invalidates_cached_handle(self) -> None:
        rec = _Recorder(write_results=[(False, 1167, 0), (False, 1167, 0)])
        service = _make_service(rec)

        result = service.set_polling_rate(PollingRate.HZ_8000)

        self.assertEqual(result.outcome, SetPollingRateOutcome.WRITE_FAILED)
        self.assertEqual(result.error_code, 1167)
        self.assertEqual(result.bytes_written, 0)
        self.assertFalse(service.is_started)
        self.assertIsNone(service.target_path)
        self.assertIn(("close_handle", _HANDLE), rec.events)

    def test_stop_closes_handle_even_after_write_failure(self) -> None:
        rec = _Recorder(write_results=[(False, 995, 0), (False, 995, 0)])
        service = _make_service(rec)

        service.set_polling_rate(PollingRate.HZ_2000)
        service.stop()

        self.assertIn(("close_handle", _HANDLE), rec.events)
        self.assertFalse(service.is_started)

    def test_elapsed_ms_uses_injected_clock(self) -> None:
        rec = _Recorder(clock_ticks=[10.0, 10.017])
        service = _make_service(rec)

        result = service.set_polling_rate(PollingRate.HZ_2000)

        self.assertEqual(result.elapsed_ms, 17)


class TestGetPollingRate(unittest.TestCase):
    def test_get_polling_rate_parses_response_byte(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(value=PollingRate.HZ_8000.value)])
        service = _make_service(rec)

        self.assertEqual(service.get_polling_rate(), PollingRate.HZ_8000)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_read_write_handle", "write_file", "read_file"],
        )
        self.assertEqual(rec.events[2][1], _READ_WRITE_HANDLE)
        self.assertEqual(
            rec.events[2][2],
            build_read_query_payload(CATEGORY_POLLING_RATE, POLLING_RATE_SUBCOMMAND),
        )
        self.assertEqual(
            rec.events[3],
            ("read_file", _READ_WRITE_HANDLE, HID_FEATURE_REPORT_SIZE, 1000),
        )

    def test_get_polling_rate_unknown_byte_returns_none(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(value=0xFF)])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_polling_rate())

        self.assertIn("unknown polling rate byte: 0xff", "\n".join(logs.output))

    def test_get_polling_rate_read_error_returns_none(self) -> None:
        rec = _Recorder(read_results=[SettingsServiceError("synthetic read failure")])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_polling_rate())

        self.assertIn("get_polling_rate failed: synthetic read failure", "\n".join(logs.output))

    def test_win32_err_1167_invalidates_read_write_handle(self) -> None:
        rec = _Recorder(
            read_results=[
                SettingsServiceError(
                    "ReadFile failed (Win32 err 1167)",
                    win32_error=1167,
                )
            ],
        )
        service = _make_service(rec)

        self.assertIsNone(service.get_polling_rate())

        self.assertFalse(service.is_started)
        self.assertIsNone(service.target_path)
        self.assertIn(("close_handle", _READ_WRITE_HANDLE), rec.events)

    def test_read_after_handle_invalidation_reopens(self) -> None:
        rec = _Recorder(
            write_results=[(False, 1167, 0)],
            read_results=[_make_read_response(value=PollingRate.HZ_4000.value)],
        )
        read_write_handles = [_READ_WRITE_HANDLE, 0x7171]

        def open_read_write_handle(path: str) -> tuple[int | None, int]:
            rec.events.append(("open_read_write_handle", path))
            return read_write_handles.pop(0), 0

        rec.open_read_write_handle = open_read_write_handle  # type: ignore[method-assign]
        service = _make_service(rec)

        self.assertIsNone(service.get_polling_rate())
        self.assertEqual(service.get_polling_rate(), PollingRate.HZ_4000)

        self.assertIn(("close_handle", _READ_WRITE_HANDLE), rec.events)
        self.assertEqual(
            [event[0] for event in rec.events],
            [
                "enumerate_paths",
                "open_read_write_handle",
                "write_file",
                "close_handle",
                "enumerate_paths",
                "open_read_write_handle",
                "write_file",
                "read_file",
            ],
        )
        self.assertEqual(rec.events[2][1], _READ_WRITE_HANDLE)
        self.assertEqual(rec.events[6][1], 0x7171)
        self.assertEqual(rec.events[7][1], 0x7171)

    def test_read_response_discards_stale_category_before_match(self) -> None:
        rec = _Recorder(read_results=[
            _make_read_response(category=CATEGORY_LIGHTING),
            _make_read_response(
                category=CATEGORY_POLLING_RATE,
                value=PollingRate.HZ_4000.value,
            ),
        ])
        service = _make_service(rec)

        response = service._read_response(
            build_read_query_payload(CATEGORY_POLLING_RATE),
            expected_cat=CATEGORY_POLLING_RATE,
        )

        self.assertEqual(response[5], CATEGORY_POLLING_RATE)
        self.assertEqual(response[7], PollingRate.HZ_4000.value)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_read_write_handle", "write_file", "read_file", "read_file"],
        )

    def test_read_response_discards_write_ack_then_returns_read_response(self) -> None:
        rec = _Recorder(read_results=[
            _make_write_ack_response(category=CATEGORY_POLLING_RATE),
            _make_read_response(
                category=CATEGORY_POLLING_RATE,
                value=PollingRate.HZ_4000.value,
            ),
        ])
        service = _make_service(rec)

        response = service._read_response(
            build_read_query_payload(CATEGORY_POLLING_RATE),
            expected_cat=CATEGORY_POLLING_RATE,
        )

        self.assertEqual(response[1:5], MAGIC_READ_RESPONSE_PREFIX)
        self.assertEqual(response[5], CATEGORY_POLLING_RATE)
        self.assertEqual(response[7], PollingRate.HZ_4000.value)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_read_write_handle", "write_file", "read_file", "read_file"],
        )

    def test_read_response_times_out_if_only_write_acks(self) -> None:
        rec = _Recorder(
            read_results=[_make_write_ack_response(category=CATEGORY_POLLING_RATE)],
            clock_ticks=[0.0, 2.0],
        )
        service = _make_service(rec)

        with self.assertRaisesRegex(
            SettingsServiceError,
            "write ACK received before read response; no matching response before timeout",
        ):
            service._read_response(
                build_read_query_payload(CATEGORY_POLLING_RATE),
                expected_cat=CATEGORY_POLLING_RATE,
            )

    def test_read_response_validates_magic_prefix(self) -> None:
        response = bytearray(_make_read_response())
        response[1:5] = MAGIC_WRITE_PREFIX
        rec = _Recorder(read_results=[bytes(response)])
        service = _make_service(rec)

        with self.assertRaisesRegex(SettingsServiceError, "unexpected response magic"):
            service._read_response(
                build_read_query_payload(CATEGORY_POLLING_RATE),
                expected_cat=CATEGORY_POLLING_RATE,
            )

    def test_read_response_validates_category_echo(self) -> None:
        rec = _Recorder(read_results=[
            _make_read_response(category=CATEGORY_LIGHTING),
            SettingsServiceError("synthetic timeout"),
        ])
        service = _make_service(rec)

        with self.assertRaisesRegex(
            SettingsServiceError,
            "category mismatch: expected 0x11, got 0x10; no matching response before timeout",
        ):
            service._read_response(
                build_read_query_payload(CATEGORY_POLLING_RATE),
                expected_cat=CATEGORY_POLLING_RATE,
            )


class TestSetStepSize(unittest.TestCase):
    def test_set_default_73_writes_payload(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        result = service.set_step_size(STEP_SIZE_VALUE_DEFAULT)

        self.assertEqual(result.outcome, SetStepSizeOutcome.OK)
        self.assertEqual(result.value, 73)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE)
        self.assertEqual(
            result.payload_hex,
            build_step_size_payload(STEP_SIZE_VALUE_DEFAULT).hex(),
        )
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file"],
        )

    def test_set_when_no_device_returns_device_not_found(self) -> None:
        rec = _Recorder(paths=[])
        service = _make_service(rec)

        result = service.set_step_size(100)

        self.assertEqual(result.outcome, SetStepSizeOutcome.DEVICE_NOT_FOUND)
        self.assertEqual(result.value, 100)
        self.assertIsNone(result.bytes_written)
        self.assertEqual(rec.events, [("enumerate_paths",)])

    def test_set_step_size_rejects_out_of_range_before_win32(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        with self.assertRaises(ValueError):
            service.set_step_size(0)
        with self.assertRaises(ValueError):
            service.set_step_size(256)

        self.assertEqual(rec.events, [])


class TestGetStepSize(unittest.TestCase):
    def test_get_step_size_parses_response_byte(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_STEP_SIZE, value=STEP_SIZE_VALUE_DEFAULT,
        )])
        service = _make_service(rec)

        self.assertEqual(service.get_step_size(), STEP_SIZE_VALUE_DEFAULT)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_read_write_handle", "write_file", "read_file"],
        )
        self.assertEqual(
            rec.events[2][2],
            build_read_query_payload(CATEGORY_STEP_SIZE, STEP_SIZE_SUBCOMMAND),
        )

    def test_get_step_size_out_of_range_byte_returns_none(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_STEP_SIZE, value=0x00,
        )])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_step_size())

        self.assertIn("step_size byte out of range: 0x00", "\n".join(logs.output))


class TestSetStepSizeVerified(unittest.TestCase):
    """The verify-and-retry step_size setter (revert-to-1 fix, 2026-06-23).

    The firmware silently rejects a fraction of cat-0x0d step_size writes
    (WriteFile OK, device never commits). ``set_step_size_verified`` writes,
    settles, reads back via ``get_step_size``, and re-writes on a mismatch up to
    ``attempts`` times -- surfacing ``VERIFY_FAILED`` only when the device never
    commits. The device read-back is mocked here via the recorder's scripted
    ``read_results``; true firmware commit can only be confirmed on real
    hardware (operator smoke: set step_size=73, Apply with device settings,
    confirm it holds and never reverts to 1).
    """

    @staticmethod
    def _step_size_set_writes(rec: _Recorder, value: int) -> int:
        """Count the step_size *set* writes (excludes read-query writes)."""

        target = build_step_size_payload(value)
        return sum(
            1
            for event in rec.events
            if event[0] == "write_file" and event[2] == target
        )

    def test_first_write_not_committed_then_retries_and_commits(self) -> None:
        # (a) First readback shows the floor (1, not committed) -> one retry ->
        # second readback shows the written value -> verified success.
        value = STEP_SIZE_VALUE_DEFAULT  # 73 (a valid, non-floor value)
        rec = _Recorder(
            read_results=[
                _make_read_response(category=CATEGORY_STEP_SIZE, value=STEP_SIZE_VALUE_MIN),
                _make_read_response(category=CATEGORY_STEP_SIZE, value=value),
            ]
        )
        service = _make_service(rec)

        result = service.set_step_size_verified(value)

        self.assertEqual(result.outcome, SetStepSizeOutcome.OK)
        self.assertEqual(result.value, value)
        # Exactly two device writes of the value: the rejected one + the retry.
        self.assertEqual(self._step_size_set_writes(rec, value), 2)
        # The settle seam ran between write and read-back.
        self.assertIn(("sleep", 0.1), rec.events)

    def test_never_commits_returns_verify_failed_after_attempts(self) -> None:
        # (b) The device never commits across the full attempt budget -> the
        # write layer reports OK each time, but the read-back keeps showing the
        # floor -> the setter surfaces VERIFY_FAILED (not a false success).
        value = STEP_SIZE_VALUE_DEFAULT
        attempts = 3
        rec = _Recorder(
            read_results=[
                _make_read_response(category=CATEGORY_STEP_SIZE, value=STEP_SIZE_VALUE_MIN)
                for _ in range(attempts)
            ]
        )
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING"):
            result = service.set_step_size_verified(value, attempts=attempts)

        self.assertEqual(result.outcome, SetStepSizeOutcome.VERIFY_FAILED)
        self.assertEqual(result.value, value)
        # One device write per attempt -- all rejected.
        self.assertEqual(self._step_size_set_writes(rec, value), attempts)

    def test_commits_first_try_issues_single_write(self) -> None:
        # (c) The first readback already matches -> one write, no retry, success.
        value = 200
        rec = _Recorder(
            read_results=[_make_read_response(category=CATEGORY_STEP_SIZE, value=value)]
        )
        service = _make_service(rec)

        result = service.set_step_size_verified(value)

        self.assertEqual(result.outcome, SetStepSizeOutcome.OK)
        self.assertEqual(result.value, value)
        self.assertEqual(self._step_size_set_writes(rec, value), 1)

    def test_write_layer_failure_returns_immediately_unwrapped(self) -> None:
        # A write-layer failure (no device) has no commit ambiguity to resolve:
        # it is returned as-is, with no read-back attempted.
        rec = _Recorder(paths=[])
        service = _make_service(rec)

        result = service.set_step_size_verified(100)

        self.assertEqual(result.outcome, SetStepSizeOutcome.DEVICE_NOT_FOUND)
        self.assertEqual(result.value, 100)
        # No read-back issued on a write-layer failure.
        self.assertNotIn("read_file", [event[0] for event in rec.events])

    def test_readback_timeout_degrades_to_unverified_write_no_crash(self) -> None:
        # CRASH REGRESSION: the verify read-back can RAISE a
        # TimeoutError on real hardware (HID read timed out after 1000ms). It
        # used to propagate out of set_step_size_verified to the excepthook and
        # crash the app from the live slider path. The setter must now CATCH it,
        # retry within budget, and -- when every attempt only ever failed to
        # READ (never read back a different value) -- DEGRADE to the write's OK
        # result. A verified write must never be worse than a plain one.
        value = STEP_SIZE_VALUE_DEFAULT
        attempts = 3
        rec = _Recorder(
            read_results=[
                TimeoutError("HID read timed out after 1000ms") for _ in range(attempts)
            ]
        )
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING"):
            # Must NOT raise (the crash). On base, the TimeoutError escapes here.
            result = service.set_step_size_verified(value, attempts=attempts)

        # Degraded to the underlying write's success, NOT a false VERIFY_FAILED.
        self.assertEqual(result.outcome, SetStepSizeOutcome.OK)
        self.assertEqual(result.value, value)
        # One device set-write per attempt, each followed by a read-back raise.
        self.assertEqual(self._step_size_set_writes(rec, value), attempts)

    def test_readback_oserror_degrades_to_unverified_write_no_crash(self) -> None:
        # Sibling of the TimeoutError case for the raw ReadFile OSError the HID
        # layer can also raise. The setter catches OSError (which also covers
        # TimeoutError) and degrades rather than crashing or false-failing.
        value = 200
        attempts = 2
        rec = _Recorder(
            read_results=[OSError("ReadFile device error") for _ in range(attempts)]
        )
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING"):
            result = service.set_step_size_verified(value, attempts=attempts)

        self.assertEqual(result.outcome, SetStepSizeOutcome.OK)
        self.assertEqual(result.value, value)
        self.assertEqual(self._step_size_set_writes(rec, value), attempts)

    def test_readback_none_degrades_not_verify_failed(self) -> None:
        # get_step_size returns None when the read fails gracefully (a
        # SettingsServiceError it swallows) or yields an out-of-range byte. None
        # means "could not read it back", NOT a confirmed wrong value, so it must
        # degrade to the write result -- never a false VERIFY_FAILED. (On base,
        # None was treated as a mismatch and surfaced VERIFY_FAILED.)
        value = STEP_SIZE_VALUE_DEFAULT
        attempts = 2
        rec = _Recorder(
            read_results=[
                SettingsServiceError("synthetic read failure") for _ in range(attempts)
            ]
        )
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING"):
            result = service.set_step_size_verified(value, attempts=attempts)

        self.assertEqual(result.outcome, SetStepSizeOutcome.OK)
        self.assertEqual(result.value, value)

    def test_confirmed_mismatch_then_readback_raise_still_verify_failed(self) -> None:
        # A CONFIRMED mismatch (read SUCCEEDED, showed the floor) on one attempt
        # is real evidence the device rejected the write; a later read-back that
        # raises must not erase it. With at least one confirmed mismatch in the
        # run, the budget-exhausted result is VERIFY_FAILED, not a degraded OK.
        value = STEP_SIZE_VALUE_DEFAULT
        rec = _Recorder(
            read_results=[
                # attempt 1: read succeeds, device painted the floor -> mismatch
                _make_read_response(category=CATEGORY_STEP_SIZE, value=STEP_SIZE_VALUE_MIN),
                # attempt 2: read-back raises (couldn't verify)
                TimeoutError("HID read timed out after 1000ms"),
            ]
        )
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING"):
            result = service.set_step_size_verified(value, attempts=2)

        self.assertEqual(result.outcome, SetStepSizeOutcome.VERIFY_FAILED)
        self.assertEqual(result.value, value)


class TestGetVibration(unittest.TestCase):
    def test_get_vibration_parses_response_payload(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_VIBRATION,
            payload=bytes([15, 16, 17, 18, TriggerVibrationMode.NATIVE.value]),
        )])
        service = _make_service(rec)

        self.assertEqual(
            service.get_vibration(),
            VibrationSettings(15, 16, 17, 18, TriggerVibrationMode.NATIVE),
        )
        self.assertEqual(
            rec.events[2][2],
            build_read_query_payload(CATEGORY_VIBRATION, VIBRATION_SUBCOMMAND),
        )

    def test_get_vibration_unknown_mode_returns_none(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_VIBRATION,
            payload=bytes([15, 15, 15, 15, 0xFF]),
        )])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_vibration())

        self.assertIn("unknown vibration mode byte: 0xff", "\n".join(logs.output))

    def test_get_vibration_read_error_returns_none(self) -> None:
        rec = _Recorder(read_results=[SettingsServiceError("synthetic vibration read failure")])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_vibration())

        self.assertIn("get_vibration failed: synthetic vibration read failure", "\n".join(logs.output))


class TestGetMotionSettings(unittest.TestCase):
    def test_get_motion_settings_parses_baseline_response(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_MOTION,
            payload=bytes([0x00, 0x00, 0x06, MotionMappingMode.CONTINUOUS.value, 0x32]),
        )])
        service = _make_service(rec)

        self.assertEqual(
            service.get_motion_settings(),
            MotionSettings(
                target=MotionMappingTarget.DISABLED,
                trigger_key=0x06,
                mode=MotionMappingMode.CONTINUOUS,
                sensitivity=50,
            ),
        )
        self.assertEqual(
            rec.events[2][2],
            build_read_query_payload(CATEGORY_MOTION, MOTION_SUBCOMMAND),
        )

    def test_get_motion_settings_parses_left_joystick_response(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_MOTION,
            payload=bytes([MotionMappingTarget.LEFT_JOYSTICK.value, 0x00, 0x06, 0x01, 0x32]),
        )])
        service = _make_service(rec)

        self.assertEqual(
            service.get_motion_settings(),
            MotionSettings(
                target=MotionMappingTarget.LEFT_JOYSTICK,
                trigger_key=0x06,
                mode=MotionMappingMode.CONTINUOUS,
                sensitivity=50,
            ),
        )

    def test_get_motion_settings_unknown_target_returns_none(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_MOTION,
            payload=bytes([0xFF, 0x00, 0x06, 0x01, 0x32]),
        )])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_motion_settings())

        self.assertIn("unknown motion target byte: 0xff", "\n".join(logs.output))

    def test_get_motion_settings_unknown_mode_returns_none(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_MOTION,
            payload=bytes([0x00, 0x00, 0x06, 0xFF, 0x32]),
        )])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_motion_settings())

        self.assertIn("unknown motion mode byte: 0xff", "\n".join(logs.output))

    def test_get_motion_settings_read_error_returns_none(self) -> None:
        rec = _Recorder(read_results=[SettingsServiceError("synthetic motion read failure")])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_motion_settings())

        self.assertIn("get_motion_settings failed: synthetic motion read failure", "\n".join(logs.output))


class TestGetButtonBinding(unittest.TestCase):
    def test_get_button_binding_parses_controller_target(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_BUTTON_BINDING,
            payload=bytes([
                ButtonSlot.A.value,
                0x01,
                0x00,
                ControllerButtonTarget.A.value,
            ]),
        )])
        service = _make_service(rec)

        self.assertEqual(
            service.get_button_binding(ButtonSlot.A),
            ButtonMapping.controller_button(ControllerButtonTarget.A),
        )
        self.assertEqual(
            rec.events[2][2],
            build_read_query_payload(CATEGORY_BUTTON_BINDING, selector=ButtonSlot.A.value),
        )

    def test_get_button_binding_discards_stale_slot_before_match(self) -> None:
        rec = _Recorder(read_results=[
            _make_read_response(
                category=CATEGORY_BUTTON_BINDING,
                payload=bytes([
                    ButtonSlot.B.value,
                    0x01,
                    0x00,
                    ControllerButtonTarget.A.value,
                ]),
            ),
            _make_read_response(
                category=CATEGORY_BUTTON_BINDING,
                payload=bytes([
                    ButtonSlot.A.value,
                    0x01,
                    0x00,
                    ControllerButtonTarget.B.value,
                ]),
            ),
        ])
        service = _make_service(rec)

        self.assertEqual(
            service.get_button_binding(ButtonSlot.A),
            ButtonMapping.controller_button(ControllerButtonTarget.B),
        )
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_read_write_handle", "write_file", "read_file", "read_file"],
        )

    def test_get_button_binding_selector_mismatch_returns_none_after_timeout(self) -> None:
        # The real _read_response filters a wrong-selector frame; pairing a stale
        # slot-B response with a timeout makes every attempt fail the same clean
        # way, so the per-slot read still resolves to None once the bounded retry
        # budget is spent (rather than masking a wrong slot as a real read).
        per_attempt = [
            _make_read_response(
                category=CATEGORY_BUTTON_BINDING,
                payload=bytes([
                    ButtonSlot.B.value,
                    0x01,
                    0x00,
                    ControllerButtonTarget.A.value,
                ]),
            ),
            SettingsServiceError("synthetic binding timeout"),
        ]
        rec = _Recorder(read_results=per_attempt * BUTTON_BINDING_READ_ATTEMPTS)
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_button_binding(ButtonSlot.A))

        self.assertIn("selector mismatch: expected 0x05, got 0x06", "\n".join(logs.output))
        # One discard + one timeout per attempt, repeated across the budget.
        read_calls = sum(1 for event in rec.events if event[0] == "read_file")
        self.assertEqual(read_calls, 2 * BUTTON_BINDING_READ_ATTEMPTS)

    def test_get_button_binding_unknown_controller_target_returns_none(self) -> None:
        # 0xFF is not a known ControllerButtonTarget. A genuinely-unknown target
        # is retried within budget (a garbled partial read can fabricate one)
        # but must still resolve to None -- it must not loop forever.
        unknown = _make_read_response(
            category=CATEGORY_BUTTON_BINDING,
            payload=bytes([ButtonSlot.A.value, 0x01, 0x00, 0xFF]),
        )
        rec = _Recorder(read_results=[unknown] * BUTTON_BINDING_READ_ATTEMPTS)
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_button_binding(ButtonSlot.A))

        self.assertIn("unknown controller-button target: 0xff", "\n".join(logs.output))
        read_calls = sum(1 for event in rec.events if event[0] == "read_file")
        self.assertEqual(read_calls, BUTTON_BINDING_READ_ATTEMPTS)

    def test_get_button_binding_raw_target_kind_returns_mapping(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_BUTTON_BINDING,
            payload=bytes([ButtonSlot.A.value, 0x02, 0x03, 0x04]),
        )])
        service = _make_service(rec)

        self.assertEqual(
            service.get_button_binding(ButtonSlot.A),
            ButtonMapping(target_kind=0x02, target_low=0x03, target_value=0x04),
        )

    def test_get_button_binding_read_error_returns_none(self) -> None:
        rec = _Recorder(read_results=[
            SettingsServiceError("synthetic binding read failure")
            for _ in range(BUTTON_BINDING_READ_ATTEMPTS)
        ])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_button_binding(ButtonSlot.A))

        self.assertIn(
            "get_button_binding(slot=<ButtonSlot.A: 5>) failed: synthetic binding read failure",
            "\n".join(logs.output),
        )
        # One read per attempt; a delay only between attempts (never before the
        # first), all routed through the injected no-op sleep seam.
        read_calls = sum(1 for event in rec.events if event[0] == "read_file")
        self.assertEqual(read_calls, BUTTON_BINDING_READ_ATTEMPTS)
        sleep_calls = [event for event in rec.events if event[0] == "sleep"]
        self.assertEqual(
            sleep_calls,
            [("sleep", BUTTON_BINDING_READ_RETRY_DELAY_S)]
            * (BUTTON_BINDING_READ_ATTEMPTS - 1),
        )

    def test_get_button_binding_rejects_non_slot_before_io(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        with self.assertRaises(TypeError):
            service.get_button_binding(5)  # type: ignore[arg-type]

        self.assertEqual(rec.events, [])

    def test_get_button_binding_retries_transient_error_then_succeeds(self) -> None:
        # The flaky-readback case: a single transient read drop is re-attempted,
        # so the slot still reads back correctly instead of going None and
        # triggering a false "mismatch after restore".
        rec = _Recorder()
        service = _make_service(rec)
        good = _make_read_response(
            category=CATEGORY_BUTTON_BINDING,
            payload=bytes(
                [ButtonSlot.A.value, 0x01, 0x00, ControllerButtonTarget.A.value]
            ),
        )
        scripted = _ScriptedReadResponse(
            [SettingsServiceError("transient binding read drop"), good]
        )
        service._read_response = scripted  # type: ignore[assignment]

        self.assertEqual(
            service.get_button_binding(ButtonSlot.A),
            ButtonMapping.controller_button(ControllerButtonTarget.A),
        )
        self.assertEqual(scripted.calls, 2)
        # Exactly one inter-attempt delay, on the injected no-op seam.
        self.assertEqual(
            [event for event in rec.events if event[0] == "sleep"],
            [("sleep", BUTTON_BINDING_READ_RETRY_DELAY_S)],
        )

    def test_get_button_binding_retries_slot_mismatch_then_succeeds(self) -> None:
        # A stale/interleaved frame carrying the wrong slot byte is treated as
        # transient and re-read. (The real _read_response filters this out, so it
        # is driven directly through a scripted seam.)
        rec = _Recorder()
        service = _make_service(rec)
        stale = _make_read_response(
            category=CATEGORY_BUTTON_BINDING,
            payload=bytes(
                [ButtonSlot.B.value, 0x01, 0x00, ControllerButtonTarget.A.value]
            ),
        )
        good = _make_read_response(
            category=CATEGORY_BUTTON_BINDING,
            payload=bytes(
                [ButtonSlot.A.value, 0x01, 0x00, ControllerButtonTarget.B.value]
            ),
        )
        scripted = _ScriptedReadResponse([stale, good])
        service._read_response = scripted  # type: ignore[assignment]

        with self.assertLogs(
            "zd_app.services.settings_service", level="WARNING"
        ) as logs:
            self.assertEqual(
                service.get_button_binding(ButtonSlot.A),
                ButtonMapping.controller_button(ControllerButtonTarget.B),
            )
        self.assertEqual(scripted.calls, 2)
        self.assertIn(
            "slot mismatch in response: queried 0x05, got 0x06",
            "\n".join(logs.output),
        )

    def test_get_button_binding_retries_unknown_target_then_succeeds(self) -> None:
        # A garbled partial read can fabricate an out-of-range target byte; a
        # retry that reads cleanly recovers the real mapping.
        rec = _Recorder()
        service = _make_service(rec)
        garbled = _make_read_response(
            category=CATEGORY_BUTTON_BINDING,
            payload=bytes([ButtonSlot.A.value, 0x01, 0x00, 0xFF]),
        )
        good = _make_read_response(
            category=CATEGORY_BUTTON_BINDING,
            payload=bytes(
                [ButtonSlot.A.value, 0x01, 0x00, ControllerButtonTarget.A.value]
            ),
        )
        scripted = _ScriptedReadResponse([garbled, good])
        service._read_response = scripted  # type: ignore[assignment]

        self.assertEqual(
            service.get_button_binding(ButtonSlot.A),
            ButtonMapping.controller_button(ControllerButtonTarget.A),
        )
        self.assertEqual(scripted.calls, 2)

    def test_get_button_binding_happy_path_issues_single_read(self) -> None:
        # A clean first read must not pay any retry cost: exactly one read and
        # no inter-attempt sleep (no latency regression on the common path).
        rec = _Recorder()
        service = _make_service(rec)
        good = _make_read_response(
            category=CATEGORY_BUTTON_BINDING,
            payload=bytes(
                [ButtonSlot.A.value, 0x01, 0x00, ControllerButtonTarget.A.value]
            ),
        )
        scripted = _ScriptedReadResponse([good, good, good])
        service._read_response = scripted  # type: ignore[assignment]

        self.assertEqual(
            service.get_button_binding(ButtonSlot.A),
            ButtonMapping.controller_button(ControllerButtonTarget.A),
        )
        self.assertEqual(scripted.calls, 1)
        self.assertEqual([event for event in rec.events if event[0] == "sleep"], [])

    def test_get_button_binding_persistent_failure_returns_none_after_attempts(
        self,
    ) -> None:
        rec = _Recorder()
        service = _make_service(rec)
        # Supply more failures than the budget to prove it STOPS at the cap
        # rather than draining the sequence or looping forever.
        scripted = _ScriptedReadResponse(
            [
                SettingsServiceError(f"persistent drop {i}")
                for i in range(BUTTON_BINDING_READ_ATTEMPTS + 2)
            ]
        )
        service._read_response = scripted  # type: ignore[assignment]

        self.assertIsNone(service.get_button_binding(ButtonSlot.A))
        self.assertEqual(scripted.calls, BUTTON_BINDING_READ_ATTEMPTS)

    def test_get_button_binding_retry_uses_injected_sleep_not_real_time(self) -> None:
        # Test-pollution guard: the retry delay must ride the injected seam, so a
        # real time.sleep is never touched (a real sleep once contaminated a
        # Health Report global-mock test).
        from zd_app.services import settings_service as ss

        rec = _Recorder()
        service = _make_service(rec)
        scripted = _ScriptedReadResponse(
            [SettingsServiceError("drop") for _ in range(BUTTON_BINDING_READ_ATTEMPTS)]
        )
        service._read_response = scripted  # type: ignore[assignment]

        def _explode(_seconds: float) -> None:
            raise AssertionError("real time.sleep called in button-binding retry")

        with mock.patch.object(ss.time, "sleep", _explode):
            self.assertIsNone(service.get_button_binding(ButtonSlot.A))

        # Delays went through the injected seam instead of real time.sleep.
        self.assertEqual(
            sum(1 for event in rec.events if event[0] == "sleep"),
            BUTTON_BINDING_READ_ATTEMPTS - 1,
        )

    def test_button_binding_retry_constants_are_bounded(self) -> None:
        # Bounded latency: a few attempts with a short delay so a fully
        # unresponsive device can't balloon get_all_settings.
        self.assertGreater(BUTTON_BINDING_READ_ATTEMPTS, 1)
        self.assertLessEqual(BUTTON_BINDING_READ_ATTEMPTS, 5)
        self.assertGreater(BUTTON_BINDING_READ_RETRY_DELAY_S, 0.0)
        self.assertLessEqual(BUTTON_BINDING_READ_RETRY_DELAY_S, 0.05)


class TestGetZoneLighting(unittest.TestCase):
    def test_get_zone_lighting_parses_response_payload(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_LIGHTING,
            payload=bytes([
                LightingZone.HOME.value,
                0x01,
                LightingMode.FADE.value,
                0x66,
                0x01,
                0x02,
                0x03,
            ]),
        )])
        service = _make_service(rec)

        self.assertEqual(
            service.get_zone_lighting(LightingZone.HOME),
            LightingSettings(True, LightingMode.FADE, 0x66, RgbColor(1, 2, 3)),
        )
        self.assertEqual(
            rec.events[2][2],
            build_read_query_payload(CATEGORY_LIGHTING, selector=LightingZone.HOME.value),
        )

    def test_get_zone_lighting_discards_stale_zone_before_match(self) -> None:
        rec = _Recorder(read_results=[
            _make_read_response(
                category=CATEGORY_LIGHTING,
                payload=bytes([
                    LightingZone.LEFT_LIGHT.value,
                    0x01,
                    LightingMode.FLOW.value,
                    0x66,
                    0x00,
                    0x00,
                    0xFF,
                ]),
            ),
            _make_read_response(
                category=CATEGORY_LIGHTING,
                payload=bytes([
                    LightingZone.HOME.value,
                    0x01,
                    LightingMode.FADE.value,
                    0x77,
                    0x01,
                    0x02,
                    0x03,
                ]),
            ),
        ])
        service = _make_service(rec)

        self.assertEqual(
            service.get_zone_lighting(LightingZone.HOME),
            LightingSettings(True, LightingMode.FADE, 0x77, RgbColor(1, 2, 3)),
        )
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_read_write_handle", "write_file", "read_file", "read_file"],
        )

    def test_get_zone_lighting_selector_mismatch_returns_none_after_timeout(self) -> None:
        rec = _Recorder(read_results=[
            _make_read_response(
                category=CATEGORY_LIGHTING,
                payload=bytes([
                    LightingZone.LEFT_LIGHT.value,
                    0x01,
                    LightingMode.FLOW.value,
                    0x66,
                    0x00,
                    0x00,
                    0xFF,
                ]),
            ),
            SettingsServiceError("synthetic lighting timeout"),
        ])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_zone_lighting(LightingZone.HOME))

        self.assertIn("selector mismatch: expected 0x00, got 0x01", "\n".join(logs.output))

    def test_get_zone_lighting_unknown_mode_returns_none(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_LIGHTING,
            payload=bytes([
                LightingZone.HOME.value,
                0x01,
                0xFF,
                0x66,
                0x00,
                0x00,
                0xFF,
            ]),
        )])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_zone_lighting(LightingZone.HOME))

        self.assertIn("unknown lighting mode byte: 0xff", "\n".join(logs.output))

    def test_get_zone_lighting_read_error_returns_none(self) -> None:
        rec = _Recorder(read_results=[SettingsServiceError("synthetic lighting read failure")])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_zone_lighting(LightingZone.HOME))

        self.assertIn(
            "get_zone_lighting(zone=<LightingZone.HOME: 0>) failed: synthetic lighting read failure",
            "\n".join(logs.output),
        )

    def test_get_zone_lighting_rejects_non_zone_before_io(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        with self.assertRaises(TypeError):
            service.get_zone_lighting(0)  # type: ignore[arg-type]

        self.assertEqual(rec.events, [])


class TestGetDeadzones(unittest.TestCase):
    def test_get_deadzones_parses_response_payload(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_STICK_DEADZONE,
            payload=bytes([1, 2, 3, 4]),
        )])
        service = _make_service(rec)

        self.assertEqual(service.get_deadzones(), StickDeadzones(1, 2, 3, 4))
        self.assertEqual(
            rec.events[2][2],
            build_read_query_payload(CATEGORY_STICK_DEADZONE, DEADZONE_SUBCOMMAND),
        )

    def test_get_deadzones_read_error_returns_none(self) -> None:
        rec = _Recorder(read_results=[SettingsServiceError("synthetic deadzone read failure")])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_deadzones())

        self.assertIn("get_deadzones failed: synthetic deadzone read failure", "\n".join(logs.output))


class TestGetAxisInversion(unittest.TestCase):
    # Captured response layout — verified on hardware. Payload bytes here
    # land at wrapper-indexed response[7..10]: [LX, LY, RX, RY].

    def test_get_axis_inversion_parses_both_sticks_distinctly(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_STICK_INVERSION,
            payload=bytes([
                0x01,  # byte 7: LEFT  X = inverted
                0x00,  # byte 8: LEFT  Y = not inverted
                0x00,  # byte 9: RIGHT X = not inverted
                0x01,  # byte 10: RIGHT Y = inverted
            ]),
        )])
        service = _make_service(rec)

        self.assertEqual(
            service.get_axis_inversion(),
            (
                AxisInversion(x_inverted=True, y_inverted=False),
                AxisInversion(x_inverted=False, y_inverted=True),
            ),
        )
        self.assertEqual(
            rec.events[2][2],
            build_read_query_payload(CATEGORY_STICK_INVERSION, INVERSION_SUBCOMMAND),
        )

    def test_get_axis_inversion_all_off(self) -> None:
        """All-zero response decodes to all-off on both sticks. Default state."""
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_STICK_INVERSION,
            payload=bytes([0x00, 0x00, 0x00, 0x00]),
        )])
        service = _make_service(rec)

        self.assertEqual(
            service.get_axis_inversion(),
            (
                AxisInversion(x_inverted=False, y_inverted=False),
                AxisInversion(x_inverted=False, y_inverted=False),
            ),
        )

    def test_get_axis_inversion_all_on(self) -> None:
        """All-ones response decodes to all-on on both sticks."""
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_STICK_INVERSION,
            payload=bytes([0x01, 0x01, 0x01, 0x01]),
        )])
        service = _make_service(rec)

        self.assertEqual(
            service.get_axis_inversion(),
            (
                AxisInversion(x_inverted=True, y_inverted=True),
                AxisInversion(x_inverted=True, y_inverted=True),
            ),
        )

    def test_get_axis_inversion_captured_hardware_response_L_x_only(self) -> None:
        """Regression: decode the exact 65-byte response captured after
        writing L=(x=True, y=False).
        """
        response = bytearray(HID_FEATURE_REPORT_SIZE)
        response[0] = 0x00
        response[1:5] = MAGIC_READ_RESPONSE_PREFIX
        response[5] = CATEGORY_STICK_INVERSION
        response[6] = 0x00
        response[7] = 0x01  # L X = on
        response[8] = 0x00  # L Y = off
        response[9] = 0x00  # R X = off
        response[10] = 0x00  # R Y = off

        rec = _Recorder(read_results=[bytes(response)])
        service = _make_service(rec)

        self.assertEqual(
            service.get_axis_inversion(),
            (
                AxisInversion(x_inverted=True, y_inverted=False),
                AxisInversion(x_inverted=False, y_inverted=False),
            ),
        )

    def test_get_axis_inversion_captured_hardware_response_R_xy(self) -> None:
        """Regression: decode the exact response captured after writing
        R=(x=True, y=True) while L stayed off. See probe step R_xy.
        """
        response = bytearray(HID_FEATURE_REPORT_SIZE)
        response[0] = 0x00
        response[1:5] = MAGIC_READ_RESPONSE_PREFIX
        response[5] = CATEGORY_STICK_INVERSION
        response[6] = 0x00
        response[7] = 0x00  # L X = off
        response[8] = 0x00  # L Y = off
        response[9] = 0x01  # R X = on
        response[10] = 0x01  # R Y = on

        rec = _Recorder(read_results=[bytes(response)])
        service = _make_service(rec)

        self.assertEqual(
            service.get_axis_inversion(),
            (
                AxisInversion(x_inverted=False, y_inverted=False),
                AxisInversion(x_inverted=True, y_inverted=True),
            ),
        )

    def test_get_axis_inversion_read_error_returns_none(self) -> None:
        rec = _Recorder(read_results=[SettingsServiceError("synthetic inversion read failure")])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_axis_inversion())

        self.assertIn("get_axis_inversion failed: synthetic inversion read failure", "\n".join(logs.output))


class TestGetTriggerSettings(unittest.TestCase):
    def test_get_trigger_settings_parses_both_triggers_distinctly(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_TRIGGER_SETTINGS,
            payload=bytes([
                10,
                20,
                80,
                90,
                TriggerMode.SHORT.value,
                TriggerMode.LONG.value,
                0,
                0,
            ]),
        )])
        service = _make_service(rec)

        self.assertEqual(
            service.get_trigger_settings(),
            (
                TriggerSettings(10, 90, TriggerMode.SHORT),
                TriggerSettings(20, 80, TriggerMode.LONG),
            ),
        )
        self.assertEqual(
            rec.events[2][2],
            build_read_query_payload(CATEGORY_TRIGGER_SETTINGS, TRIGGER_SUBCOMMAND),
        )

    def test_get_trigger_settings_matches_empirical_interleaved_layout(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_TRIGGER_SETTINGS,
            payload=bytes([
                0x0A,
                0x21,
                0x4D,
                0x50,
                TriggerMode.LONG.value,
                TriggerMode.SHORT.value,
                0,
                0,
            ]),
        )])
        service = _make_service(rec)

        self.assertEqual(
            service.get_trigger_settings(),
            (
                TriggerSettings(10, 80, TriggerMode.LONG),
                TriggerSettings(33, 77, TriggerMode.SHORT),
            ),
        )

    def test_get_trigger_settings_unknown_mode_returns_none(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_TRIGGER_SETTINGS,
            payload=bytes([
                10,
                20,
                80,
                90,
                0xFF,
                TriggerMode.LONG.value,
                0,
                0,
            ]),
        )])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_trigger_settings())

        self.assertIn("unknown trigger mode byte:", "\n".join(logs.output))

    def test_get_trigger_settings_read_error_returns_none(self) -> None:
        rec = _Recorder(read_results=[SettingsServiceError("synthetic trigger read failure")])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_trigger_settings())

        self.assertIn("get_trigger_settings failed: synthetic trigger read failure", "\n".join(logs.output))


class TestGetSensitivityCurves(unittest.TestCase):
    def test_get_sensitivity_curves_parses_both_sticks_distinctly(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_STICK_SENSITIVITY,
            payload=bytes([
                1,
                2,
                3,
                4,
                5,
                6,
                7,
                8,
                9,
                10,
                11,
                12,
            ]),
        )])
        service = _make_service(rec)

        self.assertEqual(
            service.get_sensitivity_curves(),
            (
                (
                    SensitivityAnchor(1, 2),
                    SensitivityAnchor(3, 4),
                    SensitivityAnchor(5, 6),
                ),
                (
                    SensitivityAnchor(7, 8),
                    SensitivityAnchor(9, 10),
                    SensitivityAnchor(11, 12),
                ),
            ),
        )
        self.assertEqual(
            rec.events[2][2],
            build_read_query_payload(CATEGORY_STICK_SENSITIVITY, SENSITIVITY_SUBCOMMAND),
        )

    def test_get_sensitivity_curves_matches_empirical_back_to_back_layout(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_STICK_SENSITIVITY,
            payload=bytes([
                0x00,
                0x00,
                0x28,
                0x1E,
                0x64,
                0x64,
                0x0B,
                0x16,
                0x21,
                0x2C,
                0x4D,
                0x58,
            ]),
        )])
        service = _make_service(rec)

        self.assertEqual(
            service.get_sensitivity_curves(),
            (
                (
                    SensitivityAnchor(0, 0),
                    SensitivityAnchor(40, 30),
                    SensitivityAnchor(100, 100),
                ),
                (
                    SensitivityAnchor(11, 22),
                    SensitivityAnchor(33, 44),
                    SensitivityAnchor(77, 88),
                ),
            ),
        )

    def test_get_sensitivity_curves_matches_capture_v3_stuck_custom_shape(self) -> None:
        rec = _Recorder(read_results=[_make_read_response(
            category=CATEGORY_STICK_SENSITIVITY,
            payload=bytes([
                0x32,
                0x32,
                0x64,
                0x64,
                0x00,
                0x00,
                0x32,
                0x32,
                0x64,
                0x64,
                0x00,
                0x00,
            ]),
        )])
        service = _make_service(rec)

        expected = (
            (
                SensitivityAnchor(50, 50),
                SensitivityAnchor(100, 100),
                SensitivityAnchor(0, 0),
            ),
            (
                SensitivityAnchor(50, 50),
                SensitivityAnchor(100, 100),
                SensitivityAnchor(0, 0),
            ),
        )
        self.assertEqual(service.get_sensitivity_curves(), expected)

    def test_get_sensitivity_curves_read_error_returns_none(self) -> None:
        rec = _Recorder(read_results=[SettingsServiceError("synthetic sensitivity read failure")])
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING") as logs:
            self.assertIsNone(service.get_sensitivity_curves())

        self.assertIn(
            "get_sensitivity_curves failed: synthetic sensitivity read failure",
            "\n".join(logs.output),
        )


class TestDecodeSensitivityCurve8Point(unittest.TestCase):
    """Direct tests for the cat-0x86 decoder against `_read_response`-shaped buffers."""

    def test_decodes_verified_left_curve(self) -> None:
        response = _make_8point_read_response(
            SENSITIVITY_STICK_LEFT, _VERIFIED_LEFT_8POINT
        )
        self.assertEqual(
            _decode_sensitivity_curve_8point(response), _VERIFIED_LEFT_8POINT
        )

    def test_decodes_default_right_curve(self) -> None:
        response = _make_8point_read_response(
            SENSITIVITY_STICK_RIGHT, _DEFAULT_RIGHT_8POINT
        )
        self.assertEqual(
            _decode_sensitivity_curve_8point(response), _DEFAULT_RIGHT_8POINT
        )

    def test_short_buffer_returns_none(self) -> None:
        # 8 pairs end at index 23, so 24 bytes are needed; 23 is one short.
        self.assertIsNone(_decode_sensitivity_curve_8point(bytes(23)))

    def test_out_of_range_value_returns_none(self) -> None:
        anchors = [SensitivityAnchor(i * 10, i * 10) for i in range(8)]
        response = bytearray(
            _make_8point_read_response(SENSITIVITY_STICK_LEFT, anchors)
        )
        response[8 + 3 * 2 + 1] = 200  # anchor[3].y raw byte = 200 (> 100)
        self.assertIsNone(_decode_sensitivity_curve_8point(bytes(response)))

    def test_non_monotonic_x_returns_none(self) -> None:
        anchors = [SensitivityAnchor(i * 10, i * 10) for i in range(8)]
        response = bytearray(
            _make_8point_read_response(SENSITIVITY_STICK_LEFT, anchors)
        )
        response[8 + 4 * 2] = 5  # anchor[4].x dips below anchor[3].x (30)
        self.assertIsNone(_decode_sensitivity_curve_8point(bytes(response)))

    def test_allows_flat_segments(self) -> None:
        flat = (
            SensitivityAnchor(0, 0),
            SensitivityAnchor(0, 0),
            SensitivityAnchor(10, 10),
            SensitivityAnchor(10, 50),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
            SensitivityAnchor(100, 100),
        )
        response = _make_8point_read_response(SENSITIVITY_STICK_LEFT, flat)
        self.assertEqual(_decode_sensitivity_curve_8point(response), flat)


class TestGetSensitivityCurve8Point(unittest.TestCase):
    """Tests for the per-stick cat-0x86 read method (mock HID layer)."""

    def test_reads_left_curve_and_issues_correct_query(self) -> None:
        rec = _Recorder(
            read_results=[
                _make_8point_read_response(
                    SENSITIVITY_STICK_LEFT, _VERIFIED_LEFT_8POINT
                )
            ]
        )
        service = _make_service(rec)

        self.assertEqual(
            service.get_sensitivity_curve_8point(SENSITIVITY_STICK_LEFT),
            _VERIFIED_LEFT_8POINT,
        )
        # events[0]=enumerate, [1]=open_read_write, [2]=write_file (the query).
        self.assertEqual(
            rec.events[2][2],
            build_read_query_payload(
                CATEGORY_STICK_SENSITIVITY_8POINT,
                SENSITIVITY_SUBCOMMAND,
                selector=SENSITIVITY_STICK_LEFT,
            ),
        )

    def test_reads_right_curve(self) -> None:
        rec = _Recorder(
            read_results=[
                _make_8point_read_response(
                    SENSITIVITY_STICK_RIGHT, _DEFAULT_RIGHT_8POINT
                )
            ]
        )
        service = _make_service(rec)

        self.assertEqual(
            service.get_sensitivity_curve_8point(SENSITIVITY_STICK_RIGHT),
            _DEFAULT_RIGHT_8POINT,
        )

    def test_invalid_selector_returns_none_without_io(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        with self.assertLogs("zd_app.services.settings_service", level="WARNING"):
            self.assertIsNone(service.get_sensitivity_curve_8point(2))
        self.assertEqual(rec.events, [])

    def test_garbage_magic_returns_none(self) -> None:
        bad = bytearray(HID_FEATURE_REPORT_SIZE)
        bad[0] = 0x00
        bad[1:5] = bytes.fromhex("deadbeef")
        bad[5] = CATEGORY_STICK_SENSITIVITY_8POINT
        rec = _Recorder(read_results=[bytes(bad)])
        service = _make_service(rec)

        self.assertIsNone(
            service.get_sensitivity_curve_8point(SENSITIVITY_STICK_LEFT)
        )

    def test_wrong_stick_returns_none(self) -> None:
        # Device echoes RIGHT selector when we queried LEFT.
        rec = _Recorder(
            read_results=[
                _make_8point_read_response(
                    SENSITIVITY_STICK_RIGHT, _DEFAULT_RIGHT_8POINT
                )
            ]
        )
        service = _make_service(rec)

        self.assertIsNone(
            service.get_sensitivity_curve_8point(SENSITIVITY_STICK_LEFT)
        )

    def test_read_error_returns_none(self) -> None:
        rec = _Recorder(read_results=[SettingsServiceError("synthetic 0x86 read failure")])
        service = _make_service(rec)

        self.assertIsNone(
            service.get_sensitivity_curve_8point(SENSITIVITY_STICK_LEFT)
        )

    def test_timeout_returns_none(self) -> None:
        # A non-capable device never answers 0x86 — _read_response lets the
        # TimeoutError propagate, and the read method must swallow it.
        rec = _Recorder(read_results=[TimeoutError("no 0x86 answer")])
        service = _make_service(rec)

        self.assertIsNone(
            service.get_sensitivity_curve_8point(SENSITIVITY_STICK_LEFT)
        )


class TestSupports8PointSensitivity(unittest.TestCase):
    def test_valid_response_is_capable(self) -> None:
        rec = _Recorder(
            read_results=[
                _make_8point_read_response(
                    SENSITIVITY_STICK_LEFT, _VERIFIED_LEFT_8POINT
                )
            ]
        )
        service = _make_service(rec)

        self.assertTrue(service.supports_8point_sensitivity())

    def test_probe_uses_short_timeout(self) -> None:
        rec = _Recorder(
            read_results=[
                _make_8point_read_response(
                    SENSITIVITY_STICK_LEFT, _VERIFIED_LEFT_8POINT
                )
            ]
        )
        service = _make_service(rec)
        service.supports_8point_sensitivity()

        read_events = [e for e in rec.events if e[0] == "read_file"]
        self.assertTrue(read_events)
        # read_file event = ("read_file", handle, length, timeout_ms).
        self.assertEqual(read_events[0][3], SENSITIVITY_8POINT_PROBE_TIMEOUT_MS)

    def test_timeout_is_not_capable(self) -> None:
        rec = _Recorder(read_results=[TimeoutError("device never answered 0x86")])
        service = _make_service(rec)

        self.assertFalse(service.supports_8point_sensitivity())

    def test_garbage_response_is_not_capable(self) -> None:
        anchors = [SensitivityAnchor(i * 10, i * 10) for i in range(8)]
        response = bytearray(
            _make_8point_read_response(SENSITIVITY_STICK_LEFT, anchors)
        )
        response[9] = 250  # anchor[0].y out of range → strict decode rejects
        rec = _Recorder(read_results=[bytes(response)])
        service = _make_service(rec)

        self.assertFalse(service.supports_8point_sensitivity())

    def test_verdict_is_cached(self) -> None:
        rec = _Recorder(
            read_results=[
                _make_8point_read_response(
                    SENSITIVITY_STICK_LEFT, _VERIFIED_LEFT_8POINT
                )
            ]
        )
        service = _make_service(rec)

        self.assertTrue(service.supports_8point_sensitivity())
        # Second call is served from cache (only one response was queued).
        self.assertTrue(service.supports_8point_sensitivity())
        read_events = [e for e in rec.events if e[0] == "read_file"]
        self.assertEqual(len(read_events), 1)

    def test_cache_reset_on_stop_reprobes(self) -> None:
        rec = _Recorder(
            read_results=[
                _make_8point_read_response(
                    SENSITIVITY_STICK_LEFT, _VERIFIED_LEFT_8POINT
                ),
                _make_8point_read_response(
                    SENSITIVITY_STICK_LEFT, _VERIFIED_LEFT_8POINT
                ),
            ]
        )
        service = _make_service(rec)

        self.assertTrue(service.supports_8point_sensitivity())
        service.stop()  # drops handles → clears the cached verdict
        self.assertTrue(service.supports_8point_sensitivity())
        read_events = [e for e in rec.events if e[0] == "read_file"]
        self.assertEqual(len(read_events), 2)


class TestSetSensitivityCurve8Point(unittest.TestCase):
    """The cat-0x86 writers mirror the 3-point writers' result/retry shape."""

    def test_set_left_8point_writes_0x86_frame(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        result = service.set_left_stick_sensitivity_curve_8point(_VERIFIED_LEFT_8POINT)

        self.assertEqual(result.outcome, SetSensitivityCurveOutcome.OK)
        self.assertEqual(result.anchors, _VERIFIED_LEFT_8POINT)
        write_events = [e for e in rec.events if e[0] == "write_file"]
        self.assertEqual(len(write_events), 1)
        payload = write_events[0][2]
        self.assertEqual(
            payload,
            build_left_stick_sensitivity_curve_payload_8point(_VERIFIED_LEFT_8POINT),
        )
        self.assertEqual(payload[5], CATEGORY_STICK_SENSITIVITY_8POINT)
        self.assertEqual(payload[7], SENSITIVITY_STICK_LEFT)

    def test_set_right_8point_writes_0x86_frame(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        result = service.set_right_stick_sensitivity_curve_8point(_DEFAULT_RIGHT_8POINT)

        self.assertEqual(result.outcome, SetSensitivityCurveOutcome.OK)
        payload = [e for e in rec.events if e[0] == "write_file"][0][2]
        self.assertEqual(payload[5], CATEGORY_STICK_SENSITIVITY_8POINT)
        self.assertEqual(payload[7], SENSITIVITY_STICK_RIGHT)

    def test_set_8point_rejects_wrong_length_before_win32(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        with self.assertRaises(TypeError):
            service.set_left_stick_sensitivity_curve_8point(
                tuple(SensitivityAnchor(i, i) for i in range(3))  # type: ignore[arg-type]
            )
        self.assertEqual(rec.events, [])

    def test_set_8point_write_failure_reports_outcome(self) -> None:
        rec = _Recorder(write_results=[(False, 5, 0), (False, 5, 0)])
        service = _make_service(rec)

        result = service.set_left_stick_sensitivity_curve_8point(_VERIFIED_LEFT_8POINT)

        self.assertEqual(result.outcome, SetSensitivityCurveOutcome.WRITE_FAILED)
        self.assertEqual(result.error_code, 5)


class _SnapshotProbe(SettingsService):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []
        self.polling_rate_result: PollingRate | None = PollingRate.HZ_8000
        self.vibration_result: VibrationSettings | None = VibrationSettings(
            10,
            20,
            30,
            40,
            TriggerVibrationMode.STEREO_RESONANCE,
        )
        self.deadzones_result: StickDeadzones | None = StickDeadzones(1, 2, 3, 4)
        self.axis_result: tuple[AxisInversion, AxisInversion] | None = (
            AxisInversion(True, False),
            AxisInversion(False, True),
        )
        self.sensitivity_result: tuple[
            tuple[SensitivityAnchor, SensitivityAnchor, SensitivityAnchor],
            tuple[SensitivityAnchor, SensitivityAnchor, SensitivityAnchor],
        ] | None = (
            (
                SensitivityAnchor(0, 0),
                SensitivityAnchor(50, 50),
                SensitivityAnchor(100, 100),
            ),
            (
                SensitivityAnchor(1, 2),
                SensitivityAnchor(50, 60),
                SensitivityAnchor(100, 99),
            ),
        )
        self.trigger_result: tuple[TriggerSettings, TriggerSettings] | None = (
            TriggerSettings(0, 100, TriggerMode.SHORT),
            TriggerSettings(10, 90, TriggerMode.LONG),
        )
        self.button_results: dict[ButtonSlot, ButtonMapping | None] = {
            slot: ButtonMapping.controller_button(ControllerButtonTarget.B)
            for slot in ButtonSlot
        }
        self.lighting_results: dict[LightingZone, LightingSettings | None] = {
            zone: LightingSettings(
                light_on=True,
                mode=LightingMode.FLOW,
                brightness_byte=128,
                color=RgbColor(1, 2, 3),
            )
            for zone in LightingZone
        }
        self.motion_result: MotionSettings | None = MotionSettings(
            target=MotionMappingTarget.LEFT_JOYSTICK,
            trigger_key=0x06,
            mode=MotionMappingMode.CONTINUOUS,
            sensitivity=50,
        )
        self.back_paddle_results: dict[MacroSlot, BackPaddleBinding] = {
            MacroSlot.M1: BackPaddleBinding(ControllerButtonTarget.A),
            MacroSlot.M2: BackPaddleBinding(None),
        }
        self.step_size_result: int | None = STEP_SIZE_VALUE_DEFAULT
        # 8-point capability + per-stick curve fakes. Default: not capable, so
        # get_all_settings leaves the 8-point snapshot fields None (and skips
        # the per-stick 8-point reads) exactly like an fw-1.18 device.
        self.supports_8point_result: bool = False
        self.sensitivity_8point_results: dict[int, tuple | None] = {}

    def get_polling_rate(self) -> PollingRate | None:
        self.calls.append(("get_polling_rate",))
        return self.polling_rate_result

    def get_vibration(self) -> VibrationSettings | None:
        self.calls.append(("get_vibration",))
        return self.vibration_result

    def get_deadzones(self) -> StickDeadzones | None:
        self.calls.append(("get_deadzones",))
        return self.deadzones_result

    def get_axis_inversion(self) -> tuple[AxisInversion, AxisInversion] | None:
        self.calls.append(("get_axis_inversion",))
        return self.axis_result

    def get_sensitivity_curves(
        self,
    ) -> tuple[
        tuple[SensitivityAnchor, SensitivityAnchor, SensitivityAnchor],
        tuple[SensitivityAnchor, SensitivityAnchor, SensitivityAnchor],
    ] | None:
        self.calls.append(("get_sensitivity_curves",))
        return self.sensitivity_result

    def get_trigger_settings(self) -> tuple[TriggerSettings, TriggerSettings] | None:
        self.calls.append(("get_trigger_settings",))
        return self.trigger_result

    def get_button_binding(self, slot: ButtonSlot) -> ButtonMapping | None:
        self.calls.append(("get_button_binding", slot))
        return self.button_results[slot]

    def get_zone_lighting(self, zone: LightingZone) -> LightingSettings | None:
        self.calls.append(("get_zone_lighting", zone))
        return self.lighting_results[zone]

    def get_motion_settings(self) -> MotionSettings | None:
        self.calls.append(("get_motion_settings",))
        return self.motion_result

    def get_all_back_paddle_bindings(self) -> dict[MacroSlot, BackPaddleBinding]:
        self.calls.append(("get_all_back_paddle_bindings",))
        return self.back_paddle_results

    def get_step_size(self) -> int | None:
        self.calls.append(("get_step_size",))
        return self.step_size_result

    def supports_8point_sensitivity(self) -> bool:
        self.calls.append(("supports_8point_sensitivity",))
        return self.supports_8point_result

    def get_sensitivity_curve_8point(self, stick, *, timeout_ms=None):
        self.calls.append(("get_sensitivity_curve_8point", stick))
        return self.sensitivity_8point_results.get(stick)


class TestGetAllSettings(unittest.TestCase):
    def test_get_all_settings_calls_all_methods(self) -> None:
        service = _SnapshotProbe()

        service.get_all_settings()

        expected_calls = [
            ("get_polling_rate",),
            ("get_vibration",),
            ("get_deadzones",),
            ("get_axis_inversion",),
            ("get_sensitivity_curves",),
            # Default probe is not-capable, so only the probe runs (no per-stick
            # 8-point reads) — mirrors an fw-1.18 device.
            ("supports_8point_sensitivity",),
            ("get_trigger_settings",),
            *[("get_button_binding", slot) for slot in ButtonSlot],
            *[("get_zone_lighting", zone) for zone in LightingZone],
            ("get_motion_settings",),
            ("get_all_back_paddle_bindings",),
            ("get_step_size",),
        ]
        self.assertEqual(service.calls, expected_calls)

    def test_get_all_settings_full_snapshot(self) -> None:
        service = _SnapshotProbe()

        snapshot = service.get_all_settings()

        self.assertEqual(snapshot.polling_rate, service.polling_rate_result)
        self.assertEqual(snapshot.vibration, service.vibration_result)
        self.assertEqual(snapshot.deadzones, service.deadzones_result)
        self.assertEqual(snapshot.axis_inversion_left, service.axis_result[0])
        self.assertEqual(snapshot.axis_inversion_right, service.axis_result[1])
        self.assertEqual(snapshot.sensitivity_left, service.sensitivity_result[0])
        self.assertEqual(snapshot.sensitivity_right, service.sensitivity_result[1])
        self.assertEqual(snapshot.trigger_left, service.trigger_result[0])
        self.assertEqual(snapshot.trigger_right, service.trigger_result[1])
        self.assertEqual(snapshot.button_bindings, service.button_results)
        self.assertEqual(snapshot.lighting_zones, service.lighting_results)
        self.assertEqual(snapshot.motion_settings, service.motion_result)
        self.assertEqual(snapshot.back_paddle_bindings, service.back_paddle_results)
        self.assertEqual(snapshot.step_size, service.step_size_result)
        self.assertEqual(len(snapshot.button_bindings), len(ButtonSlot))
        self.assertEqual(len(snapshot.lighting_zones), len(LightingZone))

    def test_get_all_settings_partial_failures_recover(self) -> None:
        service = _SnapshotProbe()
        service.vibration_result = None
        service.axis_result = None
        service.sensitivity_result = None
        service.trigger_result = None
        service.button_results[ButtonSlot.A] = None
        service.lighting_results[LightingZone.HOME] = None
        service.motion_result = None
        service.back_paddle_results = {}

        snapshot = service.get_all_settings()

        self.assertIsNone(snapshot.vibration)
        self.assertIsNone(snapshot.axis_inversion_left)
        self.assertIsNone(snapshot.axis_inversion_right)
        self.assertIsNone(snapshot.sensitivity_left)
        self.assertIsNone(snapshot.sensitivity_right)
        self.assertIsNone(snapshot.trigger_left)
        self.assertIsNone(snapshot.trigger_right)
        self.assertEqual(snapshot.polling_rate, PollingRate.HZ_8000)
        self.assertEqual(snapshot.deadzones, StickDeadzones(1, 2, 3, 4))
        self.assertIsNone(snapshot.motion_settings)
        self.assertEqual(snapshot.back_paddle_bindings, {})
        self.assertNotIn(ButtonSlot.A, snapshot.button_bindings)
        self.assertNotIn(LightingZone.HOME, snapshot.lighting_zones)
        self.assertEqual(len(snapshot.button_bindings), len(ButtonSlot) - 1)
        self.assertEqual(len(snapshot.lighting_zones), len(LightingZone) - 1)

    def test_get_all_settings_motion_none_keeps_other_fields(self) -> None:
        service = _SnapshotProbe()
        service.motion_result = None

        snapshot = service.get_all_settings()

        self.assertIsNone(snapshot.motion_settings)
        self.assertEqual(snapshot.polling_rate, PollingRate.HZ_8000)
        self.assertEqual(len(snapshot.button_bindings), len(ButtonSlot))

    def test_get_all_settings_returns_dataclass_instance(self) -> None:
        service = _SnapshotProbe()

        self.assertIsInstance(service.get_all_settings(), ControllerSnapshot)

    def test_get_all_settings_populates_8point_when_capable(self) -> None:
        service = _SnapshotProbe()
        service.supports_8point_result = True
        service.sensitivity_8point_results = {
            SENSITIVITY_STICK_LEFT: _VERIFIED_LEFT_8POINT,
            SENSITIVITY_STICK_RIGHT: _DEFAULT_RIGHT_8POINT,
        }

        snapshot = service.get_all_settings()

        self.assertEqual(snapshot.sensitivity_left_8point, _VERIFIED_LEFT_8POINT)
        self.assertEqual(snapshot.sensitivity_right_8point, _DEFAULT_RIGHT_8POINT)
        # 3-point fields stay populated alongside — the fallback is always present.
        self.assertEqual(snapshot.sensitivity_left, service.sensitivity_result[0])
        self.assertEqual(snapshot.sensitivity_right, service.sensitivity_result[1])
        self.assertIn(
            ("get_sensitivity_curve_8point", SENSITIVITY_STICK_LEFT), service.calls
        )
        self.assertIn(
            ("get_sensitivity_curve_8point", SENSITIVITY_STICK_RIGHT), service.calls
        )

    def test_get_all_settings_skips_8point_reads_when_not_capable(self) -> None:
        service = _SnapshotProbe()  # default supports_8point_result = False

        snapshot = service.get_all_settings()

        self.assertIsNone(snapshot.sensitivity_left_8point)
        self.assertIsNone(snapshot.sensitivity_right_8point)
        self.assertNotIn(
            ("get_sensitivity_curve_8point", SENSITIVITY_STICK_LEFT), service.calls
        )
        self.assertNotIn(
            ("get_sensitivity_curve_8point", SENSITIVITY_STICK_RIGHT), service.calls
        )


def _make_service_with_clock(
    rec: _Recorder,
    clock,
    **service_kwargs,
) -> SettingsService:
    """`_make_service` but with an explicit clock + extra constructor kwargs
    (the batch-budget tests need both)."""

    return SettingsService(
        enumerate_paths=rec.enumerate_paths,
        open_write_handle=rec.open_write_handle,
        open_read_write_handle=rec.open_read_write_handle,
        write_file=rec.write_file,
        read_file=rec.read_file,
        close_handle=rec.close_handle,
        clock=clock,
        sleep=rec.sleep,
        **service_kwargs,
    )


class _SteppingClock:
    """Deterministic injected clock: advances ``step`` seconds per call."""

    def __init__(self, step: float):
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class _JumpAfterFirstReadClock:
    """Stays near zero until the first HID read lands, then jumps past any
    batch deadline. Keyed off the recorder's event log rather than clock-call
    counts so the test does not depend on how many times production code
    consults the clock around the first getter.
    """

    def __init__(self, rec: _Recorder, jump_to: float = 10_000.0):
        self.rec = rec
        self.jump_to = jump_to
        self.now = 0.0

    def __call__(self) -> float:
        if any(event[0] == "read_file" for event in self.rec.events):
            self.now = self.jump_to
        else:
            self.now += 0.001
        return self.now


def _happy_path_read_frames() -> list[bytes]:
    """One valid response frame per ``get_all_settings`` read, in issue order,
    for an 8-point-capable device: polling, vibration, deadzones, inversion,
    3-point sensitivity, 8-point probe (LEFT), 8-point LEFT, 8-point RIGHT,
    triggers, 16 button slots, 3 lighting zones, motion, step size = 30 frames
    (back paddles never issue a read).
    """

    eight_point_pairs: list[int] = []
    for i in range(SENSITIVITY_ANCHOR_COUNT_8POINT):
        value = min(100, i * 14)
        eight_point_pairs += [value, value]

    def _frame(category: int, payload: list[int]) -> bytes:
        return _make_read_response(category=category, payload=bytes(payload))

    frames = [
        _frame(CATEGORY_POLLING_RATE, [PollingRate.HZ_8000.value]),
        _frame(
            CATEGORY_VIBRATION,
            [10, 20, 30, 40, TriggerVibrationMode.STEREO_RESONANCE.value],
        ),
        _frame(CATEGORY_STICK_DEADZONE, [1, 2, 3, 4]),
        _frame(CATEGORY_STICK_INVERSION, [1, 0, 0, 1]),
        _frame(
            CATEGORY_STICK_SENSITIVITY,
            [0, 0, 50, 50, 100, 100, 0, 0, 50, 50, 100, 100],
        ),
        _frame(
            CATEGORY_STICK_SENSITIVITY_8POINT,
            [SENSITIVITY_STICK_LEFT] + eight_point_pairs,
        ),
        _frame(
            CATEGORY_STICK_SENSITIVITY_8POINT,
            [SENSITIVITY_STICK_LEFT] + eight_point_pairs,
        ),
        _frame(
            CATEGORY_STICK_SENSITIVITY_8POINT,
            [SENSITIVITY_STICK_RIGHT] + eight_point_pairs,
        ),
        _frame(
            CATEGORY_TRIGGER_SETTINGS,
            [0, 10, 90, 100, TriggerMode.SHORT.value, TriggerMode.SHORT.value],
        ),
    ]
    for slot in ButtonSlot:
        frames.append(
            _frame(
                CATEGORY_BUTTON_BINDING,
                [slot.value, 0x01, 0x00, ControllerButtonTarget.B.value],
            )
        )
    for zone in LightingZone:
        frames.append(
            _frame(
                CATEGORY_LIGHTING,
                [zone.value, 1, LightingMode.ALWAYS_ON.value, 128, 1, 2, 3],
            )
        )
    frames.append(
        _frame(
            CATEGORY_MOTION,
            [
                MotionMappingTarget.LEFT_JOYSTICK.value,
                0x00,
                0x06,
                MotionMappingMode.CONTINUOUS.value,
                50,
            ],
        )
    )
    frames.append(_frame(CATEGORY_STEP_SIZE, [STEP_SIZE_VALUE_DEFAULT]))
    return frames


class TestGetAllSettingsBatchReadBudget(unittest.TestCase):
    """Batch-level wall-clock budget for the full-controller read batch.

    Per-read timeouts already bound each getter; these tests pin the new
    batch budget: exhaustion stops further HID traffic (B1), healthy timing
    is byte-identical to the pre-budget implementation (B2), and 0/negative
    disables the cap (B3).
    """

    def test_budget_exhaustion_skips_remaining_getters_and_hid_traffic(self) -> None:
        # B1: the clock jumps past the 8s deadline right after the first HID
        # read. The in-flight getter completes; every later getter is skipped
        # without a single further HID round-trip.
        rec = _Recorder()  # default frames: valid polling-rate responses
        service = _make_service_with_clock(rec, _JumpAfterFirstReadClock(rec))

        with self.assertLogs(
            "zd_app.services.settings_service", level="WARNING"
        ) as cm:
            snapshot = service.get_all_settings()

        # First getter ran and decoded.
        self.assertEqual(snapshot.polling_rate, PollingRate.HZ_8000)
        # Everything after the exhaustion point stayed unread.
        self.assertIsNone(snapshot.vibration)
        self.assertIsNone(snapshot.deadzones)
        self.assertIsNone(snapshot.axis_inversion_left)
        self.assertIsNone(snapshot.axis_inversion_right)
        self.assertIsNone(snapshot.sensitivity_left)
        self.assertIsNone(snapshot.sensitivity_right)
        self.assertIsNone(snapshot.sensitivity_left_8point)
        self.assertIsNone(snapshot.sensitivity_right_8point)
        self.assertIsNone(snapshot.trigger_left)
        self.assertIsNone(snapshot.trigger_right)
        self.assertEqual(snapshot.button_bindings, {})
        self.assertEqual(snapshot.lighting_zones, {})
        self.assertIsNone(snapshot.motion_settings)
        self.assertEqual(snapshot.back_paddle_bindings, {})
        self.assertIsNone(snapshot.step_size)
        # The critical guarantee: no further HID round-trips after exhaustion.
        self.assertEqual(1, sum(1 for e in rec.events if e[0] == "read_file"))
        self.assertEqual(1, sum(1 for e in rec.events if e[0] == "write_file"))
        # Exactly one budget warning for the whole batch.
        self.assertEqual(len(cm.records), 1)
        self.assertIn("batch read budget (8.0s) exhausted", cm.output[0])
        self.assertIn("remaining fields unread", cm.output[0])

    def test_healthy_timing_issues_every_getter_with_pre_change_read_count(
        self,
    ) -> None:
        # B2: a trivially advancing clock (the recorder's default 1ms step)
        # never trips the budget, and the HID traffic is identical to the
        # pre-budget implementation: 30 reads / 30 writes were measured on
        # these exact stubs at base 1d1d407 before the budget existed.
        rec = _Recorder(read_results=_happy_path_read_frames())
        service = _make_service(rec)

        with self.assertNoLogs("zd_app.services.settings_service", level="WARNING"):
            snapshot = service.get_all_settings()

        self.assertEqual(30, sum(1 for e in rec.events if e[0] == "read_file"))
        self.assertEqual(30, sum(1 for e in rec.events if e[0] == "write_file"))
        # Every scripted frame consumed -> the read sequence itself (order and
        # count) matched the pre-change batch, not just the totals.
        self.assertEqual(rec.read_results, [])
        self.assertEqual(snapshot.polling_rate, PollingRate.HZ_8000)
        self.assertEqual(
            snapshot.vibration,
            VibrationSettings(10, 20, 30, 40, TriggerVibrationMode.STEREO_RESONANCE),
        )
        self.assertEqual(snapshot.deadzones, StickDeadzones(1, 2, 3, 4))
        self.assertEqual(
            snapshot.axis_inversion_left, AxisInversion(x_inverted=True, y_inverted=False)
        )
        self.assertIsNotNone(snapshot.sensitivity_left)
        self.assertIsNotNone(snapshot.sensitivity_left_8point)
        self.assertIsNotNone(snapshot.sensitivity_right_8point)
        self.assertIsNotNone(snapshot.trigger_left)
        self.assertIsNotNone(snapshot.motion_settings)
        self.assertEqual(snapshot.step_size, STEP_SIZE_VALUE_DEFAULT)
        self.assertEqual(len(snapshot.button_bindings), len(ButtonSlot))
        self.assertEqual(len(snapshot.lighting_zones), len(LightingZone))

    def test_budget_zero_or_negative_disables_the_cap(self) -> None:
        # B3: 0/negative budget = disabled. The clock leaps 10s per call --
        # far past any 8s window -- yet every getter still issues.
        for budget in (0, -1.0):
            with self.subTest(budget=budget):
                rec = _Recorder(read_results=_happy_path_read_frames())
                service = _make_service_with_clock(
                    rec, _SteppingClock(10.0), batch_read_budget_s=budget
                )

                snapshot = service.get_all_settings()

                self.assertEqual(
                    30, sum(1 for e in rec.events if e[0] == "read_file")
                )
                self.assertEqual(rec.read_results, [])
                self.assertEqual(len(snapshot.button_bindings), len(ButtonSlot))
                self.assertEqual(len(snapshot.lighting_zones), len(LightingZone))
                self.assertEqual(snapshot.step_size, STEP_SIZE_VALUE_DEFAULT)

    def test_default_budget_trips_on_the_same_slow_clock(self) -> None:
        # Contrast for B3: identical stubs and the same 10s-per-call clock,
        # but with the default budget left enabled, the batch cannot finish.
        rec = _Recorder(read_results=_happy_path_read_frames())
        service = _make_service_with_clock(rec, _SteppingClock(10.0))

        with self.assertLogs(
            "zd_app.services.settings_service", level="WARNING"
        ) as cm:
            snapshot = service.get_all_settings()

        self.assertLess(sum(1 for e in rec.events if e[0] == "read_file"), 30)
        self.assertIsNone(snapshot.step_size)
        self.assertEqual(len(cm.records), 1)
        self.assertIn("batch read budget", cm.output[0])


class TestSetButtonBinding(unittest.TestCase):
    def test_set_a_to_b_before_start_auto_opens_and_writes(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        result = service.set_button_binding(
            ButtonSlot.A,
            ButtonMapping.controller_button(ControllerButtonTarget.B),
        )

        self.assertEqual(result.outcome, SetButtonBindingOutcome.OK)
        self.assertEqual(result.slot, ButtonSlot.A)
        self.assertEqual(result.mapping, ButtonMapping.controller_button(ControllerButtonTarget.B))
        self.assertIsNone(result.error_code)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE)
        self.assertEqual(result.payload_hex, "001055aa5102000501001000" + ("00" * 53))
        self.assertTrue(service.is_started)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file"],
        )

    def test_set_button_binding_reuses_cached_handle(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        first = service.set_polling_rate(PollingRate.HZ_8000)
        second = service.set_button_binding(
            ButtonSlot.X,
            ButtonMapping.controller_button(ControllerButtonTarget.LB),
        )

        self.assertEqual(first.outcome, SetPollingRateOutcome.OK)
        self.assertEqual(second.outcome, SetButtonBindingOutcome.OK)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file", "write_file"],
        )
        self.assertEqual(rec.events[2][1], _HANDLE)
        self.assertEqual(rec.events[3][1], _HANDLE)

    def test_set_button_binding_no_device_returns_device_not_found(self) -> None:
        rec = _Recorder(paths=[])
        service = _make_service(rec)

        result = service.set_button_binding(
            ButtonSlot.LB,
            ButtonMapping.controller_button(ControllerButtonTarget.A),
        )

        self.assertEqual(result.outcome, SetButtonBindingOutcome.DEVICE_NOT_FOUND)
        self.assertEqual(result.slot, ButtonSlot.LB)
        self.assertEqual(result.mapping, ButtonMapping.controller_button(ControllerButtonTarget.A))
        self.assertIsNone(result.error_code)
        self.assertIsNone(result.bytes_written)
        self.assertEqual(result.payload_hex, "001055aa5102000901000f00" + ("00" * 53))
        self.assertEqual(rec.events, [("enumerate_paths",)])

    def test_set_button_binding_open_failure_returns_open_failed(self) -> None:
        rec = _Recorder(open_result=(None, 32))
        service = _make_service(rec)

        result = service.set_button_binding(
            ButtonSlot.B,
            ButtonMapping.controller_button(ControllerButtonTarget.Y),
        )

        self.assertEqual(result.outcome, SetButtonBindingOutcome.OPEN_FAILED)
        self.assertEqual(result.error_code, 32)
        self.assertIsNone(result.bytes_written)
        self.assertFalse(service.is_started)

    def test_set_button_binding_write_failure_returns_write_failed(self) -> None:
        rec = _Recorder(write_results=[(False, 995, 0), (False, 995, 0)])
        service = _make_service(rec)

        result = service.set_button_binding(
            ButtonSlot.Y,
            ButtonMapping.controller_button(ControllerButtonTarget.X),
        )

        self.assertEqual(result.outcome, SetButtonBindingOutcome.WRITE_FAILED)
        self.assertEqual(result.error_code, 995)
        self.assertEqual(result.bytes_written, 0)
        self.assertTrue(service.is_started)

    def test_set_button_binding_retries_once_on_write_failed(self) -> None:
        rec = _Recorder(write_results=[(False, 995, 0), (True, 0, HID_FEATURE_REPORT_SIZE)])
        service = _make_service(rec)

        result = service.set_button_binding(
            ButtonSlot.Y,
            ButtonMapping.controller_button(ControllerButtonTarget.X),
        )

        self.assertEqual(result.outcome, SetButtonBindingOutcome.OK_WITH_RETRY)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file", "sleep", "write_file"],
        )

    def test_stop_closes_handle_even_after_button_binding_write_failure(self) -> None:
        rec = _Recorder(write_results=[(False, 995, 0), (False, 995, 0)])
        service = _make_service(rec)

        service.set_button_binding(
            ButtonSlot.A,
            ButtonMapping.controller_button(ControllerButtonTarget.B),
        )
        service.stop()

        self.assertIn(("close_handle", _HANDLE), rec.events)
        self.assertFalse(service.is_started)

    def test_button_binding_elapsed_ms_uses_injected_clock(self) -> None:
        rec = _Recorder(clock_ticks=[10.0, 10.023])
        service = _make_service(rec)

        result = service.set_button_binding(
            ButtonSlot.A,
            ButtonMapping.controller_button(ControllerButtonTarget.B),
        )

        self.assertEqual(result.elapsed_ms, 23)


class TestBackPaddleBinding(unittest.TestCase):
    def test_build_back_paddle_binding_payloads(self) -> None:
        payloads = build_back_paddle_binding_payloads(
            MacroSlot.M1,
            ControllerButtonTarget.X,
        )

        self.assertEqual(len(payloads), 2)
        binding, terminator = payloads
        self.assertEqual(len(binding), HID_FEATURE_REPORT_SIZE)
        self.assertEqual(binding[1:5], MAGIC_WRITE_PREFIX)
        self.assertEqual(binding[5], CATEGORY_BACK_PADDLE_BINDING)
        self.assertEqual(binding[6], BACK_PADDLE_SUBCOMMAND)
        self.assertEqual(binding[8], MacroSlot.M1.value)
        self.assertEqual(binding[9], BACK_PADDLE_EVENT_INDEX_BINDING)
        self.assertEqual(binding[10], ControllerButtonTarget.X.value)
        self.assertEqual(binding[12], BACK_PADDLE_DEFAULT_DURATION_MS)
        self.assertEqual(binding[14], BACK_PADDLE_DEFAULT_CYCLE_MS)
        self.assertEqual(binding[48], MacroSlot.M1.value)
        self.assertEqual(binding[49], ControllerButtonTarget.X.value)
        self.assertEqual(binding[57], BACK_PADDLE_DEFAULT_DURATION_MS)
        self.assertEqual(terminator[8], MacroSlot.M1.value)
        self.assertEqual(terminator[9], BACK_PADDLE_EVENT_INDEX_TERMINATOR)
        self.assertEqual(terminator[45], BACK_PADDLE_EVENT_INDEX_TERMINATOR)
        self.assertEqual(terminator[48], MacroSlot.M1.value)

    def test_build_back_paddle_unbound_payload(self) -> None:
        payloads = build_back_paddle_binding_payloads(MacroSlot.M2, None)

        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload[5], CATEGORY_BACK_PADDLE_BINDING)
        self.assertEqual(payload[8], MacroSlot.M2.value)
        self.assertEqual(payload[9], BACK_PADDLE_EVENT_INDEX_BINDING)
        self.assertEqual(payload[15], BACK_PADDLE_UNBOUND_FLAG)
        self.assertEqual(payload[48], MacroSlot.M2.value)

    def test_set_back_paddle_binding_writes_expected_frames(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        result = service.set_back_paddle_binding(MacroSlot.M1, ControllerButtonTarget.A)

        expected_payloads = build_back_paddle_binding_payloads(
            MacroSlot.M1,
            ControllerButtonTarget.A,
        )
        self.assertEqual(result.outcome, SetBackPaddleBindingOutcome.OK)
        self.assertEqual(result.slot, MacroSlot.M1)
        self.assertEqual(result.binding, BackPaddleBinding(ControllerButtonTarget.A))
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE * 2)
        self.assertEqual(
            [event[2] for event in rec.events if event[0] == "write_file"],
            list(expected_payloads),
        )

    def test_set_back_paddle_binding_unbound(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        result = service.set_back_paddle_unbound(MacroSlot.LM)

        expected_payloads = build_back_paddle_binding_payloads(MacroSlot.LM, None)
        self.assertEqual(result.outcome, SetBackPaddleBindingOutcome.OK)
        self.assertEqual(result.binding, BackPaddleBinding(None))
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE)
        self.assertEqual(
            [event[2] for event in rec.events if event[0] == "write_file"],
            list(expected_payloads),
        )

    def test_set_back_paddle_binding_retries_failed_frame_once(self) -> None:
        rec = _Recorder(
            write_results=[
                (True, 0, HID_FEATURE_REPORT_SIZE),
                (False, 995, 0),
                (True, 0, HID_FEATURE_REPORT_SIZE),
            ],
        )
        service = _make_service(rec)

        result = service.set_back_paddle_binding(MacroSlot.M1, ControllerButtonTarget.A)

        self.assertEqual(result.outcome, SetBackPaddleBindingOutcome.OK_WITH_RETRY)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE * 2)
        self.assertEqual(
            [event[0] for event in rec.events],
            [
                "enumerate_paths",
                "open_write_handle",
                "write_file",
                "write_file",
                "sleep",
                "write_file",
            ],
        )

    def test_set_back_paddle_binding_retries_full_pair_on_terminator_failure(self) -> None:
        # A10: back-paddle frames are write-only (no read-back), so a binding-OK
        # + terminator-permanent-fail leaves the firmware half-configured with
        # nothing able to detect it. When the terminator fails even after its
        # own per-frame retry, the FULL (binding+terminator) pair is re-attempted
        # as a unit; a recovery yields OK_WITH_RETRY (never a silent half-write).
        rec = _Recorder(
            write_results=[
                (True, 0, HID_FEATURE_REPORT_SIZE),   # pair 1: binding OK
                (False, 995, 0),                      # pair 1: terminator fail
                (False, 995, 0),                      # pair 1: terminator retry fail
                (True, 0, HID_FEATURE_REPORT_SIZE),   # pair 2: binding OK (re-sent)
                (True, 0, HID_FEATURE_REPORT_SIZE),   # pair 2: terminator OK
            ],
        )
        service = _make_service(rec)

        result = service.set_back_paddle_binding(MacroSlot.M1, ControllerButtonTarget.A)

        self.assertEqual(result.outcome, SetBackPaddleBindingOutcome.OK_WITH_RETRY)
        # The binding frame was re-sent as part of the unit retry (written twice).
        binding_payload = build_back_paddle_binding_payloads(
            MacroSlot.M1, ControllerButtonTarget.A
        )[0]
        binding_writes = [
            event
            for event in rec.events
            if event[0] == "write_file" and event[2] == binding_payload
        ]
        self.assertEqual(len(binding_writes), 2)

    def test_get_back_paddle_binding_returns_none_when_read_unsupported(self) -> None:
        service = _make_service(_Recorder())

        self.assertIsNone(service.get_back_paddle_binding(MacroSlot.M1))
        self.assertEqual(service.get_all_back_paddle_bindings(), {})


class TestSetDeadzone(unittest.TestCase):
    def test_set_deadzone_before_start_auto_opens_and_writes(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)
        deadzones = StickDeadzones(5, 0, 0, 0)

        result = service.set_all_deadzones(deadzones)

        self.assertEqual(result.outcome, SetDeadzoneOutcome.OK)
        self.assertEqual(result.deadzones, deadzones)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE)
        self.assertEqual(result.payload_hex, "001055aa51090005000000" + ("00" * 54))
        self.assertTrue(service.is_started)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file"],
        )

    def test_set_deadzone_reuses_cached_handle_after_polling_and_remap(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        polling = service.set_polling_rate(PollingRate.HZ_8000)
        remap = service.set_button_binding(
            ButtonSlot.X,
            ButtonMapping.controller_button(ControllerButtonTarget.LB),
        )
        deadzone = service.set_all_deadzones(StickDeadzones(0, 0, 0, 0))

        self.assertEqual(polling.outcome, SetPollingRateOutcome.OK)
        self.assertEqual(remap.outcome, SetButtonBindingOutcome.OK)
        self.assertEqual(deadzone.outcome, SetDeadzoneOutcome.OK)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file", "write_file", "write_file"],
        )
        self.assertEqual(rec.events[2][1], _HANDLE)
        self.assertEqual(rec.events[3][1], _HANDLE)
        self.assertEqual(rec.events[4][1], _HANDLE)

    def test_set_deadzone_no_device_returns_device_not_found(self) -> None:
        rec = _Recorder(paths=[])
        service = _make_service(rec)
        deadzones = StickDeadzones(100, 0, 0, 0)

        result = service.set_all_deadzones(deadzones)

        self.assertEqual(result.outcome, SetDeadzoneOutcome.DEVICE_NOT_FOUND)
        self.assertEqual(result.deadzones, deadzones)
        self.assertIsNone(result.error_code)
        self.assertIsNone(result.bytes_written)
        self.assertEqual(result.payload_hex, "001055aa51090064000000" + ("00" * 54))
        self.assertEqual(rec.events, [("enumerate_paths",)])

    def test_set_deadzone_open_failure_returns_open_failed(self) -> None:
        rec = _Recorder(open_result=(None, 32))
        service = _make_service(rec)

        result = service.set_all_deadzones(StickDeadzones(5, 0, 0, 0))

        self.assertEqual(result.outcome, SetDeadzoneOutcome.OPEN_FAILED)
        self.assertEqual(result.error_code, 32)
        self.assertIsNone(result.bytes_written)
        self.assertFalse(service.is_started)

    def test_set_deadzone_write_failure_returns_write_failed(self) -> None:
        rec = _Recorder(write_results=[(False, 995, 0), (False, 995, 0)])
        service = _make_service(rec)

        result = service.set_all_deadzones(StickDeadzones(5, 0, 0, 0))

        self.assertEqual(result.outcome, SetDeadzoneOutcome.WRITE_FAILED)
        self.assertEqual(result.error_code, 995)
        self.assertEqual(result.bytes_written, 0)
        self.assertTrue(service.is_started)

    def test_stop_closes_handle_even_after_deadzone_write_failure(self) -> None:
        rec = _Recorder(write_results=[(False, 995, 0), (False, 995, 0)])
        service = _make_service(rec)

        service.set_all_deadzones(StickDeadzones(5, 0, 0, 0))
        service.stop()

        self.assertIn(("close_handle", _HANDLE), rec.events)
        self.assertFalse(service.is_started)

    def test_deadzone_elapsed_ms_uses_injected_clock(self) -> None:
        rec = _Recorder(clock_ticks=[10.0, 10.019])
        service = _make_service(rec)

        result = service.set_all_deadzones(StickDeadzones(5, 0, 0, 0))

        self.assertEqual(result.elapsed_ms, 19)


class TestSetSensitivityCurve(unittest.TestCase):
    def test_set_sensitivity_before_start_auto_opens_and_writes(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)
        anchors = (
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
        )

        result = service.set_left_stick_sensitivity_curve(anchors)

        self.assertEqual(result.outcome, SetSensitivityCurveOutcome.OK)
        self.assertEqual(result.anchors, anchors)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE)
        self.assertEqual(result.payload_hex, "001055aa51060000000032326464" + ("00" * 51))
        self.assertTrue(service.is_started)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file"],
        )

    def test_set_sensitivity_reuses_cached_handle_after_deadzone(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)
        anchors = (
            SensitivityAnchor(24, 24),
            SensitivityAnchor(54, 80),
            SensitivityAnchor(100, 100),
        )

        deadzone = service.set_all_deadzones(StickDeadzones(0, 0, 0, 0))
        sensitivity = service.set_left_stick_sensitivity_curve(anchors)

        self.assertEqual(deadzone.outcome, SetDeadzoneOutcome.OK)
        self.assertEqual(sensitivity.outcome, SetSensitivityCurveOutcome.OK)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file", "write_file"],
        )
        self.assertEqual(rec.events[2][1], _HANDLE)
        self.assertEqual(rec.events[3][1], _HANDLE)

    def test_set_right_sensitivity_before_start_auto_opens_and_writes(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)
        anchors = (
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
        )

        result = service.set_right_stick_sensitivity_curve(anchors)

        self.assertEqual(result.outcome, SetSensitivityCurveOutcome.OK)
        self.assertEqual(result.anchors, anchors)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE)
        self.assertEqual(result.payload_hex, "001055aa51060001000032326464" + ("00" * 51))
        self.assertTrue(service.is_started)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file"],
        )

    def test_set_sensitivity_failure_outcomes(self) -> None:
        anchors = (
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
        )
        cases = [
            (
                "no_device",
                _Recorder(paths=[]),
                SetSensitivityCurveOutcome.DEVICE_NOT_FOUND,
                None,
                None,
            ),
            (
                "open_failed",
                _Recorder(open_result=(None, 32)),
                SetSensitivityCurveOutcome.OPEN_FAILED,
                32,
                None,
            ),
            (
                "write_failed",
                _Recorder(write_results=[(False, 995, 0), (False, 995, 0)]),
                SetSensitivityCurveOutcome.WRITE_FAILED,
                995,
                0,
            ),
        ]
        for name, rec, outcome, error_code, bytes_written in cases:
            with self.subTest(name=name):
                service = _make_service(rec)
                result = service.set_left_stick_sensitivity_curve(anchors)

                self.assertEqual(result.outcome, outcome)
                self.assertEqual(result.anchors, anchors)
                self.assertEqual(result.error_code, error_code)
                self.assertEqual(result.bytes_written, bytes_written)
                self.assertEqual(
                    result.payload_hex,
                    "001055aa51060000000032326464" + ("00" * 51),
                )

    def test_stop_closes_handle_even_after_sensitivity_write_failure(self) -> None:
        rec = _Recorder(write_results=[(False, 995, 0), (False, 995, 0)])
        service = _make_service(rec)

        service.set_left_stick_sensitivity_curve((
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
        ))
        service.stop()

        self.assertIn(("close_handle", _HANDLE), rec.events)
        self.assertFalse(service.is_started)

    def test_sensitivity_elapsed_ms_uses_injected_clock(self) -> None:
        rec = _Recorder(clock_ticks=[10.0, 10.021])
        service = _make_service(rec)

        result = service.set_left_stick_sensitivity_curve((
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
        ))

        self.assertEqual(result.elapsed_ms, 21)


class TestSetAxisInversion(unittest.TestCase):
    def test_set_left_axis_inversion_before_start_auto_opens_and_writes(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)
        inversion = AxisInversion(False, True)

        result = service.set_left_stick_inversion(inversion)

        self.assertEqual(result.outcome, SetAxisInversionOutcome.OK)
        self.assertEqual(result.stick, "left")
        self.assertEqual(result.inversion, inversion)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE)
        self.assertEqual(result.payload_hex, "001055aa510700000001" + ("00" * 55))
        self.assertTrue(service.is_started)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file"],
        )

    def test_set_right_axis_inversion_before_start_auto_opens_and_writes(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)
        inversion = AxisInversion(True, True)

        result = service.set_right_stick_inversion(inversion)

        self.assertEqual(result.outcome, SetAxisInversionOutcome.OK)
        self.assertEqual(result.stick, "right")
        self.assertEqual(result.inversion, inversion)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE)
        self.assertEqual(result.payload_hex, "001055aa510700010101" + ("00" * 55))
        self.assertTrue(service.is_started)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file"],
        )

    def test_set_axis_inversion_reuses_cached_handle_after_polling_and_sensitivity(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)
        anchors = (
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
        )

        polling = service.set_polling_rate(PollingRate.HZ_1000)
        sensitivity = service.set_right_stick_sensitivity_curve(anchors)
        inversion = service.set_left_stick_inversion(AxisInversion(True, False))

        self.assertEqual(polling.outcome, SetPollingRateOutcome.OK)
        self.assertEqual(sensitivity.outcome, SetSensitivityCurveOutcome.OK)
        self.assertEqual(inversion.outcome, SetAxisInversionOutcome.OK)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file", "write_file", "write_file"],
        )
        self.assertEqual(rec.events[2][1], _HANDLE)
        self.assertEqual(rec.events[3][1], _HANDLE)
        self.assertEqual(rec.events[4][1], _HANDLE)

    def test_set_axis_inversion_failure_outcomes_for_both_sticks(self) -> None:
        inversion = AxisInversion(True, False)
        cases = [
            (
                "left_no_device",
                "set_left_stick_inversion",
                "left",
                _Recorder(paths=[]),
                SetAxisInversionOutcome.DEVICE_NOT_FOUND,
                None,
                None,
                "001055aa510700000100" + ("00" * 55),
            ),
            (
                "right_no_device",
                "set_right_stick_inversion",
                "right",
                _Recorder(paths=[]),
                SetAxisInversionOutcome.DEVICE_NOT_FOUND,
                None,
                None,
                "001055aa510700010100" + ("00" * 55),
            ),
            (
                "left_open_failed",
                "set_left_stick_inversion",
                "left",
                _Recorder(open_result=(None, 32)),
                SetAxisInversionOutcome.OPEN_FAILED,
                32,
                None,
                "001055aa510700000100" + ("00" * 55),
            ),
            (
                "right_open_failed",
                "set_right_stick_inversion",
                "right",
                _Recorder(open_result=(None, 32)),
                SetAxisInversionOutcome.OPEN_FAILED,
                32,
                None,
                "001055aa510700010100" + ("00" * 55),
            ),
            (
                "left_write_failed",
                "set_left_stick_inversion",
                "left",
                _Recorder(write_results=[(False, 995, 0), (False, 995, 0)]),
                SetAxisInversionOutcome.WRITE_FAILED,
                995,
                0,
                "001055aa510700000100" + ("00" * 55),
            ),
            (
                "right_write_failed",
                "set_right_stick_inversion",
                "right",
                _Recorder(write_results=[(False, 995, 0), (False, 995, 0)]),
                SetAxisInversionOutcome.WRITE_FAILED,
                995,
                0,
                "001055aa510700010100" + ("00" * 55),
            ),
        ]
        for name, method_name, stick, rec, outcome, error_code, bytes_written, payload_hex in cases:
            with self.subTest(name=name):
                service = _make_service(rec)
                result = getattr(service, method_name)(inversion)

                self.assertEqual(result.outcome, outcome)
                self.assertEqual(result.stick, stick)
                self.assertEqual(result.inversion, inversion)
                self.assertEqual(result.error_code, error_code)
                self.assertEqual(result.bytes_written, bytes_written)
                self.assertEqual(result.payload_hex, payload_hex)

    def test_stop_closes_handle_even_after_axis_inversion_write_failure_for_both_sticks(self) -> None:
        cases = [
            ("left", "set_left_stick_inversion"),
            ("right", "set_right_stick_inversion"),
        ]
        for stick, method_name in cases:
            with self.subTest(stick=stick):
                rec = _Recorder(write_results=[(False, 995, 0), (False, 995, 0)])
                service = _make_service(rec)

                getattr(service, method_name)(AxisInversion(False, True))
                service.stop()

                self.assertIn(("close_handle", _HANDLE), rec.events)
                self.assertFalse(service.is_started)

    def test_axis_inversion_elapsed_ms_uses_injected_clock(self) -> None:
        rec = _Recorder(clock_ticks=[10.0, 10.022])
        service = _make_service(rec)

        result = service.set_right_stick_inversion(AxisInversion(False, True))

        self.assertEqual(result.elapsed_ms, 22)


class TestSetTriggerSettings(unittest.TestCase):
    def test_set_left_trigger_before_start_auto_opens_and_writes(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)
        settings = TriggerSettings(20, 80, TriggerMode.LONG)

        result = service.set_left_trigger_settings(settings)

        self.assertEqual(result.outcome, SetTriggerSettingsOutcome.OK)
        self.assertEqual(result.trigger, "left")
        self.assertEqual(result.settings, settings)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE)
        self.assertEqual(result.payload_hex, "001055aa510a0000145000" + ("00" * 54))
        self.assertTrue(service.is_started)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file"],
        )

    def test_set_right_trigger_before_start_auto_opens_and_writes(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)
        settings = TriggerSettings(0, 100, TriggerMode.SHORT)

        result = service.set_right_trigger_settings(settings)

        self.assertEqual(result.outcome, SetTriggerSettingsOutcome.OK)
        self.assertEqual(result.trigger, "right")
        self.assertEqual(result.settings, settings)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE)
        self.assertEqual(result.payload_hex, "001055aa510a0001006401" + ("00" * 54))
        self.assertTrue(service.is_started)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file"],
        )

    def test_set_trigger_reuses_cached_handle_after_remap_and_inversion(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        remap = service.set_button_binding(
            ButtonSlot.UP,
            ButtonMapping.controller_button(ControllerButtonTarget.RB),
        )
        inversion = service.set_left_stick_inversion(AxisInversion(True, False))
        trigger = service.set_right_trigger_settings(TriggerSettings(10, 100, TriggerMode.LONG))

        self.assertEqual(remap.outcome, SetButtonBindingOutcome.OK)
        self.assertEqual(inversion.outcome, SetAxisInversionOutcome.OK)
        self.assertEqual(trigger.outcome, SetTriggerSettingsOutcome.OK)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file", "write_file", "write_file"],
        )
        self.assertEqual(rec.events[2][1], _HANDLE)
        self.assertEqual(rec.events[3][1], _HANDLE)
        self.assertEqual(rec.events[4][1], _HANDLE)

    def test_set_trigger_failure_outcomes_for_both_triggers(self) -> None:
        settings = TriggerSettings(10, 100, TriggerMode.LONG)
        cases = [
            (
                "left_no_device",
                "set_left_trigger_settings",
                "left",
                _Recorder(paths=[]),
                SetTriggerSettingsOutcome.DEVICE_NOT_FOUND,
                None,
                None,
                "001055aa510a00000a6400" + ("00" * 54),
            ),
            (
                "right_no_device",
                "set_right_trigger_settings",
                "right",
                _Recorder(paths=[]),
                SetTriggerSettingsOutcome.DEVICE_NOT_FOUND,
                None,
                None,
                "001055aa510a00010a6400" + ("00" * 54),
            ),
            (
                "left_open_failed",
                "set_left_trigger_settings",
                "left",
                _Recorder(open_result=(None, 32)),
                SetTriggerSettingsOutcome.OPEN_FAILED,
                32,
                None,
                "001055aa510a00000a6400" + ("00" * 54),
            ),
            (
                "right_open_failed",
                "set_right_trigger_settings",
                "right",
                _Recorder(open_result=(None, 32)),
                SetTriggerSettingsOutcome.OPEN_FAILED,
                32,
                None,
                "001055aa510a00010a6400" + ("00" * 54),
            ),
            (
                "left_write_failed",
                "set_left_trigger_settings",
                "left",
                _Recorder(write_results=[(False, 995, 0), (False, 995, 0)]),
                SetTriggerSettingsOutcome.WRITE_FAILED,
                995,
                0,
                "001055aa510a00000a6400" + ("00" * 54),
            ),
            (
                "right_write_failed",
                "set_right_trigger_settings",
                "right",
                _Recorder(write_results=[(False, 995, 0), (False, 995, 0)]),
                SetTriggerSettingsOutcome.WRITE_FAILED,
                995,
                0,
                "001055aa510a00010a6400" + ("00" * 54),
            ),
        ]
        for name, method_name, trigger, rec, outcome, error_code, bytes_written, payload_hex in cases:
            with self.subTest(name=name):
                service = _make_service(rec)
                result = getattr(service, method_name)(settings)

                self.assertEqual(result.outcome, outcome)
                self.assertEqual(result.trigger, trigger)
                self.assertEqual(result.settings, settings)
                self.assertEqual(result.error_code, error_code)
                self.assertEqual(result.bytes_written, bytes_written)
                self.assertEqual(result.payload_hex, payload_hex)

    def test_stop_closes_handle_even_after_trigger_write_failure_for_both_triggers(self) -> None:
        cases = [
            ("left", "set_left_trigger_settings"),
            ("right", "set_right_trigger_settings"),
        ]
        for trigger, method_name in cases:
            with self.subTest(trigger=trigger):
                rec = _Recorder(write_results=[(False, 995, 0), (False, 995, 0)])
                service = _make_service(rec)

                getattr(service, method_name)(TriggerSettings(0, 100, TriggerMode.SHORT))
                service.stop()

                self.assertIn(("close_handle", _HANDLE), rec.events)
                self.assertFalse(service.is_started)

    def test_trigger_elapsed_ms_uses_injected_clock(self) -> None:
        rec = _Recorder(clock_ticks=[10.0, 10.024])
        service = _make_service(rec)

        result = service.set_left_trigger_settings(TriggerSettings(0, 100, TriggerMode.SHORT))

        self.assertEqual(result.elapsed_ms, 24)


class TestSetVibration(unittest.TestCase):
    def test_set_vibration_before_start_auto_opens_and_writes(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)
        settings = VibrationSettings(
            30,
            50,
            70,
            90,
            TriggerVibrationMode.STEREO_RESONANCE,
        )

        result = service.set_vibration(settings)

        self.assertEqual(result.outcome, SetVibrationOutcome.OK)
        self.assertEqual(result.settings, settings)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE)
        self.assertEqual(result.payload_hex, "001055aa510c001e32465a01" + ("00" * 53))
        self.assertTrue(service.is_started)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file"],
        )

    def test_set_vibration_reuses_cached_handle_after_trigger_and_remap(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        trigger = service.set_left_trigger_settings(
            TriggerSettings(20, 80, TriggerMode.LONG)
        )
        remap = service.set_button_binding(
            ButtonSlot.UP,
            ButtonMapping.controller_button(ControllerButtonTarget.RB),
        )
        vibration = service.set_vibration(
            VibrationSettings(30, 50, 70, 90, TriggerVibrationMode.TRIGGER_VIBRATION)
        )

        self.assertEqual(trigger.outcome, SetTriggerSettingsOutcome.OK)
        self.assertEqual(remap.outcome, SetButtonBindingOutcome.OK)
        self.assertEqual(vibration.outcome, SetVibrationOutcome.OK)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file", "write_file", "write_file"],
        )
        self.assertEqual(rec.events[2][1], _HANDLE)
        self.assertEqual(rec.events[3][1], _HANDLE)
        self.assertEqual(rec.events[4][1], _HANDLE)

    def test_set_vibration_failure_outcomes(self) -> None:
        settings = VibrationSettings(30, 50, 70, 90, TriggerVibrationMode.NATIVE)
        cases = [
            (
                "no_device",
                _Recorder(paths=[]),
                SetVibrationOutcome.DEVICE_NOT_FOUND,
                None,
                None,
            ),
            (
                "open_failed",
                _Recorder(open_result=(None, 32)),
                SetVibrationOutcome.OPEN_FAILED,
                32,
                None,
            ),
            (
                "write_failed",
                _Recorder(write_results=[(False, 995, 0), (False, 995, 0)]),
                SetVibrationOutcome.WRITE_FAILED,
                995,
                0,
            ),
        ]
        for name, rec, outcome, error_code, bytes_written in cases:
            with self.subTest(name=name):
                service = _make_service(rec)
                result = service.set_vibration(settings)

                self.assertEqual(result.outcome, outcome)
                self.assertEqual(result.settings, settings)
                self.assertEqual(result.error_code, error_code)
                self.assertEqual(result.bytes_written, bytes_written)
                self.assertEqual(
                    result.payload_hex,
                    "001055aa510c001e32465a00" + ("00" * 53),
                )

    def test_stop_closes_handle_even_after_vibration_write_failure(self) -> None:
        rec = _Recorder(write_results=[(False, 995, 0), (False, 995, 0)])
        service = _make_service(rec)

        service.set_vibration(
            VibrationSettings(30, 15, 15, 15, TriggerVibrationMode.NATIVE)
        )
        service.stop()

        self.assertIn(("close_handle", _HANDLE), rec.events)
        self.assertFalse(service.is_started)

    def test_vibration_elapsed_ms_uses_injected_clock(self) -> None:
        rec = _Recorder(clock_ticks=[10.0, 10.026])
        service = _make_service(rec)

        result = service.set_vibration(
            VibrationSettings(30, 15, 15, 15, TriggerVibrationMode.NATIVE)
        )

        self.assertEqual(result.elapsed_ms, 26)


class TestSetLighting(unittest.TestCase):
    def test_set_home_lighting_before_start_auto_opens_and_writes(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)
        settings = LightingSettings(
            True,
            LightingMode.ALWAYS_ON,
            0xCC,
            RgbColor(255, 0, 0),
        )

        result = service.set_zone_lighting(LightingZone.HOME, settings)

        self.assertEqual(result.outcome, SetLightingOutcome.OK)
        self.assertEqual(result.zone, "home")
        self.assertEqual(result.settings, settings)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.bytes_written, HID_FEATURE_REPORT_SIZE)
        self.assertEqual(result.payload_hex, "001055aa511000000101ccff0000" + ("00" * 51))
        self.assertTrue(service.is_started)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file"],
        )

    def test_set_lighting_reuses_cached_handle_after_vibration_and_remap(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        vibration = service.set_vibration(
            VibrationSettings(30, 50, 70, 90, TriggerVibrationMode.TRIGGER_VIBRATION)
        )
        remap = service.set_button_binding(
            ButtonSlot.UP,
            ButtonMapping.controller_button(ControllerButtonTarget.RB),
        )
        lighting = service.set_zone_lighting(
            LightingZone.LEFT_LIGHT,
            LightingSettings(True, LightingMode.FLOW, 0x66, RgbColor(255, 0, 0)),
        )

        self.assertEqual(vibration.outcome, SetVibrationOutcome.OK)
        self.assertEqual(remap.outcome, SetButtonBindingOutcome.OK)
        self.assertEqual(lighting.outcome, SetLightingOutcome.OK)
        self.assertEqual(
            [event[0] for event in rec.events],
            ["enumerate_paths", "open_write_handle", "write_file", "write_file", "write_file"],
        )
        self.assertEqual(rec.events[2][1], _HANDLE)
        self.assertEqual(rec.events[3][1], _HANDLE)
        self.assertEqual(rec.events[4][1], _HANDLE)

    def test_set_lighting_failure_outcomes(self) -> None:
        settings = LightingSettings(True, LightingMode.FLOW, 0x66, RgbColor(0, 255, 0))
        cases = [
            (
                "no_device",
                _Recorder(paths=[]),
                SetLightingOutcome.DEVICE_NOT_FOUND,
                None,
                None,
            ),
            (
                "open_failed",
                _Recorder(open_result=(None, 32)),
                SetLightingOutcome.OPEN_FAILED,
                32,
                None,
            ),
            (
                "write_failed",
                _Recorder(write_results=[(False, 995, 0), (False, 995, 0)]),
                SetLightingOutcome.WRITE_FAILED,
                995,
                0,
            ),
        ]
        for name, rec, outcome, error_code, bytes_written in cases:
            with self.subTest(name=name):
                service = _make_service(rec)
                result = service.set_zone_lighting(LightingZone.RIGHT_LIGHT, settings)

                self.assertEqual(result.outcome, outcome)
                self.assertEqual(result.zone, "right_light")
                self.assertEqual(result.settings, settings)
                self.assertEqual(result.error_code, error_code)
                self.assertEqual(result.bytes_written, bytes_written)
                self.assertEqual(
                    result.payload_hex,
                    "001055aa5110000201046600ff00" + ("00" * 51),
                )

    def test_stop_closes_handle_even_after_lighting_write_failure(self) -> None:
        rec = _Recorder(write_results=[(False, 995, 0), (False, 995, 0)])
        service = _make_service(rec)

        service.set_zone_lighting(
            LightingZone.HOME,
            LightingSettings(True, LightingMode.FLOW, 0x66, RgbColor(0, 0, 255)),
        )
        service.stop()

        self.assertIn(("close_handle", _HANDLE), rec.events)
        self.assertFalse(service.is_started)

    def test_lighting_elapsed_ms_uses_injected_clock(self) -> None:
        rec = _Recorder(clock_ticks=[10.0, 10.027])
        service = _make_service(rec)

        result = service.set_zone_lighting(
            LightingZone.HOME,
            LightingSettings(True, LightingMode.FLOW, 0x66, RgbColor(0, 0, 255)),
        )

        self.assertEqual(result.elapsed_ms, 27)


class TestSettingsServiceCleanup(unittest.TestCase):
    def test_stop_then_set_reopens_handle(self) -> None:
        rec = _Recorder()
        service = _make_service(rec)

        service.set_polling_rate(PollingRate.HZ_2000)
        service.stop()
        service.set_polling_rate(PollingRate.HZ_8000)

        self.assertEqual(
            [event[0] for event in rec.events],
            [
                "enumerate_paths",
                "open_write_handle",
                "write_file",
                "close_handle",
                "enumerate_paths",
                "open_write_handle",
                "write_file",
            ],
        )

    def test_stop_closes_cached_read_write_handle(self) -> None:
        rec = _Recorder(read_results=[_make_read_response()])
        service = _make_service(rec)

        service.get_polling_rate()
        service.stop()

        self.assertIn(("close_handle", _READ_WRITE_HANDLE), rec.events)
        self.assertFalse(service.is_started)

    def test_path_selection_can_ignore_non_matching_paths_from_fake(self) -> None:
        rec = _Recorder(paths=[_OTHER_PATH, _FAKE_PATH])
        service = _make_service(rec)

        service.start()

        # _choose_mi02_path only chooses from already-filtered candidates;
        # production filtering lives in enumerate_mi02_hid_paths. A fake that
        # returns multiple candidates should still choose the first HID path.
        self.assertEqual(service.target_path, _OTHER_PATH)


@unittest.skipUnless(sys.platform == "win32", "kernel32 only present on Windows")
class TestDefaultReadFileTimeout(unittest.TestCase):
    """_default_read_file must raise TimeoutError instead of hanging
    when the device doesn't respond. Strategy is worker-thread + CancelIoEx;
    we patch _Win32.kernel32 with a fake whose ReadFile blocks on an Event
    until cancelled, exercising the timeout path end-to-end."""

    def test_default_read_file_times_out_on_unresponsive_handle(self) -> None:
        from zd_app.services import settings_service as ss

        cancelled = threading.Event()
        read_returned = threading.Event()

        class _FakeKernel32:
            def __init__(self):
                self.cancel_io_ex_calls = 0
                self.get_last_error_value = 0

            def ReadFile(self, handle, buf, length, bytes_read_ptr, overlapped):
                # Block until CancelIoEx unblocks us, or until a 5s safety
                # ceiling — if the cancel never fires the test still
                # eventually proceeds, just with a louder failure mode.
                cancelled.wait(timeout=5.0)
                read_returned.set()
                # Simulate ReadFile returning FALSE with ERROR_OPERATION_ABORTED
                # (995) after the cancel, matching real Windows behavior.
                self.get_last_error_value = 995
                return 0

            def CancelIoEx(self, handle, overlapped_ptr):
                self.cancel_io_ex_calls += 1
                cancelled.set()
                return 1

            def GetLastError(self):
                return self.get_last_error_value

        fake = _FakeKernel32()
        with mock.patch.object(ss._Win32, "kernel32", return_value=fake):
            started = time.perf_counter()
            with self.assertRaises(TimeoutError) as ctx:
                ss._default_read_file(handle=0x1234, length=65, timeout_ms=200)
            elapsed_ms = (time.perf_counter() - started) * 1000
        # The cancel must have fired; reader thread should observe and exit.
        self.assertEqual(fake.cancel_io_ex_calls, 1)
        self.assertTrue(read_returned.wait(timeout=1.0), "reader thread did not exit after cancel")
        # Timeout error message includes the configured budget.
        self.assertIn("200ms", str(ctx.exception))
        # Elapsed must be at-least the budget but not much more (worker
        # cancel + cleanup adds <100ms in practice).
        self.assertGreaterEqual(elapsed_ms, 200.0)
        self.assertLess(elapsed_ms, 1500.0)

    def test_default_read_file_timeout_falls_back_to_default_when_zero(self) -> None:
        """timeout_ms <= 0 falls back to READ_FILE_TIMEOUT_MS_DEFAULT (1500ms).
        We don't want to actually wait 1500ms in tests — patch the default to
        a small value and just verify the fallback path was taken."""
        from zd_app.services import settings_service as ss

        cancelled = threading.Event()

        class _FakeKernel32:
            def __init__(self):
                self.last_timeout_observed_ms = None

            def ReadFile(self, handle, buf, length, bytes_read_ptr, overlapped):
                cancelled.wait(timeout=5.0)
                return 0

            def CancelIoEx(self, handle, overlapped_ptr):
                cancelled.set()
                return 1

            def GetLastError(self):
                return 995

        with mock.patch.object(ss, "READ_FILE_TIMEOUT_MS_DEFAULT", 100), \
             mock.patch.object(ss._Win32, "kernel32", return_value=_FakeKernel32()):
            with self.assertRaises(TimeoutError) as ctx:
                ss._default_read_file(handle=0x1234, length=65, timeout_ms=0)
        # The fallback budget of 100ms shows up in the exception message.
        self.assertIn("100ms", str(ctx.exception))
