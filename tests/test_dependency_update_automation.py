"""Contracts for coordinated GitHub Actions dependency maintenance."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CODEQL_WORKFLOW = ROOT / ".github" / "workflows" / "codeql.yml"
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


if __name__ == "__main__":
    unittest.main()
