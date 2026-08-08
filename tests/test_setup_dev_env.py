"""Static checks for tools/setup_dev_env.ps1.

The script bootstraps the .venv-zd Python venv that tools/build_release.ps1
depends on. We don't actually execute it from the suite — that would touch
the user's real Python install and venv path. Instead we assert the script
exists, contains the expected key invocations, and that build_release.ps1's
error message points at it.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SETUP_PATH = _REPO_ROOT / "tools" / "setup_dev_env.ps1"
_BUILD_PATH = _REPO_ROOT / "tools" / "build_release.ps1"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class SetupScriptPresenceTests(unittest.TestCase):
    def test_setup_script_exists(self) -> None:
        self.assertTrue(_SETUP_PATH.is_file(), f"missing {_SETUP_PATH}")


class SetupScriptContentTests(unittest.TestCase):
    """Assert the setup script contains the canonical bootstrap commands.

    The exact invocations are what make the script useful — if a future edit
    drops one of them, the script silently no longer bootstraps a build-ready
    venv. These checks pin the contract.
    """

    def test_invokes_py312_to_create_venv(self) -> None:
        body = _read(_SETUP_PATH)
        # The script resolves Python 3.12 into $py312 then calls it with
        # `-m venv` against the .venv-zd directory. Match the invocation
        # without depending on the exact variable name for $venvDir.
        self.assertRegex(
            body,
            re.compile(r"&\s*\$py312\s+-m\s+venv\b"),
            "setup script must invoke py312 -m venv to create .venv-zd",
        )

    def test_targets_venv_zd_directory(self) -> None:
        body = _read(_SETUP_PATH)
        self.assertIn(
            ".venv-zd",
            body,
            "setup script must target the .venv-zd directory (matches build_release.ps1)",
        )

    def test_installs_hash_locked_release_closure_offline(self) -> None:
        body = _read(_SETUP_PATH)
        # The setup path must first download the reviewed wheel closure with
        # hash enforcement, then install only from that wheelhouse. This is
        # stronger than a live `pip install -r requirements-build.txt`.
        self.assertIn("pip download --disable-pip-version-check --only-binary=:all: --no-deps", body)
        self.assertIn("--require-hashes --dest $wheelhouse -r $releaseLock", body)
        self.assertIn("--no-index --find-links $wheelhouse", body)
        self.assertIn("requirements-release.lock", body)

    def test_resolves_python_312_at_expected_path(self) -> None:
        """The setup script encodes the canonical Python 3.12 install path.
        If the operator
        has Python elsewhere, the script must fail loudly with a clear hint,
        not silently fall through to a system python that may be the wrong
        version."""
        body = _read(_SETUP_PATH)
        self.assertIn(
            r"$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
            body,
            "setup script must reference the canonical per-user Python 3.12 install path",
        )

    def test_short_circuits_only_when_venv_matches_the_release_lock(self) -> None:
        """Idempotent re-runs preserve a venv only when its lock marker matches."""
        body = _read(_SETUP_PATH)
        # A bare Scripts\python.exe is not enough: it could have been populated
        # by an unreviewed live resolver.
        self.assertRegex(
            body,
            re.compile(r"Test-Path\s+\$venvPython"),
            "setup script must inspect .venv-zd\\Scripts\\python.exe",
        )
        self.assertIn(".legendctl-release-lock.sha256", body)
        self.assertIn("$recordedHash -eq $lockHash", body)


class BuildReleaseErrorPointsAtSetupScript(unittest.TestCase):
    """build_release.ps1's missing-venv error message must point at the
    setup script — otherwise the user is back to guessing at incantations."""

    def test_error_message_references_setup_dev_env(self) -> None:
        body = _read(_BUILD_PATH)
        self.assertIn(
            "setup_dev_env.ps1",
            body,
            "build_release.ps1's venv-missing Write-Error must reference setup_dev_env.ps1",
        )


if __name__ == "__main__":
    unittest.main()
