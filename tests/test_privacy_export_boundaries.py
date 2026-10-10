"""Exercise export boundaries with real source files and ZIP serialization."""

import json
import os
import dataclasses
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from zd_app.models import DeviceState
from zd_app.services.diagnostic_bundle.service import DiagnosticBundleService, _BundleMember
from zd_app.services.diagnostics_service import DiagnosticsService
from zd_app.services.module_passport import ModulePassportService
from zd_app.storage._import_guards import MAX_IMPORT_BYTES


class ExportBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reports = self.root / "health"
        self.reports.mkdir()
        self.service = DiagnosticBundleService(
            base_dir=self.root / "bundles", health_report_dir=self.reports
        )

    def report(self, stem="health", payload=None):
        (self.reports / f"{stem}.md").write_text("# Health\n", encoding="utf-8")
        if payload is not None:
            (self.reports / f"{stem}.json").write_text(payload, encoding="utf-8")

    def test_copied_json_decodes_escaped_keys_and_preserves_scalar_types(self):
        raw = {
            r"C:\Users\privacy-test\key.json": [{
                "id": r"HID\VID_413D&PID_2104\UNIT-PRIVATE",
                "path": r"C:\Users\privacy-test\capture\trace.json",
                "count": 3, "ratio": 0.25, "ready": True, "missing": None,
                "model": "VID_413D&PID_2104", "name": "operator-module-7",
            }]
        }
        # Literal JSON Unicode escapes evade a text-only path scrubber.
        self.report(payload=json.dumps(raw).replace("\\\\", "\\u005c"))
        target = self.root / "export.zip"
        self.assertEqual(self.service.generate_bundle_zip(target), target)
        with zipfile.ZipFile(target) as archive:
            text = archive.read("health_reports/health.json").decode()
            result = json.loads(text)
        self.assertNotIn("privacy-test", text)
        self.assertNotIn("UNIT-PRIVATE", text)
        row = result["key.json"][0]
        self.assertEqual(row["path"], "trace.json")
        self.assertEqual(row["model"], "VID_413D&PID_2104")
        self.assertEqual(row["name"], "operator-module-7")
        self.assertIs(type(row["count"]), int)
        self.assertIs(type(row["ratio"]), float)
        self.assertIs(row["ready"], True)
        self.assertIsNone(row["missing"])

    def test_invalid_oversized_deep_nonfinite_and_colliding_copied_json_omitted_honestly(self):
        cases = [
            "{invalid", '"' + "a" * MAX_IMPORT_BYTES + '"',
            "[" * 100 + "]" * 100, '{"value":NaN}',
            json.dumps({r"C:\one\same.json": 1, r"D:\two\same.json": 2}),
        ]
        for payload in cases:
            with self.subTest(payload_size=len(payload)):
                self.report(payload=payload)
                target = self.root / "export.zip"
                self.assertEqual(self.service.generate_bundle_zip(target), target)
                with zipfile.ZipFile(target) as archive:
                    self.assertNotIn("health_reports/health.json", archive.namelist())
                    self.assertIn("JSON sibling unreadable", archive.read("report.md").decode())
                    self.assertIn("health_reports/health.md", archive.namelist())

    def test_real_health_filenames_colliding_after_sanitation_preserve_target(self):
        for stem in ("VID_413D&PID_2104#UNIT-A", "VID_413D&PID_2104#UNIT-B"):
            self.report(stem)
        self.assert_failed_export_preserves_target()
        with self.assertRaises(ValueError):
            self.service.preview_bundle_manifest()

    def test_filename_instance_tail_removed_and_extension_preserved(self):
        self.report("VID_413D&PID_2104#UNIT-PRIVATE")
        target = self.root / "export.zip"
        self.assertEqual(self.service.generate_bundle_zip(target), target)
        with zipfile.ZipFile(target) as archive:
            self.assertIn("health_reports/VID_413D&PID_2104.md", archive.namelist())
            self.assertNotIn("UNIT-PRIVATE", "\n".join(archive.namelist()))
        self.assertEqual(self.service.preview_bundle_manifest().member_paths, tuple(archive.namelist()))

    def assert_failed_export_preserves_target(self):
        for exists in (False, True):
            target = self.root / ("existing.zip" if exists else "missing.zip")
            if exists:
                target.write_bytes(b"original user output")
            self.assertIsNone(self.service.generate_bundle_zip(target))
            if exists:
                self.assertEqual(target.read_bytes(), b"original user output")
            else:
                self.assertFalse(target.exists())

    def test_final_zip_boundary_rejects_unsafe_paths_casefold_and_invalid_text(self):
        # Supply a prospective producer, retaining the real final validation,
        # serialization and ZipFile path. Do not mock the changed boundary.
        cases = [[_BundleMember(path, "{}", "health_report")] for path in (
            "../outside.json", "health/../outside.json", "/absolute.json",
            r"\rooted.json", r"C:\absolute.json", "C:relative.json",
            "health/file.json:stream", "health//file.json", "health/./file.json",
            "health/CON.json", "health/file.json.", "health/file\x00.json",
        )]
        cases += [
            [_BundleMember("Report.md", "safe", "report"), _BundleMember("report.MD", "safe", "report")],
            [_BundleMember("a.md", "safe", "report"), _BundleMember("a.md/child.md", "safe", "report")],
            [_BundleMember("raw.json", "{bad", "report")],
            [_BundleMember("raw.md", "\ud800", "report")],
            [_BundleMember("raw.json", json.dumps({r"C:\a\same": 1, r"D:\b\same": 2}), "report")],
        ]
        for members in cases:
            with self.subTest(paths=[m.path for m in members]):
                with patch.object(self.service, "_bundle_members", return_value=members):
                    self.assert_failed_export_preserves_target()

    def test_legacy_json_export_sanitizes_other_device_fields(self):
        state = DeviceState(firmware_version=r"HID\VID_413D&PID_2104\UNIT-PRIVATE")
        with patch.dict(os.environ, {"ZDUL_DATA_DIR": str(self.root)}):
            path = DiagnosticsService().export_bundle(state, 0.5, None, str(self.root))
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("UNIT-PRIVATE", text)
        data = json.loads(text)
        self.assertEqual(data["snapshot"]["last_read_duration_ms"], 0.5)
        self.assertIsNone(data["snapshot"]["last_write_duration_ms"])


