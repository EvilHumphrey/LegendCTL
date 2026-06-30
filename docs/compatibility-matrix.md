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

## How To Add A Report

Use **Diagnostics -> Create Compatibility Report** in the app, then paste the
copied issue body into the GitHub **Compatibility report** form. A Diagnostic
Bundle is optional, but reports with a bundle can be classified more strongly.
Read-only and Live-Verify-only reports are still useful when they are labeled as
limited evidence.
