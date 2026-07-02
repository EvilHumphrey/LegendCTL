#!/usr/bin/env python3
"""Adversarial re-analysis of the 2026-07-01 step-size capture.

Motivated by the operator's re-verification request (prior: "step 255 = 8-bit").
Tests the strongest alternative hypotheses the original analysis did not rule out:

H1  GRID+SMOOTHING: firmware quantizes to a coarse grid (e.g. 255 levels ~= 257
    spacing) but smooths/interpolates BETWEEN grid points, so transient samples
    show min-gap=1 even though the stick *settles* only on grid values.
    Test: plateau ("dwell") values — where the output rests — should sit on the
    lattice if H1 is true. Metrics: nearest-neighbor gap histogram of the dwell-
    value set + modular concentration R against candidate spacings.

H2  REPORT-RATE, NOT DENSITY: higher step lowers the internal update rate; a
    fixed 1 kHz sampler then sees fewer distinct values per sweep even if value
    granularity is unchanged (Tieba owner reports step<->rate coupling).
    Test: per-run fraction of consecutive samples whose (LX,LY) changed +
    mean plateau duration in ms. If change-rate drops with step, the distinct-
    count decline is (partly) a rate effect.

H3  UNCOMMITTED WRITES (test-matrix integrity): if a step write silently failed,
    that run's stats should match the PREVIOUS step's, not its own.
    Test: run1-vs-run2 agreement per step + cross-step monotonicity + half-split
    stability within each run.

Also computes a GamepadLA-style view: distinct values on near-axis passes and
what a float-rounding pipeline (2/3 decimals) would report for the same data —
to reconcile scene numbers with raw int16 counts.
"""

from __future__ import annotations

import cmath
import json
import math
import statistics
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
AXES = ("LX", "LY")
DWELL_MIN_SAMPLES = 5  # >=5 ms at ~1 kHz counts as "the output rested here"


def load_sweep(path: Path) -> dict[str, list]:
    t, lx, ly = [], [], []
    with path.open("r", encoding="utf-8-sig") as fh:
        header = fh.readline().rstrip("\n").split(",")
        idx = {name: header.index(name) for name in ("t_ms", "LX", "LY", "phase")}
        for line in fh:
            parts = line.rstrip("\n").split(",")
            if parts[idx["phase"]] != "sweep":
                continue
            t.append(float(parts[idx["t_ms"]]))
            lx.append(int(parts[idx["LX"]]))
            ly.append(int(parts[idx["LY"]]))
    return {"t": t, "LX": lx, "LY": ly}


def plateaus(values: list[int], times: list[float]) -> list[tuple[int, int, float]]:
    """(value, run_length, duration_ms) for maximal constant runs."""
    out = []
    i, n = 0, len(values)
    while i < n:
        j = i
        while j + 1 < n and values[j + 1] == values[i]:
            j += 1
        dur = times[j] - times[i] if j > i else 0.0
        out.append((values[i], j - i + 1, dur))
        i = j + 1
    return out


def nn_gap_hist(level_set: set[int]) -> Counter:
    s = sorted(level_set)
    return Counter(b - a for a, b in zip(s, s[1:]))


def modular_r(values: list[int], spacing: float) -> float:
    """Mean resultant length of values folded onto a lattice of given spacing.
    ~1.0 = perfectly on-lattice, ~1/sqrt(N) = no lattice structure."""
    if not values or spacing <= 0:
        return 0.0
    z = sum(cmath.exp(2j * math.pi * (v / spacing)) for v in values)
    return abs(z) / len(values)


