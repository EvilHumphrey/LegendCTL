# Local release source provenance

Run `tools/setup_dev_env.ps1` first, then `tools/build_release.ps1` from a clean
committed checkout. The setup script still uses the public release lock, a
hash-verified wheelhouse and an offline install into `.venv-zd`. The builder
checks that environment's lock marker against the **exported** release lock,
runs `pip check` and `pip-audit`, and never resolves or installs packages.
Existing setup markers may hash an LF or CRLF checkout of that lock. The
builder accepts SHA-256 only for its exact raw bytes or LF/CRLF-only variants;
it does not normalize any other character, pin, or wheel hash. This preserves
existing environments without weakening the locked dependency identity.

The default artifact destination is `dist`. It must not exist. To preserve an
older build, choose a new location with `-ArtifactDestination <new-directory>`;
its parent must already exist. Destinations within the checkout must be new
top-level directories. Existing outputs are never removed or overwritten.
Network/device paths, alternate data streams, symlinks and directory junctions
on the source/output ancestor chain are rejected. Windows directory handles
deny replacement while the builder uses those paths; identity checks also run
at the export and publication boundaries.

## Admission and export

The builder rejects staged changes, missing/modified tracked files,
assume-unchanged/skip-worktree flags and non-generated untracked/ignored files.
Ordinary CRLF checkout conversion is allowed for files governed by text/EOL
attributes or `core.autocrlf`; the export always uses the raw committed bytes.
Clean tracked files are accepted even if their names match an ignore pattern.

The only untracked/ignored allowances are `.venv-zd`, `build`, `dist`,
`local-install`, `zd_data`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`,
`.release-wheelhouse`, `.release-tools`, and ordinary `.pyc` files under `__pycache__` directories.
An importable `.py`/`.pyd` outside those excluded generated roots is rejected;
`__pycache__` is inspected and may contain only ordinary bytecode/cache
directories. Whole generated roots are excluded without traversing their
contents. Their root itself may not be a link or junction. A leftover
`.release-wheelhouse-<id>` from interrupted setup is not an allowance: inspect
and relocate it before building. None of these excluded bytes enters the source
export. The existing release venv is separately trusted executable tooling;
the lock marker is not a claim that an adversarial local account cannot tamper
with installed tools. The excluded `.release-tools` compiler cache is separately
checked against the commit-bound Inno Setup file hashes before use; its bytes
are not application source and do not enter the raw export.

`zd_data` is the application's ignored runtime-data directory. Tests and render
fixtures may leave logs/crash notices there before a build. Its untracked
contents, including nested user data or importable code, are neither inspected
nor copied into the raw Git export; the builder never cleans up that data. Its
root must still be free of links and junctions, and any tracked file under that name
is still checked against its committed bytes before export. This exemption does
not allow adjacent untracked application code elsewhere in the checkout.

The selected full commit ID is exported via raw Git tree/blob reads. Blob IDs
are verified. Replace refs, inherited Git overrides, smudge/clean filters,
checkout hooks and archive attributes cannot rewrite the exported source.
Release queries use the protected Program Files Git for Windows executable,
authenticated by its approved signer and owner/write permissions before use.
PATH entries, aliases, PowerShell functions and caller-supplied alternative Git
paths cannot select a release executable. System/global Git configuration and
execution-bearing fsmonitor/hooks options are suppressed for every query.
Lazy object fetching and every transport protocol are disabled, so a missing
promisor object fails locally even if repository configuration permits a remote helper.
Symlinks, submodules, Windows device/alias paths and case-colliding tree names
are rejected. The raw export has no Git checkout metadata and the build runs
only there. Python runs in isolated mode, and inherited Python/pip configuration
is removed from the build child. The PowerShell child initializes its own
standard module path rather than inheriting another PowerShell edition's path.

## Identity, output and failures

The internal worker independently compares its complete raw file inventory and
bytes to the supplied immutable commit in the source repository before changing
version metadata or running dependency/build commands. A caller-supplied
manifest is not accepted as source authority. Direct internal invocation must
supply that repository, and its Git executable is independently authenticated;
a no-Git directory and a claimed commit string alone are insufficient. Added
files, directories and hardlinked source files are rejected before the build.
After the build only the explicit `build` and `dist` output roots may have been
added. Python packaging and manifest commands disable bytecode writes so they
do not create extra source inputs.

The full commit ID is stamped into the exported `zd_app/version.py`, used by the
trust manifest, and recorded in `SOURCE_PROVENANCE.json`. Before copying output,
the orchestrator rechecks every exported file, allowing only the version file's
build-commit/date metadata changes. It verifies the packaged trust label and
`SHA256SUMS.txt`, rejects linked artifacts, copies into the new destination with
exclusive file creation, and verifies copied hashes. The final provenance
receipt contains the full source/build ID, raw source SHA-256 map, stamped
version-file hash and artifact hashes. It is written last; only then does the
builder report `Build complete`. This local receipt is not a signed attestation.

On failure, the command exits nonzero and retains the isolated source/build
directory printed at startup, including compiler diagnostics. A failure during
copying may leave partial destination files without a valid completion receipt.
Neither failure nor success recursively deletes temporary source or old output.
Review and remove retained task-owned directories explicitly when no longer
needed. Concurrent hostile writes by another process with the same account's
rights are outside this local source-provenance boundary; it is not an OS sandbox.

The builder no longer refreshes the local install or desktop shortcuts.
`tools/install_local.ps1` remains an explicit, separate local-install action for
the standard `dist` layout. Its native mirror pins source/destination directories
against replacement, opens files without following reparses, rejects hardlinks,
and creates each destination file exclusively. Existing leaves are removed by
their opened handles and never overwritten through an alias. It removes stale
files and then empty directories through those handles, then checks the final
file inventory. A failure can leave a partial local install; rerunning repairs it,
and shortcut updates occur only after a successful mirror. A custom artifact
destination is for review/export;
do not assume the installer helper consumes it. A compiler reporting success
without its expected installer is now a build failure. If Inno Setup is absent,
the existing ZIP-only warning/behavior remains.

The hosted release workflow calls this same builder from its checked-out ref;
its test log lives under `RUNNER_TEMP` so it cannot dirty admitted source.
Its later checksum regeneration and attestation/publication steps are separate.
The local completion receipt describes files at builder completion; the hosted
workflow retains that unsigned receipt in its distribution workflow artifact,
then uses its separate regenerated checksum file for attesting the two binaries.
The receipt's checksum-file hash describes the original builder output, not the
later regenerated checksum-file bytes. The receipt is not a release asset or an
attestation subject. This builder does not push, publish, tag, dispatch workflows, or prove hardware
behavior. Qualification still requires the project's release gates and an
actual compiler/packager rehearsal on the reviewed candidate.

The hosted build also runs an actual installer lifecycle gate on its disposable
Windows Server runner. It checks the built installer, upgrade from pinned 2.6.2
bytes, uninstall, payload identity, registration, permissions and preservation
of test data. Its separate evidence artifact does not replace Windows client
UAC/SmartScreen checks or physical-controller qualification.
