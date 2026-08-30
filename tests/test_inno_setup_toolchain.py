"""Exercise the real PowerShell hash boundary without running fixture binaries.

All fixture .exe/.dll files are inert text. Native execution is deliberately
absent; the acquisition controls replace only download/signature/process APIs.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools" / "inno_setup.ps1"
PIN = ROOT / "tools" / "inno_setup.lock.json"
SHELLS = [shell for name in ("powershell.exe", "pwsh.exe") if (shell := shutil.which(name))]


@unittest.skipUnless(os.name == "nt" and SHELLS, "requires Windows PowerShell")
class InnoSetupExecutionBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = ROOT / "build" / "inno-boundary-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="with spaces-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.compiler = self.root / "compiler"
        self.compiler.mkdir()
        self.pin = json.loads(PIN.read_text(encoding="utf-8-sig"))
        for name in self.pin["files"]:
            data = ("inert fixture: " + name).encode()
            (self.compiler / name).write_bytes(data)
            self.pin["files"][name] = hashlib.sha256(data).hexdigest()
        self.pin_path = self.root / "pin.json"
        self.write_pin()

    def write_pin(self) -> None:
        self.pin_path.write_text(json.dumps(self.pin), encoding="utf-8")

    def run_ps(self, body: str, *, success: bool = True, message: str = "") -> None:
        script = self.root / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            ". $env:INNO_HELPER\n"
            + body,
            encoding="utf-8-sig",
        )
        env = os.environ | {
            "INNO_HELPER": str(HELPER), "INNO_FIXTURE": str(self.compiler),
            "INNO_PIN": str(self.pin_path), "INNO_TEST_ROOT": str(self.root),
        }
        # A Python child does not apply pwsh's native powershell.exe launch
        # fixup. Let each shell establish its own standard-library module path.
        for key in list(env):
            if key.casefold() == "psmodulepath":
                del env[key]
        for shell in SHELLS:
            with self.subTest(shell=Path(shell).name):
                result = subprocess.run(
                    [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
                )
                output = result.stdout + result.stderr
                if success:
                    self.assertEqual(result.returncode, 0, output)
                else:
                    self.assertNotEqual(result.returncode, 0, output)
                self.assertIn(message, output)

    @staticmethod
    def validate() -> str:
        return "$pin = Read-InnoSetupPin -Path $env:INNO_PIN\nAssert-InnoSetupCompiler -Directory $env:INNO_FIXTURE -Pin $pin\n"

    def test_complete_fixture_closure_is_accepted_without_execution(self) -> None:
        self.run_ps(self.validate(), message="ISCC.exe")

    def test_changed_compiler_and_sibling_dll_are_rejected(self) -> None:
        for name in ("ISCC.exe", "ISCmplr.dll", "ISPPBuiltins.iss", "Setup.e32"):
            with self.subTest(asset=name):
                target = self.compiler / name
                original = target.read_bytes()
                target.write_bytes(b"same version banner, different untrusted bytes")
                self.run_ps(self.validate(), success=False, message="SHA-256 mismatch")
                target.write_bytes(original)

    def test_missing_runtime_asset_is_rejected(self) -> None:
        (self.compiler / "islzma64.exe").unlink()
        self.run_ps(self.validate(), success=False, message="islzma64.exe")

    def test_locked_unreadable_asset_is_rejected(self) -> None:
        self.run_ps(
            "$locked = [IO.File]::Open((Join-Path $env:INNO_FIXTURE 'ISCC.exe'), 'Open', 'Read', 'None')\n"
            "try {\n" + self.validate() + "} finally { $locked.Dispose() }",
            success=False, message="ISCC.exe",
        )

    def test_missing_hash_cannot_reduce_the_required_file_set(self) -> None:
        del self.pin["files"]["ISCmplr.dll"]
        self.write_pin()
        self.run_ps(self.validate(), success=False, message="Invalid Inno Setup compiler file set")

    def test_malformed_hash_is_rejected(self) -> None:
        self.pin["files"]["ISCC.exe"] = "6.7.1"
        self.write_pin()
        self.run_ps(self.validate(), success=False, message="Invalid Inno Setup compiler pin")

    def test_unreviewed_download_source_is_rejected(self) -> None:
        self.pin["installer"]["url"] = "https://example.invalid/innosetup-6.7.1.exe"
        self.write_pin()
        self.run_ps(self.validate(), success=False, message="Invalid Inno Setup installer pin")

    def test_unexpected_dll_or_redirection_file_is_rejected(self) -> None:
        for name in ("version.dll", "ISCC.exe.local"):
            with self.subTest(asset=name):
                path = self.compiler / name
                path.write_bytes(b"unreviewed")
                self.run_ps(self.validate(), success=False, message="Unexpected executable input")
                path.unlink()

    def test_compiler_directory_junction_is_rejected(self) -> None:
        self.run_ps(
            "$link = Join-Path $env:INNO_TEST_ROOT ('junction-' + [Guid]::NewGuid().ToString('N'))\n"
            "New-Item -ItemType Junction -Path $link -Target $env:INNO_FIXTURE | Out-Null\n"
            "$pin = Read-InnoSetupPin -Path $env:INNO_PIN\n"
            "Assert-InnoSetupCompiler -Directory $link -Pin $pin",
            success=False, message="reparse point",
        )

    def test_ancestor_junction_is_rejected(self) -> None:
        self.run_ps(
            "$link = Join-Path $env:INNO_TEST_ROOT ('parent-' + [Guid]::NewGuid().ToString('N'))\n"
            "New-Item -ItemType Junction -Path $link -Target $env:INNO_TEST_ROOT | Out-Null\n"
            "$pin = Read-InnoSetupPin -Path $env:INNO_PIN\n"
            "Assert-InnoSetupCompiler -Directory (Join-Path $link 'compiler') -Pin $pin",
            success=False, message="reparse point",
        )

    def test_runtime_asset_directory_junction_is_rejected(self) -> None:
        (self.compiler / "ISCC.exe").unlink()
        target = self.root / "inert directory"
        target.mkdir()
        self.run_ps(
            "$link = Join-Path $env:INNO_FIXTURE 'ISCC.exe'\n"
            "if (-not (Test-Path -LiteralPath $link)) { New-Item -ItemType Junction -Path $link -Target (Join-Path $env:INNO_TEST_ROOT 'inert directory') | Out-Null }\n"
            + self.validate(), success=False, message="reparse point",
        )

    def test_absence_keeps_zip_only_but_present_invalid_does_not_fall_back(self) -> None:
        self.run_ps(
            "function Read-InnoSetupPin { Microsoft.PowerShell.Utility\\ConvertFrom-Json (Get-Content -LiteralPath $env:INNO_PIN -Raw) }\n"
            "$env:ProgramFiles = Join-Path $env:INNO_TEST_ROOT 'absent64'\n"
            "${env:ProgramFiles(x86)} = Join-Path $env:INNO_TEST_ROOT 'absent32'\n"
            "$repo = Join-Path $env:INNO_TEST_ROOT ([Guid]::NewGuid().ToString('N'))\n"
            "New-Item -ItemType Directory -Path $repo | Out-Null\n"
            "$result = Find-VerifiedInnoSetupCompiler -RepoRoot $repo\n"
            "if ($null -ne $result) { throw 'Absent compiler should return null' }\n"
            "$candidate = Get-InnoSetupDirectory -RepoRoot $repo -Pin (Read-InnoSetupPin)\n"
            "New-Item -ItemType Directory -Force -Path $candidate | Out-Null\n"
            "Find-VerifiedInnoSetupCompiler -RepoRoot $repo",
            success=False, message="Compil32.exe",
        )

    def test_download_hash_mismatch_is_rejected_before_signature_or_execution(self) -> None:
        self.run_ps(
            "function Get-AuthenticodeSignature { throw 'SIGNATURE_SHOULD_NOT_RUN' }\n"
            "$pin = Read-InnoSetupPin -Path $env:INNO_PIN\n"
            "Assert-InnoSetupInstaller -Path (Join-Path $env:INNO_FIXTURE 'ISCC.exe') -Pin $pin",
            success=False, message="installer SHA-256 mismatch",
        )

    def test_publisher_and_invalid_signature_are_rejected(self) -> None:
        self.pin["installer"]["sha256"] = self.pin["files"]["ISCC.exe"]
        self.write_pin()
        for status, publisher in (("Valid", "Unreviewed Publisher"), ("NotSigned", self.pin["installer"]["authenticodeSubject"])):
            with self.subTest(status=status):
                self.run_ps(
                    "function Get-AuthenticodeSignature { [pscustomobject]@{Status='" + status
                    + "'; SignerCertificate=[pscustomobject]@{Subject='" + publisher + "'}} }\n"
                    "$pin = Read-InnoSetupPin -Path $env:INNO_PIN\n"
                    "Assert-InnoSetupInstaller -Path (Join-Path $env:INNO_FIXTURE 'ISCC.exe') -Pin $pin",
                    success=False, message="publisher/signature mismatch",
                )

    def test_valid_hash_and_expected_publisher_control(self) -> None:
        self.pin["installer"]["sha256"] = self.pin["files"]["ISCC.exe"]
        self.write_pin()
        self.run_ps(
            "function Get-AuthenticodeSignature { [pscustomobject]@{Status='Valid'; SignerCertificate=[pscustomobject]@{Subject='"
            + self.pin["installer"]["authenticodeSubject"] + "'}} }\n"
            "$pin = Read-InnoSetupPin -Path $env:INNO_PIN\n"
            "Assert-InnoSetupInstaller -Path (Join-Path $env:INNO_FIXTURE 'ISCC.exe') -Pin $pin\n"
            "Write-Output 'verified without executing fixture'", message="verified without executing fixture",
        )

    @staticmethod
    def acquisition_harness() -> str:
        return (
            "$repo = Join-Path $env:INNO_TEST_ROOT ([Guid]::NewGuid().ToString('N'))\n"
            "$tools = Join-Path $repo 'tools'\n"
            "New-Item -ItemType Directory -Path $tools -Force | Out-Null\n"
            "Copy-Item -LiteralPath $env:INNO_HELPER -Destination $tools\n"
            "Copy-Item -LiteralPath (Join-Path (Split-Path $env:INNO_HELPER -Parent) 'setup_inno_setup_ci.ps1') -Destination $tools\n"
            "Copy-Item -LiteralPath $env:INNO_PIN -Destination (Join-Path $tools 'inno_setup.lock.json')\n"
            "$env:GITHUB_ACTIONS = 'true'\n"
            "$global:innoTestEvents = @()\n"
            "function Invoke-WebRequest { param([switch]$UseBasicParsing, $Uri, $OutFile, $ErrorAction)\n"
            "  $global:innoTestEvents += 'download'\n"
            "  Copy-Item -LiteralPath (Join-Path $env:INNO_FIXTURE 'ISCC.exe') -Destination $OutFile\n"
            "}\n"
            "function Get-AuthenticodeSignature { param($LiteralPath, $ErrorAction)\n"
            "  $global:innoTestEvents += 'signature'\n"
            "  [pscustomobject]@{Status='Valid'; SignerCertificate=[pscustomobject]@{Subject='CN=Pyrsys B.V., O=Pyrsys B.V., S=Noord-Holland, C=NL'}}\n"
            "}\n"
            "function Start-Process { param($FilePath, $ArgumentList, $WorkingDirectory, [switch]$Wait, [switch]$PassThru, $WindowStyle)\n"
            "  $global:innoTestEvents += 'prepare'\n"
            "  $arg = $ArgumentList | Where-Object { $_ -like '/DIR=*' }\n"
            "  $destination = $arg.Substring(6).TrimEnd('" + '"' + "')\n"
            "  New-Item -ItemType Directory -Path $destination | Out-Null\n"
            "  Get-ChildItem -LiteralPath $env:INNO_FIXTURE | Copy-Item -Destination $destination\n"
            "  [pscustomobject]@{ExitCode=0}\n"
            "}\n"
        )

    def test_acquisition_rejects_tampered_download_before_native_process(self) -> None:
        self.run_ps(
            self.acquisition_harness()
            + "try { & (Join-Path $tools 'setup_inno_setup_ci.ps1'); throw 'Unexpected success' }\n"
            "catch { if ($_.Exception.Message -notlike '*installer SHA-256 mismatch*') { throw }; }\n"
            "if (($global:innoTestEvents -join ',') -ne 'download') { throw 'Unverified bytes reached signature/process API' }\n"
            "Write-Output 'tampered download stopped before native process'",
            message="tampered download stopped before native process",
        )

    def test_acquisition_control_verifies_extracted_closure(self) -> None:
        self.pin["installer"]["sha256"] = self.pin["files"]["ISCC.exe"]
        self.write_pin()
        self.run_ps(
            self.acquisition_harness()
            + "& (Join-Path $tools 'setup_inno_setup_ci.ps1')\n"
            "if (($global:innoTestEvents -join ',') -ne 'download,signature,prepare') { throw 'Incorrect authentication order' }\n"
            "Write-Output 'acquisition control passed without executing fixture binaries'",
            message="acquisition control passed without executing fixture binaries",
        )


class InnoSetupCallerContractTests(unittest.TestCase):
    def test_ci_and_local_builder_share_hash_boundary(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release-build.yml").read_text(encoding="utf-8")
        build = (ROOT / "tools" / "build_release.ps1").read_text(encoding="utf-8")
        setup = (ROOT / "tools" / "setup_inno_setup_ci.ps1").read_text(encoding="utf-8")
        self.assertNotIn("choco install", workflow)
        self.assertNotIn("DisplayVersion", workflow)
        self.assertIn("tools\\setup_inno_setup_ci.ps1", workflow)
        self.assertLess(setup.index("Assert-InnoSetupInstaller -Path"), setup.index("$process = Start-Process"))
        self.assertIn("Assert-InnoSetupCompiler -Directory $compilerDir", setup)
        self.assertIn("Find-VerifiedInnoSetupCompiler -RepoRoot $repoRoot", build)
        self.assertLess(build.index("Assert-InnoSetupCompiler -Directory"), build.index('Invoke-NativeCommand -Label "Inno Setup compile"'))
        self.assertNotIn("Invoke-WebRequest", build)
        self.assertNotIn("setup_inno_setup_ci.ps1", build)


if __name__ == "__main__":
    unittest.main()
