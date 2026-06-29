---
title: "Official ZD App vs LegendCTL: Which Tool to Use"
description: "Compare the official ZD app/PC software with standalone LegendCTL for ZD Ultimate Legend settings, trust checks, and safe boundaries. No official ZD app required."
---

# Official ZD app vs LegendCTL: what each tool is for

Both exist, and they do different jobs. This page explains the difference plainly so you can pick the right tool
without confusion.

**TL;DR:** The official ZD app/PC software is the vendor's tool for ZD's own settings and officially-supported
workflows. **Firmware updates** are done with ZD's **separate** official firmware-update utility — not the config app,
and not LegendCTL. **LegendCTL** is an independent, open-source, fully-local Windows app that configures the ZD
Ultimate Legend's **settings** over USB-HID. LegendCTL is **standalone — you do not need the official ZD app installed
to use it** — and it is *not* a firmware updater. Installing LegendCTL doesn't require, remove, or replace ZD's official
tools — they're independent. Keep ZD's official tools for firmware and supported workflows; use LegendCTL separately for
fast, local, no-telemetry settings.

> LegendCTL is an unofficial, third-party project — not developed by, affiliated with, or endorsed by ZD Gaming.

## What ZD's official tools are for

- **Firmware updates** — ZD ships a **separate** official firmware-update utility (a dedicated tool, distinct from the
  ZD config app) to update or recover the controller's firmware. LegendCTL cannot do this (by design), and neither does
  the config app.
- **Anything ZD officially supports** — the vendor's ZD app/PC software is the source of truth for ZD's own feature
  set, official documentation, and warranty-relevant operations.

If your goal involves firmware or a soft-bricked controller, use ZD's official firmware-update utility. For other
officially-supported workflows, use the official ZD app.

## What LegendCTL is for

LegendCTL configures the **settings** the controller stores onboard, over standard USB-HID:

- Stick **deadzones**, **sensitivity curves**, **axis inversion**, and **step size**
- **Polling rate** (including 8000 Hz on supported firmware)
- **Button mapping** and **back-paddle** bindings
- **Trigger** range and modes, **vibration**, and **lighting**
- **Profiles** you can save and re-apply, with **Restore Points** before risky changes
- A read-only **live stick / circularity tester** (this viewing tool works on any XInput controller; the *settings*
  above are ZD-Ultimate-Legend-specific)

It writes those settings as HID feature reports and **reports each field's write outcome**, then refreshes the
on-screen state by re-reading the device. Restore Points, Safe Import, and inline-deadzone writes additionally **read
back and compare** the written value; back-paddle bindings are reported as *sent, not verified*. It is **not** a
firmware flasher and cannot change firmware.

## Which should I use?

| If you want to… | Use |
|---|---|
| Update or recover **firmware** | ZD's official firmware-update utility (separate tool) |
| An officially-supported / warranty-relevant operation | Official ZD app |
| Configure **deadzones, sensitivity, polling, buttons, triggers, vibration, lighting, profiles** on Windows | **LegendCTL** |
| A **fully-local, open-source, no-telemetry** way to manage settings | **LegendCTL** |
| **View** live stick output / circularity (any XInput pad) | **LegendCTL** (read-only) |
| Macros / turbo / rapid-fire / virtual-device remapping | Neither — LegendCTL deliberately omits these |

LegendCTL doesn't require the official app. Keep ZD's official tools for firmware/reset workflows; use LegendCTL
separately for the supported settings above.

## The trust difference (and how to verify it yourself)

LegendCTL is built so you don't have to take its privacy claims on faith — you can check them:

- **Open source (MIT).** The full source is on [GitHub](https://github.com/EvilHumphrey/LegendCTL); you can read it
  or [build it yourself](../README.md#build-from-source). The "deliberately absent" constraints below are enforced by
  the test suite, not just promised.
- **Zero network calls — verifiable.** The LegendCTL process makes no network connections: no telemetry, no analytics,
  no auto-update. You can confirm it in a couple of minutes with the built-in check in
  [verifying-no-network.md](verifying-no-network.md).
- **No drivers, no virtual devices, no background service, no input injection, no macros.** It only runs while its
  window is open, and it only configures your controller — it never plays it for you.
- **Honest write reporting.** A normal Apply reports each field's write outcome and refreshes from the device;
  Restore Points, Safe Import, and inline-deadzone writes additionally read back and compare the value, and
  back-paddle bindings are reported as sent, not verified.

This is a statement about what LegendCTL *is*, not a claim about any other software — closed-source tools simply can't
be independently audited the same way, which is exactly why LegendCTL keeps everything open and test-enforced.

## What LegendCTL deliberately does *not* do

- **No firmware flashing/updating** — use ZD's official firmware-update utility for that.
- **No macros, turbo, rapid-fire, or automation**, and **no input injection** — intentionally absent and test-enforced.
- **No drivers / virtual gamepads / background service.**
- **No network calls or telemetry.**

## Will it work on my controller?

LegendCTL was developed and bench-tested on a single ZD Ultimate Legend (known-working firmware **v1.18**, incl. 8K
polling, and **v1.24**, incl. 8-point sensitivity curves). The controller ships in several variants with different
stick modules/firmware; others are **best-effort** and the app is built to say so honestly rather than fake a write.
See the [FAQ](FAQ.md) and, if something's off on your unit, please file a
[compatibility report](https://github.com/EvilHumphrey/LegendCTL/issues/new/choose).

## See also

- [Deadzone & circularity tuning guide](zd-ultimate-legend-deadzone-circularity.md) — reduce unwanted input; read the live circularity view
- [Calibration after a stick-module swap](zd-ultimate-legend-stick-swap-calibration.md) — what to test and adjust after hardware changes
- [SmartScreen, unsigned builds & verifying the SHA-256](install-windows-and-smartscreen.md) — install-trust checks
- [README](../README.md) — overview, download, Quick Start · [FAQ](FAQ.md) · [Verifying no network access](verifying-no-network.md)

---

*LegendCTL is free and open-source (MIT). The official ZD app/PC software exists for ZD's supported settings/workflows,
and firmware updates use ZD's separate official firmware utility; LegendCTL is the standalone, local, open-source way to
manage supported settings.*
