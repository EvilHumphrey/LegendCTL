# LegendCTL v2.4.0

The same honest tool, with a layout that keeps the important state in view. v2.4.0 is about
**context**: on a wide window the app now shows a live context rail — device, connection,
firmware, active profile, pending changes, and the trust posture — beside a focused work column,
so you never lose track of what the app knows (and what it doesn't) while you work.

- **Adaptive context rail.** Maximize the window (or widen it past ~1600px) and Home, Controller,
  Diagnostics, and Live Verify gain a right-hand rail with your device model, connection state,
  firmware, active profile, pending changes, and the standing trust line — plus one-click Read,
  Health Check, and Trust Self-Check. Narrow windows keep the familiar single-column layout; the
  rail only appears where it genuinely fits.
- **Home, reorganized around orientation.** Home now leads with what LegendCTL is and what to do
  first: an orientation card (read first, verify live, then decide what to write), a compact
  device & profile status card, the trust front door, and a state-aware next step — connected and
  no-controller states each get honest, useful guidance.
- **Trust, one click from anywhere.** The verify-it-yourself surfaces from v2.3.0 (Trust
  Self-Check, Compatibility Report, evidence card) are now front-door items: linked from Home,
  from the Diagnostics status tab, and from the context rail. A round of wording refinements also
  tightened several trust texts so each claim states exactly its scope — nothing broader, nothing
  vaguer.
- **Wider Live Verify workspace.** On wide windows the live controller model gets a larger canvas
  and a proportionally scaled diagram — same behavior, same honest labels (it lights XInput
  *output* only, and says so).
- **Sticks first.** Controller settings now open on the Sticks tab — the settings people
  actually come for — with the rest of the order unchanged in spirit: Buttons, Triggers,
  Vibration, Lighting, Motion, Profiles.
- **Launches as a normal window.** The app no longer starts maximized; it opens as a regular
  centered window at its reference size (and sizes down to fit smaller displays). Maximize it
  whenever you want the rail layout.

Everything here is display/layout-only — no new wrapper-written controller settings, no network
calls, nothing uploaded, and the write paths are byte-for-byte the ones already shipped. Same
release discipline: full suite green on Python 3.12 / DearPyGui 2.3.1, including a real-window
layout budget check that now runs process-isolated for deterministic results.

LegendCTL is a standalone, unofficial configurator for the ZD Ultimate Legend — no official ZD
app required, local, no telemetry, no drivers, and honest about what it can and can't verify.
