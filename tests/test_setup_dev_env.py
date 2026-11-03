"""Executable safety and release-contract tests for setup_dev_env.ps1."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SETUP_PATH = _REPO_ROOT / "tools" / "setup_dev_env.ps1"
_BUILD_PATH = _REPO_ROOT / "tools" / "build_release.ps1"
_POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


class SetupScriptContentTests(unittest.TestCase):
    def test_preserves_hash_locked_offline_install_contract(self) -> None:
        body = _read(_SETUP_PATH)
        self.assertRegex(body, re.compile(r"&\s*\$py312\s+-m\s+venv\b"))
        self.assertIn("pip --isolated download", body)
        self.assertIn("--only-binary=:all: --no-deps", body)
        self.assertIn("--require-hashes --dest $wheelhouse -r $releaseLock", body)
        self.assertIn("pip --isolated install", body)
        self.assertIn("--no-index --find-links $wheelhouse", body)
        self.assertIn("pip --isolated check", body)
        self.assertIn("requirements-release.lock", body)

    def test_uses_canonical_python_and_sanitizes_pip_environment(self) -> None:
        body = _read(_SETUP_PATH)
        self.assertIn('"Programs\\Python\\Python312\\python.exe"', body)
        self.assertNotIn("LEGENDCTL_SETUP_TEST_SEAM", body)
        self.assertNotIn("LEGENDCTL_SETUP_PYTHON", body)
        self.assertIn('Where-Object { $_.Name -like "PIP_*" }', body)
        self.assertIn("Remove-Item -LiteralPath", body)

    def test_never_recursively_deletes_or_recreates_existing_venv(self) -> None:
        body = _read(_SETUP_PATH)
        self.assertNotRegex(body, re.compile(r"Remove-Item[^\r\n]*-Recurse", re.IGNORECASE))
        self.assertIn("-Recreate is no longer supported", body)
        self.assertIn("move it aside manually", body)

    def test_has_exclusive_lock_incomplete_marker_and_nofollow_checks(self) -> None:
        body = _read(_SETUP_PATH)
        self.assertIn(".venv-zd.setup-lock", body)
        self.assertIn(".venv-zd.setup-incomplete", body)
        self.assertIn("[IO.FileMode]::CreateNew", body)
        self.assertIn("[IO.FileShare]::None", body)
        self.assertIn("[IO.FileAttributes]::ReparsePoint", body)
        self.assertIn("Assert-NoReparseTree", body)

    def test_wheelhouse_cleanup_uses_guarded_mirror_and_nonrecursive_delete(self) -> None:
        body = _read(_SETUP_PATH)
        self.assertIn("LegendCtlReleaseMirror", body)
        self.assertIn("Assert-ReleasePath", body)
        self.assertIn("[IO.Directory]::Delete($Wheelhouse, $false)", body)

    def test_build_refuses_setup_markers_and_checks_lock_before_python(self) -> None:
        body = _read(_BUILD_PATH)
        marker_check = body.index(".venv-zd.setup-incomplete")
        python_invocation = body.index('Invoke-NativeCommand -Label "commit-bound release build"')
        hash_check = body.index("$checkoutRecordedHash -notin $acceptedCheckoutLockHashes")
        self.assertLess(marker_check, python_invocation)
        self.assertLess(hash_check, python_invocation)
        self.assertNotIn("setup_dev_env.ps1 -Recreate", body)
        self.assertGreaterEqual(body.count("Get-AcceptedReleaseLockHashes -LiteralPath"), 2)


@unittest.skipUnless(os.name == "nt" and _POWERSHELL, "Windows PowerShell required")
class SetupScriptExecutableTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="legendctl-setup-test-")
        # Brackets are intentional: setup must use literal paths throughout.
        self.root = Path(self._temp.name) / "repo[guard]"
        tools = self.root / "tools"
        tools.mkdir(parents=True)
        for name in ("setup_dev_env.ps1", "release_paths.ps1", "release_mirror.cs"):
            shutil.copy2(_REPO_ROOT / "tools" / name, tools / name)
        self.lock = self.root / "requirements-release.lock"
        self.lock.write_bytes(b"")
        self.local_app_data = Path(self._temp.name) / "local-app-data"
        python_parent = self.local_app_data / "Programs" / "Python"
        python_parent.mkdir(parents=True)
        self.python_link = python_parent / "Python312"
        base_python = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
        self._make_junction(self.python_link, base_python.parent)

    def tearDown(self) -> None:
        if self.python_link.exists():
            os.rmdir(self.python_link)
        self._temp.cleanup()

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["LOCALAPPDATA"] = str(self.local_app_data)
        environment.pop("LEGENDCTL_SETUP_TEST_SEAM", None)
        environment.pop("LEGENDCTL_SETUP_PYTHON", None)
        return subprocess.run(
            [
                str(_POWERSHELL),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.root / "tools" / "setup_dev_env.ps1"),
                *arguments,
            ],
            cwd=self.root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )

    @staticmethod
    def _make_junction(link: Path, target: Path) -> None:
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if result.returncode:
            raise AssertionError(result.stdout)

    def test_fresh_setup_succeeds_and_matching_rerun_is_idempotent(self) -> None:
        first = self._run()
        self.assertEqual(first.returncode, 0, first.stdout)
        marker = self.root / ".venv-zd" / ".legendctl-release-lock.sha256"
        self.assertEqual(marker.read_text(encoding="ascii"), hashlib.sha256(b"").hexdigest())
        self.assertFalse((self.root / ".venv-zd.setup-lock").exists())
        self.assertFalse((self.root / ".venv-zd.setup-incomplete").exists())
        self.assertEqual(list(self.root.glob(".release-wheelhouse-*")), [])
        second = self._run()
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertIn("Nothing to do", second.stdout)

    def test_delayed_contender_rechecks_completed_environment_under_lock(self) -> None:
        script = self.root / "tools" / "setup_dev_env.ps1"
        body = _read(script)
        lock_open = (
            "        $setupLockStream = [IO.File]::Open($setupLock, "
            "[IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, "
            "[IO.FileShare]::None)"
        )
        self.assertEqual(body.count(lock_open), 1)
        barrier = r'''
        if ($env:LEGENDCTL_TEST_DELAYED_READY) {
            [IO.File]::WriteAllText($env:LEGENDCTL_TEST_DELAYED_READY, "ready")
            while (-not [IO.File]::Exists($env:LEGENDCTL_TEST_DELAYED_RELEASE)) {
                Start-Sleep -Milliseconds 20
            }
        }
'''
        script.write_text(body.replace(lock_open, barrier + lock_open, 1), encoding="utf-8")

        ready = Path(self._temp.name) / "delayed-ready"
        release = Path(self._temp.name) / "delayed-release"
        environment = os.environ.copy()
        environment["LOCALAPPDATA"] = str(self.local_app_data)
        environment["LEGENDCTL_TEST_DELAYED_READY"] = str(ready)
        environment["LEGENDCTL_TEST_DELAYED_RELEASE"] = str(release)
        delayed = subprocess.Popen(
            [
                str(_POWERSHELL),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ],
            cwd=self.root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while not ready.exists() and delayed.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "delayed setup did not reach the pre-lock barrier")

            winner = self._run()
            self.assertEqual(winner.returncode, 0, winner.stdout)
            marker = self.root / ".venv-zd" / ".legendctl-release-lock.sha256"
            expected_marker = marker.read_bytes()

            release.write_text("continue", encoding="ascii")
            delayed_output, _ = delayed.communicate(timeout=120)
            self.assertEqual(delayed.returncode, 0, delayed_output)
            self.assertIn("Nothing to do", delayed_output)
            self.assertNotIn("Creating venv", delayed_output)
            self.assertEqual(marker.read_bytes(), expected_marker)
            self.assertFalse((self.root / ".venv-zd.setup-lock").exists())
            self.assertFalse((self.root / ".venv-zd.setup-incomplete").exists())
            self.assertEqual(list(self.root.glob(".release-wheelhouse-*")), [])
        finally:
            if delayed.poll() is None:
                release.write_text("continue", encoding="ascii")
                delayed.kill()
                delayed.communicate(timeout=30)

    def test_build_lock_hash_helper_accepts_only_line_ending_variants(self) -> None:
        candidate = self.root / "line-endings.lock"
        candidate.write_bytes(b"alpha\nbeta\n")
        command = r"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:BUILD_SCRIPT_UNDER_TEST, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw $errors[0] }
$function = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Get-AcceptedReleaseLockHashes'
}, $true)
if (-not $function) { throw 'hash helper not found' }
Invoke-Expression $function.Extent.Text
(Get-AcceptedReleaseLockHashes -LiteralPath $env:LOCK_PATH_UNDER_TEST) -join ','
"""
        environment = os.environ.copy()
        environment["BUILD_SCRIPT_UNDER_TEST"] = str(_BUILD_PATH)
        environment["LOCK_PATH_UNDER_TEST"] = str(candidate)
        result = subprocess.run(
            [
                str(_POWERSHELL),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        hashes = set(result.stdout.strip().split(","))
        lf_hash = hashlib.sha256(b"alpha\nbeta\n").hexdigest()
        crlf_hash = hashlib.sha256(b"alpha\r\nbeta\r\n").hexdigest()
        changed_hash = hashlib.sha256(b"alpha\nbeta changed\n").hexdigest()
        self.assertIn(lf_hash, hashes)
        self.assertIn(crlf_hash, hashes)
        self.assertNotIn(changed_hash, hashes)

    def test_stale_environment_and_recreate_are_retained_and_refused(self) -> None:
        scripts = self.root / ".venv-zd" / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "python.exe").write_bytes(b"not executed")
        (self.root / ".venv-zd" / ".legendctl-release-lock.sha256").write_text("stale", encoding="ascii")
        sentinel = self.root / ".venv-zd" / "keep.txt"
        sentinel.write_text("retain", encoding="ascii")
        result = self._run()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(sentinel.read_text(encoding="ascii"), "retain")
        recreate = self._run("-Recreate")
        self.assertNotEqual(recreate.returncode, 0, recreate.stdout)
        self.assertEqual(sentinel.read_text(encoding="ascii"), "retain")

    def test_existing_incomplete_environment_is_retained(self) -> None:
        venv = self.root / ".venv-zd"
        venv.mkdir()
        sentinel = venv / "keep.txt"
        sentinel.write_text("retain", encoding="ascii")
        result = self._run()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(sentinel.read_text(encoding="ascii"), "retain")

    def test_existing_lock_and_incomplete_marker_refuse_before_creation(self) -> None:
        (self.root / ".venv-zd.setup-lock").write_text("busy", encoding="ascii")
        locked = self._run()
        self.assertNotEqual(locked.returncode, 0, locked.stdout)
        self.assertFalse((self.root / ".venv-zd").exists())
        (self.root / ".venv-zd.setup-lock").unlink()
        (self.root / ".venv-zd.setup-incomplete").write_text("failed", encoding="ascii")
        incomplete = self._run()
        self.assertNotEqual(incomplete.returncode, 0, incomplete.stdout)
        self.assertFalse((self.root / ".venv-zd").exists())

    def test_root_junction_is_refused_without_touching_target(self) -> None:
        outside = Path(self._temp.name) / "outside-root"
        scripts = outside / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "python.exe").write_bytes(b"outside")
        (outside / ".legendctl-release-lock.sha256").write_text(
            hashlib.sha256(b"").hexdigest(), encoding="ascii"
        )
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("safe", encoding="ascii")
        self._make_junction(self.root / ".venv-zd", outside)
        result = self._run()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(sentinel.read_text(encoding="ascii"), "safe")

    def test_nested_junction_is_refused_without_touching_target(self) -> None:
        venv = self.root / ".venv-zd"
        venv.mkdir()
        outside = Path(self._temp.name) / "outside-scripts"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("safe", encoding="ascii")
        (outside / "python.exe").write_bytes(b"outside")
        self._make_junction(venv / "Scripts", outside)
        (venv / ".legendctl-release-lock.sha256").write_text(
            hashlib.sha256(b"").hexdigest(), encoding="ascii"
        )
        result = self._run()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(sentinel.read_text(encoding="ascii"), "safe")

    def test_pip_failure_retains_partial_environment_and_incomplete_marker(self) -> None:
        self.lock.write_text("this is not a valid requirement !!!\n", encoding="utf-8")
        result = self._run()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue((self.root / ".venv-zd").is_dir())
        self.assertTrue((self.root / ".venv-zd.setup-incomplete").is_file())
        retry = self._run()
        self.assertNotEqual(retry.returncode, 0, retry.stdout)
        self.assertIn("prior setup did not complete", retry.stdout.lower())


if __name__ == "__main__":
    unittest.main()
