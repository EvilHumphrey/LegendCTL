# Security Policy

This is an **unofficial** local Windows tool for the ZD Ultimate Legend
controller. It is not developed or endorsed by ZD Gaming.

## Security posture

The app is designed to do as little as possible to the rest of your system. The
no-network and no-input-injection constraints below are guarded by an
import-boundary test (`tests/test_import_boundary.py`): it fails the build if any
shipped module imports a network or injection library, or anything outside the
app's own packages plus DearPyGui (no-telemetry and no-auto-update follow from
having no network access). See also
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), "Constraint architecture." These are
not aspirational promises:

- **Local-only.** All data (settings, wrapper profiles, restore points, wear
  ledger, logs) lives under `%APPDATA%\ZDUltimateLegend\` as plain JSON/JSONL.
- **No network calls.** The app never connects to the internet — no accounts,
  no remote config, no analytics endpoints. You can confirm this yourself with
  TCPView — see [docs/verifying-no-network.md](docs/verifying-no-network.md). The
  one deliberate exception is the About screen's GitHub links, which hand a URL
  to your **default browser** (a separate process) on click; the app process
  itself still opens no socket and has no network stack or telemetry.
- **No telemetry.** Nothing about you, your machine, or your controller is
  collected or transmitted.
- **No auto-update.** The app never downloads or runs code after install. You
  update it by manually replacing the build.
- **No drivers, no virtual devices, no background service.** The app installs
  no kernel driver or filter, creates no virtual gamepad, and runs no service
  or scheduled task. It only runs while you have its window open.
- **No input injection, no game-process hooking, no automation** (no
  macros/turbo/scripting).
- **HID-only, verified writes.** Controller settings are written as standard
  USB/HID feature reports to the controller's onboard memory and verified by
  read-back wherever the controller exposes the value; the write-only back-paddle
  bindings are reported as sent, not verified.

## Distribution & signing

The released executable is currently **unsigned** (no Authenticode code-signing
certificate). Distribution is manual: builds are published with their
`SHA256SUMS.txt` so you can verify integrity before running. Code-signing via
the SignPath Foundation OSS program is planned — see
[docs/code-signing-policy.md](docs/code-signing-policy.md). See the README's
"Distribution safety" section for what to expect from SmartScreen and antivirus,
and how to verify the hash.

## How to report a vulnerability

If you find a security issue, please report it privately rather than opening a
public issue, so it can be addressed before disclosure.

- **Report privately:** Please report security issues privately through GitHub's
  private vulnerability reporting — open a report at
  <https://github.com/EvilHumphrey/LegendCTL/security/advisories/new>. Please do not
  file public issues for security problems.

Please include the app version (About → Version), your Windows version, and
steps to reproduce. This is a hobby project maintained best-effort; there is no
formal SLA, but reports are taken seriously and you'll get an acknowledgement.

## Scope

In scope: the wrapper application code under `zd_app/`, the build/install
tooling under `tools/`, and the distributed executable. Out of scope: the
official ZD Gaming app, the controller firmware itself, and any non-shipped
development prototypes (not shipped, not imported by the app).
