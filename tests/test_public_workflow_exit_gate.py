"""Regression contract for the public CI unittest exit-code gate."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    REPO_ROOT / ".github" / "workflows" / "ci.yml",
    REPO_ROOT / ".github" / "workflows" / "release-build.yml",
)
EVALUATOR = REPO_ROOT / ".github" / "scripts" / "evaluate-unittest-result.ps1"


class PublicWorkflowExitGateTests(unittest.TestCase):
    def test_both_workflows_use_the_shared_evaluator(self) -> None:
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow.name):
                text = workflow.read_text(encoding="utf-8")
                self.assertEqual(text.count("evaluate-unittest-result.ps1"), 1)
                self.assertNotIn("elseif ($log -match", text)

    def test_evaluator_matrix(self) -> None:
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("pwsh is required to exercise the public workflow gate")

        good_summary = (
            "................................\n"
            "----------------------------------------------------------------------\n"
            "Ran 32 tests in 1.250s\n\n"
            "OK (skipped=2, expected failures=1)\n"
        )
        cases = (
            ("success", good_summary, 0, 0),
            ("known teardown", good_summary, 139, 0),
            ("unexpected exit", good_summary, 7, 1),
            ("early fake OK", "OK\nthen stopped\n", 0, 1),
            (
                "nonterminal fake summary",
                good_summary + "post-summary process failure\n",
                0,
                1,
            ),
            (
                "failed summary",
                "Ran 1 test in 0.1s\n\nFAILED (failures=1)\n",
                0,
                1,
            ),
            ("missing summary", "test process stopped\n", 139, 1),
        )

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "test-output.txt"
            for name, log, process_code, expected in cases:
                with self.subTest(case=name):
                    log_path.write_text(log, encoding="utf-8", newline="\n")
                    proc = subprocess.run(
                        [
                            pwsh,
                            "-NoProfile",
                            "-File",
                            str(EVALUATOR),
                            "-LogPath",
                            str(log_path),
                            "-ProcessExitCode",
                            str(process_code),
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(
                        proc.returncode,
                        expected,
                        proc.stdout + proc.stderr,
                    )

    def test_release_tag_is_bound_to_main_and_every_version_surface(self) -> None:
        release = (WORKFLOWS[1]).read_text(encoding="utf-8")
        self.assertIn("fetch-depth: 0", release)
        self.assertIn("git merge-base --is-ancestor HEAD origin/main", release)
        self.assertIn("__version_tuple__", release)
        self.assertIn("version_info.txt", release)
        self.assertIn("@('filevers', 'prodvers')", release)
        self.assertIn("@('FileVersion', 'ProductVersion')", release)


if __name__ == "__main__":
    unittest.main()
