"""Protocol checks for decoded back-paddle binding frames."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from zd_app.services.settings_service import (
    BACK_PADDLE_DEFAULT_CYCLE_MS,
    BACK_PADDLE_DEFAULT_DURATION_MS,
    BACK_PADDLE_EVENT_INDEX_BINDING,
    BACK_PADDLE_EVENT_INDEX_TERMINATOR,
    BACK_PADDLE_UNBOUND_FLAG,
    CATEGORY_BACK_PADDLE_BINDING,
    ControllerButtonTarget,
    HID_FEATURE_REPORT_SIZE,
    MAGIC_WRITE_PREFIX,
    MacroSlot,
    build_back_paddle_binding_payloads,
)

# D0B capture predates the 2026-05-08 firmware-cycle characterization;
# vendor default at capture time was Duration=96ms (0x60).
HISTORICAL_D0B_DURATION_BYTE = 0x60


D0B_CAPTURE = Path("logs/back_paddle_capture.jsonl")


def _write_frames(path: Path, category: int) -> list[bytes]:
    frames: list[bytes] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("type") not in ("writefile", "ntwritefile"):
            continue
        if "MI_02" not in (event.get("path", "") or ""):
            continue
        payload_hex = event.get("in_hex", "") or ""
        if len(payload_hex) < 12:
            continue
        if payload_hex[2:10] != "1055aa51":
            continue
        if int(payload_hex[10:12], 16) == category:
            frames.append(bytes.fromhex(payload_hex))
    return frames


def _read_response_categories(path: Path) -> list[int]:
    categories: list[int] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("type") not in ("readfile", "ntreadfile"):
            continue
        if "MI_02" not in (event.get("path", "") or ""):
            continue
        payload_hex = event.get("out_hex", "") or event.get("in_hex", "") or ""
        if len(payload_hex) >= 12 and payload_hex[2:10] == "3055aad0":
            categories.append(int(payload_hex[10:12], 16))
    return categories


class BackPaddleProtocolTests(unittest.TestCase):
    def test_d0b_capture_decoded_into_minimum_frames(self) -> None:
        if not D0B_CAPTURE.exists():
            self.skipTest(f"{D0B_CAPTURE} is not available in this checkout")

        frames = _write_frames(D0B_CAPTURE, CATEGORY_BACK_PADDLE_BINDING)

        self.assertEqual(len(frames), 6)
        binding_shape = (
            MacroSlot.M1.value,
            BACK_PADDLE_EVENT_INDEX_BINDING,
            ControllerButtonTarget.X.value,
            HISTORICAL_D0B_DURATION_BYTE,
            MacroSlot.M1.value,
            ControllerButtonTarget.X.value,
            HISTORICAL_D0B_DURATION_BYTE,
        )
        terminator_shape = (
            MacroSlot.M1.value,
            BACK_PADDLE_EVENT_INDEX_TERMINATOR,
            BACK_PADDLE_EVENT_INDEX_TERMINATOR,
            MacroSlot.M1.value,
        )
        self.assertEqual(
            [(f[8], f[9], f[10], f[12], f[48], f[49], f[57]) for f in frames[:3]],
            [binding_shape] * 3,
        )
        # byte[14] (Cycle) = 0x00 is the historical one-shot baseline; paddle-timing-pass flipped it to 0x0A.
        self.assertEqual([f[14] for f in frames[:3]], [0x00, 0x00, 0x00])
        self.assertEqual(
            [(f[8], f[9], f[45], f[48]) for f in frames[3:]],
            [terminator_shape] * 3,
        )
        self.assertNotIn(CATEGORY_BACK_PADDLE_BINDING, _read_response_categories(D0B_CAPTURE))

    def test_build_back_paddle_frames_for_each_target(self) -> None:
        for slot in MacroSlot:
            for target in ControllerButtonTarget:
                with self.subTest(slot=slot, target=target):
                    binding, terminator = build_back_paddle_binding_payloads(slot, target)
                    self.assertEqual(len(binding), HID_FEATURE_REPORT_SIZE)
                    self.assertEqual(binding[1:5], MAGIC_WRITE_PREFIX)
                    self.assertEqual(binding[5], CATEGORY_BACK_PADDLE_BINDING)
                    self.assertEqual(binding[8], slot.value)
                    self.assertEqual(binding[10], target.value)
                    self.assertEqual(binding[12], BACK_PADDLE_DEFAULT_DURATION_MS)
                    self.assertEqual(binding[14], BACK_PADDLE_DEFAULT_CYCLE_MS)
                    self.assertEqual(binding[48], slot.value)
                    self.assertEqual(binding[49], target.value)
                    self.assertEqual(binding[57], BACK_PADDLE_DEFAULT_DURATION_MS)
                    self.assertEqual(terminator[8], slot.value)
                    self.assertEqual(terminator[9], BACK_PADDLE_EVENT_INDEX_TERMINATOR)
                    self.assertEqual(terminator[45], BACK_PADDLE_EVENT_INDEX_TERMINATOR)
                    self.assertEqual(terminator[48], slot.value)

    def test_build_back_paddle_unbound_frames(self) -> None:
        for slot in MacroSlot:
            with self.subTest(slot=slot):
                (payload,) = build_back_paddle_binding_payloads(slot, None)
                self.assertEqual(payload[5], CATEGORY_BACK_PADDLE_BINDING)
                self.assertEqual(payload[8], slot.value)
                self.assertEqual(payload[9], BACK_PADDLE_EVENT_INDEX_BINDING)
                self.assertEqual(payload[15], BACK_PADDLE_UNBOUND_FLAG)
                self.assertEqual(payload[48], slot.value)
                self.assertEqual(payload[10], 0x00)
                self.assertEqual(payload[49], 0x00)

    def test_back_paddle_binding_frame_writes_continuous_cycle(self) -> None:
        binding, _terminator = build_back_paddle_binding_payloads(
            MacroSlot.M1, ControllerButtonTarget.A
        )
        self.assertEqual(binding[12], 0x0A)
        self.assertEqual(binding[14], 0x0A)
        self.assertEqual(binding[57], 0x0A)


if __name__ == "__main__":
    unittest.main()
