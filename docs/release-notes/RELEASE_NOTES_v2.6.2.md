# LegendCTL v2.6.2

Verification and localization release: profile Apply now checks its writes by read-back the way
Restore always has, and the app speaks Korean.

## Apply now checks its writes by read-back

- **Applying a profile now reads back every readable value it writes** and compares it against
  what was sent — the same post-apply verification Restore, Safe Import, and the inline deadzone
  flow already performed. Previously a profile Apply read-back-checked step size and lighting
  zones only; every other field's write was reported without a read-back check.
- **A per-field verification view.** After any profile Apply, "Verification details" shows each
  field's write result and read-back outcome — matched, mismatched (with expected vs. observed),
  or could not verify. Write-only fields (like back-paddle bindings) can't be read back and are
  reported as sent, not verified.
- **The result line tells you which of three states you're in.** All readable changes confirmed;
  some couldn't be verified (with the count); or a read-back mismatch — a mismatch is reported as
  fact, not softened. If the verification pass itself fails, the Apply now reports every written
  field as "could not verify" instead of showing an unqualified success — a write the app couldn't
  check is never presented as a confirmed one.
- **Counting honesty, checked adversarially.** A field whose write failed is disclosed as a write
  failure — it is never counted among "written but couldn't be confirmed", and an Apply with any
  failed write never claims "all changes confirmed", even when a read-back happens to match a
  pre-existing value. (Three adversarial review rounds each caught and closed a distinct gap
  here before release.)
- **The trust surfaces state this scope consistently** — the first-connect trust card, the
  About disclaimer, the Diagnostics trust panel, and the Trust Matrix "Applied changes" row all
  describe the same read-back behavior, in every shipped language. The "Applied changes" row still carries the muted
  "Verification policy" chip — it states the app's policy and is not per-apply evidence.

## 한국어 지원 — Korean UI

- **The full interface is now available in Korean** (Settings → Language → 한국어): all 1,931
  interface strings, including every trust and verification surface. A native Korean reviewer
  confirmed the bounded core-terminology glossary (including the 프로필 form) — not a review of
  all 1,931 strings — and the trust-critical strings are byte-pinned by tests the same
  way the Simplified Chinese ones are, so their wording cannot drift silently.
- The Korean UI font path explicitly registers the Hangul glyph ranges, and a real-render
  regression test guards sampled strings against missing-glyph (tofu) rendering.

## Fixes

- **Startup crash with a VMware virtual machine active.** Installed (windowed) builds could crash
  at startup while showing the VMware USB-passthrough notice, because the warning path wrote to a
  console stream that doesn't exist in windowed builds. Fixed with regression tests.
- **Diagnostics tabs no longer snap back to Status.** Clicking a Diagnostics tab silently failed
  to record the selection (the UI library reports the selection differently in real use than the
  test double did), so the next refresh returned you to the Status tab — this is also why the
  first-run notice's "How to verify this" link landed on the wrong tab. Fixed app-wide, with a
  real-rendering regression test.

## Same discipline

No new wrapper-written controller settings — the read-back verification only reads. No network
calls, nothing uploaded. Before publication, the asset-producing CI build passed the full
suite; the release build passed a real-hardware smoke including a Korean-locale pass and the
exact `한국어` language-picker label; and the release assets passed `gh attestation verify`.
The published assets carry GitHub build provenance you can verify with `gh attestation verify`.

LegendCTL is a standalone, unofficial configurator for the ZD Ultimate Legend — no official ZD app
required, local, no telemetry, no drivers, and honest about what it can and can't verify.
