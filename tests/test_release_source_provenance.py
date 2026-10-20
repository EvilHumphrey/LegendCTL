"""Pure fixture repositories: no compiler, dependency install, UI or device I/O."""
from __future__ import annotations

import ctypes
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
        self.write(".gitignore", ".venv-zd/\nbuild/\ndist/\nzd_data/\n__pycache__/\n*.pyd\n")
        self.write("main.py", "VALUE = 'committed'\n")
        self.write("zd_app/version.py", '__version__ = "1.2.3"\n__build_commit__ = ""\n__build_date__ = ""\n')
        self.commit()

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def commit(self, *, allow_empty=False):
        git(self.repo, "add", ".")
        git(self.repo, "commit", *(["--allow-empty"] if allow_empty else []), "-qm", "fixture")
        self.commit_id = git(self.repo, "rev-parse", "HEAD")


@unittest.skipUnless(exporter, "raw export helper not present on this revision")
class RawSourceTests(RepositoryFixture):
    @unittest.skipUnless(os.name == "nt", "Windows authentication diagnostics")
    def test_authentication_failure_reports_bounded_helper_reason(self):
        result = subprocess.CompletedProcess([], 7, b"", b"fixture authentication refused\n" + b"x" * 4000)
        with patch.object(exporter.subprocess, "run", return_value=result):
            with self.assertRaises(exporter.ExportError) as caught:
                exporter.trusted_git.__wrapped__()
        message = str(caught.exception)
        self.assertIn("authentication failed (exit 7): fixture authentication refused", message)
        self.assertLess(len(message), 2100)
        self.assertNotIn("\n", message)

    def test_direct_worker_rejects_changed_and_added_source_against_full_commit(self):
        for name, content in (("main.py", b"modified"), ("extra.py", b"inert fixture"),
                              ("assets/extra.dat", b"inert asset")):
            with self.subTest(name=name):
                source = self.root / ("source-" + name.replace("/", "-"))
                source.mkdir()
                with exporter.PathGuard() as guard:
                    exporter.export_tree(self.repo, self.commit_id, source, guard)
                exporter.verify_source(self.repo, source, self.commit_id)
                changed = source / name
                changed.parent.mkdir(parents=True, exist_ok=True)
                changed.write_bytes(content)
                with self.assertRaises(exporter.ExportError):
                    exporter.verify_source(self.repo, source, self.commit_id)

    def test_worker_identity_is_immutable_commit_not_later_head(self):
        source = self.root / "source"
        source.mkdir()
        original = self.commit_id
        with exporter.PathGuard() as guard:
            exporter.export_tree(self.repo, original, source, guard)
        self.write("main.py", "later committed bytes")
        self.commit()
        exporter.verify_source(self.repo, source, original)
        with self.assertRaisesRegex(exporter.ExportError, "Exported source changed"):
            exporter.verify_source(self.repo, source, self.commit_id)

    def test_worker_checks_bytes_even_when_size_and_timestamp_are_preserved(self):
        source = self.root / "source"
        source.mkdir()
        with exporter.PathGuard() as guard:
            exporter.export_tree(self.repo, self.commit_id, source, guard)
        changed = source / "main.py"
        before = changed.stat()
        changed.write_bytes(changed.read_bytes().replace(b"committed", b"corrupted"))
        os.utime(changed, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(changed.stat().st_size, before.st_size)
        with self.assertRaisesRegex(exporter.ExportError, "Exported source changed"):
            exporter.verify_source(self.repo, source, self.commit_id)

    def test_unapproved_git_path_is_rejected_without_execution(self):
        unapproved = self.root / ("git.exe" if os.name == "nt" else "git")
        unapproved.write_bytes(b"inert fixture, never an executable")
        with self.assertRaises(exporter.ExportError):
            exporter.trusted_git(str(unapproved))
        self.assertEqual(unapproved.read_bytes(), b"inert fixture, never an executable")

    def test_post_build_inventory_rejects_extra_inputs_outside_outputs(self):
        source = self.root / "source"
        source.mkdir()
        with exporter.PathGuard() as guard:
            manifest = exporter.export_tree(self.repo, self.commit_id, source, guard)
        version = source / "zd_app/version.py"
        version.write_bytes(version.read_bytes().replace(b'__build_commit__ = ""',
                            f'__build_commit__ = "{self.commit_id}"'.encode()))
        (source / "build").mkdir()
        (source / "dist").mkdir()
        exporter.verify_build_identity(source, self.commit_id, manifest)
        (source / "zd_app/extra.py").write_text("inert extra source")
        with self.assertRaisesRegex(exporter.ExportError, "Unexpected exported source file"):
            exporter.verify_build_identity(source, self.commit_id, manifest)

    def test_git_uses_authenticated_path_and_suppresses_execution_bearing_configuration(self):
        actual_git = exporter.trusted_git()
        real_run = subprocess.run
        calls = []

        def capture(args, **kwargs):
            calls.append((args, kwargs))
            return real_run(args, **kwargs)

        git(self.repo, "config", "core.fsmonitor", "inert-not-a-program")
        (self.root / ("git.exe" if os.name == "nt" else "git")).write_bytes(b"inert PATH decoy")
        with patch.object(exporter.subprocess, "run", side_effect=capture), patch.dict(
                os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor",
                             "GIT_CONFIG_VALUE_0": "inert-alternate", "GIT_EXEC_PATH": "inert-path",
                             "PATH": str(self.root)}):
            self.assertEqual(exporter.assert_clean_tree(self.repo), self.commit_id)
        self.assertTrue(calls)
        for args, kwargs in calls:
            self.assertEqual(args[0], actual_git)
            self.assertIn("core.fsmonitor=false", args)
            self.assertIn(f"core.hooksPath={os.devnull}", args)
            self.assertIn("protocol.allow=never", args)
            self.assertEqual(kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
            self.assertEqual(kwargs["env"]["GIT_ALLOW_PROTOCOL"], ":")
            self.assertNotIn("GIT_CONFIG_COUNT", kwargs["env"])
            self.assertNotIn("GIT_EXEC_PATH", kwargs["env"])

    def test_missing_promisor_blob_does_not_start_a_transport_helper(self):
        blob = git(self.repo, "rev-parse", "HEAD:main.py")
        missing_helper = self.root / "INERT-MISSING-HELPER.exe"
        self.assertFalse(missing_helper.exists())
        git(self.repo, "config", "remote.fixture.promisor", "true")
        git(self.repo, "config", "extensions.partialClone", "fixture")
        git(self.repo, "config", "protocol.ext.allow", "always")
        git(self.repo, "config", "remote.fixture.url", f"ext::{missing_helper.as_posix()}")
        missing_blob = self.repo / ".git/objects" / blob[:2] / blob[2:]
        missing_blob.chmod(0o600)
        missing_blob.unlink()
        output = exporter._git(self.repo, "cat-file", "--batch", data=blob.encode() + b"\n")
        self.assertEqual(output, blob.encode() + b" missing\n")
        with self.assertRaisesRegex(exporter.ExportError, "Raw Git blob unavailable"):
            list(exporter._blobs(self.repo, exporter._tree_entries(self.repo, self.commit_id)))
        # Even an explicit fetch cannot override the empty transport allowlist
        # through a protocol-specific local setting or a command-line override.
        with self.assertRaisesRegex(exporter.ExportError, "transport 'ext' not allowed"):
            exporter._git(self.repo, "-c", "protocol.ext.allow=always", "fetch", "fixture")
        self.assertFalse(missing_helper.exists())

    @unittest.skipUnless(os.name == "nt", "Windows short-path identity")
    def test_real_short_path_alias_is_the_same_clean_git_root(self):
        get_short = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
        get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        get_short.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_short(str(self.repo), buffer, len(buffer))
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())
        self.assertLess(length, len(buffer))
        alias = Path(buffer.value)
        reported = Path(git(alias, "rev-parse", "--show-toplevel"))
        if alias == reported:
            self.skipTest("fixture filesystem does not provide a distinct 8.3 alias")
        self.assertNotEqual(alias, reported)
        self.assertTrue(alias.samefile(reported))
        self.assertEqual(exporter.assert_clean_tree(alias), self.commit_id)
        # The alias must not bypass later content admission.
        self.write("main.py", "dirty through the long spelling")
        with self.assertRaisesRegex(exporter.ExportError, "Dirty tracked"):
            exporter.assert_clean_tree(alias)

    def test_subdirectory_and_different_git_reported_root_remain_rejected(self):
        with self.assertRaisesRegex(exporter.ExportError, "Git worktree root"):
            exporter.assert_clean_tree(self.repo / "zd_app")
        other_root = self.root / "repo-other"
        other_root.mkdir()
        real_git = exporter._git

        def other_report(repo, *args, **kwargs):
            if args == ("rev-parse", "--show-toplevel"):
                return str(other_root).encode() + b"\n"
            return real_git(repo, *args, **kwargs)

        with patch.object(exporter, "_git", side_effect=other_report):
            with self.assertRaisesRegex(exporter.ExportError, "Git worktree root"):
                exporter.assert_clean_tree(self.repo)

    @unittest.skipUnless(os.name == "nt", "Windows reparse alias boundary")
    def test_submitted_and_git_reported_junction_ancestors_remain_rejected(self):
        alias_parent = self.root / "junction-alias"
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
            "New-Item -ItemType Junction -Path $env:PROVENANCE_LINK -Target $env:PROVENANCE_TARGET | Out-Null"],
            env={**os.environ, "PROVENANCE_LINK": str(alias_parent), "PROVENANCE_TARGET": str(self.root)}, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        alias = alias_parent / self.repo.name
        real_git = exporter._git

        def alias_report(repo, *args, **kwargs):
            if args == ("rev-parse", "--show-toplevel"):
                return str(alias).encode() + b"\n"
            return real_git(repo, *args, **kwargs)

        try:
            self.assertTrue(alias.samefile(self.repo))
            with self.assertRaisesRegex(exporter.ExportError, "Reparse"):
                exporter.assert_clean_tree(alias)
            with patch.object(exporter, "_git", side_effect=alias_report):
                with self.assertRaisesRegex(exporter.ExportError, "Reparse"):
                    exporter.assert_clean_tree(self.repo)
        finally:
            alias_parent.rmdir()  # unlink only this task-owned junction

    def test_generated_runtime_data_is_never_read_or_exported(self):
        runtime = self.repo / "zd_data"
        self.write("zd_data/logs/zd_wrapper.log", "fixture runtime log")
        self.write("zd_data/crashes/_README.txt", "fixture crash notice")
        self.write("zd_data/crashes/_faulthandler.log", "fixture crash log")
        self.write("zd_data/nested/user-data.json", '{"fixture": true}')
        self.write("zd_data/nested/injected.py", 'raise AssertionError("must never run")')
        (runtime / "empty").mkdir()
        source = self.root / "source"
        source.mkdir()
        original_read = Path.read_bytes
        original_iterdir = Path.iterdir

        def without_runtime_read(path, *args, **kwargs):
            if path == runtime or runtime in path.parents:
                raise AssertionError("Generated runtime content must not be read")
            return original_read(path, *args, **kwargs)

        def without_runtime_walk(path):
            if path == runtime or runtime in path.parents:
                raise AssertionError("Generated runtime content must not be traversed")
            return original_iterdir(path)

        with patch.object(Path, "read_bytes", without_runtime_read), \
             patch.object(Path, "iterdir", without_runtime_walk):
            self.assertEqual(exporter.assert_clean_tree(self.repo), self.commit_id)
            with exporter.PathGuard() as guard:
                manifest = exporter.export_tree(self.repo, self.commit_id, source, guard)
                exporter.verify_snapshot(source, manifest)
        self.assertFalse((source / "zd_data").exists())
        self.assertFalse(any(name.startswith("zd_data/") for name in manifest))
        self.assertEqual((runtime / "nested/injected.py").read_text(), 'raise AssertionError("must never run")')
        self.write("injected.py", "outside generated root")
        with self.assertRaisesRegex(exporter.ExportError, "Unsafe untracked/ignored"):
            exporter.assert_clean_tree(self.repo)

    def test_tracked_runtime_file_still_requires_commit_identical_bytes(self):
        self.write("zd_data/committed-fixture.json", '{"fixture": true}')
        git(self.repo, "add", "-f", "zd_data/committed-fixture.json")
        git(self.repo, "commit", "-qm", "tracked runtime-named fixture")
        self.assertEqual(exporter.assert_clean_tree(self.repo), git(self.repo, "rev-parse", "HEAD"))
        self.write("zd_data/committed-fixture.json", '{"changed": true}')
        with self.assertRaisesRegex(exporter.ExportError, "Dirty tracked"):
            exporter.assert_clean_tree(self.repo)

    @unittest.skipUnless(os.name == "nt", "Windows generated-root junction boundary")
    def test_runtime_data_root_junction_is_rejected_without_touching_target(self):
        target = self.root / "outside-runtime"
        target.mkdir()
        sentinel = target / "keep.py"
        sentinel.write_text("fixture sentinel")
        junction = self.repo / "zd_data"
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
            "New-Item -ItemType Junction -Path $env:PROVENANCE_LINK -Target $env:PROVENANCE_TARGET | Out-Null"],
            env={**os.environ, "PROVENANCE_LINK": str(junction), "PROVENANCE_TARGET": str(target)}, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        try:
            with self.assertRaisesRegex(exporter.ExportError, "Reparse"):
                exporter.assert_clean_tree(self.repo)
            self.assertEqual(sentinel.read_text(), "fixture sentinel")
            self.assertEqual(list(target.iterdir()), [sentinel])
        finally:
            junction.rmdir()  # unlink only this task-owned junction

    @unittest.skipUnless(os.name == "nt", "Windows generated-root junction boundary")
    def test_generated_compiler_cache_root_junction_is_rejected(self):
        target = self.root / "outside-runtime"
        target.mkdir()
        sentinel = target / "keep.py"
        sentinel.write_text("fixture sentinel")
        junction = self.repo / ".release-tools"
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
            "New-Item -ItemType Junction -Path $env:PROVENANCE_LINK -Target $env:PROVENANCE_TARGET | Out-Null"],
            env={**os.environ, "PROVENANCE_LINK": str(junction), "PROVENANCE_TARGET": str(target)}, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        try:
            with self.assertRaisesRegex(exporter.ExportError, "Reparse"):
                exporter.assert_clean_tree(self.repo)
            self.assertEqual(sentinel.read_text(), "fixture sentinel")
            self.assertEqual(list(target.iterdir()), [sentinel])
        finally:
            junction.rmdir()  # unlink only this task-owned junction

    def test_orchestrator_binds_source_label_trust_record_and_artifact_receipt(self):
        real_run = subprocess.run
        original_version = (self.repo / "zd_app/version.py").read_bytes()
        self.write(".release-tools/inno-setup-6.7.1/ISCC.exe", "not source or executed by fixture")

        def fixture_packager(args, **kwargs):
            if args[0] != "powershell.exe":
                return real_run(args, **kwargs)
            source = Path(kwargs["cwd"])
            self.assertFalse((source / ".git").exists())
            self.assertEqual(args[args.index("-ExpectedCommit") + 1], self.commit_id)
            self.assertEqual(Path(args[args.index("-InnoToolchainRoot") + 1]), self.repo / ".release-tools")
            self.assertFalse((source / ".release-tools").exists())
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
        for helper in ("release_git.ps1", "release_paths.ps1"):
            shutil.copyfile(ROOT / "tools" / helper, self.repo / "tools" / helper)
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
            for name in ("build_release.ps1", "setup_dev_env.ps1", "release_git.ps1",
                         "release_paths.ps1", "install_local.ps1"):
                with self.subTest(host=host, script=name):
                    result = subprocess.run([host, "-NoProfile", "-Command",
                        "$tokens=$null; $parseErrors=$null; [void][System.Management.Automation.Language.Parser]::ParseFile($env:PROVENANCE_PARSE_FILE,[ref]$tokens,[ref]$parseErrors); if ($parseErrors.Count) { $parseErrors | Out-String | Write-Error; exit 1 }"],
                        env={**os.environ, "PROVENANCE_PARSE_FILE": str(ROOT / "tools" / name)}, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))


