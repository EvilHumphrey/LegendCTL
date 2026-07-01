#!/usr/bin/env python3
"""Analyze ZD step-size quantization capture CSVs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional


AXES = ("LX", "LY")
STEP_RE = re.compile(r"step(?P<step>\d+)_run(?P<run>\d+)", re.IGNORECASE)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def parse_step_run(path: Path, meta: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    step = meta.get("step")
    run = meta.get("run")
    match = STEP_RE.search(path.stem)
    if match:
        step = int(match.group("step")) if step is None else int(step)
        run = int(match.group("run")) if run is None else int(run)
    return (int(step) if step is not None else None, int(run) if run is not None else None)


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            row: dict[str, Any] = {}
            for key, value in raw.items():
                if value is None:
                    row[key] = value
                    continue
                text = value.strip()
                if key in {"LX", "LY", "RX", "RY", "slot", "packet_number", "buttons", "LT", "RT"}:
                    row[key] = int(text)
                elif key == "t_ms":
                    row[key] = float(text)
                else:
                    row[key] = text
            rows.append(row)
    return rows


def median(values: list[int]) -> float:
    if not values:
        return 0.0
    return float(statistics.median(values))


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * pct
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(ordered[lo])
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo))


def gap_metrics(levels: Iterable[int]) -> dict[str, Any]:
    distinct = sorted(set(levels))
    gaps = [b - a for a, b in zip(distinct, distinct[1:]) if b > a]
    span = distinct[-1] - distinct[0] if distinct else 0
    min_gap = min(gaps) if gaps else None
    hist = Counter(gaps)
    effective_bits = None
    if min_gap and span > 0:
        effective_bits = math.log2((span / min_gap) + 1.0)
    return {
        "distinct_count": len(distinct),
        "min": distinct[0] if distinct else None,
        "max": distinct[-1] if distinct else None,
        "span": span,
        "min_nonzero_gap": min_gap,
        "effective_bits": effective_bits,
        "gap_histogram": dict(sorted(hist.items())),
        "gap_histogram_top": hist.most_common(12),
        "levels": distinct,
    }


def rest_quality(rest: list[dict[str, Any]], axis: str) -> dict[str, Any]:
    values = [int(row[axis]) for row in rest if axis in row]
    if not values:
        return {
            "median": None,
            "min": None,
            "max": None,
            "max_deviation": None,
            "likely_contaminated": True,
        }
    center = median(values)
    deviations = [abs(value - center) for value in values]
    max_deviation = max(deviations)
    return {
        "median": center,
        "min": min(values),
        "max": max(values),
        "max_deviation": max_deviation,
        # The early protocol was live/manual; several runs start moving before
        # the scripted three-second rest window ends. Mark, do not discard, those
        # rest windows. The primary level metric uses the explicit sweep phase.
        "likely_contaminated": max_deviation > 2000,
    }


def phase_rows(rows: list[dict[str, Any]], phase: str, fallback_rest_ms: float) -> list[dict[str, Any]]:
    with_phase = [row for row in rows if row.get("phase") == phase]
    if with_phase:
        return with_phase
    if phase == "rest":
        return [row for row in rows if float(row["t_ms"]) <= fallback_rest_ms]
    if phase == "sweep":
        return [row for row in rows if float(row["t_ms"]) > fallback_rest_ms]
    return []


def analyze_axis(
    *,
    rows: list[dict[str, Any]],
    rest: list[dict[str, Any]],
    sweep: list[dict[str, Any]],
    axis: str,
    noise_margin: int,
) -> dict[str, Any]:
    rest_values = [int(row[axis]) for row in rest if axis in row]
    center = median(rest_values)
    deviations = [abs(value - center) for value in rest_values]
    jitter_max = max(deviations) if deviations else 0.0
    jitter_p95 = percentile(deviations, 0.95)
    movement_threshold = max(1, int(math.ceil(jitter_max + noise_margin)))

    moving_values = [
        int(row[axis])
        for row in sweep
        if axis in row and abs(int(row[axis]) - center) > movement_threshold
    ]
    all_values = [int(row[axis]) for row in rows if axis in row]
    metrics = gap_metrics(moving_values)
    metrics.update(
        {
            "rest_center": center,
            "rest_jitter_max": jitter_max,
            "rest_jitter_p95": jitter_p95,
            "movement_threshold": movement_threshold,
            "moving_samples": len(moving_values),
            "total_samples": len(all_values),
        }
    )
    return metrics


def analyze_sweep_axis(*, sweep: list[dict[str, Any]], axis: str) -> dict[str, Any]:
    values = [int(row[axis]) for row in sweep if axis in row]
    metrics = gap_metrics(values)
    metrics.update({"sweep_samples": len(values)})
    return metrics


def analyze_file(path: Path, noise_margin: int) -> dict[str, Any]:
    meta = load_json(path.with_suffix(".meta.json"))
    step, run = parse_step_run(path, meta)
    rows = read_rows(path)
    rest_ms = float(meta.get("rest_ms", 3000.0))
    rest = phase_rows(rows, "rest", rest_ms)
    sweep = phase_rows(rows, "sweep", rest_ms)
    duration_ms = max((float(row["t_ms"]) for row in rows), default=0.0)
    result = {
        "file": path.name,
        "step": step,
        "run": run,
        "metadata": {
            key: meta.get(key)
            for key in (
                "step_readback",
                "slot",
                "xinput_dll",
                "device",
                "firmware",
                "connection",
                "module",
                "calibration",
                "legendctl_version",
                "operator_note",
            )
            if meta.get(key) not in (None, "")
        },
        "sample_count": len(rows),
        "rest_samples": len(rest),
        "sweep_samples": len(sweep),
        "duration_ms": duration_ms,
        "axes": {},
    }
    for axis in AXES:
        result["axes"][axis] = analyze_sweep_axis(sweep=sweep, axis=axis)
        result["axes"][axis]["rest_quality"] = rest_quality(rest, axis)
        result["axes"][axis]["legacy_moving_filter"] = analyze_axis(
            rows=rows,
            rest=rest,
            sweep=sweep,
            axis=axis,
            noise_margin=noise_margin,
        )
    return result


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_step: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        if result.get("step") is not None:
            by_step[int(result["step"])].append(result)

    summary: dict[str, Any] = {}
    for step, step_results in sorted(by_step.items()):
        step_entry: dict[str, Any] = {
            "runs": sorted(result.get("run") for result in step_results),
            "files": [result["file"] for result in step_results],
            "axes": {},
        }
        for axis in AXES:
            levels: list[int] = []
            moving_samples = 0
            contaminated_rest_runs = 0
            rest_max_deviation = 0.0
            for result in step_results:
                axis_result = result["axes"][axis]
                levels.extend(axis_result.get("levels", []))
                moving_samples += int(axis_result.get("sweep_samples", 0))
                rest = axis_result.get("rest_quality", {})
                if rest.get("likely_contaminated"):
                    contaminated_rest_runs += 1
                if rest.get("max_deviation") is not None:
                    rest_max_deviation = max(rest_max_deviation, float(rest["max_deviation"]))
            metrics = gap_metrics(levels)
            metrics.pop("levels", None)
            metrics.update(
                {
                    "sweep_samples": moving_samples,
                    "contaminated_rest_runs": contaminated_rest_runs,
                    "rest_max_deviation_across_runs": rest_max_deviation,
                }
            )
            step_entry["axes"][axis] = metrics
        summary[str(step)] = step_entry
    return summary


def fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def markdown_table(summary: dict[str, Any]) -> str:
    lines = [
        "| Step | Axis | Runs | Distinct sweep levels | Min gap | Observed span | Effective bits | Sweep samples | Rest windows flagged |",
        "|---:|:---:|:---|---:|---:|---:|---:|---:|---:|",
    ]
    for step, entry in sorted(summary.items(), key=lambda item: int(item[0])):
        runs = ", ".join(str(run) for run in entry["runs"])
        for axis in AXES:
            data = entry["axes"][axis]
            lines.append(
                "| {step} | {axis} | {runs} | {levels} | {gap} | {span} | {bits} | {samples} | {jitter} |".format(
                    step=step,
                    axis=axis,
                    runs=runs,
                    levels=fmt(data["distinct_count"], 0),
                    gap=fmt(data["min_nonzero_gap"], 0),
                    span=fmt(data["span"], 0),
                    bits=fmt(data["effective_bits"], 2),
                    samples=fmt(data["sweep_samples"], 0),
                    jitter=fmt(data["contaminated_rest_runs"], 0),
                )
            )
    return "\n".join(lines)


def short_gap_histogram(data: dict[str, Any], limit: int = 10) -> str:
    hist = data.get("gap_histogram", {})
    if not hist:
        return "n/a"
    pairs = sorted((int(gap), int(count)) for gap, count in hist.items())
    if len(pairs) > limit:
        head = pairs[:limit]
        return ", ".join(f"{gap}:{count}" for gap, count in head) + f", ... ({len(pairs)} gap sizes)"
    return ", ".join(f"{gap}:{count}" for gap, count in pairs)


def write_markdown(path: Path, results: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    lines = [
        "# Step Quantization Analysis Summary",
        "",
        markdown_table(summary),
        "",
        "## Gap Histograms",
        "",
        "Histogram entries are sorted-level gaps as `gap:count`, computed over the explicit sweep phase. Contaminated rest windows are flagged separately.",
        "",
    ]
    for step, entry in sorted(summary.items(), key=lambda item: int(item[0])):
        lines.append(f"### Step {step}")
        lines.append("")
        for axis in AXES:
            data = entry["axes"][axis]
            lines.append(f"- {axis}: {short_gap_histogram(data)}")
        lines.append("")

    lines.extend(["## Per-Run Detail", ""])
    for result in sorted(results, key=lambda item: (item.get("step") or 0, item.get("run") or 0, item["file"])):
        lines.append(
            f"- `{result['file']}`: step={result.get('step')}, run={result.get('run')}, "
            f"samples={result['sample_count']}, sweep_samples={result['sweep_samples']}, "
            f"duration_ms={result['duration_ms']:.1f}"
        )
        for axis in AXES:
            data = result["axes"][axis]
            lines.append(
                f"  - {axis}: distinct={data['distinct_count']}, min_gap={fmt(data['min_nonzero_gap'], 0)}, "
                f"span={data['span']}, bits={fmt(data['effective_bits'], 2)}, "
                f"rest_max_deviation={fmt(data['rest_quality']['max_deviation'], 0)}, "
                f"rest_contaminated={data['rest_quality']['likely_contaminated']}"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def maybe_plot(input_dir: Path, results: list[dict[str, Any]], output: Path) -> Optional[str]:
    try:
        import matplotlib.pyplot as plt  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - matplotlib is optional.
        return f"matplotlib unavailable: {exc}"

    if not results:
        return "no results to plot"

    fig, axes = plt.subplots(2, 1, figsize=(10, 8), constrained_layout=True)
    for result in sorted(results, key=lambda item: (item.get("step") or 0, item.get("run") or 0)):
        csv_path = input_dir / result["file"]
        rows = read_rows(csv_path)
        sweep_rows = phase_rows(rows, "sweep", 3000.0)
        if not sweep_rows:
            sweep_rows = rows
        t0 = float(sweep_rows[0]["t_ms"]) if sweep_rows else 0.0
        times = [(float(row["t_ms"]) - t0) / 1000.0 for row in sweep_rows]
        label_prefix = f"step {result.get('step')} run {result.get('run')}"
        axes[0].plot(times, [int(row["LX"]) for row in sweep_rows], linewidth=0.8, label=f"{label_prefix} LX")
        axes[1].plot(times, [int(row["LY"]) for row in sweep_rows], linewidth=0.8, label=f"{label_prefix} LY")
    axes[0].set_title("LX value vs sweep time")
    axes[1].set_title("LY value vs sweep time")
    for axis in axes:
        axis.set_xlabel("sweep time (s)")
        axis.set_ylabel("raw int16")
        axis.grid(True, alpha=0.25)
        axis.legend(fontsize=7, ncol=2)
    fig.savefig(output, dpi=150)
    plt.close(fig)
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze step-size raw XInput capture CSVs.")
    parser.add_argument("--input-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--glob", default="step*_run*.csv")
    parser.add_argument("--noise-margin", type=int, default=1)
    parser.add_argument("--json-output", type=Path, default=None)
    parser.add_argument("--markdown-output", type=Path, default=None)
    parser.add_argument("--plot-output", type=Path, default=None)
    parser.add_argument("--no-plot", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.resolve()
    csv_paths = sorted(input_dir.glob(args.glob))
    if not csv_paths:
        raise SystemExit(f"No CSV files matched {args.glob!r} in {input_dir}")

    results = [analyze_file(path, args.noise_margin) for path in csv_paths]
    summary = aggregate(results)
    payload = {"files": [path.name for path in csv_paths], "runs": results, "summary_by_step": summary}

    json_output = args.json_output or (input_dir / "analysis_summary.json")
    markdown_output = args.markdown_output or (input_dir / "analysis_summary.md")
    plot_output = args.plot_output or (input_dir / "step_quantization_plot.png")

    json_output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_markdown(markdown_output, results, summary)
    print(markdown_table(summary))
    print(f"Wrote {json_output}")
    print(f"Wrote {markdown_output}")

    if not args.no_plot:
        plot_issue = maybe_plot(input_dir, results, plot_output)
        if plot_issue:
            print(f"Plot not written ({plot_issue})")
        else:
            print(f"Wrote {plot_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
