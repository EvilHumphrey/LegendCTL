"""Contracts for coordinated GitHub Actions dependency maintenance."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CODEQL_WORKFLOW = ROOT / ".github" / "workflows" / "codeql.yml"
WEEKLY_AUDIT_WORKFLOW = (
    ROOT / ".github" / "workflows" / "weekly-dependency-audit.yml"
)
DEPENDABOT = ROOT / ".github" / "dependabot.yml"


class DependencyUpdateAutomationTests(unittest.TestCase):
    def test_codeql_init_and_analyze_use_one_full_sha_and_version(self) -> None:
        workflow = CODEQL_WORKFLOW.read_text(encoding="utf-8")
        matches = re.findall(
            r"github/codeql-action/(init|analyze)@([0-9a-f]{40})\s+#\s+(v[^\s]+)",
            workflow,
        )
        self.assertEqual({component for component, _, _ in matches}, {"init", "analyze"})
        self.assertEqual(len({sha for _, sha, _ in matches}), 1, matches)
        self.assertEqual(len({version for _, _, version in matches}), 1, matches)

    def test_dependabot_groups_all_codeql_action_components(self) -> None:
        config = DEPENDABOT.read_text(encoding="utf-8")
        self.assertIn("codeql-components:", config)
        self.assertIn('          - "github/codeql-action/*"', config)

    def test_weekly_audit_is_hash_locked_read_only_and_audit_only(self) -> None:
        workflow = WEEKLY_AUDIT_WORKFLOW.read_text(encoding="utf-8")

        self.assertRegex(workflow, r"(?m)^  schedule:\s*$")
        self.assertRegex(workflow, r"(?m)^    - cron: '[^']+'\s*$")
        self.assertRegex(workflow, r"(?m)^  workflow_dispatch:\s*$")
        self.assertNotRegex(workflow, r"(?m)^  (?:push|pull_request):\s*$")
        self.assertRegex(
            workflow,
            r"(?ms)^permissions:\s*\n  contents: read\s*$",
        )
        self.assertIn("group: weekly-dependency-audit-${{ github.ref }}", workflow)
        self.assertEqual(workflow.count("\n  audit:\n"), 1)
        self.assertEqual(workflow.count("runs-on:"), 1)

        uses = re.findall(r"(?m)^\s+uses:\s+([^\s#]+)(?:\s+#.*)?$", workflow)
        self.assertEqual(len(uses), 2, uses)
        for action in uses:
            with self.subTest(action=action):
                self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

        self.assertIn("requirements-release.lock", workflow)
        self.assertIn("--require-hashes --dest .audit-wheelhouse", workflow)
        self.assertIn("--no-index --find-links .audit-wheelhouse", workflow)
        self.assertIn("python -m pip check", workflow)
        self.assertIn("python -m pip_audit", workflow)
        self.assertEqual(workflow.count("if ($LASTEXITCODE -ne 0)"), 4)

        # A weekly audit must never grow into the GUI-producing product suite.
        self.assertNotIn("unittest", workflow)
        self.assertNotIn("evaluate-unittest-result", workflow)
        self.assertNotIn("tests/", workflow)


if __name__ == "__main__":
    unittest.main()