@unittest.skipUnless(os.name == "nt", "real Windows PowerShell worker prefix")
class WorkerMetadataTests(RepositoryFixture):
    """Execute the real worker only through lock admission and metadata stamp.

    The extracted prefix ends before the first pip/compiler invocation. A
    The source verifier runs on an empty temporary venv. No dependency,
    compiler, installer or product executable is invoked.
    """
    LOCK = b"fixture-package==1.2.3 \\\n    --hash=sha256:" + b"a" * 64 + b"\n"
    def worker_prefix(self, root: Path, marker_lock: bytes, *, mutation: str | None = None):
        self.write("requirements-release.lock", self.LOCK.decode())
        body = (ROOT / "tools/build_release.ps1").read_text(encoding="utf-8")
        prefix = body.split('Write-Host "Building ZDUltimateLegend', 1)[0]
        self.assertNotEqual(prefix, body, "worker extraction boundary changed")
        self.write("tools/build_release.ps1", prefix)
        for helper in ("export_git_tree.py", "release_git.ps1", "release_paths.ps1"):
            shutil.copyfile(ROOT / "tools" / helper, self.repo / "tools" / helper)
        self.commit(allow_empty=True)
        source = root / "source"
        source.mkdir()
        with exporter.PathGuard() as guard:
            exporter.export_tree(self.repo, self.commit_id, source, guard)
        if mutation == "hardlinked-version":
            original = source / "zd_app/version.py"
            outside = root / "outside-version.py"
            outside.write_bytes(original.read_bytes())
            original.unlink()
            os.link(outside, original)
        elif mutation:
            (source / mutation).write_text("inert unexpected input")
        version = source / "zd_app/version.py"
        worker = source / "tools/build_release.ps1"
        venv = root / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, capture_output=True)
        python = venv / "Scripts/python.exe"
        (venv / ".legendctl-release-lock.sha256").write_text(hashlib.sha256(marker_lock).hexdigest())
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 str(worker), "-FromVerifiedSnapshot", "-BuildPython", str(python),
                                 "-ExpectedCommit", self.commit_id, "-SourceRepository", str(self.repo),
                                 "-GitExecutable", exporter.trusted_git()], capture_output=True,
                                env={key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"})
        return result, version

    def test_ps51_stamp_is_accepted_by_real_trust_metadata_reader(self):
        with tempfile.TemporaryDirectory(prefix="legendctl-worker-prefix-") as temporary:
            result, version = self.worker_prefix(Path(temporary), self.LOCK)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            tool_spec = importlib.util.spec_from_file_location("prefix_trust_tool", ROOT / "tools/generate_trust_manifest.py")
            module = importlib.util.module_from_spec(tool_spec)
            tool_spec.loader.exec_module(module)
            self.assertEqual(module._version_metadata(version), ("1.2.3", self.commit_id))
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
                self.assertIn("does not match requirements-release.lock", " ".join(result.stderr.decode(errors="replace").split()))
                self.assertIn(b'__build_commit__ = ""', version.read_bytes())

    def test_direct_worker_rejects_dirty_and_extra_bytes_before_metadata_stamp(self):
        for mutation in ("main.py", "extra.py", "hardlinked-version"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(prefix="legendctl-worker-prefix-") as temporary:
                result, version = self.worker_prefix(Path(temporary), self.LOCK, mutation=mutation)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'__build_commit__ = ""', version.read_bytes())
                self.assertIn("source", result.stderr.decode(errors="replace").lower())


if __name__ == "__main__":
    unittest.main()
