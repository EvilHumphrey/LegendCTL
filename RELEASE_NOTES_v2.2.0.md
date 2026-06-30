# LegendCTL v2.2.0

This release makes the live controller view easier to understand — without pretending to know
more than XInput actually exposes — and lets you inspect a diagnostics bundle before you ever
share it.

The Live Verify model is redrawn and now offers three views — Front, Back, and Top — with a
side inspector you can drive by clicking a control on the model. As before, the live lights
reflect XInput *output* only: a remapped paddle and the button it's mapped to look identical,
and the UI says so rather than guessing.

- **Front / Back / Top controller views.** The live model is redrawn closer to the real ZD
  Ultimate Legend, with smooth contours and three views you can switch between. The Top view
  shows the bumpers, triggers, and claws; the Back view shows the paddles. Live lights track
  XInput output, while source-only labels (paddles and claws) are marked as not-live, so
  nothing is implied that the device can't actually report.
- **Click the model to inspect a control.** Selecting a control — on the model or in the list
  — opens an inspector with its identity, live output, and cached binding, plus an "Edit
  binding" link that jumps to the Buttons tab. Clicking a back paddle selects the paddle, not
  the button it happens to output.
- **On-device binding guide.** A step-by-step guide explains how to assign or clear paddles
  and switch onboard profiles directly on the controller. It's clear about the boundary:
  LegendCTL can set a paddle from the Buttons tab, but it can't read paddle bindings back from
  the device — so press the paddle here to confirm the output.
- **Preview a diagnostics bundle before sharing.** Exporting a diagnostics bundle now opens a
  preview that lists exactly what the archive contains and the privacy posture of each part,
  so you can inspect the scrubbed, local file before deciding to share it. Nothing is uploaded
  — the bundle is written to a folder you choose.
- **Safer shareable reports.** Diagnostic text is scrubbed of local paths and written so
  special characters can't reformat the report when it's pasted elsewhere, and the open-folder
  action stays within a safe local target.

This update adds no new wrapper-written settings — the new surfaces are read-only displays plus
a local export preview. Same release discipline: full suite green on Python 3.12 /
DearPyGui 2.3.1.

LegendCTL is a standalone, unofficial configurator for the ZD Ultimate Legend — no official ZD
app required, local, no telemetry, no drivers, and honest about what it can and can't verify.
