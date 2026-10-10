"""Readback contracts exercised through real getters and fake transport bytes."""

import unittest
from contextlib import ExitStack
from unittest import mock

from zd_app.services import settings_service as settings
from tests.test_settings_service import (
    _Recorder, _make_read_response, _make_service, _make_write_ack_response,
)


# Category, getter, valid payload, snapshot fields populated by that category.
CASES = (
    (settings.CATEGORY_STICK_DEADZONE, "get_deadzones", [0, 100, 100, 0],
     ("deadzones",)),
    (settings.CATEGORY_STICK_SENSITIVITY, "get_sensitivity_curves",
     [50, 50, 100, 100, 0, 0] * 2, ("sensitivity_left", "sensitivity_right")),
    (settings.CATEGORY_STICK_INVERSION, "get_axis_inversion", [0, 1, 1, 0],
     ("axis_inversion_left", "axis_inversion_right")),
    (settings.CATEGORY_TRIGGER_SETTINGS, "get_trigger_settings", [0, 0, 100, 100, 0, 0],
     ("trigger_left", "trigger_right")),
    (settings.CATEGORY_VIBRATION, "get_vibration", [0, 100, 0, 100, 0],
     ("vibration",)),
)


def _frame(case, payload=None):
    return _make_read_response(
        category=case[0], payload=bytes(case[2] if payload is None else payload),
    )


class TestReadbackValidation(unittest.TestCase):
    def test_exact_minimum_frames_accept_boundaries_and_nonmonotonic_three_point(self):
        for case in CASES:
            with self.subTest(getter=case[1]):
                service = _make_service(_Recorder(read_results=[_frame(case)[:7 + len(case[2])]]))
                self.assertIsNotNone(getattr(service, case[1])())

    def test_every_matching_truncation_abstains(self):
        for case in CASES:
            for length in range(8, 7 + len(case[2])):
                with self.subTest(getter=case[1], length=length):
                    service = _make_service(_Recorder(read_results=[_frame(case)[:length]]))
                    with self.assertLogs(settings.__name__, level="WARNING"):
                        self.assertIsNone(getattr(service, case[1])())

    def test_each_percentage_byte_above_model_range_abstains_without_raw_values(self):
        for case in (CASES[0], CASES[1], CASES[3], CASES[4]):
            count = 12 if case is CASES[1] else 4
            for offset in range(count):
                for invalid in (101, 255):
                    with self.subTest(getter=case[1], offset=offset, invalid=invalid):
                        payload = list(case[2])
                        payload[offset] = invalid
                        service = _make_service(_Recorder(read_results=[_frame(case, payload)]))
                        with self.assertLogs(settings.__name__, level="WARNING") as logs:
                            self.assertIsNone(getattr(service, case[1])())
                        self.assertNotIn(str(invalid), "\n".join(logs.output))

    def test_each_inversion_flag_requires_zero_or_one(self):
        case = CASES[2]
        for offset in range(4):
            for invalid in (2, 255):
                with self.subTest(offset=offset, invalid=invalid):
                    payload = list(case[2])
                    payload[offset] = invalid
                    service = _make_service(_Recorder(read_results=[_frame(case, payload)]))
                    with self.assertLogs(settings.__name__, level="WARNING") as logs:
                        self.assertIsNone(service.get_axis_inversion())
                    self.assertIn("invalid axis inversion readback", "\n".join(logs.output))

    def test_reversed_trigger_ranges_abstain_on_either_side(self):
        for payload in ([100, 0, 100, 0, 0, 0], [0, 100, 0, 100, 0, 0]):
            with self.subTest(payload=payload):
                service = _make_service(_Recorder(read_results=[_frame(CASES[3], payload)]))
                with self.assertLogs(settings.__name__, level="WARNING"):
                    self.assertIsNone(service.get_trigger_settings())

    def test_equal_trigger_ranges_and_all_known_modes_remain_valid(self):
        for boundary in (0, 100):
            for mode in settings.TriggerMode:
                payload = [boundary] * 4 + [mode.value] * 2
                service = _make_service(_Recorder(read_results=[_frame(CASES[3], payload)]))
                result = service.get_trigger_settings()
                self.assertEqual(result, (settings.TriggerSettings(boundary, boundary, mode),) * 2)
            for mode in settings.TriggerVibrationMode:
                payload = [boundary] * 4 + [mode.value]
                service = _make_service(_Recorder(read_results=[_frame(CASES[4], payload)]))
                self.assertEqual(service.get_vibration().mode, mode)

    def test_unknown_modes_abstain_without_logging_raw_values(self):
        for case, offsets in ((CASES[3], (4, 5)), (CASES[4], (4,))):
            for offset in offsets:
                with self.subTest(getter=case[1], offset=offset):
                    payload = list(case[2])
                    payload[offset] = 255
                    service = _make_service(_Recorder(read_results=[_frame(case, payload)]))
                    with self.assertLogs(settings.__name__, level="WARNING") as logs:
                        self.assertIsNone(getattr(service, case[1])())
                    self.assertNotIn("255", "\n".join(logs.output))
                    self.assertNotIn("0xff", "\n".join(logs.output))

    def test_short_ack_and_unrelated_frame_are_discarded_before_length_contract(self):
        for case in CASES:
            with self.subTest(getter=case[1]):
                rec = _Recorder(read_results=[
                    _make_write_ack_response(category=case[0])[:8],
                    _make_read_response(category=settings.CATEGORY_POLLING_RATE)[:8],
                    _frame(case),
                ])
                self.assertIsNotNone(getattr(_make_service(rec), case[1])())
                self.assertEqual(sum(event[0] == "read_file" for event in rec.events), 3)

    def test_real_getter_failure_preserves_other_categories_in_aggregate(self):
        # Unrelated categories are omitted at their seams; all five affected
        # getters and polling decode actual frames through _read_response.
        by_getter = {case[1]: case for case in CASES}
        read_order = ("get_vibration", "get_deadzones", "get_axis_inversion",
                      "get_sensitivity_curves", "get_trigger_settings")
        for invalid_case in CASES:
            for malformed in ("short", "semantic"):
                with self.subTest(getter=invalid_case[1], malformed=malformed):
                    frames = [_make_read_response(value=settings.PollingRate.HZ_8000.value)]
                    for getter in read_order:
                        case = by_getter[getter]
                        frame = _frame(case)
                        if case is invalid_case:
                            if malformed == "short":
                                frame = frame[:6 + len(case[2])]
                            else:
                                raw = bytearray(frame)
                                raw[7] = 255
                                frame = bytes(raw)
                        frames.append(frame)
                    service = _make_service(_Recorder(read_results=frames))
                    with ExitStack() as stack:
                        for getter, result in (
                            ("supports_8point_sensitivity", False),
                            ("get_button_binding", None), ("get_zone_lighting", None),
                            ("get_motion_settings", None), ("get_all_back_paddle_bindings", {}),
                            ("get_step_size", None),
                        ):
                            stack.enter_context(mock.patch.object(service, getter, return_value=result))
                        stack.enter_context(self.assertLogs(settings.__name__, level="WARNING"))
                        snapshot = service.get_all_settings()
                    self.assertEqual(snapshot.polling_rate, settings.PollingRate.HZ_8000)
                    for case in CASES:
                        for field in case[3]:
                            if case is invalid_case:
                                self.assertIsNone(getattr(snapshot, field))
                            else:
                                self.assertIsNotNone(getattr(snapshot, field))


