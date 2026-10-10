"""Exercise export boundaries with real source files and ZIP serialization."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from zd_app.models import DeviceState
from zd_app.services.diagnostic_bundle.service import DiagnosticBundleService, _BundleMember
from zd_app.services.diagnostics_service import DiagnosticsService
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
