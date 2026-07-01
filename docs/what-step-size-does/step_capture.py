#!/usr/bin/env python3
"""Raw XInput logger for the ZD step-size quantization capture.

This script intentionally does not import LegendCTL services and does not touch
HID/device write paths. It only calls XInputGetState and records the raw
game-facing signed int16 thumbstick values.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import msvcrt
except ImportError:  # pragma: no cover - this capture lane runs on Windows.
    msvcrt = None  # type: ignore[assignment]


ERROR_SUCCESS = 0
ERROR_DEVICE_NOT_CONNECTED = 1167
XUSER_MAX_COUNT = 4
DLL_FALLBACK_ORDER = ("XInput1_4.dll", "XInput1_3.dll", "XInput9_1_0.dll")
CSV_FIELDS = [
    "t_ms",
    "LX",
    "LY",
    "phase",
    "RX",
    "RY",
    "slot",
    "packet_number",
    "buttons",
    "LT",
    "RT",
]


class XInputGamepad(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XInputState(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_ulong),
        ("Gamepad", XInputGamepad),
    ]


def load_xinput() -> tuple[Any, str]:
    windll = getattr(ctypes, "WinDLL", None)
    if windll is None:
        raise RuntimeError("ctypes.WinDLL is unavailable; XInput capture requires Windows")

    for name in DLL_FALLBACK_ORDER:
        try:
            dll = windll(name)
        except OSError:
            continue
        try:
            dll.XInputGetState.argtypes = [ctypes.c_uint, ctypes.POINTER(XInputState)]
            dll.XInputGetState.restype = ctypes.c_uint
        except AttributeError:
            continue
        return dll, name

    raise RuntimeError(
        "No usable XInput DLL found; tried " + ", ".join(DLL_FALLBACK_ORDER)
    )


def query_slot(dll: Any, slot: int) -> Optional[dict[str, int]]:
    state = XInputState()
    rc = dll.XInputGetState(slot, ctypes.byref(state))
    if rc != ERROR_SUCCESS:
        return None
    gp = state.Gamepad
    return {
        "slot": int(slot),
        "packet_number": int(state.dwPacketNumber),
        "buttons": int(gp.wButtons),
        "LT": int(gp.bLeftTrigger),
        "RT": int(gp.bRightTrigger),
        "LX": int(gp.sThumbLX),
        "LY": int(gp.sThumbLY),
        "RX": int(gp.sThumbRX),
        "RY": int(gp.sThumbRY),
    }


def connected_slots(dll: Any) -> list[int]:
    return [slot for slot in range(XUSER_MAX_COUNT) if query_slot(dll, slot) is not None]


def detect_slot(dll: Any, wait_seconds: float) -> tuple[int, list[int]]:
    deadline = time.perf_counter() + max(0.0, wait_seconds)
    last_slots: list[int] = []
    while True:
        last_slots = connected_slots(dll)
        if last_slots:
            return last_slots[0], last_slots
        if time.perf_counter() >= deadline:
            break
        time.sleep(0.1)
    raise RuntimeError("No connected XInput slot detected")


def key_pressed() -> bool:
    if msvcrt is None:
        return False
    if not msvcrt.kbhit():
        return False
    # Consume exactly one key so the terminal does not receive it later.
    msvcrt.getwch()
    return True


def wait_for_enter(enabled: bool) -> None:
    if enabled:
        input("Press Enter when the test stick is centered and hands-off at rest...")


def write_sample(
    writer: csv.DictWriter,
    phase: str,
    sample: dict[str, int],
    capture_start: float,
) -> None:
    writer.writerow(
        {
            "t_ms": f"{(time.perf_counter() - capture_start) * 1000.0:.3f}",
            "LX": sample["LX"],
            "LY": sample["LY"],
            "phase": phase,
            "RX": sample["RX"],
            "RY": sample["RY"],
            "slot": sample["slot"],
            "packet_number": sample["packet_number"],
            "buttons": sample["buttons"],
            "LT": sample["LT"],
            "RT": sample["RT"],
        }
    )


def capture_phase(
    *,
    dll: Any,
    slot: int,
    writer: csv.DictWriter,
    capture_start: float,
    phase: str,
    duration_s: float,
    hz: float,
    stop_on_keypress: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    deadline = started + duration_s
    period = 1.0 / hz if hz > 0 else 0.0
    next_tick = started
    samples = 0
    disconnects = 0
    stopped_by_key = False

    while time.perf_counter() < deadline:
        sample = query_slot(dll, slot)
        if sample is None:
            disconnects += 1
        else:
            write_sample(writer, phase, sample, capture_start)
            samples += 1

        if stop_on_keypress and key_pressed():
            stopped_by_key = True
            break

        if period > 0:
            next_tick += period
            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(delay)

    ended = time.perf_counter()
    elapsed_s = ended - started
    return {
        "phase": phase,
        "started_ms": (started - capture_start) * 1000.0,
        "ended_ms": (ended - capture_start) * 1000.0,
        "duration_s": elapsed_s,
        "samples": samples,
        "disconnects": disconnects,
        "actual_hz": samples / elapsed_s if elapsed_s > 0 else None,
        "stopped_by_key": stopped_by_key,
    }


def default_output_path(args: argparse.Namespace) -> Path:
    if args.output is not None:
        return args.output
    if args.step is None or args.run is None:
        raise SystemExit("--output is required unless both --step and --run are provided")
    return Path(__file__).resolve().parent / f"step{args.step}_run{args.run}.csv"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture raw XInput thumbstick samples for step-size quantization analysis."
    )
    parser.add_argument("--output", type=Path, help="CSV output path")
    parser.add_argument("--step", type=int, help="LegendCTL step-size value under test")
    parser.add_argument("--run", type=int, help="Run number for this step")
    parser.add_argument("--step-readback", type=int, help="Verified LegendCTL readback value")
    parser.add_argument("--slot", default="auto", help="XInput slot 0-3, or auto")
    parser.add_argument("--wait-seconds", type=float, default=10.0)
    parser.add_argument("--rest-ms", type=float, default=3000.0)
    parser.add_argument("--sweep-ms", type=float, default=25000.0)
    parser.add_argument("--hz", type=float, default=1000.0, help="Best-effort polling rate; 0 = tight loop")
    parser.add_argument("--stick", default="left", choices=("left", "right"))
    parser.add_argument("--device", default="", help="Operator-confirmed controller model")
    parser.add_argument("--firmware", default="", help="Operator-confirmed firmware version")
    parser.add_argument("--connection", default="", help="Operator-confirmed connection mode")
    parser.add_argument("--module", default="", help="Operator-confirmed stick/module under test")
    parser.add_argument("--calibration", default="", help="Operator-confirmed calibration/default baseline")
    parser.add_argument("--legendctl-version", default="", help="LegendCTL app version/build")
    parser.add_argument("--operator-note", default="", help="Free-form note copied into metadata")
    parser.add_argument(
        "--no-enter-prompt",
        action="store_true",
        help="Start immediately instead of waiting for Enter before the at-rest segment",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.step is not None and not (1 <= args.step <= 255):
        raise SystemExit("--step must be in [1, 255]")
    if args.step_readback is not None and not (1 <= args.step_readback <= 255):
        raise SystemExit("--step-readback must be in [1, 255]")

    output = default_output_path(args).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    meta_path = output.with_suffix(".meta.json")

    dll, dll_name = load_xinput()
    if str(args.slot).lower() == "auto":
        slot, slots_seen = detect_slot(dll, args.wait_seconds)
    else:
        slot = int(args.slot)
        if not (0 <= slot < XUSER_MAX_COUNT):
            raise SystemExit("--slot must be auto or an integer 0-3")
        slots_seen = connected_slots(dll)
        if query_slot(dll, slot) is None:
            raise RuntimeError(f"Requested XInput slot {slot} is not connected")

    print(f"XInput DLL: {dll_name}")
    print(f"Connected XInput slots: {slots_seen}; capturing slot {slot}")
    if len(slots_seen) > 1 and str(args.slot).lower() == "auto":
        print("WARNING: multiple XInput slots are connected; auto selected the first slot.")
    print(f"Output: {output}")
    print(f"At-rest segment: {args.rest_ms:.0f} ms; sweep segment: {args.sweep_ms:.0f} ms")

    wait_for_enter(not args.no_enter_prompt)

    phase_summaries: list[dict[str, Any]] = []
    capture_started = time.perf_counter()
    started_wall = datetime.now(timezone.utc).isoformat()
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()

        print("REST segment recording now. Keep the stick untouched.")
        phase_summaries.append(
            capture_phase(
                dll=dll,
                slot=slot,
                writer=writer,
                capture_start=capture_started,
                phase="rest",
                duration_s=args.rest_ms / 1000.0,
                hz=args.hz,
                stop_on_keypress=False,
            )
        )

        print("\aSWEEP NOW: perform the agreed sweep protocol.")
        print("Press any key in this terminal to end the sweep early.")
        phase_summaries.append(
            capture_phase(
                dll=dll,
                slot=slot,
                writer=writer,
                capture_start=capture_started,
                phase="sweep",
                duration_s=args.sweep_ms / 1000.0,
                hz=args.hz,
                stop_on_keypress=True,
            )
        )

    capture_ended = time.perf_counter()
    total_samples = sum(int(item["samples"]) for item in phase_summaries)
    metadata = {
        "script": Path(__file__).name,
        "started_at_utc": started_wall,
        "ended_at_utc": datetime.now(timezone.utc).isoformat(),
        "duration_s": capture_ended - capture_started,
        "csv": output.name,
        "xinput_dll": dll_name,
        "slot": slot,
        "connected_slots_seen": slots_seen,
        "csv_fields": CSV_FIELDS,
        "step": args.step,
        "run": args.run,
        "step_readback": args.step_readback,
        "stick": args.stick,
        "rest_ms": args.rest_ms,
        "sweep_ms": args.sweep_ms,
        "target_hz": args.hz,
        "phase_summaries": phase_summaries,
        "total_samples": total_samples,
        "device": args.device,
        "firmware": args.firmware,
        "connection": args.connection,
        "module": args.module,
        "calibration": args.calibration,
        "legendctl_version": args.legendctl_version,
        "operator_note": args.operator_note,
        "python": sys.version,
        "platform": platform.platform(),
    }
    meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print(f"Capture complete: {total_samples} samples")
    for phase in phase_summaries:
        hz_text = f"{phase['actual_hz']:.1f} Hz" if phase["actual_hz"] else "n/a"
        print(
            f"  {phase['phase']}: {phase['samples']} samples, {phase['disconnects']} "
            f"disconnect polls, {hz_text}"
        )
    print(f"Metadata: {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
