---
title: "ZD Ultimate Legend Deadzone & Circularity Guide"
description: "Tune ZD Ultimate Legend deadzones and view circularity with LegendCTL's standalone Windows tester and USB-HID settings workflow. No official ZD app required."
---

# ZD Ultimate Legend deadzone & circularity tuning guide

A practical guide to reducing unwanted stick input and reading the live circularity view on the ZD Ultimate Legend,
using [LegendCTL](https://github.com/EvilHumphrey/LegendCTL) — a free, open-source, fully-local Windows app that
configures the controller's settings over USB-HID. LegendCTL is **standalone** (no official ZD app required) and is
**not** a firmware updater.

**Quick answer:** You can *tune deadzones* (and sensitivity, step size, axis inversion) as onboard settings, and you
can *view* live stick output and per-stick **circularity** in a read-only tester. Tuning deadzones changes settings;
the circularity view only **shows** what the stick is doing — it doesn't alter the stick's physical/firmware behavior.

## Before changing settings: baseline and restore point

1. Plug the controller in over USB and open LegendCTL.
2. Open the live **Live Verify** view and watch both sticks at rest and through their full range — note how they look
   *before* you change anything.
3. LegendCTL captures a **Restore Point** before risky operations, and you can save a **profile** of your current
   settings — tick **Include device settings (polling rate, step-size)** in the save dialog if you want those captured
   too. Establish that known-good baseline first so you can always roll back.

## What deadzones change on the ZD Ultimate Legend

A deadzone tells the controller to ignore part of the stick's travel:

- **Center (inner) deadzone** — an inactive zone near the resting position. Raise it to suppress small unwanted
  movement near center; raise it too far and small intentional movements get ignored.
- **Outer (peripheral) deadzone** — a saturated zone near maximum travel, so the stick reaches "full" before it
  physically bottoms out.

LegendCTL writes these as supported USB-HID settings and reports each write's outcome, refreshing the on-screen state
from the device; inline-deadzone writes additionally read back and compare the value, so it's honest about what took.

## How to read the live stick and circularity view

The **Live Verify** view plots each stick's position live and shows a per-stick **circularity** reading — how close
the stick's outer travel traces a true circle. A perfectly round trace is uncommon on *any* controller, and a stock
reading that isn't a perfect circle is usually a normal characteristic of the stick design, not a fault. Read it as a
*measurement to compare against*, not a pass/fail grade:

- Watch the trace as you roll the stick around its full range.
- Use it to compare **before vs after** a change you make (a cap, a module reseat, a deadzone tweak).

## Using the read-only tester with other XInput controllers

The live stick/circularity view reads from XInput, so the **viewing** tool is largely controller-agnostic — you can
point it at most XInput pads to see their sticks and circularity. The *settings* above (deadzone, sensitivity,
polling, etc.) are ZD-Ultimate-Legend-specific because they ride that controller's HID protocol; on a non-ZD pad the
tester stays read-only and won't attempt any write.

## Avoid chasing phantom input with too much deadzone

If you see small unwanted movement ("phantom input") at rest, it's tempting to crank the center deadzone up. A little
can help, but a large center deadzone shrinks your usable precision near center and can mask — rather than address — a
hardware/calibration issue. If phantom input or drift persists after a modest deadzone, treat it as a calibration or
hardware question (see the [stick-swap calibration guide](zd-ultimate-legend-stick-swap-calibration.md)) rather than
piling on more deadzone.

## Step size, sensitivity curves, and axis inversion are separate settings

Deadzones are only one part of stick feel. LegendCTL also configures, as distinct settings:

- **Sensitivity curves** (3-point, plus 8-point on v1.24+ firmware) — how stick travel maps to output.
- **Step size** — the granularity of the response.
- **Axis inversion.**

Change one thing at a time and re-check the live view so you know what each adjustment did.

## Troubleshooting off-center input, drift, and uneven circularity

- **Off-center at rest** → check the center deadzone, and confirm the stick is properly calibrated (calibration is
  separate from settings — see below).
- **Drift that grows or persists** → likely hardware/calibration, which is outside what settings can fix.
- **Uneven circularity** → this reflects the stick's physical/firmware behavior; the view helps you *see and compare*
  it, but LegendCTL doesn't change circularity directly.

## Limits

LegendCTL is **not** a firmware flasher, has **no macros/turbo/automation**, installs **no drivers or virtual
devices**, and makes **no network calls**. It configures supported settings and shows you the result — it doesn't
repair hardware or modify firmware.

## See also

- [Calibration after a stick-module swap](zd-ultimate-legend-stick-swap-calibration.md)
- [Official ZD app vs LegendCTL: which tool to use](official-zd-app-vs-legendctl.md)
- [FAQ](FAQ.md) — "does it have macros/turbo?", "can it fix drift?"
- [SmartScreen & SHA-256 verification](legendctl-smartscreen-sha256-verification.md) — first-install trust checks
