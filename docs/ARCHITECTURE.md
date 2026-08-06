# ZD Ultimate Legend Wrapper — technical architecture

Current as of v2.6.2 (2026-07-19). This is the repo's technical
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
honest write reporting (a normal Apply reports each field's write outcome, then reads
back and compares every readable field it wrote, refreshing on-screen state; Restore /
Safe Import / inline-deadzone writes likewise read back and compare each readable field;
step-size and lighting writes additionally verify/retry inline;
back-paddle bindings are write-only), local-only data
(`%APPDATA%\ZDUltimateLegend\`, plain JSON/JSONL).

Enforcement lives in the test suite, not in promises:

- **Forbidden-phrase gates** — per-surface tests fail the build if user-facing copy
  overclaims (e.g. "calibrated", "ban-safe"); claim-boundary paragraphs live once
  per subsystem in `boundary.py` modules and are the only whitelisted denials.
- **Field-registry drift gate** (`tests/test_field_registry_drift.py`) — the
  `ControllerSnapshot` dataclass is the source of truth; key-set parity and category
  values are pinned across every registry that mirrors it.
- **i18n parity gate** — `set(en) == set(zh-CN) == set(ko)` over entire locale files; no
  empty values.
- **Honest write reporting** — every controller write reports its **write outcome**:
  whether the synchronous `WriteFile` call succeeded for the full report (not a device
  ACK — the normal write path does not wait for one). A normal Apply then reads the
  device back and compares every readable field it wrote, reporting each field as
  matched, mismatched, or could-not-verify in the Apply details — a field that cannot
  be re-read (unreadable, or skipped by the read budget) is disclosed as
  could-not-verify, never presented as verified — and refreshes on-screen state from
  that read. **Read-back verification** — re-reading a field and comparing it to
  what was sent — thus runs in profile Apply and in the Restore, Safe Import, and
  inline-deadzone flows. The flows differ in reach: Restore compares every field it attempted
  and uses read provenance to classify write-only or unreadable fields as
  could-not-verify; Safe Import runs its whole-snapshot compare only when every write
  reported success (any failed write downgrades the audit to "sent" and skips the
  compare) and classifies write-only entries as unverifiable, though a readable entry
  missing from its read-back is reported as a mismatch rather than unverifiable; the
  inline-deadzone panel confirms the settled value of each change and reports a
  confirm read it couldn't complete as sent-unverified. Step-size and lighting writes
  additionally go through read-back-and-retry setters during the Apply itself. Those setters are best-effort:
  a confirmed mismatch (a successful read returning a different value) is surfaced as
  not-committed once the retry budget is exhausted, never as a false success — and a
  setter read-back that could not be read at all collapses to the plain write outcome,
  with the post-apply sweep still reporting that field's verify state separately.
  Write-only surfaces (back-paddle bindings) have no read path and are reported as sent,
  not verified.

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
    each field's write outcome, then runs a post-apply read-back sweep comparing every
    readable field it attempted (per-field matched / mismatched / could-not-verify,
    surfaced in the Apply details); step size and lighting zones additionally go
    through verify/retry setters during the write (best-effort — an unreadable setter
    read-back collapses to the plain write outcome, with the sweep still reporting the
    field's verify state). The restore/Safe-Import/inline-deadzone paths keep their own
    apply-then-compare verification.
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
- `i18n/` — locale loader + `en.json` / `zh-CN.json` / `ko.json` (full-file parity gate).

Entry point: `main_zd.py` (wires real stores/services/executor; `AppShell` accepts
injected fakes for tests). Version constants: `zd_app/version.py`.

## Data directories

- `%APPDATA%\ZDUltimateLegend\` — settings, wrapper profiles, restore points, wear
  ledger, module passports, diagnostic bundles, logs (rotating), crash reports.
- Source runs use a local `zd_data/` directory instead (gitignored).

## Test architecture

3,172 unittest tests (v2.6.2 public maintenance gate, 2026-08-06). Services are tested headlessly; screens are tested
against a patched DearPyGui that records widget calls (no real rendering — which is why
real-DPG behaviors like the modal law are additionally pinned by the manual bench tool
in `tools/`). Suite conventions: system Python 3.12 with `dearpygui` installed (the
release CI additionally runs the full suite on Python 3.13 — the runtime the release
binaries ship with — before building); exit
code 139 (or Windows' signed/unsigned `0xC0000005` form after a complete `OK`
summary) on teardown is a known DPG artifact, not a failure. The current 36 skips
include ten exact-signature quarantines for latent font-pressure findings; a timeout,
unrelated assertion, changed signature, or unexpected success fails the matrix gate.
Drift/parity/forbidden-phrase gates run as ordinary tests so they fail the build on violation.

## Build & distribution

`tools\build_release.ps1` → PyInstaller portable folder + ZIP + (when Inno Setup 6 is
present) an installer EXE, plus `SHA256SUMS.txt`; `tools\install_local.ps1` mirrors the
freshest build to `local-install\` and refreshes Desktop/Start-Menu shortcuts. Official
release assets (v2.5.0+) are CI-built on Python 3.13 from the release tag and carry
build-provenance attestations (`gh attestation verify`); there is no AuthentiCode code
signing yet, and every release publishes hashes. No auto-update, no telemetry —
matching the constraint architecture.

## External tooling

The shipped app imports nothing outside `zd_app/`, `main_zd.py`, and the build tools — a
boundary enforced by tests.
