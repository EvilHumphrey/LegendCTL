# LegendCTL v2.5.0

A feature release built around knowing your hardware and finding your feet: LegendCTL now
collects a read-only model fingerprint of the connected controller (local-only, serial-free,
shared only if you paste it yourself), and the Home screen gains a "First steps" guide that
walks a new install from first read to first restore point — and gets out of the way once
you're set up.

- **Model fingerprint (read-only).** The Trust Self-Check now shows a fingerprint of the
  connected device: VID/PID, HID usage page, report shape, interface inventory, and a SHA-256
  digest — the serial number is excluded by construction, and nothing is uploaded or written.
  The manual Compatibility Report includes the same fingerprint, so community reports can build
  a model corpus that makes future device support decisions evidence-based instead of guesswork.
  This is collection and honest display only: no gating, no behavior change, and the write
  paths are byte-for-byte the ones already shipped.
- **Home "First steps" guide.** A fresh install now starts with a short guided card on Home:
  read your controller's settings, capture a restore point, know where the health check lives.
  The steps tick off live as you complete them — no navigating away and back — and once
  everything is done the card collapses to a single "First steps complete" row. Dismiss it and
  it stays dismissed across restarts.
- **Sharing surfaces hardened.** The share-safe exports (Compatibility Report, Trust
  Self-Check) now escape angle brackets and backslashes too, so a crafted USB descriptor string
  can't smuggle formatting or HTML into a pasted GitHub issue.
- **Honesty around device swaps.** The First steps card is scoped to the controller that earned
  it: plug in a different controller and the steps reflect that device, not its predecessor.
  The card also comes back correctly after a disconnect/reconnect on Home.
- **Chinese localization pass.** A native-speaker review unified 35 drifted translations,
  fully translated the new fingerprint labels, and polished the First-steps wording; the legacy
  buttons screen's Back/Home labels are now properly localized. Regression-guarded so future
  drift gets caught.
- Assorted hardening from an eight-lane pre-release review: partial fingerprint reads are
  retried rather than cached for the session, Win32 calls declare correct return types, and the
  test suite grew ordering-aware coverage for state that arrives after a screen builds.

No new wrapper-written controller settings, no network calls, nothing uploaded. Same release
discipline: full suite green on Python 3.12 / DearPyGui, and the fingerprint and First-steps
paths were smoke-tested on real hardware before this cut. This is also the first LegendCTL
release built and attested by CI — the release assets carry GitHub build provenance you can
verify with `gh attestation verify`.

LegendCTL is a standalone, unofficial configurator for the ZD Ultimate Legend — no official ZD
app required, local, no telemetry, no drivers, and honest about what it can and can't verify.
