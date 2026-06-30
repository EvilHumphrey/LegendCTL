# ZD Ultimate Legend Wrapper — technical architecture

Current as of v2.1.0 (2026-06-29). This is the repo's technical
architecture document.

## What the app is

A local Windows configuration and lifecycle tool for the ZD Ultimate Legend controller.
It reads and writes the controller's onboard settings over standard USB HID feature
reports, and layers a controller-health/lifecycle surface on top (restore points, health
reports, wear ledger, module passports). It is deliberately NOT a remapper, virtual
device, overlay, or background service.

## Constraint architecture (enforced, not aspirational)

Ten product constraints define the design space: HID-only writes (MI_02 feature
reports), no drivers, no virtual devices, no input injection, no game-process hooking,
no background service, no automation (no macros/turbo/scripting), no network calls, ​
honest write reporting (a normal Apply reports each field's write outcome and refreshes
on-screen state by re-reading; Restore / Safe Import / inline-deadzone writes additionally
read back and compare per attempted field; back-paddle bindings are write-only), local-only data
(`%APPDATA%\ZDUltimateLegend\`, plain JSON/JSONL).

Enforcement lives in the test suite, not in promises:

- **Forbidden-phrase gates** — per-surface tests fail the build if user-facing copy
  overclaims (e.g. "calibrated", "ban-safe"); claim-boundary paragraphs live once
  per subsystem in `boundary.py` modules and are the only whitelisted denials.
- **Field-registry drift gate** (`tests/test_field_registry_drift.py`) — the
  `ControllerSnapshot` dataclass is the source of truth; key-set parity and category
  values are pinned across every registry that mirrors it.
- **i18n parity gate** — `set(en) == set(zh-CN)` over entire locale files; no empty
  values.
- **Honest write reporting** — a normal Apply records each field's write outcome (the
  write ACK) and then refreshes on-screen state by re-reading the device; it does not
  compare every applied field for a "verified" result. Apply-then-compare read-back
  verification (apply, re-read, compare per attempted field) is reserved for Restore,
  Safe Import, and inline-deadzone writes. Write-only surfaces (back-paddle bindings) are
  reported as sent, not verified.

## Package map (`zd_app/`)

- `protocol/` — stable HID protocol layer: interface enumeration (`hid_transport`),
  preflight visibility checks, trigger-interface coordination. Production code, shipped in dist.
- `services/` — business logic, zero UI imports (enforced by structure + tests):
  - `settings_service.py` — raw HID transport + per-field read/write codecs on the
    `1055aaXX` feature-report family; single `WriteOutcome` enum; batch reads carry a
    deadline budget and per-field provenance.
  - `settings_apply_coordinator.py` — apply pipeline with per-field trailer writes
    (the firmware silently rejects some fields inside multi-field bursts; trailers +
    settles + retry-once mitigate the documented quirk family). A normal Apply reports
    each field's write outcome; apply-then-compare read-back verification lives in the
    restore/Safe-Import/inline-deadzone paths, not here.
  - `restore_point_service.py` + `storage/restore_point_store.py` — full-state
    capture/restore with provenance-honest reads (`_do_fresh_read` returns snapshot +
    read_success + read_errors), per-entry restore verification, retention pruning,
    atomic temp-then-replace persistence.
  - `snapshot_diff.py` — pure differ behind the Device-vs-Profile screen; three-way
    (device / selected profile / last-applied) with drift detection, 8-point sensitivity
    rider folding, and unreadable-vs-absent honesty driven by provenance maps.
  - `health_report/`, `wear_ledger/`, `module_passport/`, `diagnostic_bundle/` — the
    lifecycle layer: guided measurement workflows, append-only maintenance log,
    per-side stick-module fingerprints, and operator-triggered, path-sanitized export
    bundles.
  - `device_service.py`, `xinput_poll_service.py`, `preflight_service.py` — presence
    polling, live XInput diagnostics, transport preflight.
- `storage/` — JSON/JSONL stores: wrapper profiles, app settings, restore points,
  last-applied record (`last_applied_store.py`), snapshot codec. All writes are atomic
  temp-then-replace; corrupt files degrade to disclosure cards or logged no-ops, never
  crashes.
- `ui/` — DearPyGui screens + the `AppShell` coordinator (`app_shell.py`). One screen
  module per sidebar entry under `ui/screens/`. Two load-bearing seams live here:
  - **Threaded HID-job seam** — long device flows (profile apply, restore, full read,
    Safe Import apply+verify, retry bursts) run as jobs on a worker executor;
    `_run_hid_job` owns a busy flag, completions drain at the top of `_tick` on the
    render thread, and every device-touching UI entry point refuses (with status) while
    a job is in flight. Sync mode (no executor) preserves byte-for-byte legacy behavior
    for tests and headless paths.
  - **Deferred-UI / modal-swap seam** — DearPyGui never renders a modal created in the
    same pass another modal was showing (empirically benched, thread-independent; see
    `tools/diag_dpg_modal_thread_visibility.py`). Modal chains therefore route through
    `_defer_modal_swap` (teardown pass → rendered frame → create pass, with coalescing
    keys); two modals never show stacked — lower modals hide and re-show instead.
- `i18n/` — locale loader + `en.json` / `zh-CN.json` (full-file parity gate).

Entry point: `main_zd.py` (wires real stores/services/executor; `AppShell` accepts
injected fakes for tests). Version constants: `zd_app/version.py`.

## Data directories

- `%APPDATA%\ZDUltimateLegend\` — settings, wrapper profiles, restore points, wear
  ledger, module passports, diagnostic bundles, logs (rotating), crash reports.
- Source runs use a local `zd_data/` directory instead (gitignored).

## Test architecture

~2,700 unittest tests (v2.1.0). Services are tested headlessly; screens are tested
against a patched DearPyGui that records widget calls (no real rendering — which is why
real-DPG behaviors like the modal law are additionally pinned by the manual bench tool
in `tools/`). Suite conventions: system Python 3.12 with `dearpygui` installed; exit
code 139 on teardown is a known DPG segfault artifact, not a failure. Drift/parity/
forbidden-phrase gates run as ordinary tests so they fail the build on violation.

## Build & distribution

`tools\build_release.ps1` → PyInstaller portable folder + ZIP + (when Inno Setup 6 is
present) an installer EXE, plus `SHA256SUMS.txt`; `tools\install_local.ps1` mirrors the
freshest build to `local-install\` and refreshes Desktop/Start-Menu shortcuts. No code
signing; distribution is manual with published hashes. No auto-update, no telemetry —
matching the constraint architecture.

## External tooling

The shipped app imports nothing outside `zd_app/`, `main_zd.py`, and the build tools — a
boundary enforced by tests.
