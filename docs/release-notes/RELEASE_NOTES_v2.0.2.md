# LegendCTL v2.0.2

The Live Verify tester now works on any connected XInput controller. Sticks, buttons,
triggers, and circularity are read-only XInput surfaces, so they are available beyond the
ZD Ultimate Legend.

HID writes stay ZD-only. Controller settings, profile apply, restore, and firmware
deadzone tuning still require the verified ZD Ultimate Legend allowlist
(`VID_413D&PID_2104`), and non-ZD controllers are labeled read-only with no settings
write attempted.

This update adds no new wrapper-written settings. It widens only the read-only tester
surface and keeps the same release discipline: full suite green on Python 3.12 /
DearPyGui 2.3.1.

LegendCTL remains a standalone unofficial configurator for the ZD Ultimate Legend.
