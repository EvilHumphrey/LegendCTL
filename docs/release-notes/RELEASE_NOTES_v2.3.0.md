# LegendCTL v2.3.0

This release is about one idea: **don't take our word for it — verify it yourself.** LegendCTL
already claims to be local, driver-free, and honest about what it can and can't see; v2.3.0 turns
those claims into evidence you can check inside the app and share in a couple of clicks.

- **Trust Self-Check.** A new Diagnostics panel that *demonstrates* the trust posture instead of
  just asserting it: it scans the shipped app for any networking code, confirms it installs no
  driver, virtual device, or background service, and shows where its local data lives — each row
  scoped honestly to "what this build does, this session," not a system-wide audit. One click
  copies the whole self-check as a shareable artifact.
- **Compatibility Report.** An opt-in "Create Compatibility Report" flow that turns a local run
  into a share-safe, copy-pasteable packet — your controller variant, firmware, app version, and
  what you actually tested — aligned to the GitHub compatibility-report template, with a claim
  boundary that's clear it's self-reported evidence (not vendor certification, not a
  tournament/anti-cheat ruling). Paired with a new public **compatibility matrix** so reports
  accumulate into an honest, maintained ledger.
- **Shareable evidence card.** Export a single self-contained page (HTML or Markdown) summarizing
  the trust posture, your device/config summary, the diagnostic-bundle privacy posture, and the
  claim boundary — screenshotable and pasteable, fully offline, with nothing uploaded.
- **Clearer live controller model.** The Live Verify model's edge-lighting and labels were tidied
  for a cleaner read; behavior is unchanged — it still lights XInput *output* only, and says so.

Everything new here is opt-in, local, and display/export-only — no new wrapper-written controller
settings, no network calls, nothing uploaded. The diagnostic outputs scrub local paths and never
invent a claim the app can't back. Same release discipline: full suite green on Python 3.12 /
DearPyGui 2.3.1.

LegendCTL is a standalone, unofficial configurator for the ZD Ultimate Legend — no official ZD
app required, local, no telemetry, no drivers, and honest about what it can and can't verify.
