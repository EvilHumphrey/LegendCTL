# What ZD's "step size" actually does to your stick output — measured (corrected)

**TL;DR.** We measured the ZD Ultimate Legend's stick output at the game-facing XInput layer across nine "step size" settings from 1 to 255. **Step size is a motion-increment quantizer:** the stick reports a new position only after moving at least ~step raw units (of 65,535 full-scale) from the last position it reported. The measured movement between reports equals the step setting within a few percent at every step we can resolve. That makes the community's "255 ≈ 8-bit" intuition **right in practice** — at step 255, motion happens in ~257-count chunks, ~126 steps from center to edge, ≈ 8.0-bit granularity in GamepadLA's True-Bitness convention — with one refinement: it's a *relative* grid (anchored to the last reported value), not a fixed 256-point lattice.

> **Correction notice (2026-07-01).** The first version of this page concluded step size was "a density knob, not a grid," based on pooled distinct-value counts and minimum gaps. A skeptical second pass — prompted by how consistently the community holds the "255 ≈ 8-bit" view — re-analyzed the same capture adversarially and showed those metrics are the wrong ones for a *relative* quantizer. The community was right; our first framing was wrong. This page is the corrected analysis of the same data, and the superseded metrics are explained below so you can reproduce both and see the difference yourself.

## What we measured

Left stick · firmware 1.24 · wired USB · no deadzones · linear curve. For each step value we set it in LegendCTL and confirmed the readback, then logged the raw 16-bit XInput axis values (`sThumbLX`/`sThumbLY` — exactly what a game sees, with no deadzone or smoothing applied) at ~1000 Hz through two slow 25-second full-range sweeps. **~504,000 samples** total across 9 step values × 2 runs.

## What we found: a motion-increment quantizer

**The stick reports a new position only after moving at least ~step raw units from the last position it reported.** That's the whole mechanism, and it's visible directly in the data: the median movement between consecutive stick reports ("pitch") equals the step setting, within a few percent, at every step value we can resolve — on both axes, in both runs:

| step setting | measured jump between reports | ≈ steps center-to-edge | ≈ "True Bitness"* |
|---:|---:|---:|---:|
| 73 (default) | 76–82 | ~430 | ~9.7 |
| 100 | 107–110 | ~300 | ~9.2 |
| 144 | 152 | ~215 | ~8.75 |
| 180 | 190 | ~172 | ~8.4 |
| 220 | 228 | ~144 | ~8.2 |
| 255 | 261–265 | **~126** | **~8.0** |

\* GamepadLA's convention: True Bitness = log2(2 × steps-from-center). Steps ≤ 50 were sampling-limited in our run (1 kHz can't resolve increments smaller than the per-sample motion) — consistent with the same rule, not separately proven.

![Measured movement between stick reports vs. set step size](what-step-size-does/step_increment_curve.svg)

The jump sizes are extremely tight per step (spread ~6–13% of the median) while the *durations* between reports vary widely with sweep speed — the signature of an increment threshold, not of a rate limiter.

## So is "255 = 8-bit" right?

**In practice, yes — with one refinement.** At step 255 any single stick motion moves in ~257-count chunks: ~126 distinct positions from center to edge, which is 8-bit-equivalent granularity in the metric the community already uses. The refinement: it's a *relative* grid, anchored to the last reported value, not a fixed 256-point lattice. Successive passes over the range land at offset positions.

## Where the first analysis went wrong

Our first pass pooled all distinct values across a whole sweep and checked the minimum gap between them. On a relative grid those metrics mislead: each pass re-anchors, so the union of passes fills the range in (we saw ~5,600 pooled distinct values at step 255, with adjacent-value gaps of 1) — and we wrongly concluded "no quantization." Pooled counts and min-gaps measure the union of many offset grids; **the movement between consecutive reports is the right metric for a relative quantizer,** and it reads ≈ the step setting. Both analyses are published below — run them side by side.

## Independent cross-check

GamepadLA's published ZD Ultimate Legend test (independent rig, wired XInput, stock settings) measured Step Resolution 0.00226 — that's **74 raw counts per step, i.e. the factory-default step size (73) showing up on an independent tool**, matching our step-73 measurement (76–82). Their 443 steps-from-center / 9.8-bit figures line up with the same rule. Our table predicts what their StickAnalyzer tool should report at any other step value — e.g. ~0.008 Step Resolution / ~126 SFC / ~8.0-bit at step 255 — so anyone can verify this independently with community-standard tooling.

## What it means for feel and settings

- **Step 1** = finest possible output — but it also passes the sensor's own noise straight through, which is why very low steps can feel alive/twitchy.
- **Default 73** = the vendor's noise-vs-precision compromise (~74-count granularity, ~9.8-bit).
- **Higher steps** = progressively chunkier motion; **255 ≈ 8-bit-like coarseness**. Because a report requires crossing the increment, higher steps also *reduce the effective update rate* during motion (fewer, bigger steps per second) — consistent with owner reports that step affects report rate.
- Practical rule: run the lowest step that's stable for your stick; raise it only enough to calm jitter.

Unrelated but often conflated: "I set 255 and it didn't stick" in the official app is a separate write-reliability quirk (the firmware silently drops some setting writes; LegendCTL verifies every write and retries until the controller confirms).

## Honest scope

Manual sweep (not a motorized fixture); XInput/game-facing layer (not the internal sensor); steps ≤ 50 under-resolved at 1 kHz sampling; a secondary population of ±1–2-count micro-adjustments near rest exists (source not fully characterized) — none of which changes the headline. Raw data and every script are published; verify it yourself.

## Verify it yourself

Nothing here is meant to be taken on faith. Published alongside this note in [`what-step-size-does/`](what-step-size-does/):

- **`step_capture.py`** — the raw XInput logger (standalone; only calls `XInputGetState`, no device writes).
- **`reanalyze_2026-07-01.py`** — the corrected analysis (plateau segmentation, per-step pitch, jump histograms) and its output **`reanalysis_2026-07-01.json`**.
- **`discriminate_model_2026-07-01.py`** — the increment-vs-rate-limiter discrimination and its output **`discrimination_2026-07-01.json`**.
- **`analyze.py`** + **`analysis_summary.md`** — the *original, superseded* analysis and its per-run results, kept so you can reproduce exactly how the misleading metrics looked and why.
- **`step_increment_curve.svg`** — the chart above (measured pitch vs. set step, y = x reference).

The raw per-sample CSVs (~29 MB, ~504k samples) are available on request. Data captured 2026-07-01 with **LegendCTL** — an independent, local, no-telemetry configurator for the ZD Ultimate Legend.

---

*Revision history: v1 (2026-07-01) concluded "density change, not a grid" from pooled distinct-value counts; corrected the same day after an adversarial re-analysis of the same capture (this page).*
