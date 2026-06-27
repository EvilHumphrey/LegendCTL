# LegendCTL — FAQ

Short answers to the common questions, with links to the deeper docs. If your
question isn't here, check [Discussions](https://github.com/EvilHumphrey/LegendCTL/discussions)
or [SUPPORT.md](../SUPPORT.md).

## Trust & privacy

### Is it safe? Will it brick my controller?
LegendCTL only writes the controller's *settings* over standard USB-HID — the
same kinds of values the official app changes. It is **not** a firmware flasher,
so it can't overwrite firmware. Before risky operations it captures a **Restore
Point**, wrapper events are recorded to a local append-only ledger, and there
is always a clear way back (see [Recovery in the README](../README.md#recovery--if-your-controller-feels-off)).
That said, it writes to hardware and is provided "as is" — see
[Disclaimer & risk](../README.md#disclaimer--risk). Nothing can promise zero risk
when touching hardware, but the safety net is real and the writes are honest about
what did and didn't take.

### Does it phone home? Any telemetry or tracking?
No. The LegendCTL process makes **zero** network calls — no telemetry, no
analytics, no auto-update, no "usage data." The only outbound action anywhere in
the app is the About screen's GitHub links, which hand a URL to your default
browser; the app itself opens no socket. You don't have to take that on faith —
[verify it yourself in a couple of minutes](verifying-no-network.md).

### Is it open source? Can I read/build it myself?
Yes — MIT licensed, the full source is on GitHub, and the README has a
[Build from source](../README.md#build-from-source) section. The "deliberately
absent" constraints (no drivers, no network, no macros, …) are enforced by the
test suite, not just promised.

### Was this built with AI?
Yes — LegendCTL was built with substantial AI assistance, under human direction
and review throughout, and every change was human-reviewed and hardware-tested
before shipping. We say so plainly in the [Acknowledgments](../README.md#acknowledgments) —
honesty is the whole point of the project. The code is fully open and
test-enforced precisely so you don't have to trust the process; you can read it.

### Can I trust an unsigned app from a new, pseudonymous repo?
Fair question — you shouldn't trust any of them blindly. Here's what you *can* do
instead of trusting: read the source (it's all here), build it yourself, run the
[no-network check](verifying-no-network.md), and verify the published **SHA-256**
of your download against `SHA256SUMS.txt` in each release. Code signing (via the
free [SignPath Foundation](https://signpath.org) program for open-source projects)
is planned once the project is eligible — see the
[code-signing policy](code-signing-policy.md).

## Installing & running

### Why does Windows show "Windows protected your PC" (SmartScreen)?
Because the build is currently **unsigned**. That prompt is what Windows shows for
*any* new unsigned app until it builds download reputation — it is not, by itself,
a sign of malware. Verify the SHA-256 (above), then choose **More info → Run
anyway**. Full detail under [Distribution safety](../README.md#distribution-safety).

### My antivirus flagged it — is it a virus?
Almost certainly a false positive. LegendCTL is packaged with PyInstaller, whose
self-extracting bundle pattern is a well-known source of heuristic AV false
positives. You can cross-check the file on [VirusTotal](https://www.virustotal.com/)
against many engines. If you want zero packaged binaries, build from source.

### Do I need the official ZD app installed?
No. LegendCTL runs **standalone** — it talks directly to the controller over
USB-HID, so the official ZD app is not required. Keep the official app for firmware
updates if you like; LegendCTL works independently of it and doesn't depend on it.

### Installer or portable ZIP — which should I use?
The **portable ZIP** is the simplest: no admin, no UAC, runs from any folder, and
you uninstall by deleting the folder. Use the installer if you want a Start-Menu
entry and an entry in Windows' Apps list. Both ship the identical wrapper.

## Compatibility

### Will it work on my controller?
If you have a **ZD Ultimate Legend**, the core settings very likely work. It was
developed and bench-tested on a single unit (known-working firmware **v1.18**,
incl. 8K polling, and **v1.24**, incl. 8-point sensitivity curves and the latest "0609"
optimized-latency build). The controller
ships in six variants with different stick modules/firmware; other variants and
firmware are **best-effort** — the HID protocol may differ. The app is built to
say so honestly rather than fake a successful write. If something's off on your
unit, please file a [compatibility report](https://github.com/EvilHumphrey/LegendCTL/issues/new/choose).

### Does the live stick tester work on non-ZD controllers?
The **Live Verify** stick/circularity view reads from XInput, so it is largely
controller-agnostic for *viewing*. The full settings (deadzone, sensitivity,
polling, etc.) are ZD-Ultimate-Legend-specific because they ride that controller's
HID protocol.

## What it does

### What settings can it actually change?
Polling rate, the button-binding matrix, stick deadzones / sensitivity curves /
axis inversion / step-size, trigger range & modes, lighting, vibration, and
back-paddle bindings, plus wrapper profiles and Restore Points. The full list is
in the README [Status](../README.md#status) section.

### Does it have macros, turbo, or rapid-fire?
No — and that's deliberate. Macros / turbo / automation / input injection / a
background service are *intentionally absent* (and enforced by tests). LegendCTL
configures your controller; it doesn't play it for you.

### Does it update or change my controller's firmware?
No. It reads and writes *settings* via HID. It is not a firmware updater and
cannot flash firmware.

## About the project

### Is this affiliated with ZD Gaming?
No. It's an independent, unofficial third-party project — not developed by,
affiliated with, or endorsed by ZD Gaming. ZD asked the project to carry a short
disclaimer, which it does verbatim. Trademarks belong to their owners.

### How do I report a bug or ask a question?
See [SUPPORT.md](../SUPPORT.md). Reproducible bugs → a **Bug report** Issue;
questions and "is this expected?" → **Discussions**; hardware that wasn't on the
bench → a **Compatibility report**. There's no private DM/email support channel —
keeping it public means the next person finds the answer.
