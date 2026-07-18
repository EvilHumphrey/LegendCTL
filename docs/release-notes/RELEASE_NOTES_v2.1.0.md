# LegendCTL v2.1.0

This release is about making your controller's real state visible — and honest about what
LegendCTL can and can't see.

The Buttons tab now shows your controller's actual current button bindings, read live from the
device, so you can see what's really remapped instead of a generic "unassigned". Where
LegendCTL genuinely can't read a value, it says so plainly rather than guessing.

- **See your real button bindings.** The Buttons tab reads and displays the controller's
  current per-button mapping, and refreshes in place after you apply a remap in the app.
- **Honest paddle + profile display.** Back paddles that LegendCTL can't read show
  **"Not set in LegendCTL"** — never a misleading "Unbound" — alongside a plain note on what
  it can and can't see. On-device profile slots are labeled **Profile 1–4** (their names
  aren't readable over USB).
- **Back-paddle map.** A new code-drawn diagram shows where each paddle (M1, M2, LM, RM, LK,
  RK) physically sits; selecting a paddle's row lights its spot. It's drawn from the official
  manual layout and labeled as a guide to your selection, not a device read.
- **Live controller visualizer.** Live Verify now shows a code-drawn controller that lights up
  as you press buttons and triggers and tracks your sticks live — with an honest note that it
  reflects XInput *output*, not which physical control you pressed (a remapped paddle and its
  mapped button look the same to XInput).
- **Clearer "Profile: Not verified" status.** A tooltip explains it's an informational
  active-slot indicator — your settings are unaffected — and how to confirm the slot.
- **Readable Device vs Profile.** Settings now show human labels ("8000 Hz", "Not inverted",
  "Linear") instead of internal values, and the comparison defaults to showing only what
  changed.
- **Tidier Restore Points.** Routine per-session auto-captures collapse behind a count with a
  one-click "Show all", so the meaningful safety captures stand out. Nothing is deleted —
  every restore point stays restorable.

This update adds no new wrapper-written settings — the new surfaces are read-only displays of
what's already on the device, and the bindings list never invents a button name for a mapping
kind LegendCTL doesn't model. Same release discipline: full suite green on Python 3.12 /
DearPyGui 2.3.1.

LegendCTL is a standalone, unofficial configurator for the ZD Ultimate Legend — no official ZD
app required, local, no telemetry, no drivers, and honest about what it can and can't verify.
