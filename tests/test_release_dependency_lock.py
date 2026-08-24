"""Regression contract for the release Python wheel supply-chain boundary.

These assertions deliberately inspect the real workflow and local scripts. A
version-only pin is not enough: the artifact path must first hash-verify the
complete wheel set, then install and audit the same venv offline.
"""

from __future__ import annotations

import re
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements-release.lock"
LOCK_INPUT = ROOT / "requirements-release.in"
LOCK_TOOLS = ROOT / "requirements-lock-tools.lock"
LOCK_TOOLS_INPUT = ROOT / "requirements-lock-tools.in"
BUILD_INPUT = ROOT / "requirements-build.txt"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release-build.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SETUP = ROOT / "tools" / "setup_dev_env.ps1"
BUILD = ROOT / "tools" / "build_release.ps1"
REFRESH = ROOT / "tools" / "refresh_release_lock.ps1"
PY312 = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python312" / "python.exe"


def _locked_sections(path: Path) -> dict[str, str]:
    """Return each normalized package section without relying on pip-tools."""
    sections: dict[str, str] = {}
    for section in re.split(r"(?m)(?=^[A-Za-z0-9][A-Za-z0-9_.-]*==)", path.read_text(encoding="utf-8")):
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==[^\s]+", section)
        if match:
            sections[match.group(1).lower().replace("_", "-")] = section
    return sections


def _normalized_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _exact_requirement_roots(
    path: Path, *, _seen: set[Path] | None = None
) -> dict[str, str]:
    """Recursively collect exact ``name==version`` roots from input files."""

    seen = _seen if _seen is not None else set()
    resolved = path.resolve()
    if resolved in seen:
        return {}
    seen.add(resolved)

    roots: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        include = re.fullmatch(r"(?:-r|--requirement)\s+(.+)", line)
        if include:
            included_path = (path.parent / include.group(1).strip()).resolve()
            included_roots = _exact_requirement_roots(included_path, _seen=seen)
            for name, version in included_roots.items():
                if name in roots and roots[name] != version:
                    raise AssertionError(
                        f"conflicting exact pins for {name}: {roots[name]} and {version}"
                    )
                roots[name] = version
            continue

        requirement = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;\\]+)(?:\s*;.*)?", line
        )
        if requirement is None:
            raise AssertionError(
                f"{path.name}:{line_number} is not an exact requirement root: {line!r}"
            )
        name = _normalized_package_name(requirement.group(1))
        version = requirement.group(2)
        if name in roots and roots[name] != version:
            raise AssertionError(
                f"conflicting exact pins for {name}: {roots[name]} and {version}"
            )
        roots[name] = version
    return roots


def _locked_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for name, section in _locked_sections(path).items():
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9_.-]*==([^\s]+)", section)
        if match:
            versions[_normalized_package_name(name)] = match.group(1)
    return versions


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
        build_input = BUILD_INPUT.read_text(encoding="utf-8")
        release_input = LOCK_INPUT.read_text(encoding="utf-8")
        tools_input = LOCK_TOOLS_INPUT.read_text(encoding="utf-8")
        release_lock = _locked_sections(LOCK)
        lock_tools = _locked_sections(LOCK_TOOLS)
        self.assertIn("pyinstaller==6.22.2", build_input)
        self.assertIn("pyinstaller==6.22.2", release_lock["pyinstaller"])
        self.assertIn("pip-audit==2.10.1", release_input)
        self.assertIn("pip==26.2.1", release_input)
        self.assertIn("pip==26.2.1", release_lock["pip"])
        self.assertIn("pip-tools==7.6.1", tools_input)
        self.assertIn("pip==26.2.1", tools_input)
        self.assertIn("pip-tools", lock_tools)
        self.assertIn("pip-tools==7.6.1", lock_tools["pip-tools"])
        self.assertIn("--hash=sha256:", lock_tools["pip-tools"])
        self.assertIn("pip==26.2.1", lock_tools["pip"])

    def test_release_lock_versions_match_all_recursive_build_roots(self) -> None:
        lock_versions = _locked_versions(LOCK)
        # Parse the build file independently as well as the full release input:
        # this keeps a future edit to either include graph from hiding drift.
        human_roots: dict[str, str] = {}
        for input_path in (BUILD_INPUT, LOCK_INPUT):
            for name, version in _exact_requirement_roots(input_path).items():
                self.assertTrue(
                    name not in human_roots or human_roots[name] == version,
                    f"conflicting human-maintained pins for {name}: "
                    f"{human_roots.get(name)} and {version}",
                )
                human_roots[name] = version

        self.assertTrue(human_roots)
        for name, expected_version in human_roots.items():
            with self.subTest(package=name):
                self.assertIn(name, lock_versions)
                self.assertEqual(lock_versions[name], expected_version)


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
        self.assertIn("Where-Object { $_.Name -like 'PIP_*' }", refresh)
        self.assertIn("--no-index --find-links $toolWheelhouse", refresh)
        self.assertEqual(refresh.count("-m pip --isolated download --disable-pip-version-check"), 2)
        self.assertIn("-m piptools compile --no-config --allow-unsafe --generate-hashes", refresh)
        self.assertIn("--pip-args '--isolated --only-binary=:all:'", refresh)
        self.assertIn("verify release lock wheel closure", refresh)
        self.assertIn("--require-hashes --dest $releaseWheelhouse", refresh)

    def test_generated_lock_header_records_the_no_config_wheel_only_resolution(self) -> None:
        header = "\n".join(LOCK.read_text(encoding="utf-8").splitlines()[:8])
        self.assertIn("--no-config", header)
        self.assertIn("--pip-args='--isolated --only-binary=:all:'", header)
        self.assertIn("--only-binary :all:", LOCK.read_text(encoding="utf-8"))

    @unittest.skipUnless(PY312.is_file(), "requires the supported local Python 3.12 refresh runtime")
    def test_refresher_ignores_hostile_pip_environment(self) -> None:
        """The real refresh path must ignore hostile pip env/config, not just name flags."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "hostile-pip.conf"
            config.write_text(
                "[global]\nindex-url = http://127.0.0.1:9/simple\nno-index = true\n",
                encoding="utf-8",
            )
            environment = os.environ | {
                "PIP_CONFIG_FILE": str(config),
                "PIP_INDEX_URL": "http://127.0.0.1:9/simple",
                "PIP_NO_INDEX": "1",
                "PIP_NO_CACHE_DIR": "1",
            }
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(REFRESH)],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=180,
            )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("Release lock refreshed and hash-verified.", completed.stdout)


if __name__ == "__main__":
    unittest.main()
