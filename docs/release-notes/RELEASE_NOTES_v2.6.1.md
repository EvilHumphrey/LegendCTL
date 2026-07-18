# LegendCTL v2.6.1

A point release focused on **honesty wording** and **robustness** on top of v2.6.0 — the Trust
Matrix now states exactly what a profile Apply does and doesn't read back, plus a wave of
under-the-hood hardening.

## What's changed

- **The Trust Matrix "Applied changes" row is now labeled as policy, not evidence.** It previously
  showed a green "Verified by read-back" chip no matter what — even before a controller was
  connected. It now carries a muted **"Verification policy"** chip and states the real, narrow
  scope: every write reports its outcome; the Restore, Safe Import and settled inline-deadzone
  flows run a final read-back comparison, and a profile Apply attempts read-back checks for step
  size and lighting zones **only** — successful writes to its other fields, including write-only
  ones like back-paddle bindings, are reported as sent, not verified. A write Windows reports as
  successful is not proof the controller stored the value.

- **Scoped wording on the other trust surfaces.** The first-connect trust card, the Diagnostics
  trust panel, and the About screen's disclaimer now name the specific read-back paths — Restore,
  Safe Import, inline deadzone, and the attempted step-size/lighting checks — instead of implying
  every write is verified.

- **Simplified Chinese: localized support guides.** The Firmware and Windows Stack support guides
  now render in Chinese for zh-CN users, matching the rest of the interface.

## Robustness

- **HID read cancellation, hardened.** Cancelling or recovering an in-flight read of the controller
  is now bounded and safe — a cancelled or stranded read can never be mistaken for a completed one,
  and a poisoned read handle is retired and reopened rather than reused.

- **Build self-verification.** Release builds now carry a recorded manifest of the exact source
  files scanned at build time, and the in-app Trust Self-Check compares the shipped non-EXE payload
  files against it instead of reporting a clean result over nothing. The running EXE can't verify
  itself from inside — for that, the app points at the external `gh attestation verify` check.

## Same discipline

No new wrapper-written controller settings, no network calls, nothing uploaded. Full suite green on
Python 3.13 / DearPyGui in the CI build that produced these assets, and the device-facing paths were
smoke-tested on real hardware before this cut. The release assets carry GitHub build provenance you
can verify with `gh attestation verify`.

LegendCTL is a standalone, unofficial configurator for the ZD Ultimate Legend — no official ZD app
required, local, no telemetry, no drivers.
