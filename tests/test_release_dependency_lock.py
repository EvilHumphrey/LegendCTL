"""Regression contract for the release Python wheel supply-chain boundary.

These assertions deliberately inspect the real workflow and local scripts. A
version-only pin is not enough: the artifact path must first hash-verify the
complete wheel set, then install and audit the same venv offline.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements-release.lock"
LOCK_INPUT = ROOT / "requirements-release.in"
LOCK_TOOLS = ROOT / "requirements-lock-tools.lock"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release-build.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SETUP = ROOT / "tools" / "setup_dev_env.ps1"
BUILD = ROOT / "tools" / "build_release.ps1"
REFRESH = ROOT / "tools" / "refresh_release_lock.ps1"


def _locked_sections(path: Path) -> dict[str, str]:
    """Return each normalized package section without relying on pip-tools."""
    sections: dict[str, str] = {}
    for section in re.split(r"(?m)(?=^[A-Za-z0-9][A-Za-z0-9_.-]*==)", path.read_text(encoding="utf-8")):
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==[^\s]+", section)
        if match:
            sections[match.group(1).lower().replace("_", "-")] = section
    return sections


class ReleaseLockClosureTests(unittest.TestCase):
    def test_release_lock_is_complete_and_hashes_the_pyinstaller_and_audit_closures(self) -> None:
        self.assertTrue(LOCK.is_file())
        text = LOCK.read_text(encoding="utf-8")
        self.assertNotIn("# WARNING:", text)
        sections = _locked_sections(LOCK)
        # These are the direct build/runtime roots and the platform/runtime
        # transitive packages PyInstaller needs on Windows, plus the audit tool.
        required = {
            "dearpygui",
            "pyinstaller",
            "altgraph",
            "packaging",
            "pefile",
            "pyinstaller-hooks-contrib",
            "pywin32-ctypes",
            "setuptools",
            "pip-audit",
            "pip-api",
            "pip-requirements-parser",
            "requests",
        }
        self.assertTrue(required <= sections.keys(), sorted(required - sections.keys()))
        for name, section in sections.items():
            with self.subTest(package=name):
                self.assertIn("--hash=sha256:", section)

    def test_human_roots_and_refresher_tool_are_exactly_pinned(self) -> None:
        self.assertIn("pip-audit==2.10.1", LOCK_INPUT.read_text(encoding="utf-8"))
        self.assertIn("pip==26.1.2", LOCK_INPUT.read_text(encoding="utf-8"))
        lock_tools = _locked_sections(LOCK_TOOLS)
        self.assertIn("pip-tools", lock_tools)
        self.assertIn("pip-tools==7.6.0", lock_tools["pip-tools"])
        self.assertIn("--hash=sha256:", lock_tools["pip-tools"])
        self.assertIn("pip==26.1.2", lock_tools["pip"])


class ReleaseOfflineInstallationTests(unittest.TestCase):
    def test_release_workflow_downloads_once_then_tests_audits_and_builds_the_same_offline_venv(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("requirements-release.lock", workflow)
        self.assertIn("pip download --disable-pip-version-check --only-binary=:all: --no-deps", workflow)
        self.assertIn("--require-hashes --dest .release-wheelhouse", workflow)
        self.assertIn("--no-index `", workflow)
        self.assertIn("--find-links .release-wheelhouse", workflow)
        self.assertIn(".legendctl-release-lock.sha256", workflow)
        self.assertIn(".\\.venv-zd\\Scripts\\python.exe -m unittest discover -s tests", workflow)
        self.assertIn(".\\.venv-zd\\Scripts\\python.exe -m pip_audit", workflow)
        self.assertNotIn("pip install --upgrade pip-audit", workflow)
        self.assertLess(
            workflow.index("pip download --disable-pip-version-check"),
            workflow.index("--no-index `"),
            "offline install must follow the only live wheel download",
        )

    def test_local_setup_and_builder_cannot_do_a_second_live_resolution(self) -> None:
        setup = SETUP.read_text(encoding="utf-8")
        build = BUILD.read_text(encoding="utf-8")
        self.assertIn("requirements-release.lock", setup)
        self.assertIn("--require-hashes --dest $wheelhouse", setup)
        self.assertIn("--no-index --find-links $wheelhouse", setup)
        self.assertIn(".legendctl-release-lock.sha256", setup)
        self.assertIn(".legendctl-release-lock.sha256", build)
        self.assertIn("pip check (hash-locked release venv)", build)
        self.assertIn("pip-audit (hash-locked release venv)", build)
        self.assertNotIn("pip install --quiet -r requirements-build.txt", build)

    def test_refresher_bootstraps_from_its_own_hash_lock_and_verifies_the_new_lock(self) -> None:
        refresh = REFRESH.read_text(encoding="utf-8")
        self.assertIn("requirements-lock-tools.lock", refresh)
        self.assertIn("--no-index --find-links $toolWheelhouse", refresh)
        self.assertIn("-m piptools compile --no-config --allow-unsafe --generate-hashes", refresh)
        self.assertIn("--pip-args '--only-binary=:all:'", refresh)
        self.assertIn("verify release lock wheel closure", refresh)
        self.assertIn("--require-hashes --dest $releaseWheelhouse", refresh)

    def test_generated_lock_header_records_the_no_config_wheel_only_resolution(self) -> None:
        header = "\n".join(LOCK.read_text(encoding="utf-8").splitlines()[:8])
        self.assertIn("--no-config", header)
        self.assertIn("--pip-args='--only-binary=:all:'", header)


if __name__ == "__main__":
    unittest.main()
