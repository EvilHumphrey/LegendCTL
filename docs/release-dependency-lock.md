# Release dependency lock

`requirements.txt` and `requirements-build.txt` remain the small, human-maintained
inputs. The release artifact is built from `requirements-release.lock` instead:
it is a transitive-complete, exact-version SHA-256 lock for the runtime,
PyInstaller, and the pinned `pip-audit` tool that audits the build venv.

The release workflow downloads every wheel into a per-run wheelhouse using
`--require-hashes --only-binary=:all: --no-deps`, then creates `.venv-zd` by
installing only from that wheelhouse with `--no-index`. `pip check`, the complete
test suite, `python -m pip_audit`, and PyInstaller all run from that same venv.
There is no second online package resolution after the wheelhouse download.

Local release builds retain the supported Python 3.12 workflow:

```powershell
.\tools\setup_dev_env.ps1
.\tools\build_release.ps1
```

`setup_dev_env.ps1` makes and removes its own wheelhouse. It is the required
bootstrap path; `build_release.ps1` deliberately does not run `pip install`.
After refreshing the lock, rerun setup with `-Recreate` to intentionally replace
the existing build venv. Hosted releases make a new venv for every run.
Hosted releases remain on the exact Python 3.13.14 pin in
`.github/workflows/release-build.yml`.

## Refreshing after a reviewed dependency change

Edit a human input (`requirements.txt`, `requirements-build.txt`, or the
pinned `pip-audit` root in `requirements-release.in`), then run:

```powershell
.\tools\refresh_release_lock.ps1
git diff --check
git diff -- requirements-release.in requirements-release.lock
```

The refresh command bootstraps its pinned `pip-tools` version from the separate
hash lock `requirements-lock-tools.lock`, ignores ambient pip configuration,
and forwards `--only-binary=:all:` to its resolver. It therefore resolves only
the declared roots as wheels before downloading the selected release wheels
again with hash verification. Review the resulting lock diff and run the normal
test gate before committing.

This patch does not change the independently tracked Inno Setup / Chocolatey
installation path. That remains an explicit residual release-supply-chain risk
outside the Python wheel closure.
