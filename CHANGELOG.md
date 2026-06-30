# Changelog

## v2.3.0 — 2026-07-01

Verify-it-yourself release: the app now demonstrates its local / no-network / no-driver claims,
turns a run into a share-safe compatibility report, and exports a one-page evidence card. All
opt-in, local, display/export-only — no new wrapper-written settings, no network calls, nothing
uploaded; same test discipline (full suite green on Python 3.12 / DearPyGui 2.3.1).

- **Trust Self-Check.** A Diagnostics panel that backs the trust posture with evidence — a static
  no-networking-imports scan; confirmation of no driver / virtual device / background service; the
  local-data location — claim-bounded per build/session, with one-click "Copy self-check."
- **Compatibility Report.** Opt-in "Create Compatibility Report" → a share-safe, copy-pasteable
  packet aligned to the compatibility-report issue template, plus a maintained public
  `docs/compatibility-matrix.md`; carries a self-reported-evidence claim boundary (not vendor
  certification / tournament / anti-cheat).
- **Shareable evidence card.** A single self-contained HTML/Markdown page (trust posture +
  device/config + diagnostic-bundle privacy posture + claim boundary); screenshotable, offline,
  nothing uploaded.
- **Live model polish.** Tidied edge-lighting and labels on the Live Verify controller model;
  behavior unchanged (lights XInput output only).

## v2.2.0 — 2026-06-30

Visibility-and-sharing release: the live controller model is redrawn with Front/Back/Top views
and a click-to-inspect panel, and diagnostics bundles can now be previewed before sharing. No
new settings are written by the wrapper (the new surfaces are read-only displays plus a local
export preview); same test discipline (full suite green on Python 3.12 / DearPyGui 2.3.1).

- **Front / Back / Top controller views.** The Live Verify model is redrawn closer to the real
  ZD Ultimate Legend with smooth contours and three switchable views; live lights track XInput
  output, while source-only labels (paddles, claws) are marked as not-live.
- **Click the model to inspect a control.** Selecting a control on the model or in the list
  opens an inspector (identity, live output, cached binding) with an "Edit binding" link to the
  Buttons tab; clicking a back paddle selects the paddle, not the button it outputs.
- **On-device binding guide.** Step-by-step instructions for assigning/clearing paddles and
  switching onboard profiles on the controller, with a clear note that LegendCTL can set a
  paddle but can't read paddle bindings back from the device.
- **Preview a diagnostics bundle before sharing.** Export opens a preview of exactly what the
  archive contains and the privacy posture of each part; the scrubbed file is written locally
  and nothing is uploaded.
- **Safer shareable reports.** Diagnostic text is scrubbed of local paths and written so
  special characters can't reformat the report when pasted elsewhere; the open-folder action
  stays within a safe local target.

## v2.1.0 — 2026-06-28

Visibility-and-honesty release: the app now shows the controller's real button bindings and
lights up controls live, while saying plainly where it can't read the device. No new settings
are written by the wrapper (the new surfaces are read-only displays); same test discipline
(full suite green on Python 3.12 / DearPyGui 2.3.1).

- **See your real button bindings.** The Buttons tab reads and displays the controller's
  current per-button mapping and refreshes in place after an in-app remap.
- **Honest paddle + profile display.** Unreadable back paddles show "Not set in LegendCTL"
  (never a false "Unbound") with a note on what can and can't be read; on-device profile slots
  are labeled "Profile 1–4" since their names aren't readable over USB.
- **Back-paddle map.** A code-drawn diagram shows where each paddle (M1, M2, LM, RM, LK, RK)
  sits, lighting the selected paddle's spot; drawn from the official manual layout and labeled
  as a selection guide, not a device read.
- **Live controller visualizer.** Live Verify shows a code-drawn controller that lights up as
  you press buttons/triggers and tracks the sticks live, with an honest note that it reflects
  XInput output, not which physical control was pressed.
- **Clearer "Profile: Not verified" status.** A tooltip explains it's an informational
  active-slot indicator (settings unaffected) and how to confirm it.
- **Readable Device vs Profile.** Human labels ("8000 Hz", "Not inverted", "Linear") replace
  internal values, and the comparison defaults to showing only what changed.
- **Tidier Restore Points.** Routine per-session auto-captures collapse behind a count with a
  one-click "Show all"; nothing is deleted and every restore point stays restorable.
- **Honesty/reliability fixes.** The display never invents a button name for a mapping kind
  LegendCTL doesn't model.

## v2.0.3 — 2026-06-27

LegendCTL rebrand plus a multi-slot live tester and a lighting-apply reliability fix. No new
settings are written by the wrapper; same test discipline (full suite green on Python 3.12 /
DearPyGui 2.3.1).

- **New LegendCTL look.** The app accent is now the LegendCTL blue, the About screen shows a
  radar mark drawn from the live deadzone visual, and the in-app display name reads
  "LegendCTL" across the window title, status bar, About panel, and first-run dialog.
- **Live Verify finds the pad on any XInput slot.** The tester scans player slots 0–3,
  auto-selects the first connected pad and sticks to it, re-scans on disconnect, and adds an
  Auto / Player 1–4 override with a live "Active: Player N" readout — so the ZD pad is found
  even when it enumerates as player 2–4 on a multi-pad bench.
- **Per-zone lighting applies reliably.** The apply path writes each lighting zone, reads it
  back, and retries on a confirmed mismatch, fixing a silent-reject that could drop the
  right-zone color on a profile apply.
