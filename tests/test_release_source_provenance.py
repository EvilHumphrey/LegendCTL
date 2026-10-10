"""Pure fixture repositories: no compiler, dependency install, UI or device I/O."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools" / "export_git_tree.py"
spec = importlib.util.spec_from_file_location("release_export", HELPER)
if spec and spec.loader and HELPER.exists():
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
else:
    exporter = None


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True,
                            capture_output=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
    return result.stdout.decode().strip()


class RepositoryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="legendctl-provenance-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Fixture")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "config", "core.autocrlf", "false")
        self.write(".gitignore", ".venv-zd/\nbuild/\ndist/\n__pycache__/\n*.pyd\n")
        self.write("main.py", "VALUE = 'committed'\n")
        self.write("zd_app/version.py", '__version__ = "1.2.3"\n__build_commit__ = ""\n__build_date__ = ""\n')
        self.commit()

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def commit(self):
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "fixture")
        self.commit_id = git(self.repo, "rev-parse", "HEAD")


@unittest.skipUnless(exporter, "raw export helper not present on this revision")
class RawSourceTests(RepositoryFixture):
    def test_orchestrator_binds_source_label_trust_record_and_artifact_receipt(self):
        real_run = subprocess.run
        original_version = (self.repo / "zd_app/version.py").read_bytes()

        def fixture_packager(args, **kwargs):
            if args[0] != "powershell.exe":
                return real_run(args, **kwargs)
            source = Path(kwargs["cwd"])
            self.assertFalse((source / ".git").exists())
            self.assertEqual(args[-1], self.commit_id)
            self.assertNotIn("PYTHONPATH", kwargs["env"])
            version = source / "zd_app/version.py"
            version.write_bytes(original_version.replace(b'__build_commit__ = ""',
                                                         f'__build_commit__ = "{self.commit_id}"'.encode()))
            dist = source / "dist"
            trust = dist / "ZDUltimateLegend-v1.2.3/_internal/zd_app/trust_manifest.json"
            trust.parent.mkdir(parents=True)
            trust.write_text(json.dumps({"build_commit": self.commit_id, "build_commit_short": self.commit_id}))
            archive = dist / "fixture-windows.zip"
            archive.write_bytes(b"fixture compiler output")
            (dist / "SHA256SUMS.txt").write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  fixture-windows.zip\n")
            return subprocess.CompletedProcess(args, 0)

        real_mkdtemp = tempfile.mkdtemp
        with patch.object(exporter.tempfile, "mkdtemp", side_effect=lambda **kw: real_mkdtemp(dir=self.root, **kw)), \
             patch.object(exporter.subprocess, "run", side_effect=fixture_packager), \
             patch.dict(os.environ, {"PYTHONPATH": str(self.repo)}):
            exporter.run_build(self.repo, self.root / "output", Path(sys.executable))
        receipt = json.loads((self.root / "output/SOURCE_PROVENANCE.json").read_text())
        self.assertEqual(receipt["source_commit"], self.commit_id)
        self.assertEqual(receipt["raw_source_sha256"]["zd_app/version.py"], hashlib.sha256(original_version).hexdigest())
        self.assertEqual((self.repo / "zd_app/version.py").read_bytes(), original_version)
        self.assertTrue(receipt["complete"])

    def test_unsafe_git_paths_and_mutated_version_program_fail_closed(self):
        for name in ("../escape", "CON.py", "folder./file", "a:stream", "a\\escape", ".git/config"):
            with self.subTest(name=name), self.assertRaises(exporter.ExportError):
                exporter._safe_destination(self.root, name)
        source = self.root / "source"
        source.mkdir()
        raw = (self.repo / "zd_app/version.py").read_bytes()
        with exporter.PathGuard() as guard:
            manifest = exporter.export_tree(self.repo, self.commit_id, source, guard)
            (source / "zd_app/version.py").write_text(f'__build_commit__ = "{self.commit_id}"\nprint("injected")\n')
            with self.assertRaisesRegex(exporter.ExportError, "beyond build metadata"):
                exporter.verify_build_identity(source, self.commit_id, manifest, raw)

    def test_failure_retains_diagnostics_without_publishing_output(self):
        real_run = subprocess.run
        snapshots = []

        def fake_compiler(args, **kwargs):
            if args[0] != "powershell.exe":
                return real_run(args, **kwargs)
            source = Path(kwargs["cwd"])
            snapshots.append(source)
            (source / "compiler-diagnostic.txt").write_text("fixture compiler failure")
            return subprocess.CompletedProcess(args, 37)

        # Keep retained production diagnostics task-owned by this fixture.
        real_mkdtemp = tempfile.mkdtemp
        with patch.object(exporter.tempfile, "mkdtemp", side_effect=lambda **kw: real_mkdtemp(dir=self.root, **kw)), \
             patch.object(exporter.subprocess, "run", side_effect=fake_compiler):
            with self.assertRaisesRegex(exporter.ExportError, "exit 37"):
                exporter.run_build(self.repo, self.root / "output", Path(sys.executable))
        self.assertEqual((snapshots[0] / "compiler-diagnostic.txt").read_text(), "fixture compiler failure")
        self.assertFalse((self.root / "output").exists())
        self.assertEqual((self.repo / "zd_app/version.py").read_text().splitlines()[1], '__build_commit__ = ""')

    def test_output_parent_replacement_is_blocked_or_detected(self):
        parent = self.root / "artifact-parent"
        parent.mkdir()
        with exporter.PathGuard() as guard:
            guard.add(parent)
            if os.name == "nt":
                with self.assertRaises(OSError):
                    parent.rename(self.root / "old-parent")
            else:
                parent.rename(self.root / "old-parent")
                parent.mkdir()
                with self.assertRaisesRegex(exporter.ExportError, "replaced"):
                    guard.check()

    def test_clean_and_standard_generated_caches_are_allowed_but_not_exported(self):
        self.write("build/old.py", "not source")
        self.write("dist/old.exe", "not source")
        self.write("__pycache__/main.cpython-312.pyc", "cached")
        self.assertEqual(exporter.assert_clean_tree(self.repo), self.commit_id)
        source = self.root / "source"
        source.mkdir()
        with exporter.PathGuard() as guard:
            manifest = exporter.export_tree(self.repo, self.commit_id, source, guard)
            exporter.verify_snapshot(source, manifest)
        self.assertEqual((source / "main.py").read_bytes(), b"VALUE = 'committed'\n")
        self.assertFalse((source / "build").exists())
        self.assertFalse((source / "__pycache__").exists())

    def test_tracked_ignored_file_is_not_rejected_merely_for_matching_ignore(self):
        self.write("tracked.pyd", "committed fixture data")
        git(self.repo, "add", "-f", "tracked.pyd")
        git(self.repo, "commit", "-qm", "tracked ignored input")
        self.assertEqual(exporter.assert_clean_tree(self.repo), git(self.repo, "rev-parse", "HEAD"))
        self.write("tracked.pyd", "modified")
        with self.assertRaisesRegex(exporter.ExportError, "Dirty tracked"):
            exporter.assert_clean_tree(self.repo)

    def test_regular_bytecode_is_allowed_but_importable_code_in_cache_is_rejected(self):
        self.write("__pycache__/evil.py", "bad")
        with self.assertRaisesRegex(exporter.ExportError, "Unsafe untracked/ignored"):
            exporter.assert_clean_tree(self.repo)

    def test_crlf_checkout_is_allowed_only_by_text_policy(self):
        self.write(".gitattributes", "*.py text\n")
        self.commit()
        (self.repo / "main.py").write_bytes(b"VALUE = 'committed'\r\n")
        self.assertEqual(exporter.assert_clean_tree(self.repo), self.commit_id)

    def test_smudge_and_replace_refs_do_not_change_exported_raw_blobs(self):
        self.write(".gitattributes", "main.py filter=hostile\n")
        self.commit()
        original = self.commit_id
        git(self.repo, "config", "filter.hostile.smudge", "echo transformed")
        git(self.repo, "config", "filter.hostile.clean", "echo hidden")
        self.write("main.py", "VALUE = 'replacement'\n")
        self.commit()
        replacement = self.commit_id
        git(self.repo, "replace", original, replacement)
        source = self.root / "source"
        source.mkdir()
        with exporter.PathGuard() as guard:
            exporter.export_tree(self.repo, original, source, guard)
        self.assertEqual((source / "main.py").read_bytes(), b"VALUE = 'committed'\n")

    def test_inherited_git_index_or_directory_cannot_redirect_admission(self):
        self.write("main.py", "dirty")
        with patch.dict(os.environ, {"GIT_DIR": str(self.root / "absent"), "GIT_INDEX_FILE": str(self.root / "fake-index")}):
            with self.assertRaisesRegex(exporter.ExportError, "Dirty tracked"):
                exporter.assert_clean_tree(self.repo)

    def test_source_path_replacement_is_detected(self):
        folder = self.root / "source"
        folder.mkdir()
        with exporter.PathGuard() as guard:
            guard.add(folder)
            if os.name == "nt":
                with self.assertRaises(OSError):
                    folder.rename(self.root / "displaced")
            else:
                folder.rename(self.root / "displaced")
                folder.mkdir()
                with self.assertRaisesRegex(exporter.ExportError, "replaced"):
                    guard.check()

    @unittest.skipUnless(os.name == "nt", "Windows junction boundary")
    def test_output_root_junction_cannot_touch_target(self):
        target = self.root / "outside"
        target.mkdir()
        sentinel = target / "keep.txt"
        sentinel.write_text("keep")
        junction = self.root / "redirect"
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
            "New-Item -ItemType Junction -Path $env:PROVENANCE_LINK -Target $env:PROVENANCE_TARGET | Out-Null"],
            env={**os.environ, "PROVENANCE_LINK": str(junction), "PROVENANCE_TARGET": str(target)}, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        try:
            with self.assertRaisesRegex(exporter.ExportError, "Reparse"):
                exporter.run_build(self.repo, junction / "output", Path(sys.executable))
            self.assertEqual(sentinel.read_text(), "keep")
            self.assertEqual(list(target.iterdir()), [sentinel])
        finally:
            junction.rmdir()  # unlink the task-owned junction, never recurse

    def test_label_mismatch_and_changed_export_fail(self):
        source = self.root / "source"
        source.mkdir()
        with exporter.PathGuard() as guard:
            manifest = exporter.export_tree(self.repo, self.commit_id, source, guard)
            with self.assertRaisesRegex(exporter.ExportError, "label"):
                exporter.verify_build_identity(source, self.commit_id, manifest)
            (source / "zd_app/version.py").write_text(f'__build_commit__ = "{self.commit_id}"\n')
            self.assertTrue(exporter.verify_build_identity(source, self.commit_id, manifest))
            (source / "main.py").write_text("changed")
            with self.assertRaisesRegex(exporter.ExportError, "changed"):
                exporter.verify_build_identity(source, self.commit_id, manifest)

    def test_publish_receipt_binds_copied_artifact_hashes_and_preserves_existing_output(self):
        dist = self.root / "dist"
        dist.mkdir()
        archive = dist / "fixture-windows.zip"
        archive.write_bytes(b"fixture archive")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (dist / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n")
        output = self.root / "output"
        with exporter.PathGuard() as guard:
            exporter.publish_artifacts(dist, output, self.commit_id, guard)
            receipt = json.loads((output / "SOURCE_PROVENANCE.json").read_text())
            self.assertEqual(receipt["source_commit"], self.commit_id)
            self.assertEqual(receipt["build_commit"], self.commit_id)
            self.assertEqual(receipt["artifact_sha256"][archive.name], digest)
            with self.assertRaisesRegex(exporter.ExportError, "already exists"):
                exporter.publish_artifacts(dist, output, self.commit_id, guard)
        self.assertEqual((output / archive.name).read_bytes(), archive.read_bytes())

    def test_wrong_checksum_has_no_published_completion(self):
        dist = self.root / "dist"
        dist.mkdir()
        (dist / "fixture-windows.zip").write_bytes(b"bad")
        (dist / "SHA256SUMS.txt").write_text("0" * 64 + "  fixture-windows.zip\n")
        output = self.root / "output"
        with exporter.PathGuard() as guard:
            with self.assertRaisesRegex(exporter.ExportError, "SHA256SUMS"):
                exporter.publish_artifacts(dist, output, self.commit_id, guard)
        self.assertFalse(output.exists())


@unittest.skipUnless(os.name == "nt", "real PowerShell builder boundary")
class BuilderAdmissionTests(RepositoryFixture):
    """Also runs on the public baseline: its actual builder executes the trap.

    The trap is a local pip module, not a missing-helper assertion or real pip
    invocation. It exits before PyInstaller, networking or installation.
    """
    def setUp(self):
        super().setUp()
        (self.repo / "tools").mkdir()
        shutil.copyfile(ROOT / "tools/build_release.ps1", self.repo / "tools/build_release.ps1")
        if HELPER.exists():
            shutil.copyfile(HELPER, self.repo / "tools/export_git_tree.py")
        lock = self.write("requirements-release.lock", "fixture lock\n")
        self.write("pip.py", "from pathlib import Path\nPath('executed-unverified').write_text('live checkout executed')\nraise SystemExit(71)\n")
        self.commit()
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(self.repo / ".venv-zd")], check=True, capture_output=True)
        (self.repo / ".venv-zd/.legendctl-release-lock.sha256").write_text(hashlib.sha256(lock.read_bytes()).hexdigest())

    def assert_rejected_before_live_source_execution(self, diagnostic):
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 str(self.repo / "tools/build_release.ps1")], cwd=self.repo, capture_output=True,
                                env={key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"})
        text = (result.stdout + result.stderr).decode(errors="replace")
        self.assertNotEqual(result.returncode, 0, text)
        self.assertFalse((self.repo / "executed-unverified").exists(), "Builder executed unverified live source before rejecting it")
        self.assertIn(diagnostic, text)
        self.assertFalse((self.repo / "dist").exists())

    def test_dirty_tracked_rejected_before_build(self):
        self.write("main.py", "dirty")
        self.assert_rejected_before_live_source_execution("Dirty tracked")

    def test_staged_rejected_before_build(self):
        self.write("main.py", "staged")
        git(self.repo, "add", "main.py")
        self.assert_rejected_before_live_source_execution("Staged source")

    def test_untracked_import_rejected_before_build(self):
        self.write("injected.py", "untracked")
        self.assert_rejected_before_live_source_execution("Unsafe untracked/ignored")

    def test_ignored_import_rejected_before_build(self):
        self.write("injected.pyd", "ignored")
        self.assert_rejected_before_live_source_execution("Unsafe untracked/ignored")

    def test_assume_unchanged_rejected_before_build(self):
        git(self.repo, "update-index", "--assume-unchanged", "main.py")
        self.write("main.py", "hidden")
        self.assert_rejected_before_live_source_execution("Exceptional index flags")

    def test_skip_worktree_rejected_before_build(self):
        git(self.repo, "update-index", "--skip-worktree", "main.py")
        self.write("main.py", "hidden")
        self.assert_rejected_before_live_source_execution("Exceptional index flags")


class PowerShellParserTests(unittest.TestCase):
    def test_release_workflow_keeps_test_log_outside_source_with_same_evaluator_path(self):
        workflow = (ROOT / ".github/workflows/release-build.yml").read_text(encoding="utf-8")
        step = workflow.split("- name: Run test suite before building", 1)[1].split("\n      - name:", 1)[0]
        self.assertIn('$testLog = Join-Path $env:RUNNER_TEMP "legendctl-release-tests.txt"', step)
        self.assertIn("Tee-Object -FilePath $testLog", step)
        self.assertIn("-LogPath $testLog", step)
        self.assertIn("-ProcessExitCode $code", step)
        self.assertIn("-MinimumTestCount 3000", step)
        self.assertIn("exit $LASTEXITCODE", step)
        self.assertNotIn("test_output.txt", step)

    def test_builder_never_recursively_replaces_checkout_output_roots(self):
        # Source contract with a concrete failing baseline operation: its
        # Remove-Item -Recurse build/dist traverses caller-owned output paths.
        body = (ROOT / "tools/build_release.ps1").read_text(encoding="utf-8")
        self.assertNotRegex(body, r"Remove-Item\s+-Recurse\s+-Force\s+\"(?:build|dist)\"")

    def test_label_is_not_taken_from_unverified_live_head(self):
        # Baseline binds the label to HEAD while compiling the mutable cwd.
        body = (ROOT / "tools/build_release.ps1").read_text(encoding="utf-8")
        self.assertNotRegex(body, r"\$buildCommit\s*=\s*\(& git rev-parse --short HEAD\)")
        self.assertIn("$buildCommit = $ExpectedCommit", body)

    def test_release_and_setup_parse_in_available_powershell_hosts(self):
        hosts = [host for host in ("powershell.exe", "pwsh.exe") if shutil.which(host)]
        if not hosts:
            self.skipTest("PowerShell unavailable")
        for host in hosts:
            for name in ("build_release.ps1", "setup_dev_env.ps1"):
                with self.subTest(host=host, script=name):
                    result = subprocess.run([host, "-NoProfile", "-Command",
                        "$tokens=$null; $parseErrors=$null; [void][System.Management.Automation.Language.Parser]::ParseFile($env:PROVENANCE_PARSE_FILE,[ref]$tokens,[ref]$parseErrors); if ($parseErrors.Count) { $parseErrors | Out-String | Write-Error; exit 1 }"],
                        env={**os.environ, "PROVENANCE_PARSE_FILE": str(ROOT / "tools" / name)}, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))


@unittest.skipUnless(os.name == "nt", "real Windows PowerShell worker prefix")
class WorkerMetadataTests(unittest.TestCase):
    """Execute the real worker only through lock admission and metadata stamp.

    The extracted prefix ends before the first pip/compiler invocation. A
    placeholder executable is sufficient: these tests never launch it.
    """
    LOCK = b"fixture-package==1.2.3 \\\n    --hash=sha256:" + b"a" * 64 + b"\n"
    COMMIT = "1234567" + "a" * 33

    def worker_prefix(self, root: Path, marker_lock: bytes):
        source = root / "source"
        (source / "tools").mkdir(parents=True)
        (source / "zd_app").mkdir()
        version = source / "zd_app/version.py"
        version.write_bytes(b'__version__ = "1.2.3"\n__build_commit__ = ""\n__build_date__ = ""\n')
        (source / "requirements-release.lock").write_bytes(self.LOCK)
        python = root / "venv/Scripts/python.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"not executed by this prefix fixture")
        (root / "venv/.legendctl-release-lock.sha256").write_text(hashlib.sha256(marker_lock).hexdigest())
        body = (ROOT / "tools/build_release.ps1").read_text(encoding="utf-8")
        prefix = body.split('Write-Host "Building ZDUltimateLegend', 1)[0]
        self.assertNotEqual(prefix, body, "worker extraction boundary changed")
        worker = source / "tools/build_release.ps1"
        worker.write_text(prefix, encoding="utf-8")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 str(worker), "-FromVerifiedSnapshot", "-BuildPython", str(python),
                                 "-ExpectedCommit", self.COMMIT], capture_output=True,
                                env={key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"})
        return result, version

    def test_ps51_stamp_is_accepted_by_real_trust_metadata_reader(self):
        with tempfile.TemporaryDirectory(prefix="legendctl-worker-prefix-") as temporary:
            result, version = self.worker_prefix(Path(temporary), self.LOCK)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            tool_spec = importlib.util.spec_from_file_location("prefix_trust_tool", ROOT / "tools/generate_trust_manifest.py")
            module = importlib.util.module_from_spec(tool_spec)
            tool_spec.loader.exec_module(module)
            self.assertEqual(module._version_metadata(version), ("1.2.3", self.COMMIT))
            self.assertFalse(version.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_legacy_crlf_checkout_marker_accepts_identical_raw_lf_dependencies(self):
        with tempfile.TemporaryDirectory(prefix="legendctl-worker-prefix-") as temporary:
            result, _ = self.worker_prefix(Path(temporary), self.LOCK.replace(b"\n", b"\r\n"))
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_pin_hash_and_non_newline_edits_never_match_environment_marker(self):
        for changed in (self.LOCK.replace(b"1.2.3", b"1.2.4"),
                        self.LOCK.replace(b"a" * 64, b"b" * 64),
                        self.LOCK.replace(b"package", b"pack\rage")):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory(prefix="legendctl-worker-prefix-") as temporary:
                result, version = self.worker_prefix(Path(temporary), changed)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("does not match requirements-release.lock", result.stderr.decode(errors="replace"))
                self.assertIn(b'__build_commit__ = ""', version.read_bytes())


if __name__ == "__main__":
    unittest.main()
