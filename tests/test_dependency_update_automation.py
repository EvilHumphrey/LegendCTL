"""Contracts for coordinated GitHub Actions dependency maintenance."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CODEQL_WORKFLOW = ROOT / ".github" / "workflows" / "codeql.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
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
        self.assertIn(r".\.venv-audit\Scripts\python.exe -m pip check", workflow)
        self.assertIn(r".\.venv-audit\Scripts\python.exe -m pip_audit", workflow)

        # A weekly audit must never grow into the GUI-producing product suite.
        self.assertNotIn("unittest", workflow)
        self.assertNotIn("evaluate-unittest-result", workflow)
        self.assertNotIn("tests/", workflow)

    def test_audits_install_and_check_only_the_isolated_hash_locked_environment(self) -> None:
        audit_interpreter = r".\.venv-audit\Scripts\python.exe"
        tools_interpreter = r".\.venv-lock-tools\Scripts\python.exe"
        for path in (CI_WORKFLOW, WEEKLY_AUDIT_WORKFLOW):
            with self.subTest(workflow=path.name):
                # Inspect only the audit job; the independent product-test job
                # is intentionally outside this dependency-closure contract.
                audit_job = path.read_text(encoding="utf-8").split("\n  audit:\n", 1)[1]
                scripts = re.findall(
                    r"(?ms)^        run: \|\n(.*?)(?=^      - name:|\Z)", audit_job,
                )
                commands = []
                for script in scripts:
                    # Fold PowerShell continuations so each native command's
                    # flags and immediate exit guard are checked together.
                    script = re.sub(r"`\s*\n\s*", " ", script)
                    lines = [line.strip() for line in script.splitlines()
                             if line.strip() and not line.lstrip().startswith("#")]
                    for index, line in enumerate(lines):
                        if re.match(r"(?:python\b|\.\\.*python\.exe\b)", line):
                            commands.append(line)
                            self.assertLess(index + 1, len(lines), line)
                            self.assertEqual(lines[index + 1], "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }", line)
                self.assertEqual(len(commands), 10, commands)
                self.assertEqual(commands[0], "python -m venv .venv-audit")
                for command in commands[1:4]:
                    self.assertTrue(command.startswith(audit_interpreter + " -m "), command)
                rel_download, rel_install, rel_check = commands[1:4]
                self.assertIn(" -m pip download ", rel_download)
                self.assertIn(" -m pip install ", rel_install)
                for command in (rel_download, rel_install):
                    for flag in ("--only-binary=:all:", "--no-deps", "--require-hashes", "-r requirements-release.lock"):
                        self.assertIn(flag, command)
                self.assertIn("--dest .audit-wheelhouse", rel_download)
                self.assertIn("--no-index --find-links .audit-wheelhouse", rel_install)
                self.assertEqual(rel_check, audit_interpreter + " -m pip check")

                self.assertEqual(commands[4], "python -m venv .venv-lock-tools")
                for command in commands[5:8]:
                    self.assertTrue(command.startswith(tools_interpreter + " -m "), command)
                tools_download, tools_install, tools_check = commands[5:8]
                self.assertIn(" -m pip download ", tools_download)
                self.assertIn(" -m pip install ", tools_install)
                for command in (tools_download, tools_install):
                    for flag in ("--only-binary=:all:", "--no-deps", "--require-hashes", "-r requirements-lock-tools.lock"):
                        self.assertIn(flag, command)
                self.assertIn("--dest .lock-tools-wheelhouse", tools_download)
                self.assertIn("--no-index --find-links .lock-tools-wheelhouse", tools_install)
                self.assertEqual(tools_check, tools_interpreter + " -m pip check")

                self.assertEqual(commands[8], audit_interpreter + " -m pip_audit")
                self.assertEqual(
                    commands[9],
                    audit_interpreter + " -m pip_audit -r requirements-lock-tools.lock --no-deps --disable-pip",
                )
                for forbidden in ("--system-site-packages", "--user", "--target", "--ignore-vuln", "continue-on-error"):
                    self.assertNotIn(forbidden, audit_job)


if __name__ == "__main__":
    unittest.main()