class ClipboardBoundaryTests(unittest.TestCase):
    def test_event_copy_uses_sanitized_text_and_preserves_plain_events(self):
        from tests.r2_shell_test_helpers import make_shell
        from tests.test_screens_diagnostics import _DiagnosticsChildWindowRecorder
        from zd_app import i18n
        from zd_app.ui.screens import diagnostics

        i18n.set_locale("en")
        shell = make_shell(settings_service=MagicMock())
        shell.device_service.recent_events = MagicMock(return_value=[
            "Connected successfully", r"HID\VID_413D&PID_2104\UNIT-PRIVATE",
            r"Loaded C:\Users\privacy-test\capture\trace.json",
        ])
        with _DiagnosticsChildWindowRecorder():
            diagnostics.build(shell, "content_region")
            buttons = diagnostics.dpg.add_button.call_args_list
            callback = next(c.kwargs["callback"] for c in buttons if c.kwargs.get("label") == i18n.t("diagnostics.event_log.copy"))
            callback()
            copied = diagnostics.dpg.set_clipboard_text.call_args.args[0]
        self.assertIn("Connected successfully", copied)
        self.assertIn("VID_413D&PID_2104", copied)
        self.assertIn("trace.json", copied)
        self.assertNotIn("UNIT-PRIVATE", copied)
        self.assertNotIn("privacy-test", copied)


class ReviewedPrivacyBypassTests(unittest.TestCase):
    setUp = ExportBoundaryTests.setUp
    report = ExportBoundaryTests.report

    def test_qualified_instance_tails_removed_from_copied_markdown_and_nested_json(self):
        identifiers = [
            r"\\?\HID#VID_413D&PID_2104&IG_00#PRIVATEHASH#{1234}",
            r"HID\VID_413D&PID_2104&MI_00&Col01\PRIVATEHASH",
            r"USB\VID_413D&PID_2104&REV_0100\PRIVATEHASH",
            r"\\?\hid#vid_413d&pid_2104&mi_00&col01#PRIVATEHASH",
        ]
        for identifier in identifiers:
            with self.subTest(identifier=identifier):
                self.report(payload=json.dumps({"nested": [{"device": identifier}]}))
                (self.reports / "health.md").write_text(identifier, encoding="utf-8")
                target = self.root / "qualified.zip"
                self.assertEqual(self.service.generate_bundle_zip(target), target)
                with zipfile.ZipFile(target) as archive:
                    for member in ("health_reports/health.md", "health_reports/health.json"):
                        with self.subTest(member=member):
                            text = archive.read(member).decode()
                            self.assertNotIn("PRIVATEHASH", text)
                            self.assertIn("vid_413d&pid_2104", text.lower())
                    parsed = json.loads(archive.read("health_reports/health.json"))
                    self.assertIn("vid_413d&pid_2104", parsed["nested"][0]["device"].lower())

    def test_qualifier_recognition_keeps_adjacent_ordinary_status(self):
        from zd_app.services.path_scrub import sanitize_shareable_text

        for text in ("VID_413D&PID_2104&status=failed", "VID_413D&PID_2104&status_failed", "ordinary module name"):
            self.assertEqual(sanitize_shareable_text(text), text)

    def test_archive_timestamp_scrubbed_before_filename_derivation(self):
        passports = ModulePassportService(base_dir=self.root / "passports")
        passports.archive_dir.mkdir(parents=True, exist_ok=True)
        source = passports.archive_dir / "archived.json"
        self.service = DiagnosticBundleService(
            base_dir=self.root / "bundles", module_passport_service=passports
        )
        for stamp, expected in (
            (r"C:\Users\PRIVATEACCOUNT\capture\run.json", "left_stock_run.json_000.json"),
            ("2026-05-26T18:00:00Z", "left_stock_2026-05-26T18_00_00Z_000.json"),
        ):
            with self.subTest(stamp=stamp):
                raw = json.dumps({"side": "left", "module_id": "stock", "assigned_at_utc": stamp, "notes": ""})
                source.write_text(raw, encoding="utf-8")
                manifest = self.service.preview_bundle_manifest()
                with self.subTest(boundary="preview"):
                    self.assertNotIn("PRIVATEACCOUNT", repr(manifest))
                    self.assertIn("module_passports/archive/" + expected, manifest.member_paths)
                target = self.root / "passport.zip"
                self.assertEqual(self.service.generate_bundle_zip(target), target)
                with zipfile.ZipFile(target) as archive:
                    self.assertEqual(tuple(archive.namelist()), manifest.member_paths)
                    for name in archive.namelist():
                        with self.subTest(boundary="zip", member=name):
                            self.assertNotIn("PRIVATEACCOUNT", name)
                            self.assertNotIn("PRIVATEACCOUNT", archive.read(name).decode())
                self.assertEqual(source.read_text(encoding="utf-8"), raw)


