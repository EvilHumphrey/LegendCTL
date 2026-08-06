"""Regression contract for the public CI unittest exit-code gate."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    REPO_ROOT / ".github" / "workflows" / "ci.yml",
    REPO_ROOT / ".github" / "workflows" / "release-build.yml",
)
OK_BRANCH = "elseif ($log -match '(?m)^OK\\b') {"


class PublicWorkflowExitGateTests(unittest.TestCase):
    def test_ok_summary_allows_only_exit_zero_or_known_dpg_139(self) -> None:
        branch_contract = re.compile(
            r"if\s*\(\$code\s*-eq\s*0\)\s*\{.*?exit\s+0.*?"
            r"elseif\s*\(\$code\s*-eq\s*139\)\s*\{.*?exit\s+0.*?"
            r"else\s*\{.*?unexpected code \$code.*?exit\s+1",
            re.DOTALL,
        )

        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow.name):
                text = workflow.read_text(encoding="utf-8")
                self.assertEqual(text.count(OK_BRANCH), 1)
                ok_branch = text[text.index(OK_BRANCH) :]
                self.assertRegex(ok_branch, branch_contract)

    def test_ok_summary_cannot_directly_force_success(self) -> None:
        unsafe_contract = re.compile(
            re.escape(OK_BRANCH)
            + r"\s*Write-Host\s+\"Suite PASSED\..*?\"\s*exit\s+0",
            re.DOTALL,
        )

        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow.name):
                text = workflow.read_text(encoding="utf-8")
                self.assertNotRegex(text, unsafe_contract)

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