class TestAdditionalReadbackFraming(unittest.TestCase):
    CASES = (
        (settings.CATEGORY_BUTTON_BINDING, "get_button_binding", (settings.ButtonSlot.A,),
         [settings.ButtonSlot.A.value, 1, 0, settings.ControllerButtonTarget.B.value]),
        (settings.CATEGORY_MOTION, "get_motion_settings", (),
         [settings.MotionMappingTarget.DISABLED.value, 0, 0,
          settings.MotionMappingMode.INSTANT.value, 100]),
        (settings.CATEGORY_LIGHTING, "get_zone_lighting", (settings.LightingZone.HOME,),
         [settings.LightingZone.HOME.value, 1, settings.LightingMode.ALWAYS_ON.value,
          255, 255, 255, 255]),
    )

    def test_matching_truncated_frames_abstain(self):
        for category, getter, args, payload in self.CASES:
            frame = _make_read_response(category=category, payload=bytes(payload))
            for length in range(8, 7 + len(payload)):
                with self.subTest(getter=getter, length=length):
                    # Button binding retries remain bounded and receive the same
                    # malformed reply on each attempt, never a fabricated success.
                    rec = _Recorder(read_results=[frame[:length]] * settings.BUTTON_BINDING_READ_ATTEMPTS)
                    with self.assertLogs(settings.__name__, level="WARNING"):
                        self.assertIsNone(getattr(_make_service(rec), getter)(*args))

    def test_shortest_valid_frames_and_stale_selector_filtering(self):
        for category, getter, args, payload in self.CASES:
            with self.subTest(getter=getter):
                valid = _make_read_response(category=category, payload=bytes(payload))[:7 + len(payload)]
                frames = [_make_write_ack_response(category=category)[:8]]
                if args:
                    stale = bytearray(valid[:8])
                    stale[7] = payload[0] + 1
                    frames.append(bytes(stale))
                else:
                    frames.append(_make_read_response(category=settings.CATEGORY_POLLING_RATE)[:8])
                frames.append(valid)
                rec = _Recorder(read_results=frames)
                self.assertIsNotNone(getattr(_make_service(rec), getter)(*args))
                self.assertEqual(sum(event[0] == "read_file" for event in rec.events), 3)


if __name__ == "__main__":
    unittest.main()
