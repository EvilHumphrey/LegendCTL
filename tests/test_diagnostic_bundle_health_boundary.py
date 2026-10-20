"""Health-report ingestion stays bounded and does not follow Windows reparses."""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from zd_app.services.diagnostic_bundle import DiagnosticBundleService
from zd_app.storage import _import_guards


class HealthReportIngestionBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.health = self.root / "health_reports"
        self.health.mkdir()
        self.name = "zd_health_report_2026-10-20_120000.md"
        self.md = self.health / self.name
        self.md.write_text("# Health report\nordinary report\n", encoding="utf-8")
        self.md.with_suffix(".json").write_text(
            '{"overall_status":"ready","device":{"controller_name":"ordinary"}}',
            encoding="utf-8",
        )
        self.service = DiagnosticBundleService(
            base_dir=self.root / "bundles", health_report_dir=self.health
        )
        self.sentinel = "OUTSIDE_SENTINEL_DO_NOT_SHARE"
        logger_patch = patch("zd_app.services.diagnostic_bundle.service.logger")
        logger_patch.start()
        self.addCleanup(logger_patch.stop)

    def _preview_and_zip(self) -> tuple[set[str], str]:
        manifest = self.service.preview_bundle_manifest()
        target = self.root / "bundle.zip"
        self.assertEqual(self.service.generate_bundle_zip(target), target)
        with zipfile.ZipFile(target) as archive:
            names = set(archive.namelist())
            text = "\n".join(archive.read(name).decode("utf-8") for name in names)
        self.assertEqual(set(manifest.member_paths), names)
        self.assertNotIn(self.sentinel, text)
        return names, text

    def _symlink(self, link: Path, target: Path, *, directory: bool = False) -> None:
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

    def test_canonical_reports_remain_in_preview_and_sanitized_zip(self) -> None:
        self.md.write_text("# Health\nC:/Users/ExampleUser/private/report.txt", encoding="utf-8")
        names, text = self._preview_and_zip()
        self.assertIn(f"health_reports/{self.name}", names)
        self.assertIn(f"health_reports/{self.md.with_suffix('.json').name}", names)
        self.assertIn("ordinary", text)
        self.assertNotIn("ExampleUser", text)

    def test_noncanonical_names_are_not_selected_or_read(self) -> None:
        for name in (
            "foreign.md",
            "zd_health_report_2026-10-20_120000_backup.md",
            "zd_health_report_\uff12\uff10\uff12\uff16-10-20_120000.md",
            "zd_health_report_2026-10-20_120000.md.extra.md",
        ):
            (self.health / name).write_text(self.sentinel, encoding="utf-8")
        names, _text = self._preview_and_zip()
        self.assertEqual(
            {name for name in names if name.startswith("health_reports/")},
            {f"health_reports/{self.name}", f"health_reports/{self.md.with_suffix('.json').name}"},
        )

    def test_oversized_markdown_is_omitted_without_blocking_bundle(self) -> None:
        self.md.write_bytes(b"x" * (_import_guards.MAX_IMPORT_BYTES + 1))
        names, _text = self._preview_and_zip()
        self.assertNotIn(f"health_reports/{self.name}", names)

    def test_invalid_utf8_markdown_is_omitted_without_blocking_bundle(self) -> None:
        self.md.write_bytes(b"\xff\xfe")
        names, _text = self._preview_and_zip()
        self.assertNotIn(f"health_reports/{self.name}", names)

    def test_network_root_is_rejected_before_filesystem_discovery(self) -> None:
        self.service._health_report_dir = Path(r"\\fixture.invalid\share\health_reports")
        with (
            patch.object(Path, "exists", side_effect=AssertionError("probed network root")),
            patch.object(Path, "glob", side_effect=AssertionError("listed network root")),
        ):
            self.assertEqual(self.service._list_recent_health_reports(limit=5), [])

    @unittest.skipUnless(os.name == "nt", "Windows no-reparse boundary")
    def test_markdown_leaf_symlink_never_enters_preview_or_zip(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text(self.sentinel, encoding="utf-8")
        self.md.unlink()
        self._symlink(self.md, outside)
        names, _text = self._preview_and_zip()
        self.assertNotIn(f"health_reports/{self.name}", names)

    @unittest.skipUnless(os.name == "nt", "Windows no-reparse boundary")
    def test_json_leaf_symlink_never_enters_summary_preview_or_zip(self) -> None:
        outside = self.root / "outside.json"
        outside.write_text(
            '{"overall_status":"' + self.sentinel + '"}', encoding="utf-8"
        )
        sibling = self.md.with_suffix(".json")
        sibling.unlink()
        self._symlink(sibling, outside)
        self.assertNotIn(self.sentinel, self.service.generate_markdown())
        names, _text = self._preview_and_zip()
        self.assertIn(f"health_reports/{self.name}", names)
        self.assertNotIn(f"health_reports/{sibling.name}", names)

    def test_markdown_hardlink_never_enters_preview_or_zip(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text(self.sentinel, encoding="utf-8")
        self.md.unlink()
        self.md.hardlink_to(outside)
        self.assertEqual(self.md.stat().st_nlink, 2)
        names, _text = self._preview_and_zip()
        self.assertNotIn(f"health_reports/{self.name}", names)

    def test_json_hardlink_never_enters_summary_preview_or_zip(self) -> None:
        outside = self.root / "outside.json"
        outside.write_text(
            '{"overall_status":"' + self.sentinel + '"}', encoding="utf-8"
        )
        sibling = self.md.with_suffix(".json")
        sibling.unlink()
        sibling.hardlink_to(outside)
        self.assertEqual(sibling.stat().st_nlink, 2)
        self.assertNotIn(self.sentinel, self.service.generate_markdown())
        names, _text = self._preview_and_zip()
        self.assertIn(f"health_reports/{self.name}", names)
        self.assertNotIn(f"health_reports/{sibling.name}", names)

    @unittest.skipUnless(os.name == "nt", "Windows no-reparse boundary")
    def test_directory_junction_never_enters_summary_preview_or_zip(self) -> None:
        import _winapi

        self._check_directory_reparse(
            lambda outside: _winapi.CreateJunction(str(outside), str(self.health))
        )

    @unittest.skipUnless(os.name == "nt", "Windows no-reparse directory discovery")
    def test_directory_and_ancestor_junction_rejected_before_discovery(self) -> None:
        import _winapi

        outside = self.root / "fixture_outside"
        reports = outside / "reports"
        reports.mkdir(parents=True)
        (reports / self.name).write_text(self.sentinel, encoding="utf-8")
        for ancestor in (False, True):
            with self.subTest(ancestor=ancestor):
                junction = self.root / f"junction_{ancestor}"
                _winapi.CreateJunction(str(outside if ancestor else reports), str(junction))
                self.addCleanup(os.rmdir, junction)
                self.service._health_report_dir = junction / "reports" if ancestor else junction
                with (
                    patch.object(
                        Path, "glob", side_effect=AssertionError("enumerated a reparse path")
                    ),
                    patch.object(
                        _import_guards, "_query_windows_directory_names",
                        side_effect=AssertionError("queried a rejected directory"),
                        create=True,  # Also runs against the pre-fix implementation.
                    ),
                ):
                    self.assertEqual(self.service._list_recent_health_reports(limit=5), [])

    def test_directory_overflow_omits_reports_without_blocking_bundle(self) -> None:
        from zd_app.services.diagnostic_bundle import service

        def bounded_list(path):
            return _import_guards.list_guarded_directory(path, max_entries=1)

        with patch.object(service, "list_guarded_directory", side_effect=bounded_list):
            names, _text = self._preview_and_zip()
        self.assertFalse(any(name.startswith("health_reports/") for name in names))

    @unittest.skipUnless(os.name == "nt", "Windows no-reparse boundary")
    def test_directory_symlink_never_enters_summary_preview_or_zip(self) -> None:
        self._check_directory_reparse(
            lambda outside: self.health.symlink_to(outside, target_is_directory=True)
        )

    def _check_directory_reparse(self, create_reparse) -> None:
        outside = self.root / "outside"
        self.health.rename(outside)
        (outside / self.name).write_text(self.sentinel, encoding="utf-8")
        (outside / self.md.with_suffix(".json").name).write_text(
            '{"overall_status":"' + self.sentinel + '"}', encoding="utf-8"
        )
        try:
            create_reparse(outside)
        except OSError as exc:
            self.skipTest(f"directory reparse creation unavailable: {exc}")
        self.addCleanup(os.rmdir, self.health)
        self.assertNotIn(self.sentinel, self.service.generate_markdown())
        names, _text = self._preview_and_zip()
        self.assertFalse(any(name.startswith("health_reports/") for name in names))

    @unittest.skipUnless(os.name == "nt", "Windows no-reparse atomic open")
    def test_leaf_replaced_after_enumeration_is_not_read(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text(self.sentinel, encoding="utf-8")
        original_list = self.service._list_recent_health_reports
        replaced = False

        def enumerate_then_replace(*, limit: int) -> list[Path]:
            nonlocal replaced
            paths = original_list(limit=limit)
            if not replaced:
                self.md.unlink()
                self._symlink(self.md, outside)
                replaced = True
            return paths

        with patch.object(self.service, "_list_recent_health_reports", side_effect=enumerate_then_replace):
            names, _text = self._preview_and_zip()
        self.assertNotIn(f"health_reports/{self.name}", names)

    @unittest.skipUnless(os.name == "nt", "Windows no-reparse atomic open")
    def test_parent_replaced_between_path_check_and_open_is_not_read(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / self.name).write_text(self.sentinel, encoding="utf-8")
        (outside / self.md.with_suffix(".json").name).write_text(
            '{"overall_status":"' + self.sentinel + '"}', encoding="utf-8"
        )
        original_require = _import_guards.require_local_import_path
        replaced = False

        def check_then_replace(path: str | Path) -> str:
            nonlocal replaced
            checked = original_require(path)
            if Path(path).name == self.name and not replaced:
                self.health.rename(self.root / "original")
                self._symlink(self.health, outside, directory=True)
                self.addCleanup(os.rmdir, self.health)
                replaced = True
            return checked

        with patch.object(_import_guards, "require_local_import_path", side_effect=check_then_replace):
            names, _text = self._preview_and_zip()
        self.assertTrue(replaced)
        self.assertFalse(any(name.startswith("health_reports/") for name in names))


if __name__ == "__main__":
    unittest.main()
