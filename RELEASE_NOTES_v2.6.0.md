# LegendCTL v2.6.0

This release is about making the app **more honest about what it knows** — a new provenance
surface, source labels on the values it shows, and a wave of trust-hardening fixes from an
independent adversarial review.

## What's new

- **"What we know right now" provenance card (Diagnostics → Guidance).** A live matrix that shows,
  for each fact about your controller — identity, firmware, active profile, settings, fingerprint,
  applied changes — whether it was verified from the device, inferred locally, or unknown. It
  updates in place as you connect and disconnect, and it never claims a device read that didn't
  happen. A new "What we know now" link on Home and the Diagnostics status strip jumps you to it.

- **Firmware and active profile now populate — labeled by source.** When the official ZD app's
  Controller Settings window is already open, LegendCTL can read four summary labels from it via
  Windows UI Automation and show them tagged **"Official App UI"**. A value read this way can never
  render as "Verified from device" — the label is structural, not cosmetic. The right rail and Home
  now show the source alongside firmware and active profile, and keep a "(last read)" qualifier
  until a fresh read lands this connection.

- **Provenance in shared exports.** Compatibility reports, share cards, and diagnostic bundles now
  label the firmware value's source (Official App UI / Controller Protocol / Manually entered /
  Not verified) instead of presenting it as a bare device-verified fact.

- **A shorter, honest first-run notice.** The consent gate is now three factual lines plus the
  legal disclaimer, with a **"How to verify this"** link that opens the trust surfaces before you
  accept. It states plainly that settings are written when you act — via Apply, or the live controls
  that write immediately.

- **Simplified Chinese: localized settings choices.** The Controller screen's vibration, trigger,
  and lighting choices now render in Chinese for zh-CN users, matching the rest of the interface.

## Trust hardening (independent adversarial review)

An independent multi-lens review of this release's changes found and we fixed:

- **Restore-point contract made truthful.** Safe Import now creates its checkpoint before it writes
  anything and aborts honestly if it can't; other changes that touch the device disclose when no
  restore point could be created rather than proceeding silently.
- **"Last applied" shows what was actually sent.** When a controller can't confirm the 8-point
  sensitivity curve, LegendCTL applies the standard 3-point curve, says so in the result, and no
  longer records the richer curve as if it had been written.
- **Trust Self-Check fails closed.** Its "no networking / no drivers" claims now require the scan to
  have actually run over real files; if it can't, it says so instead of reporting a clean result
  over nothing.
- **Durable local storage.** Restore points, module records, and first-run data migration are now
  crash-safe: an interrupted write can never be mistaken for a completed one, and deleted data is
  never silently resurrected.

## Same discipline

No new wrapper-written controller settings, no network calls, nothing uploaded. Full suite green on
Python 3.13 / DearPyGui in the CI build that produced these assets, and the device-facing paths were
smoke-tested on real hardware before this cut. The release assets carry GitHub build provenance you
can verify with `gh attestation verify`.

LegendCTL is a standalone, unofficial configurator for the ZD Ultimate Legend — no official ZD app
required, local, no telemetry, no drivers, and honest about what it can and can't verify.
