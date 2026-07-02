#!/usr/bin/env python3
"""Discriminate INCREMENT-QUANTIZER vs RATE-LIMITER for the step setting.

Increment model: emit a new value when the sensor moves >= ~step counts from
the last emitted value. Prediction during a manual sweep with varying speed:
  - jump sizes cluster tightly at ~step (low coefficient of variation),
  - plateau durations vary widely (inverse to speed),
  - within a run, speed correlates with 1/plateau_duration but NOT jump size.

Rate model: emit at a fixed period T (T grows with step), full resolution.
Prediction:
  - plateau durations cluster tightly at ~T (low CV),
  - jump sizes vary widely (proportional to speed),
  - within a run, speed correlates with jump size but NOT plateau duration.

We compute, per run and axis, the CV (IQR/median) of both distributions and
the Spearman-ish rank correlation between each plateau's duration and the
size of the jump that ends it. Uses mid-speed plateaus only (drops the tails
where the stick reverses/lingers).
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

HERE = Path(__file__).parent


def load_sweep(path: Path):
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
    return t, lx, ly


def plateaus(values, times):
    out = []
    i, n = 0, len(values)
    while i < n:
        j = i
        while j + 1 < n and values[j + 1] == values[i]:
            j += 1
        out.append((values[i], times[i], times[j]))
        i = j + 1
    return out


def iqr_over_median(vals):
    if len(vals) < 8:
        return None
    v = sorted(vals)
    q1 = v[len(v) // 4]
    q3 = v[(3 * len(v)) // 4]
    med = statistics.median(v)
    return round((q3 - q1) / med, 3) if med else None


def rank_corr(a, b):
    """Spearman rank correlation (simple, ties get order rank)."""
    if len(a) < 10:
        return None
    ra = {id_: r for r, (id_, _) in enumerate(sorted(enumerate(a), key=lambda x: x[1]))}
    rb = {id_: r for r, (id_, _) in enumerate(sorted(enumerate(b), key=lambda x: x[1]))}
    n = len(a)
    xs = [ra[i] for i in range(n)]
    ys = [rb[i] for i in range(n)]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    return round(cov / (vx * vy) ** 0.5, 3) if vx and vy else None


def analyze(step, run, csv_path):
    t, lx, ly = load_sweep(csv_path)
    rows = []
    for axis_name, vals in (("LX", lx), ("LY", ly)):
        pl = plateaus(vals, t)
        # pair each plateau with the jump that ENDS it; keep active-motion pairs
        durs, jumps = [], []
        for k in range(len(pl) - 1):
            v, t0, t1 = pl[k]
            nxt = pl[k + 1][0]
            jump = abs(nxt - v)
            dur = max(t1 - t0, 0.5)  # ms; single-sample plateau ~ 1 sample
            if jump == 0:
                continue
            durs.append(dur)
            jumps.append(jump)
        if len(jumps) < 10:
            continue
        # trim to the central motion regime: drop the slowest 20% and fastest 5%
        order = sorted(range(len(durs)), key=lambda i: durs[i])
        keep = order[: int(0.95 * len(order))]
        keep = [i for i in keep if durs[i] <= sorted(durs)[int(0.8 * (len(durs) - 1))]]
        d = [durs[i] for i in keep]
        j = [jumps[i] for i in keep]
        rows.append(
            {
                "axis": axis_name,
                "pairs": len(j),
                "jump_median": statistics.median(j),
                "jump_CV": iqr_over_median(j),
                "plateau_median_ms": round(statistics.median(d), 2),
                "plateau_CV": iqr_over_median(d),
                "corr_dur_vs_jump": rank_corr(d, j),
            }
        )
    return rows


def main():
    results = {}
    for csv_path in sorted(HERE.glob("step*_run*.csv")):
        stem = csv_path.stem
        step = int(stem.split("_")[0][4:])
        run = int(stem.split("_")[1][3:])
        if step not in (50, 144, 255):
            continue
        results[f"step{step}_run{run}"] = analyze(step, run, csv_path)

    print(
        f"{'run':>14} {'axis':>4} {'pairs':>6} {'jumpMed':>7} {'jumpCV':>7} "
        f"{'platMed':>8} {'platCV':>7} {'corr(d,j)':>9}"
    )
    for name, rows in results.items():
        for r in rows:
            print(
                f"{name:>14} {r['axis']:>4} {r['pairs']:>6} {r['jump_median']:>7} "
                f"{str(r['jump_CV']):>7} {r['plateau_median_ms']:>8} "
                f"{str(r['plateau_CV']):>7} {str(r['corr_dur_vs_jump']):>9}"
            )
    (HERE / "discrimination_2026-07-01.json").write_text(
        json.dumps(results, indent=1), encoding="utf-8"
    )
    print("\nIncrement model => jumpCV small, platCV large, corr ~ 0/negative")
    print("Rate model      => platCV small, jumpCV large, corr irrelevant (jump tracks speed)")


if __name__ == "__main__":
    main()