class IdentifierCharacterBoundaryTests(unittest.TestCase):
    setUp = ExportBoundaryTests.setUp
    report = ExportBoundaryTests.report

    def test_every_legal_windows_identifier_character_is_consumed_in_tail(self):
        from zd_app.services.path_scrub import sanitize_shareable_text

        # IRP_MN_QUERY_ID: legal characters are 0x21..0x7f except comma.
        for codepoint in range(0x21, 0x80):
            if codepoint == 0x2c:
                continue
            with self.subTest(codepoint=codepoint):
                identifier = "HID\\VID_413D&PID_2104\\UNIT" + chr(codepoint) + "PRIVATE"
                self.assertEqual(sanitize_shareable_text(identifier), r"HID\VID_413D&PID_2104")

    def test_pipe_and_backtick_do_not_leak_through_actual_export_boundaries(self):
        from tests.test_health_report_exporters import _fixture_report
        from zd_app.services.health_report.json_export import to_json
        from zd_app.services.health_report.markdown_export import to_markdown

        fixture = _fixture_report()
        for delimiter in ("|", "`"):
            with self.subTest(delimiter=delimiter):
                identifier = "HID\\VID_413D&PID_2104&IG_00\\UNIT" + delimiter + "PRIVATE"
                self.report(payload=json.dumps({"nested": [{"id": identifier}]}))
                # Already-formatted imported Markdown gets the same privacy
                # priority; a punctuation character is not proof of a boundary.
                (self.reports / "health.md").write_text("`" + identifier + "`", encoding="utf-8")
                target = self.root / "punctuation.zip"
                self.assertEqual(self.service.generate_bundle_zip(target), target)
                with zipfile.ZipFile(target) as archive:
                    for name in ("health_reports/health.md", "health_reports/health.json"):
                        with self.subTest(boundary=name):
                            text = archive.read(name).decode()
                            self.assertNotIn("PRIVATE", text)
                            self.assertIn("VID_413D&PID_2104", text)
                report = dataclasses.replace(fixture, device=dataclasses.replace(fixture.device, controller_name=identifier))
                for text in (to_json(report), to_markdown(report)):
                    with self.subTest(boundary="standalone", text=text[:20]):
                        self.assertNotIn("PRIVATE", text)
                        self.assertIn("VID_413D&PID_2104", text)

    def test_backtick_filename_tail_is_removed_in_zip_and_preview(self):
        self.report("VID_413D&PID_2104#UNIT`PRIVATE", payload="{}")
        target = self.root / "filename.zip"
        self.assertEqual(self.service.generate_bundle_zip(target), target)
        with zipfile.ZipFile(target) as archive:
            with self.subTest(boundary="zip"):
                self.assertNotIn("PRIVATE", repr(archive.namelist()))
                self.assertIn("health_reports/VID_413D&PID_2104.md", archive.namelist())
            with self.subTest(boundary="preview"):
                paths = self.service.preview_bundle_manifest().member_paths
                self.assertNotIn("PRIVATE", repr(paths))
                self.assertEqual(tuple(archive.namelist()), paths)

    def test_real_boundaries_and_unrelated_prose_remain_intact(self):
        from zd_app.services.path_scrub import sanitize_shareable_text

        for separator in (" ", "\t", "\n", ",", "\u0080"):
            with self.subTest(separator=separator):
                text = r"HID\VID_413D&PID_2104\UNIT" + separator + "ordinary status"
                self.assertEqual(sanitize_shareable_text(text), r"HID\VID_413D&PID_2104" + separator + "ordinary status")
        for text in ("ordinary | prose ` code", "VID_413D&PID_2104", "VID_413D&PID_2104&status=failed"):
            self.assertEqual(sanitize_shareable_text(text), text)
