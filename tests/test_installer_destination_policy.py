"""Installer wiring checks plus an explicit, source-bound native evidence gate.

The Pascal predicate is deliberately not reimplemented in Python. Run
tools/test_installer_destination.ps1 -RunNativePolicy, then set
LEGENDCTL_INSTALLER_NATIVE_RESULTS to that run's summary.json to include the
native gate. Without it, unittest reports a visible skip, never a native pass.
VM install/upgrade/uninstall qualification remains a separate release gate.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "tools/installer/install_directory_policy.iss"
INSTALLER = ROOT / "tools/installer/inno_setup_zd_wrapper.iss"
HARNESS = ROOT / "tools/test_installer_destination.ps1"
REQUIRED_NATIVE_CASES = {
    "existing-directory", "missing-leaf", "case-insensitive", "trailing-separator",
    "drive-root", "different-destination", "sibling-prefix", "app-subdirectory",
    "dot-segment-escape", "dot-segment-safe-alias", "forward-slashes", "device-path",
    "unc-destination", "empty-destination", "missing-ancestor", "file-collision",
    "file-ancestor", "junction-root", "junction-ancestor", "invalid-attribute-name",
    "residual-descendant-junction",
}


class InstallerDestinationWiringTests(unittest.TestCase):
    """Static wiring supplements native behavior; it does not prove behavior."""

    def test_final_resolved_app_path_enters_shared_policy(self) -> None:
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('#include "install_directory_policy.iss"', source)
        prepare = re.search(
            r"function PrepareToInstall\(var NeedsRestart: Boolean\): String;(?P<body>.*?)\nend;",
            source, re.DOTALL,
        )
        self.assertIsNotNone(prepare, "No final installer enforcement callback")
        body = prepare.group("body")
        self.assertIn("ExpandConstant('{autopf}\\ZDUltimateLegend')", body)
        self.assertIn("LegendInstallDirectoryAllowed(ExpandConstant('{app}'), ManagedDir)", body)
        self.assertIn("LegendPreviousInstallAllowed(ManagedDir)", body)
        self.assertIn("not IsAdminInstallMode", body)
        self.assertIn("Result := SetupMessage(msgInvalidDirName)", body)

    def test_directory_page_and_previous_directory_are_disabled(self) -> None:
        source = INSTALLER.read_text(encoding="utf-8")
        for directive in ("DisableDirPage=yes", "UsePreviousAppDir=no"):
            self.assertRegex(source, re.compile(r"^" + re.escape(directive) + r"$", re.MULTILINE))

    def test_previous_registration_checks_cover_declared_roots(self) -> None:
        source = POLICY.read_text(encoding="utf-8")
        for root in ("HKLM32", "HKLM64", "HKCU32", "HKCU64"):
            self.assertIn(f"LegendPreviousInstallAtRootAllowed({root}, ManagedDir)", source)


class InstallerNativePolicyEvidenceTests(unittest.TestCase):
    def test_real_pascal_policy_results_match_current_sources(self) -> None:
        evidence_path = os.environ.get("LEGENDCTL_INSTALLER_NATIVE_RESULTS")
        if not evidence_path:
            self.skipTest(
                "Native Inno gate NOT RUN: run tools/test_installer_destination.ps1 "
                "-RunNativePolicy and set LEGENDCTL_INSTALLER_NATIVE_RESULTS"
            )
        evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8-sig"))
        self.assertEqual(1, evidence["schema"])
        self.assertEqual("inno-pascal-script", evidence["backend"])
        self.assertEqual("PASS", evidence["native_status"])
        self.assertEqual(1, evidence["native_exit_code"], "Fixture must abort before installation")
        for field, path in (
            ("policy_sha256", POLICY), ("included_policy_sha256", POLICY),
            ("installer_sha256", INSTALLER), ("harness_sha256", HARNESS),
        ):
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), evidence[field],
                             f"Stale native evidence: {path.name} changed")
        cases = evidence["cases"]
        self.assertEqual(REQUIRED_NATIVE_CASES, {case["id"] for case in cases})
        self.assertEqual(len(REQUIRED_NATIVE_CASES), len(cases), "Duplicate or absent native cases")
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertIn(case["expected"], ("allow", "reject"))
                self.assertEqual(case["expected"], case["actual"])


if __name__ == "__main__":
    unittest.main()
