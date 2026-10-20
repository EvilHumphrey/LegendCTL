"""Inert layout fixtures; never install or execute a fixture Git executable."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Git for Windows installation boundary")
class ReleaseGitLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="legendctl-git-layout-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.program_files = self.root / "Program Files"
        self.program_files.mkdir()

    def implementation(self, layout):
        candidate = self.program_files / "Git" / layout / "bin/git.exe"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"inert unsigned fixture")
        return candidate

    def run_helper(self, command):
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "$ErrorActionPreference='Stop'; . $env:FIXTURE_RELEASE_GIT; " + command],
            env={
                **{key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"},
                "FIXTURE_RELEASE_GIT": str(ROOT / "tools/release_git.ps1"),
                "FIXTURE_PROGRAM_FILES": str(self.program_files),
            },
            capture_output=True, text=True, check=False,
        )

    def discover(self):
        return self.run_helper("Find-ReleaseGitImplementation -ProgramFiles $env:FIXTURE_PROGRAM_FILES")

    def test_modern_and_legacy_layouts_are_discovered(self):
        legacy = self.implementation("mingw64")
        result = self.discover()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), legacy)
        modern = self.implementation("ucrt64")
        result = self.discover()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), modern)
        legacy.unlink()
        result = self.discover()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), modern)

    def test_no_supported_layout_is_rejected_without_path_fallback(self):
        # A launcher is not an approved implementation, even inside Git's tree.
        launcher = self.program_files / "Git/cmd/git.exe"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"inert launcher fixture")
        result = self.discover()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("require Git for Windows in Program Files", result.stderr)

    def test_existing_modern_directory_is_not_skipped_for_legacy(self):
        self.implementation("mingw64")
        (self.program_files / "Git/ucrt64/bin/git.exe").mkdir(parents=True)
        result = self.discover()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ordinary file", result.stderr)

    def test_malformed_modern_ancestor_is_not_skipped_for_legacy(self):
        self.implementation("mingw64")
        modern = self.program_files / "Git/ucrt64"
        modern.write_bytes(b"inert non-directory")
        result = self.discover()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ordinary directories", result.stderr)

    def test_modern_junction_is_rejected_before_legacy_fallback(self):
        self.implementation("mingw64")
        outside = self.root / "outside"
        (outside / "bin").mkdir(parents=True)
        (outside / "bin/git.exe").write_bytes(b"outside inert fixture")
        junction = self.program_files / "Git/ucrt64"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            result = self.discover()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("reparse point", result.stderr)
            self.assertEqual((outside / "bin/git.exe").read_bytes(), b"outside inert fixture")
        finally:
            junction.rmdir()

    def resolver_with_fixture_layout(self, suffix=""):
        # Substitute discovery's input only in this test process. Authentication
        # still uses the real production signature/ACL checks, with no test flag.
        return self.run_helper(
            "$discover = ${function:Find-ReleaseGitImplementation}; "
            "function Find-ReleaseGitImplementation { param($ProgramFiles) "
            "& $discover -ProgramFiles $env:FIXTURE_PROGRAM_FILES }; "
            "Resolve-TrustedReleaseGit " + suffix
        )

    def test_unsigned_modern_selection_fails_authentication_without_legacy_fallback(self):
        self.implementation("mingw64")
        modern = self.implementation("ucrt64")
        result = self.resolver_with_fixture_layout()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("valid approved Git for Windows Authenticode signature", result.stderr)
        self.assertEqual(modern.read_bytes(), b"inert unsigned fixture")

    def test_expected_path_still_binds_selected_implementation(self):
        self.implementation("mingw64")
        self.implementation("ucrt64")
        result = self.resolver_with_fixture_layout(
            "-ExpectedPath (Join-Path $env:FIXTURE_PROGRAM_FILES 'Git\\mingw64\\bin\\git.exe')"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be the approved Program Files Git executable", result.stderr)


if __name__ == "__main__":
    unittest.main()
