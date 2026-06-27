---
title: "ZD Ultimate Legend Stick Swap Calibration Guide"
description: "After a stick-module swap, test center, circularity, deadzones, and supported LegendCTL settings on Windows before changing more. No official ZD app required."
---

# ZD Ultimate Legend calibration after a stick-module swap

Swapped or reseated a TMR/Hall stick module (or changed a cap) and not sure what to check? This guide separates three
things people often blur together, and shows where [LegendCTL](https://github.com/EvilHumphrey/LegendCTL) — a free,
open-source, standalone Windows settings tool — fits.

## Start here: calibration, settings, and testing are different

- **Calibration** centers the stick and defines its full range. This is the controller's own/official operation.
  **LegendCTL is not a calibration tool and not a firmware updater** — use the official ZD app/firmware resources for
  calibration and firmware.
- **Settings** are values like deadzones, sensitivity curves, step size, and axis inversion. **This is LegendCTL's
  job**, over USB-HID, standalone.
- **Testing** is observing the live output. LegendCTL's **read-only** Live Verify view shows stick position and
  per-stick circularity so you can see the result of a calibration or a settings change.

Do them in that order: calibrate → adjust settings → verify.

## After a stick-module swap: document the before and after state

Before you change anything, capture a baseline so you can tell what the swap actually changed:

1. Open LegendCTL's **Live Verify** view and watch both sticks at rest and through full travel.
2. Save a **profile** of your current settings (tick **Include device settings** to capture polling rate and step-size) and let LegendCTL take a **Restore Point**.
3. Note the live readings (center behavior, how far each axis reaches, circularity) — you'll compare against these.

## Check center, full range, circularity, and phantom input

With the live read-only view open:

- **Center** — does the stick rest at (near) zero, or sit off-center?
- **Full range** — does each axis reach its edges cleanly?
- **Circularity** — how round is the outer trace? (Not perfectly round is normal on most sticks — use it to compare
  before/after, not as pass/fail.)
- **Phantom input** — any movement at rest you didn't make?

## Use LegendCTL for supported USB-HID settings

Once the stick is calibrated, LegendCTL can apply the supported settings around it — deadzones, sensitivity curves,
step size, axis inversion — reporting each write's outcome and refreshing from the device (inline-deadzone, Restore,
and Safe Import writes additionally read back and compare). See the
[deadzone & circularity tuning guide](zd-ultimate-legend-deadzone-circularity.md) for the details of each setting.

## When to use official ZD calibration or firmware resources

If the issue is **calibration** (off-center, range), a **firmware** update, dongle/connection recovery, or anything
warranty-relevant, use the **official ZD app/PC software** — that's the right tool for those, and LegendCTL can't do
them.

## What LegendCTL does not do

- **No firmware flashing/updating** and **no calibration** — those are separate, official operations.
- **No drivers, virtual devices, or background service**; **no macros/turbo/input injection**; **no network calls.**

## If the stick still drifts after settings changes

A modest deadzone can hide small noise, but if drift or phantom input **persists or grows** after calibration and
reasonable settings, that points to hardware or calibration rather than something a setting can fix. Avoid masking it
with an ever-larger deadzone (that just costs you precision) — revisit the module seating/calibration, or treat it as
a hardware matter.

## Safe rollback: restore points and profile hygiene

If a change makes things worse, roll back: re-apply your saved baseline **profile** (with **Include device settings** if you captured polling/step-size), or use a **Restore Point**.
Keeping a clean "known-good" profile before experimenting is the cheapest insurance there is.

## See also

- [Deadzone & circularity tuning guide](zd-ultimate-legend-deadzone-circularity.md)
- [Official ZD app vs LegendCTL: which tool to use](official-zd-app-vs-legendctl.md) — tool boundaries
- [FAQ](FAQ.md) — firmware and support boundaries
- [README](../README.md) — download and release notes
