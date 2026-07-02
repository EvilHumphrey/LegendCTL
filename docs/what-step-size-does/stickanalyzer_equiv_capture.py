#!/usr/bin/env python3
"""Guided StickAnalyzer-equivalent capture — step-size validation matrix.

Re-implements GamepadLA StickAnalyzer's published algorithm (v2.0.3.0 source,
github.com/cakama3a/StickAnalyzer) on the same self-contained XInput reader as
step_capture.py, so our numbers are directly comparable to gamepadla.com's:

  - slow ONE-WAY center->edge sweep (~6-8 s), one axis
  - monotonic dead-band filter: keep a sample only if it advances the running
    max by >= 0.0001 (on the [-1,1]-normalized axis)
  - avg_step_resolution = mean gap between kept points
  - SFC ("steps from center")  = int(1 / avg_step_resolution)
  - True Bitness               = log2(2 * SFC)

No HID/device writes — XInputGetState only. The operator sets each step in
LegendCTL and confirms it with the footer READ button (a true device read).

Increment-model predictions this run tests (from the 2026-07-01 re-analysis):
  step  25 -> SFC ~1250       bits ~11.3
  step  73 -> SFC ~400-445    bits ~9.6-9.8   (GamepadLA stock ZD: 443 / 9.8)
  step 144 -> SFC ~205-225    bits ~8.7-8.8
  step 255 -> SFC ~120-130    bits ~7.9-8.0   (the "255 = 8-bit" check)
  step   1 -> no firm prediction (noise + the 0.0001 dead-band set the floor)
"""

from __future__ import annotations

import ctypes
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import msvcrt
except ImportError:
    msvcrt = None

HERE = Path(__file__).parent
ERROR_SUCCESS = 0
XUSER_MAX_COUNT = 4
DLL_FALLBACK_ORDER = ("XInput1_4.dll", "XInput1_3.dll", "XInput9_1_0.dll")

STEPS = [1, 25, 73, 144, 255]
RUNS_PER_STEP = 2
REST_S = 2.0
SWEEP_MAX_S = 20.0
TARGET_HZ = 1000.0
FILTER_THRESHOLD = 0.0001  # StickAnalyzer's min_threshold on normalized axis

PREDICTIONS = {
    1: "no firm prediction (noise/dead-band floor)",
    25: "SFC ~1250, bits ~11.3",
    73: "SFC ~400-445, bits ~9.6-9.8 (GamepadLA stock: 443/9.8)",
    144: "SFC ~205-225, bits ~8.7-8.8",
    255: "SFC ~120-130, bits ~7.9-8.0",
}


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
    _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", XInputGamepad)]


def load_xinput():
    for name in DLL_FALLBACK_ORDER:
        try:
            dll = ctypes.WinDLL(name)
            dll.XInputGetState.argtypes = [ctypes.c_uint, ctypes.POINTER(XInputState)]
            dll.XInputGetState.restype = ctypes.c_uint
            return dll, name
        except (OSError, AttributeError):
            continue
    raise RuntimeError("No usable XInput DLL found")


def query(dll, slot):
    state = XInputState()
    if dll.XInputGetState(slot, ctypes.byref(state)) != ERROR_SUCCESS:
        return None
    gp = state.Gamepad
    return (int(gp.sThumbLX), int(gp.sThumbLY), int(state.dwPacketNumber))


def detect_slot(dll):
    for slot in range(XUSER_MAX_COUNT):
        if query(dll, slot) is not None:
            return slot
    return None


def drain_keys():
    if msvcrt:
        while msvcrt.kbhit():
            msvcrt.getch()


def capture(dll, slot, duration_s, phase, stop_on_enter=False):
    rows = []
    period = 1.0 / TARGET_HZ
    t0 = time.perf_counter()
    next_t = t0
    while True:
        now = time.perf_counter()
        if now - t0 >= duration_s:
            break
        if stop_on_enter and msvcrt and msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b"\r", b"\n"):
                break
        if now >= next_t:
            q = query(dll, slot)
            if q is not None:
                rows.append(((now - t0) * 1000.0, q[0], q[1], phase, q[2]))
            next_t += period
        else:
            time.sleep(0.0002)
    return rows


def stickanalyzer_stats(sweep_lx_raw):
    """StickAnalyzer's pipeline on the normalized axis; auto-flip direction."""
    vals = [v / 32767.0 for v in sweep_lx_raw]
    if not vals:
        return None
    if (vals[-1] - vals[0]) < 0:
        vals = [-v for v in vals]
    kept = []
    running_max = float("-inf")
    for v in vals:
        if not kept or (v - running_max) >= FILTER_THRESHOLD:
            kept.append(v)
            running_max = v
    if len(kept) < 3:
        return None
    gaps = [kept[i] - kept[i - 1] for i in range(1, len(kept))]
    avg = sum(gaps) / len(gaps)
    sfc = int(1.0 / avg) if avg > 0 else None
    bits = round(math.log2(2 * sfc), 2) if sfc else None
    tremor_pct = round(100.0 * (1 - len(kept) / len(vals)), 1)
    return {
        "kept_points": len(kept),
        "avg_step_resolution": round(avg, 6),
        "SFC": sfc,
        "true_bitness": bits,
        "tremor_pct": tremor_pct,
        "span_covered": round(kept[-1] - kept[0], 4),
    }


