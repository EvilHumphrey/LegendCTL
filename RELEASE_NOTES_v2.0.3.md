# LegendCTL v2.0.3

LegendCTL gets its own look and a more forgiving live tester. The app accent is now the
LegendCTL blue, the About screen carries a radar mark drawn from the live deadzone visual,
and the in-app display name reads "LegendCTL" across the window title, status bar, About
panel, and first-run dialog.

The Live Verify tester now finds your controller on any XInput slot. It scans player slots
0–3, auto-selects the first connected pad and sticks to it, re-scans when a pad
disconnects, and adds an Auto / Player 1–4 override with a live "Active: Player N" readout.
On a multi-pad bench — common on a reviewer's desk — the ZD pad often enumerates as player
2–4, where the previous slot-0-only tester showed nothing.

Per-zone lighting now applies reliably. The apply path writes each lighting zone, reads it
back, and retries on a confirmed mismatch, fixing a silent-reject that could drop the
right-zone color on a profile apply.

This update adds no new wrapper-written settings: the lighting change makes an existing
write verify-and-retry, and the XInput change only widens which slot the read-only tester
reads. Same release discipline: full suite green on Python 3.12 / DearPyGui 2.3.1. A winget
manifest is included under `packaging/winget/` for Windows Package Manager submission.

LegendCTL is a standalone, unofficial configurator for the ZD Ultimate Legend — no official
ZD app required.
