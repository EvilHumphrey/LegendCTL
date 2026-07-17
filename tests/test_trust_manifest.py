"""Tests for frozen-build trust-record generation and payload verification."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zd_app.services import trust_self_check


_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOL_PATH = _REPO_ROOT / "tools" / "generate_trust_manifest.py"
_NOW = datetime(2026, 7, 12, 9, 30, tzinfo=timezone.utc)
_COMMIT = "abcdef0" + "1" * 33
_SHORT_COMMIT = "abcdef0"


def _tool_module():
    spec = importlib.util.spec_from_file_location("generate_trust_manifest_test", _TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TrustManifestGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = _tool_module()

    def _mini_tree(self, root: Path) -> tuple[Path, Path]:
        repo = root / "repo"
        package = repo / "zd_app"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "clean.py").write_text("value = 1\n", encoding="utf-8")
        (package / "handoff.py").write_text(
            "import webbrowser\nwebbrowser.open('https://example.invalid')\n",
            encoding="utf-8",
        )
        (package / "version.py").write_text(
            '__version__ = "9.9.9"\n__build_commit__ = "abcdef0"\n',
            encoding="utf-8",
        )
        (repo / "main_zd.py").write_text("main = True\n", encoding="utf-8")
        dist = root / "dist" / "ZDUltimateLegend"
        (dist / "_internal").mkdir(parents=True)
        (dist / "ZD Ultimate Legend.exe").write_bytes(b"fake exe")
        (dist / "_internal" / "payload.dat").write_bytes(b"payload")
        return repo, dist

    def _generate(self, repo: Path, dist: Path) -> Path:
        with patch.object(self.tool, "_git", side_effect=[_COMMIT, _SHORT_COMMIT]):
            return self.tool.generate_manifest(
                repo_root=repo,
                dist_root=dist,
                generated_at=_NOW,
                scanner_repo_root=_REPO_ROOT,
            )

    def test_generator_records_real_scanners_and_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, dist = self._mini_tree(Path(temporary))
            manifest_path = self._generate(repo, dist)
            first = manifest_path.read_bytes()
            document = json.loads(first)

            self.assertEqual(document["schema"], 1)
            self.assertEqual(document["build_commit"], _COMMIT)
            self.assertEqual(document["build_commit_short"], _SHORT_COMMIT)
            self.assertEqual(document["source_scan"]["network_import_findings"], [])
            self.assertEqual(document["source_scan"]["driver_footprint_findings"], [])
            self.assertEqual(
                document["source_scan"]["browser_handoffs"],
                [
                    {
                        "relative_path": "handoff.py",
                        "line": 2,
                        "call": "webbrowser.open",
                    }
                ],
            )
            self.assertIn("main_zd.py", document["source_scan"]["source_files"])
            self.assertIn("zd_app/clean.py", document["source_scan"]["source_files"])
            self.assertIn("ZD Ultimate Legend.exe", document["payload_files"])
            self.assertNotIn("_internal/zd_app/trust_manifest.json", document["payload_files"])
            self.assertNotIn(b"\r\n", first)

            self._generate(repo, dist)
            self.assertEqual(manifest_path.read_bytes(), first)

    def test_generator_fails_closed_for_source_scan_problems(self) -> None:
        cases = {
            "network": lambda repo: (repo / "zd_app" / "clean.py").write_text(
                "import socket\n", encoding="utf-8"
            ),
            "driver": lambda repo: (repo / "zd_app" / "bridge.sys").write_bytes(b"x"),
            "parse": lambda repo: (repo / "zd_app" / "clean.py").write_text(
                "def broken(:\n", encoding="utf-8"
            ),
            "empty": lambda repo: [
                path.unlink()
                for path in (repo / "zd_app").glob("*.py")
            ]
            + [(repo / "main_zd.py").unlink()],
        }
        for name, mutate in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                repo, dist = self._mini_tree(Path(temporary))
                mutate(repo)
                with self.assertRaises(self.tool.ManifestBuildError):
                    self.tool.generate_manifest(
                        repo_root=repo,
                        dist_root=dist,
                        scanner_repo_root=_REPO_ROOT,
                    )

    def test_cli_returns_nonzero_when_generation_fails(self) -> None:
        with patch.object(
            self.tool,
            "generate_manifest",
            side_effect=self.tool.ManifestBuildError("blocked"),
        ), redirect_stderr(io.StringIO()):
            self.assertEqual(
                self.tool.main(["--dist-root", "dist", "--repo-root", "."]),
                1,
            )


class PayloadVerificationTests(unittest.TestCase):
    def _manifest(self, package_root: Path, payload_files: dict[str, str]) -> trust_self_check.TrustManifest:
        data = {
            "schema": 1,
            "version": "9.9.9",
            "build_commit": _COMMIT,
            "build_commit_short": _SHORT_COMMIT,
            "build_date": "2026-07-12",
            "generated_at": "2026-07-12T09:30:00+00:00",
            "source_scan": {
                "ruleset": {
                    "network_roots": list(trust_self_check.NETWORK_IMPORT_ROOTS),
                    "driver_suffixes": list(trust_self_check.DRIVER_ARTIFACT_SUFFIXES),
                    "virtual_device_tokens": list(
                        trust_self_check.VIRTUAL_DEVICE_NAME_TOKENS
                    ),
                },
                "python_file_count": 2,
                "parse_failures": [],
                "entry_module_scanned": True,
                "network_import_findings": [],
                "browser_handoffs": [],
                "driver_footprint_findings": [],
                "source_files": {
                    "main_zd.py": "0" * 64,
                    "zd_app/__init__.py": "1" * 64,
                },
            },
            "payload_files": payload_files,
        }
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / trust_self_check.TRUST_MANIFEST_FILENAME).write_text(
            json.dumps(data), encoding="utf-8"
        )
        manifest = trust_self_check.load_trust_manifest(package_root)
        assert manifest is not None
        self.assertTrue(manifest.integrity.valid)
        return manifest

    def test_loader_fails_closed_for_invalid_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "zd_app"
            self.assertIsNone(trust_self_check.load_trust_manifest(package))
            package.mkdir()
            path = package / trust_self_check.TRUST_MANIFEST_FILENAME
            for raw in (
                "{",
                json.dumps({"schema": 2}),
                json.dumps({"schema": 1}),
            ):
                with self.subTest(raw=raw):
                    path.write_text(raw, encoding="utf-8")
                    manifest = trust_self_check.load_trust_manifest(package)
                    assert manifest is not None
                    self.assertFalse(manifest.integrity.valid)
                    self.assertEqual(manifest.integrity.reason, "invalid")

    def test_payload_verification_matches_mismatches_missing_and_extras(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary) / "dist"
            package = dist / "_internal" / "zd_app"
            dist.mkdir(parents=True)
            executable = dist / "LegendCTL.exe"
            payload = dist / "_internal" / "payload.dat"
            executable.write_bytes(b"exe")
            payload.parent.mkdir()
            payload.write_bytes(b"payload")
            manifest = self._manifest(
                package,
                {
                    "LegendCTL.exe": "0" * 64,
                    "_internal/payload.dat": _sha256(payload),
                },
            )

            verification = trust_self_check.verify_payload_against_manifest(
                manifest, dist, running_executable=executable
            )
            self.assertEqual(verification.matched, 1)
            self.assertEqual(verification.total, 1)
            self.assertTrue(verification.clean)

            payload.write_bytes(b"changed")
            mismatch = trust_self_check.verify_payload_against_manifest(
                manifest, dist, running_executable=executable
            )
            self.assertEqual(mismatch.mismatched, ("_internal/payload.dat",))

            payload.unlink()
            missing = trust_self_check.verify_payload_against_manifest(
                manifest, dist, running_executable=executable
            )
            self.assertEqual(missing.missing, ("_internal/payload.dat",))

            (dist / "extra.dll").write_bytes(b"extra")
            (dist / "unins000.dat").write_bytes(b"installer")
            (dist / "session.log").write_bytes(b"log")
            extras = trust_self_check.verify_payload_against_manifest(
                manifest, dist, running_executable=executable
            )
            self.assertEqual(extras.extra_count, 1)


if __name__ == "__main__":
    unittest.main()
