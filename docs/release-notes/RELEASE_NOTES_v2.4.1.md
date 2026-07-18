# LegendCTL v2.4.1

A fix release, and an honest self-correction: a reviewer's screenshot showed LegendCTL failing
to recognize his genuine ZD Ultimate Legend on Russian Windows — reads worked, but the app kept
him in the read-only "unrecognized controller" lane. The bug was ours, the report was exactly the
kind of evidence this project asks for, and v2.4.1 fixes it along with a batch of honesty
improvements around disconnects and partial reads.

- **Device recognition now works on every Windows language.** Recognition used to parse the text
  output of a Windows system tool and keyed on its English field labels; on non-English Windows
  those labels are localized, so a real ZD Ultimate Legend fell back to the generic read-only
  XInput lane and writes stayed gated. Recognition now queries the Windows device API (SetupDi)
  directly — locale-independent by construction — and is regression-tested against Russian,
  German, and Chinese system configurations. If your controller was stuck "unrecognized" on a
  non-English system, this release is for you.
- **Unplugging is now visible immediately, everywhere.** Disconnect used to be easy to miss: the
  header flipped while the screen you were on kept the last connected state until you navigated
  away. Now every built screen updates in place within a few seconds of the unplug, the activity
  feed logs "Controller disconnected.", and the stray "Detected …" line that could appear right
  after an unplug is gone.
- **Values that survive a disconnect say so.** After an unplug, retained firmware and profile
  values are labeled "(last read)" instead of posing as live state — and if a different controller
  is plugged in, the retained values are cleared rather than attributed to the new device.
- **Partial reads are named, not silent.** A settings read runs on a time budget; on a slow
  device the trailing fields (motion, back paddles, step size) could be skipped without a trace.
  The read status line now says how many fields were not read and suggests reading again.
- **Compatibility Report: firmware survives a disconnect.** The report's firmware field now
  falls back to the last-read value when a live read isn't available, so a report generated
  after an unplug (or from a partially-read session) still pairs the model-level identity the
  report already carries (tail-redacted, never serials) with the firmware it was seen running.
- A Home explainer text was corrected in both languages.

No new wrapper-written controller settings, no network calls, nothing uploaded — the write paths
are byte-for-byte the ones already shipped. Same release discipline: full suite green on
Python 3.12 / DearPyGui 2.3.1, and the recognition and disconnect paths were additionally
smoke-tested on real hardware (plug, read, apply, unplug, replug) before this cut.

LegendCTL is a standalone, unofficial configurator for the ZD Ultimate Legend — no official ZD
app required, local, no telemetry, no drivers, and honest about what it can and can't verify.
