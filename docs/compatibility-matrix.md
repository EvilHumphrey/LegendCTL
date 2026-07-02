# Compatibility Matrix

LegendCTL compatibility is evidence-based. This matrix records observed runs; it
does not turn community reports into vendor certification, tournament/anti-cheat
approval, or firmware-correctness proof.

## Status Taxonomy

- **Maintainer-tested**: tested directly by the maintainer on local hardware.
- **Community-reported (with bundle)**: reported by a user with a Diagnostic Bundle
  or equivalent share-safe evidence attached.
- **Community-reported (no bundle)**: reported by a user without an attached bundle.
- **Known issue - not verified**: plausible report or known limitation that still
  needs a confirming reproduction.

## Reports

| Controller / variant | Firmware | Windows | LegendCTL | Status | Evidence | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| ZD Ultimate Legend maintainer unit (VID_413D&PID_2104) | v1.18; v1.24 / build 0609 | Windows 11 | v2.x local builds | Maintainer-tested | Local bench runs, in-app diagnostics, read/write smokes | One physical unit. Other variants, stick modules, and firmware revisions remain best-effort until reported. |
| ZD Super Legend Excellence — community unit (**unsupported model**; USB identity not captured by the v2.3.0 report) | unknown (not readable) | Windows 10 | v2.3.0 (build c45c4d2) | Community-reported (no bundle) | [Owner test notes + pasted in-app Compatibility Report (r/Controller, u/Rokofur, 2026-07-02)](https://www.reddit.com/r/Controller/comments/1ul57j9/comment/ov7cv3f/) | **Read-only evidence only — writes deliberately untested.** App declined to claim support ("No supported controller detected"), then read it as a generic XInput controller. Settings reads OK across polling / sticks (deadzones, step, curves) / triggers / vibration / button bindings; one later polling_rate read failed. Live Verify + Health Check worked. Home + Left/Right lighting read; LS/RS zones not modeled; Motion panel populated only on app relaunch. |

## How To Add A Report

Use **Diagnostics -> Create Compatibility Report** in the app, then paste the
copied issue body into the GitHub **Compatibility report** form. A Diagnostic
Bundle is optional, but reports with a bundle can be classified more strongly.
Read-only and Live-Verify-only reports are still useful when they are labeled as
limited evidence.

No GitHub account? The report is designed to be safe to paste anywhere public —
a forum or Reddit comment works too. Point the maintainer at it (or just post it
where the project is being discussed) and it can be linked from this matrix as
evidence, exactly like the community report above.