- **Packaging: winget manifest added** under `packaging/winget/` for Windows Package Manager
  submission.

## v2.0.2 — 2026-06-26

Read-only Live Verify widening. The live tester now works with any connected XInput
controller for sticks, buttons, triggers, and circularity, while all HID settings writes
remain hard-gated to the verified ZD Ultimate Legend allowlist. No new settings are
written by the wrapper; same test discipline (full suite green on Python 3.12 /
DearPyGui 2.3.1).

- **Live Verify now works on any XInput controller.** Non-ZD pads can use the read-only
  stick, circularity, button, and trigger tester without claiming settings support.
- **HID writes remain ZD-only.** The device capability map exposes write support only
  for the allowlisted ZD Ultimate Legend, and controller settings, profile apply,
  restore, and firmware-deadzone write paths refuse honestly on non-ZD devices.
- **The UI labels unverified controllers plainly.** Controller settings and the
  Live Verify deadzone card show read-only messaging instead of implying writable
  support on generic XInput hardware.

## v2.0.1 — 2026-06-24

Post-release bug-fix update. Four fixes found while smoke-testing v2.0.0 on hardware.
No change to the set of settings the wrapper writes; same test discipline (full suite
green on Python 3.12 / DearPyGui 2.3).

- **Profile delete now works reliably.** The delete-confirmation popup was built outside
  the modal-swap seam, so its Confirm button could be dead after a prior Save+Apply (a
  consequence of DearPyGui's modal-rendering law) and the profile was never deleted. The
  popup now routes through the seam, covered by a live-DearPyGui regression test.
- **Joystick step-size writes are verified.** Applying a profile that changes the
  step-size now writes, settles, reads back, and retries on a confirmed mismatch, and
  reports a real failure instead of silently leaving the device at its floor value.
- **The "Apply device settings?" confirmation now shows what it will write.** It lists
  the actual current → new device values (step size, polling rate), so applying a profile
  can no longer silently overwrite a step-size you just set; after a step-size change, a
  dismissible "Save to profile" prompt lets you persist it in one click.
- **Fixed a crash when dragging the Live Verify step-size slider.** The verified-write
  read-back could raise on real hardware (HID read timeout) and propagate to a crash; the
  verify path is now exception-safe and the live slider uses a plain write (verification
  stays on the deliberate Apply path).

## v2.0.0 — 2026-06-12

First feature-complete release of the wrapper. ~2,370 tests passing on Python 3.12 /
DearPyGui 2.3. Built as a PyInstaller portable folder + ZIP, with an optional Inno Setup
installer and published SHA-256 checksums.

### Controller settings

- Full settings surface on the controller's HID feature-report family, verified by
  read-back wherever the controller exposes the value (the write-only back-paddle
  bindings are reported as sent, not verified): USB polling rate (250–8000 Hz), 16×16 button bindings,
  deadzones, 3-anchor sensitivity curves plus 8-point curves (firmware v1.24+), axis
  inversion, joystick step-size, trigger range/mode/vibration, per-zone lighting,
  per-motor vibration, back-paddle bindings.
- Firmware write-quirk mitigations characterized on hardware and baked into the apply
  coordinator: per-field trailer writes, post-burst settles, deferred `step_size`,
  retry-once on first-read-after-burst timeouts.
- Wrapper profiles: save / apply / delete full controller state, with device-global
  fields (polling rate, step-size) confirmed separately before apply.

### Lifecycle & trust layer

- Restore Points: automatic capture before risky operations + manual capture; restore
  with per-entry read-back verification; retention pruning; per-row delete.
- Device vs Profile: read-only three-way diff (live device / selected profile /
  last-applied) with provenance-honest unreadable handling, 8-point encoding fold, and
  per-field drift highlighting backed by a persisted last-applied record.
- Health Report (guided measurement workflow, exportable Markdown/JSON) and 20-second
  Readiness Check sharing the same measurement primitives.
- Wear Ledger (append-only lifecycle log), Module Passport (per-side stick-module
  fingerprints + advisory trend analysis), Diagnostic Bundle (path-sanitized export).
- Trust card at first connect; full English + Simplified Chinese parity.

### Architecture & robustness

- Threaded HID-job seam: profile apply, restore, full reads, and import applies run off
  the render thread; every device-touching control refuses honestly while a job is in
  flight; UI stays live during multi-second device work.
- Deferred-UI / modal-swap seam encoding DearPyGui's empirically-benched modal law
  (a modal created in the same pass another modal was showing never renders; the seam
  paces teardown and create across rendered frames). Manual bench tool in `tools/`.
- Late-connect wiring fix (profile apply / restore points now work when the controller
  connects after launch), restore-point retention pruning wired on every capture,
  batch-read deadline budget, category-registry drift gates, atomic profile saves,
  honest "verified by read-back where the controller exposes a read path, sent for
  write-only fields, never by ACKs" semantics.

### Known residuals (tracked, none blocking normal use)

- Safe Import (profile sharing) is dev-gated and parked pending a maintainer decision.
- Device-vs-Profile last-applied column: logic is covered by the test suite; on-hardware
  verification is pending.
- Minor deferred polish: a few unused i18n keys, some event-log language mixing, and
  small UI tuning items.

### Historical

LegendCTL grew out of an earlier controller-input latency-analysis tool; that code is a
separate project and is not part of this repository (see "Historical: lineage" in the README).
Development tooling used to build the wrapper is kept separately and is not shipped here.
