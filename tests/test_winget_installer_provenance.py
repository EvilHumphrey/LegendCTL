"""Exercise the same generated-manifest guard used before catalog submission."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / ".github" / "scripts" / "verify-winget-installer.ps1"
WORKFLOW = ROOT / ".github" / "workflows" / "winget-publish.yml"
FIXTURES = ROOT / "packaging" / "winget" / "2.3.0"
PACKAGE = "EvilHumphrey.LegendCTL"
URL = f"https://github.com/EvilHumphrey/LegendCTL/releases/download/v2.3.0/ZDUltimateLegend-v2.3.0-Setup.exe"
HASH = "A6CA8F2DE16FBCC65C8EE825891C1EAB2694FE29C3E3C2D43DA35747C729DFB2"


class WingetInstallerProvenanceTests(unittest.TestCase):
    def test_workflow_carries_verified_digest_through_both_downloads(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('"INSTALLER_SHA256=$localHash"', workflow)
        self.assertEqual(workflow.count("& .\\.github\\scripts\\verify-winget-installer.ps1"), 2)
        self.assertEqual(workflow.count("-InstallerSha256 $env:INSTALLER_SHA256"), 2)
        self.assertLess(workflow.index("uses: actions/checkout@"), workflow.index("verify-winget-installer.ps1"))
        self.assertLess(workflow.rindex("verify-winget-installer.ps1"), workflow.index("& .\\wingetcreate.exe submit"))

    def test_generated_manifest_provenance_matrix(self) -> None:
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("pwsh is required to exercise the winget provenance guard")
        original = (FIXTURES / f"{PACKAGE}.installer.yaml").read_text(encoding="utf-8")
        no_indent = original.replace("  - Architecture:", "- Architecture:")
        before, block = no_indent.split("Installers:\n", 1)
        block, after = block.split("ManifestType:", 1)
        no_indent = before + "Installers:\n" + "\n".join(
            line[2:] if line.startswith("  ") else line for line in block.split("\n")
        ) + "ManifestType:" + after
        cases = {
            "valid catalog layout": (original, True),
            "valid lowercase digest": (original.replace(HASH, HASH.lower()), True),
            "valid CRLF": (original.replace("\n", "\r\n"), True),
            "valid unindented sequence": (no_indent, True),
            "second download changed": (original.replace(HASH, "B" * 64), False),
            "wrong URL": (original.replace(URL, URL.lower()), False),
            "wrong architecture": (original.replace("Architecture: x64", "Architecture: x86"), False),
            "missing digest": (original.replace(f"    InstallerSha256: {HASH}\n", ""), False),
            "duplicate digest": (original.replace(f"    InstallerSha256: {HASH}", f"    InstallerSha256: {HASH}\n    InstallerSha256: {HASH}"), False),
            "extra URL-first installer": (original.replace("ManifestType:", f"  - InstallerUrl: {URL}\n    Architecture: x64\n    InstallerSha256: {HASH}\nManifestType:"), False),
            "quoted critical key": (original.replace("InstallerSha256:", "'InstallerSha256':"), False),
            "quoted hash": (original.replace(HASH, f"'{HASH}'"), False),
            "multiline digest": (original.replace(f"InstallerSha256: {HASH}", f"InstallerSha256: |\n      {HASH}"), False),
            "alias digest": (original.replace(HASH, "*hash"), False),
            "anchor digest": (original.replace(HASH, f"&hash {HASH}"), False),
            "merge key": (original.replace("Installers:\n", "Installers:\n  <<: *installers\n"), False),
            "flow installers": (original.replace("Installers:", "Installers: []"), False),
            "document marker": ("---\n" + original, False),
            "explicit key": (original.replace("    InstallerSha256:", "    ? InstallerSha256\n    :"), False),
            "wrong version": (original.replace("PackageVersion: 2.3.0", "PackageVersion: 2.3.1"), False),
            "duplicate type": (original + "ManifestType: installer\n", False),
            "case variant type": (original + "manifestType: installer\n", False),
            "wrong package": (original.replace(f"PackageIdentifier: {PACKAGE}", "PackageIdentifier: Example.Other"), False),
            "root digest": (original.replace("    InstallerSha256:", "InstallerSha256:"), False),
            "extra file": (original, False),
            "locale masquerades as installer": (original, False),
        }
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for name, (installer, accepted) in cases.items():
                with self.subTest(case=name):
                    for old in directory.iterdir():
                        old.unlink()
                    for fixture in FIXTURES.glob("*.yaml"):
                        shutil.copyfile(fixture, directory / fixture.name)
                    (directory / f"{PACKAGE}.installer.yaml").write_text(installer, encoding="utf-8", newline="")
                    if name == "extra file":
                        (directory / "duplicate.txt").write_text(installer, encoding="utf-8")
                    if name == "locale masquerades as installer":
                        (directory / f"{PACKAGE}.locale.en-US.yaml").write_text(installer, encoding="utf-8")
                    proc = subprocess.run(
                        [pwsh, "-NoProfile", "-File", str(GUARD), "-ManifestDirectory", str(directory),
                         "-InstallerUrl", URL, "-InstallerSha256", HASH.lower()],
                        capture_output=True, text=True, check=False, timeout=20,
                    )
                    self.assertEqual(proc.returncode == 0, accepted, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