def pitch_stats(sweep_lx_raw):
    """Our increment metric for direct comparison: median plateau-to-plateau jump."""
    pl = []
    i, n = 0, len(sweep_lx_raw)
    while i < n:
        j = i
        while j + 1 < n and sweep_lx_raw[j + 1] == sweep_lx_raw[i]:
            j += 1
        pl.append(sweep_lx_raw[i])
        i = j + 1
    jumps = [abs(pl[k] - pl[k - 1]) for k in range(1, len(pl)) if pl[k] != pl[k - 1]]
    if not jumps:
        return None
    return {"pitch_median": statistics.median(jumps), "n_plateaus": len(pl)}


def main():
    print(__doc__)
    dll, dll_name = load_xinput()
    slot = detect_slot(dll)
    if slot is None:
        print("!! No XInput controller detected. Connect the ZD (wired) and rerun.")
        sys.exit(1)
    print(f"Controller on XInput slot {slot} via {dll_name}.\n")

    results = []
    for step in STEPS:
        print("=" * 72)
        print(f"STEP {step}  —  prediction: {PREDICTIONS[step]}")
        print("=" * 72)
        print(
            f"1. In LegendCTL: set the step-size slider to {step}.\n"
            "2. Click the footer READ button (fresh device read).\n"
            "3. Confirm the slider now shows the value you set."
        )
        readback = input(f"   Type the slider value shown after READ (expect {step}): ").strip()

        for run in range(1, RUNS_PER_STEP + 1):
            print(f"\n-- step {step} run {run}/{RUNS_PER_STEP} --")
            input("   Center the stick (hands off), then press Enter to start the 2s rest...")
            drain_keys()
            rows = capture(dll, slot, REST_S, "rest")
            print(
                "   Now SLOWLY push the LEFT stick from center to FULL RIGHT over ~6-8s\n"
                "   (one smooth motion, no reversing), hold at the edge, then press Enter."
            )
            drain_keys()
            rows += capture(dll, slot, SWEEP_MAX_S, "sweep", stop_on_enter=True)
            drain_keys()

            sweep_lx = [r[1] for r in rows if r[3] == "sweep"]
            sa = stickanalyzer_stats(sweep_lx)
            pt = pitch_stats(sweep_lx)
            stamp = datetime.now(timezone.utc).isoformat()

            csv_path = HERE / f"sa_step{step}_run{run}.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as fh:
                fh.write("t_ms,LX,LY,phase,packet_number\n")
                for t_ms, lx, ly, phase, pkt in rows:
                    fh.write(f"{t_ms:.3f},{lx},{ly},{phase},{pkt}\n")
            meta = {
                "protocol": "stickanalyzer-equivalent one-way center->right sweep",
                "step": step,
                "run": run,
                "step_readback_operator": readback,
                "captured_at_utc": stamp,
                "xinput_dll": dll_name,
                "slot": slot,
                "samples": len(rows),
                "filter_threshold": FILTER_THRESHOLD,
                "stickanalyzer_stats": sa,
                "pitch_stats": pt,
            }
            (HERE / f"sa_step{step}_run{run}.meta.json").write_text(
                json.dumps(meta, indent=1), encoding="utf-8"
            )

            if sa:
                print(
                    f"   -> SFC {sa['SFC']}  ·  True Bitness {sa['true_bitness']}  ·  "
                    f"step_res {sa['avg_step_resolution']}  ·  tremor {sa['tremor_pct']}%  ·  "
                    f"span {sa['span_covered']}"
                )
                if pt:
                    print(f"   -> pitch (median raw jump): {pt['pitch_median']}")
                if sa["span_covered"] < 0.85:
                    print("   !! span < 0.85 — sweep didn't reach the edge; consider redoing this run.")
            else:
                print("   !! Not enough motion captured — redo this run.")
            results.append({"step": step, "run": run, **(sa or {}), **(pt or {})})

    out = HERE / "stickanalyzer_equiv_RESULTS_2026-07-01.json"
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print("\n" + "=" * 72)
    print(f"{'step':>5} {'run':>3} {'SFC':>6} {'bits':>6} {'pitch':>7}")
    for r in results:
        print(
            f"{r['step']:>5} {r['run']:>3} {str(r.get('SFC')):>6} "
            f"{str(r.get('true_bitness')):>6} {str(r.get('pitch_median')):>7}"
        )
    print(f"\nSaved: {out}")
    print("Remember to restore step size to 73 in LegendCTL (set 73 -> READ -> confirm).")


if __name__ == "__main__":
    main()
