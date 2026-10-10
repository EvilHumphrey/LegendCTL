# Disposable installer lifecycle qualification

`tools/qualify_installer_lifecycle.ps1` exercises real Setup payloads on a new
GitHub-hosted Windows runner. It must never run on an operator workstation or
an existing VM. A local native path-policy fixture, a mocked unit suite, and a
successful build are not installer lifecycle qualification.

## Admission and inputs

The caller must supply `-AllowDisposableRunner`; the script also requires the
GitHub-hosted Windows environment, an already elevated 64-bit process, and no
existing LegendCTL installation, uninstall registration in either registry
view/hive, relevant shortcuts, per-user data, or app process. It never requests
elevation or changes security policy. Environment checks are an accidental-use
guard, not a security boundary against someone deliberately impersonating CI.

Pass the exact candidate Setup and expected SHA-256, the matching real build
payload directory, the independently verified published v2.6.2 Setup and hash,
expected candidate version/full source commit, and a new output directory below
`RUNNER_TEMP`. The evidence directory's parent must exist. All input/output
ancestor paths must be ordinary local paths. Candidate and previous installers
must be distinct bytes. Parent release orchestration owns downloading and
authenticating the old release and building/authenticating the candidate; this
script performs no download or publication.

```powershell
$evidence = Join-Path $env:RUNNER_TEMP 'legendctl-lifecycle'
& tools/qualify_installer_lifecycle.ps1 -AllowDisposableRunner `
    -CandidateSetup $candidateSetup -CandidateSha256 $candidateHash `
    -CandidatePayloadDirectory $candidatePayload `
    -PreviousSetup $verifiedPreviousSetup -PreviousSha256 $previousHash `
    -ExpectedVersion $candidateVersion -ExpectedBuildCommit $fullCommit `
    -EvidenceDirectory $evidence
```

Use a freshly reviewed build-only workflow. Upload the entire evidence folder
with `if: always()`, `include-hidden-files: true`, and `if-no-files-found: error`;
also retain the job log for admission failures before evidence creation. Do not
add an allowed-failure condition to the lifecycle invocation. Set a bounded job
timeout; each installer process has its own bounded wait (default 180 seconds).
An installation timeout fails the job without killing an unknown process tree;
the disposable runner's job teardown owns eventual process disposal. Do not
reuse the failed runner. A fresh job replaces VM rollback; no persistent VM or
snapshot is modified by this harness.

After the disposable-runner/output-path checks, the script creates its new
evidence directory before later preflight validation. A bad artifact hash or
non-clean runner therefore produces a `BLOCKED` preflight summary without
installer execution. Case failures retain both snapshots and a `FAIL` summary.
Refusal before that initial admission intentionally cannot write an artifact;
the job log records it. Completed case evidence includes a SHA-256 manifest.

## What runs

Before execution, every installer hash is checked and its image held against
replacement. Uninstall is resolved only to the created canonical installation,
hash-recorded, and verified immediately before process creation. Its file lock
is released after process launch so the uninstaller can delete itself.

1. Rejection controls: writable `/DIR`, hostile `/LOADINF`, synthetic HKLM64
   previous custom registration, and app-root junction. Each requires the
   production policy's exact expected log reason, Preparing-to-Install exit 7,
   and unchanged before/after filesystem/registry receipts.
2. Actual candidate clean install, then actual uninstall.
3. Actual published v2.6.2 install, candidate replacement/upgrade, then actual
   uninstall. When the candidate version is still 2.6.2 this is same-version
   replacement evidence, not proof of a higher-version upgrade. Final release
   qualification must repeat with the final versioned artifact.

Silent invocation suppresses normal installer UI, prevents restart and
application closing/restarting, and keeps the desktop shortcut task at its
default unchecked state. Unexpected reboot-required status (3010), missing
logs, security-policy launch failures, or unexpected exit codes fail. The
script does not weaken Application Control, bypass SmartScreen, or force-close
apps. The current production `[Run]` entry is skipped by silent Setup. A WMI
process-start subscription plus final process inventory checks that the app did
not launch; failure to register the subscription aborts before installation.

The installed payload must hash-match every file in the candidate reference
directory. Additional files other than expected Inno uninstaller metadata fail,
which detects obsolete dependency files left by an upgrade. The installed
trust record must carry the expected version/full build commit, and the PE
product version must agree. One canonical HKLM64 AppId registration and both
Start Menu shortcut targets are asserted; the default desktop shortcut must
remain absent. Uninstall must remove payload, registration, and owned shortcuts.

New task-owned per-user settings/profile sentinels must remain byte/ACL-identical
through installation, upgrade and uninstall. All discovered user-profile data
locations are checked for unexpected creations. These are preservation
sentinels, not loadable runtime profile fixtures. No app is launched and no
settings migration is qualified. Failure state and evidence are preserved;
there is no recursive filesystem cleanup. The only fixture removal is an exact
previously absent registry key and an identity-checked task-created junction.

## ACL and platform limits

Every installed directory is tested using an impersonated restricted token:
the Administrators SID is disabled, maximum privileges disabled (Windows keeps
SeChangeNotifyPrivilege), and membership checked
to be non-admin. An actual writable positive control must succeed before each
protected-directory file-creation attempt must fail with access denied. This
provides effective I/O evidence rather than merely matching ACL text. Owner,
SDDL, file hashes, reparse targets and empty directories are captured separately.

This token is derived from the CI account. It does not prove distinct-user
ownership behavior, interactive UAC credential switching, or every delete/
rename/replace permission. The harness does not test weakened existing ACLs,
descendant junctions, every registry-value conflict, in-use updates, reboot,
desktop-task selection, data migration, or rollback after partial installation.
Those remain separate cases in the release plan, not implicit passes.

On `windows-2025` the result is Windows Server 2025 lifecycle evidence. It does
not qualify Windows 10/11 or ARM64, interactive UAC/SmartScreen/postinstall
launch, normal-user UI, controller behavior, or operator acceptance. Hardware
smoke and final public release approval still apply to the exact final CI bytes.

## Local checks that do not install

`python -m unittest tests.test_installer_lifecycle_harness` checks refusal on a
non-CI host, ownership boundaries, snapshots, hash-before-launch, bounded hidden
launch arguments, timeout evidence, and rejection's reason/preservation checks.
All launcher calls in these tests are mocked; files are synthetic temporary
fixtures. The native ACL declarations are compiled but the impersonation/I/O
probe is not executed locally. These tests must never be reported as a real
installer PASS. The final release lane owns full-suite and hosted execution.