def analyze_run(step: int, run: int, csv_path: Path) -> dict:
    data = load_sweep(csv_path)
    n = len(data["t"])
    res: dict = {"step": step, "run": run, "sweep_samples": n}

    # H2: change rate of the (LX, LY) tuple + per-axis
    changed = sum(
        1
        for k in range(1, n)
        if data["LX"][k] != data["LX"][k - 1] or data["LY"][k] != data["LY"][k - 1]
    )
    res["pair_change_rate"] = changed / (n - 1)

    for axis in AXES:
        vals = data[axis]
        pl = plateaus(vals, data["t"])
        distinct = set(vals)
        s = sorted(distinct)
        gaps = [b - a for a, b in zip(s, s[1:])]
        jump_sizes = [abs(pl[k][0] - pl[k - 1][0]) for k in range(1, len(pl))]
        dwell_vals = [v for v, ln, _ in pl if ln >= DWELL_MIN_SAMPLES]
        dwell_set = set(dwell_vals)
        dwell_gaps = nn_gap_hist(dwell_set)
        plateau_ms = [max(d, 1.0) for _, ln, d in pl if ln > 1] or [1.0]

        # candidate lattice spacings for H1
        span = (s[-1] - s[0]) if s else 0
        candidates = {
            "grid_257.0": 257.0,           # 65535 / 255 (8-bit over full range)
            "grid_256": 256.0,
            "grid_128.5": 128.5,           # 65535 / 510 (half-step)
            f"grid_span/{step}": (span / step) if step else 0.0,
        }
        lattice = {
            name: round(modular_r(list(dwell_set), d), 4)
            for name, d in candidates.items()
            if d and d > 1
        }
        n_dw = max(len(dwell_set), 1)
        lattice["uniform_baseline"] = round(1.0 / math.sqrt(n_dw), 4)

        half = n // 2
        res[axis] = {
            "distinct": len(distinct),
            "min_gap": min(gaps) if gaps else None,
            "span": span,
            "axis_change_rate": sum(
                1 for k in range(1, n) if vals[k] != vals[k - 1]
            )
            / (n - 1),
            "mean_plateau_ms": round(statistics.mean(plateau_ms), 3),
            "median_plateau_ms": round(statistics.median(plateau_ms), 3),
            "n_plateaus": len(pl),
            "jump_median": statistics.median(jump_sizes) if jump_sizes else None,
            "jump_p90": sorted(jump_sizes)[int(0.9 * (len(jump_sizes) - 1))]
            if jump_sizes
            else None,
            "jump_hist_top": Counter(jump_sizes).most_common(6),
            "dwell_values": len(dwell_set),
            "dwell_nn_gap_top": dwell_gaps.most_common(6),
            "dwell_nn_gap_median": statistics.median(
                [g for g, c in dwell_gaps.items() for _ in range(c)]
            )
            if dwell_gaps
            else None,
            "lattice_R": lattice,
            "distinct_first_half": len(set(vals[:half])),
            "distinct_second_half": len(set(vals[half:])),
            # GamepadLA-style views
            "near_axis_distinct": len(
                {vals[k] for k in range(n) if abs(data["LY" if axis == "LX" else "LX"][k]) <= 2000}
            ),
            "float3_distinct": len({round(v / 32767.0, 3) for v in vals}),
            "float2_distinct": len({round(v / 32767.0, 2) for v in vals}),
        }
    return res


def main() -> None:
    runs = []
    for csv_path in sorted(HERE.glob("step*_run*.csv")):
        stem = csv_path.stem  # stepN_runM
        step = int(stem.split("_")[0][4:])
        run = int(stem.split("_")[1][3:])
        runs.append(analyze_run(step, run, csv_path))

    runs.sort(key=lambda r: (r["step"], r["run"]))
    out_json = HERE / "reanalysis_2026-07-01.json"
    out_json.write_text(json.dumps(runs, indent=1), encoding="utf-8")

    # Compact human table
    print(
        f"{'step':>4} {'run':>3} {'chg%':>6} {'medPl_ms':>8} "
        f"{'LXdist':>7} {'LXdwell':>7} {'dwNNgap':>8} {'jumpMed':>7} "
        f"{'R257':>6} {'Rbase':>6} {'f3':>5} {'f2':>4} {'half1':>6} {'half2':>6}"
    )
    for r in runs:
        lx = r["LX"]
        print(
            f"{r['step']:>4} {r['run']:>3} {100*r['pair_change_rate']:>5.1f}% "
            f"{lx['median_plateau_ms']:>8.2f} {lx['distinct']:>7} {lx['dwell_values']:>7} "
            f"{str(lx['dwell_nn_gap_median']):>8} {str(lx['jump_median']):>7} "
            f"{lx['lattice_R']['grid_257.0']:>6.3f} {lx['lattice_R']['uniform_baseline']:>6.3f} "
            f"{lx['float3_distinct']:>5} {lx['float2_distinct']:>4} "
            f"{lx['distinct_first_half']:>6} {lx['distinct_second_half']:>6}"
        )
    print(f"\nJSON: {out_json}")


if __name__ == "__main__":
    main()
