# Local release source provenance

Run `tools/setup_dev_env.ps1` first, then `tools/build_release.ps1` from a clean
committed checkout. The setup script still uses the public release lock, a
hash-verified wheelhouse and an offline install into `.venv-zd`. The builder
checks that environment's lock marker against the **exported** release lock,
runs `pip check` and `pip-audit`, and never resolves or installs packages.

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
`local-install`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`,
`.release-wheelhouse`, and ordinary `.pyc` files under `__pycache__` directories.
An importable `.py`/`.pyd` outside those excluded generated roots is rejected;
`__pycache__` is inspected and may contain only ordinary bytecode/cache
directories. Whole generated roots are excluded without traversing their
contents. Their root itself may not be a link or junction. A leftover
`.release-wheelhouse-<id>` from interrupted setup is not an allowance: inspect
and relocate it before building. None of these excluded bytes enters the source
export. The existing release venv is separately trusted executable tooling;
the lock marker is not a claim that an adversarial local account cannot tamper
with installed tools.

The selected full commit ID is exported via raw Git tree/blob reads. Blob IDs
are verified. Replace refs, inherited Git overrides, smudge/clean filters,
checkout hooks and archive attributes cannot rewrite the exported source.
Symlinks, submodules, Windows device/alias paths and case-colliding tree names
are rejected. The raw export has no Git checkout metadata and the build runs
only there. Python runs in isolated mode, and inherited Python/pip configuration
is removed from the build child. The PowerShell child initializes its own
standard module path rather than inheriting another PowerShell edition's path.

## Identity, output and failures

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
the standard `dist` layout. A custom artifact destination is for review/export;
do not assume the installer helper consumes it. A compiler reporting success
without its expected installer is now a build failure. If Inno Setup is absent,
the existing ZIP-only warning/behavior remains.

The hosted release workflow has its own checkout and attestation path. This
local builder does not push, publish, tag, dispatch workflows, or prove hardware
behavior. Qualification still requires the project's release gates and an
actual compiler/packager rehearsal on the reviewed candidate.
