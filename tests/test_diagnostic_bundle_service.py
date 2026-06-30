"""Tests for :class:`DiagnosticBundleService`.

Covers Markdown rendering, ZIP composition, path sanitization, missing-
data placeholders, and the wear-ledger event emission on bundle generation.
The service is exercised against real temp directories — no mocking of
disk I/O — so the bundle's actual round-trip via :mod:`zipfile` is part
of every ZIP test.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from zd_app.services.diagnostic_bundle import (
    APP_DATA_PLACEHOLDER,
    CLAIM_BOUNDARY_PARAGRAPH,
    DiagnosticBundleService,
)
from zd_app.services.module_passport import ModulePassportService
from zd_app.services.wear_ledger import WearLedgerService
from zd_app.services.wear_ledger.models import (
    DIAGNOSTIC_BUNDLE_GENERATED,
    HEALTH_REPORT,
    MODULE_CHARACTERIZED,
    READINESS_CHECK,
    RP_RESTORE,
    SERVICE_NOTE,
    SESSION_START,
)
from zd_app.storage.module_passport_models import (
    ModuleFingerprint,
    SIDE_LEFT,
    SIDE_RIGHT,
    STATUS_GOOD,
    STATUS_WATCH,
    STATUS_WEAR_OBSERVED,
)


_DEEP_JSON = "[" * 20000 + "]" * 20000


def _frozen_clock(start: datetime) -> Callable[[], datetime]:
    """Return a callable that advances by 1 second per call."""

    state = {"now": start}

    def _now() -> datetime:
        current = state["now"]
        state["now"] = current + timedelta(seconds=1)
        return current

    return _now


def _fingerprint(**overrides) -> ModuleFingerprint:
    base = dict(
        timestamp_utc="2026-05-26T18:42:11Z",
        side=SIDE_LEFT,
        duration_ms=60_000,
        samples_count=12_000,
        noise_floor_percent=1.4,
        centering_offset_x=0.2,
        centering_offset_y=-0.1,
        circularity_coverage_percent=0.97,
        outer_deadzone_min_axis=94.0,
        outer_deadzone_max_axis=98.0,
        asymmetry_score=0.05,
        bitness_observed=128,
        tremor_metric=0.2,
        linearity_score=0.04,
        overall_status=STATUS_GOOD,
        notes=None,
    )
    base.update(overrides)
    return ModuleFingerprint(**base)


def _seed_health_report(
    directory: Path,
    *,
    stamp: str = "2026-05-26_180000",
    overall_status: str = "ready",
) -> Path:
    """Write a Health Report (.md + .json siblings) into ``directory``."""

    directory.mkdir(parents=True, exist_ok=True)
    md_path = directory / f"zd_health_report_{stamp}.md"
    json_path = directory / f"zd_health_report_{stamp}.json"
    md_path.write_text(
        f"# Controller Health Report\n\nOverall: {overall_status}\n",
        encoding="utf-8",
    )
    payload = {
        "overall_status": overall_status,
        "captured_at_utc": f"{stamp[:10]}T{stamp[11:13]}:{stamp[13:15]}:00Z",
        "sample_count": 12_000,
        "device": {"controller_name": "ZD Ultimate Legend"},
    }
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return md_path


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


class GenerateMarkdownFullDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = _frozen_clock(
            datetime(2026, 5, 26, 18, 0, 0, tzinfo=timezone.utc)
        )
        self.ledger = WearLedgerService(
            base_dir=self.root / "ledger", utc_now=self.clock
        )
        self.passports = ModulePassportService(
            base_dir=self.root / "passports",
            wear_ledger=self.ledger,
            utc_now=self.clock,
        )
        self.passports.assign(SIDE_LEFT, "STOCK_BASELINE_LEFT", notes="bench baseline")
        self.passports.append_fingerprint(SIDE_LEFT, _fingerprint())
        self.passports.assign(SIDE_RIGHT, "STOCK_BASELINE_RIGHT", notes="")
        self.passports.append_fingerprint(
            SIDE_RIGHT,
            _fingerprint(side=SIDE_RIGHT, overall_status=STATUS_WATCH),
        )
        self.health_dir = self.root / "health_reports"
        _seed_health_report(self.health_dir, stamp="2026-05-26_180000")
        _seed_health_report(
            self.health_dir, stamp="2026-05-25_120000", overall_status="warn"
        )
        # Drive a few ledger events so the lifecycle counts are non-zero.
        self.ledger.append(SESSION_START, summary="boot")
        self.ledger.append(HEALTH_REPORT, summary="hr-1")
        self.ledger.append(
            RP_RESTORE,
            summary="restored",
            details={"result_label": "verified"},
        )
        self.ledger.append(
            READINESS_CHECK,
            summary="readiness",
            details={"verdict": "green"},
        )
        self.ledger.append(
            SERVICE_NOTE,
            summary="serviced",
            details={"note": "Installed new stabilizer pads."},
        )
        self.service = DiagnosticBundleService(
            base_dir=self.root / "bundles",
            module_passport_service=self.passports,
            health_report_dir=self.health_dir,
            wear_ledger=self.ledger,
            app_data_dir=self.root,
            utc_now=self.clock,
        )

    def test_markdown_contains_all_top_level_sections(self) -> None:
        md = self.service.generate_markdown()
        for heading in (
            "# ZD Ultimate Legend - Diagnostic Bundle",
            "## What this is (claim boundary)",
            "## Hardware",
            "## Module Passports",
            "## Recent Health Reports",
            "## Lifecycle summary (Wear Ledger)",
            "## What was NOT included",
        ):
            self.assertIn(heading, md, f"section missing: {heading}")

    def test_markdown_includes_verbatim_claim_boundary(self) -> None:
        md = self.service.generate_markdown()
        self.assertIn(CLAIM_BOUNDARY_PARAGRAPH, md)

    def test_markdown_renders_passport_module_ids_and_metrics(self) -> None:
        md = self.service.generate_markdown()
        self.assertIn("STOCK_BASELINE_LEFT", md)
        self.assertIn("STOCK_BASELINE_RIGHT", md)
        # 8-metric block lines for at least one passport.
        for snippet in (
            "Noise floor:",
            "Centering offset:",
            "Circularity coverage:",
            "Outer deadzone:",
            "Asymmetry:",
            "Bitness:",
            "Tremor:",
            "Linearity:",
            "Overall:",
        ):
            self.assertIn(snippet, md, f"metric line missing: {snippet}")

    def test_markdown_escapes_freeform_passport_fields(self) -> None:
        self.passports.assign(
            SIDE_LEFT,
            "Module | [link](file:///x) `id`\nline",
            notes="Notes | [link](file:///note) `note`\nsecond",
        )

        md = self.service.generate_markdown()

        self.assertIn(r"Module \| \[link\]\(file:/x\) \`id\` line", md)
        self.assertIn(
            r"Notes \| \[link\]\(file:/note\) \`note\` second",
            md,
        )
        self.assertNotIn("[link](file:///x)", md)
        self.assertNotIn("[link](file:///note)", md)
        self.assertNotIn("\nsecond", md)

    def test_markdown_includes_recent_health_reports(self) -> None:
        md = self.service.generate_markdown(health_report_limit=2)
        # Both seeded reports surface (newest first).
        self.assertIn("zd_health_report_2026-05-26_180000.md", md)
        self.assertIn("zd_health_report_2026-05-25_120000.md", md)
        # Health-report metadata extracted from the JSON sibling.
        self.assertIn("Overall: ready", md)
        self.assertIn("Overall: warn", md)

    def test_markdown_lifecycle_counts_match_emitted_events(self) -> None:
        md = self.service.generate_markdown()
        self.assertIn("Total sessions: 1", md)
        self.assertIn("Total Health Reports: 1", md)
        self.assertIn("Total RP restores: 1", md)
        self.assertIn("Total Readiness Checks: 1", md)
        self.assertIn("Total service notes: 1", md)
        # Module characterizations: one per side seed = 2.
        self.assertIn("Total module characterizations: 2", md)

    def test_markdown_recent_service_note_preview(self) -> None:
        md = self.service.generate_markdown()
        self.assertIn("Installed new stabilizer pads.", md)

    def test_markdown_health_report_limit_respected(self) -> None:
        md = self.service.generate_markdown(health_report_limit=1)
        self.assertIn("zd_health_report_2026-05-26_180000.md", md)
        self.assertNotIn("zd_health_report_2026-05-25_120000.md", md)


# ---------------------------------------------------------------------------
# Missing-data placeholders
# ---------------------------------------------------------------------------


class GenerateMarkdownMissingDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = _frozen_clock(
            datetime(2026, 5, 26, 18, 0, 0, tzinfo=timezone.utc)
        )

    def _service(
        self,
        *,
        passports: ModulePassportService | None = None,
        ledger: WearLedgerService | None = None,
        health_dir: Path | None = None,
    ) -> DiagnosticBundleService:
        return DiagnosticBundleService(
            base_dir=self.root / "bundles",
            module_passport_service=passports,
            health_report_dir=health_dir,
            wear_ledger=ledger,
            app_data_dir=self.root,
            utc_now=self.clock,
        )

    def test_missing_module_passport_renders_not_assigned(self) -> None:
        passports = ModulePassportService(
            base_dir=self.root / "passports",
            utc_now=self.clock,
        )
        service = self._service(passports=passports)
        md = service.generate_markdown()
        # Both sides present + both empty.
        self.assertEqual(md.count("- Not assigned"), 2)

    def test_missing_health_report_dir_renders_no_exports(self) -> None:
        service = self._service(health_dir=None)
        md = service.generate_markdown()
        self.assertIn("## Recent Health Reports", md)
        self.assertIn("No exports found", md)

    def test_empty_health_report_dir_renders_no_exports(self) -> None:
        empty_dir = self.root / "empty_health_reports"
        empty_dir.mkdir()
        service = self._service(health_dir=empty_dir)
        md = service.generate_markdown()
        self.assertIn("No exports found", md)

    def test_deeply_nested_health_report_json_renders_unreadable_summary(self) -> None:
        health_dir = self.root / "health_reports"
        health_dir.mkdir()
        md_path = health_dir / "zd_health_report_2026-05-26_180000.md"
        md_path.write_text("# Health Report\n", encoding="utf-8")
        md_path.with_suffix(".json").write_text(_DEEP_JSON, encoding="utf-8")
        service = self._service(health_dir=health_dir)

        with self.assertLogs("zd_app.services.diagnostic_bundle.service", level="ERROR"):
            summary = service._summarize_health_report(md_path)

        self.assertEqual(summary["lines"], ["(JSON sibling unreadable)"])

    def test_empty_wear_ledger_renders_zero_counts(self) -> None:
        ledger = WearLedgerService(
            base_dir=self.root / "ledger", utc_now=self.clock
        )
        service = self._service(ledger=ledger)
        md = service.generate_markdown()
        self.assertIn("Total sessions: 0", md)
        self.assertIn("Total profile applies: 0", md)
        self.assertIn("(none in window)", md)

    def test_missing_wear_ledger_renders_zero_counts(self) -> None:
        service = self._service(ledger=None)
        md = service.generate_markdown()
        self.assertIn("Total sessions: 0", md)

    def test_archived_section_skipped_when_include_archived_false(self) -> None:
        passports = ModulePassportService(
            base_dir=self.root / "passports",
            utc_now=self.clock,
        )
        # Force an archive by assigning a side twice.
        passports.assign(SIDE_LEFT, "M_A")
        passports.assign(SIDE_LEFT, "M_B")
        self.assertEqual(len(passports.list_archive(SIDE_LEFT)), 1)
        service = self._service(passports=passports)
        md_no_archive = service.generate_markdown(include_archived=False)
        self.assertIn(
            "Not included (set 'Include archived passports'",
            md_no_archive,
        )
        self.assertNotIn("M_A", md_no_archive)
        md_with_archive = service.generate_markdown(include_archived=True)
        self.assertIn("M_A", md_with_archive)


# ---------------------------------------------------------------------------
# Path sanitization
# ---------------------------------------------------------------------------


class PathSanitizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = _frozen_clock(
            datetime(2026, 5, 26, 18, 0, 0, tzinfo=timezone.utc)
        )
        # Use an explicit Windows-shaped app_data_dir for substring tests.
        self.app_data_dir = Path("C:/Users/exampleuser/AppData/Roaming/ZDUltimateLegend")
        self.service = DiagnosticBundleService(
            base_dir=self.root / "bundles",
            app_data_dir=self.app_data_dir,
            utc_now=self.clock,
        )

    def test_sanitize_app_data_subpath_uses_placeholder(self) -> None:
        raw = "C:/Users/exampleuser/AppData/Roaming/ZDUltimateLegend/health_reports/zd_health_report_2026-05-26_180000.md"
        sanitized = self.service._sanitize_path(raw)
        self.assertEqual(
            sanitized,
            f"{APP_DATA_PLACEHOLDER}/health_reports/zd_health_report_2026-05-26_180000.md",
        )

    def test_sanitize_app_data_subpath_windows_backslashes(self) -> None:
        raw = (
            r"C:\Users\exampleuser\AppData\Roaming\ZDUltimateLegend\wear_ledger\events.jsonl"
        )
        sanitized = self.service._sanitize_path(raw)
        # Either the configured-root rewrite OR the marker rewrite must hit;
        # both end at the placeholder/wear_ledger/events.jsonl tail.
        self.assertIn(APP_DATA_PLACEHOLDER, sanitized)
        self.assertIn("wear_ledger/events.jsonl", sanitized)

    def test_sanitize_outside_app_data_falls_back_to_basename(self) -> None:
        raw = "C:/Users/exampleuser/Documents/secret_file.txt"
        sanitized = self.service._sanitize_path(raw)
        self.assertEqual(sanitized, "secret_file.txt")

    def test_sanitize_embedded_path_in_free_text(self) -> None:
        text = "Loaded profile from C:/Users/exampleuser/AppData/Roaming/ZDUltimateLegend/wrapper_profiles/foo.json"
        sanitized = self.service._sanitize_path(text)
        self.assertIn(
            f"{APP_DATA_PLACEHOLDER}/wrapper_profiles/foo.json", sanitized
        )
        self.assertNotIn("exampleuser", sanitized)

    def test_sanitize_handles_posix_home_path(self) -> None:
        raw = "/Users/exampleuser/Library/Application Support/ZDUltimateLegend/restore_points/rp.json"
        sanitized = self.service._sanitize_path(raw)
        self.assertIn(APP_DATA_PLACEHOLDER, sanitized)
        self.assertIn("restore_points/rp.json", sanitized)
        self.assertNotIn("exampleuser", sanitized)

    def test_sanitize_non_string_value_coerces(self) -> None:
        self.assertEqual(self.service._sanitize_path(None), "")
        self.assertEqual(self.service._sanitize_path(""), "")
        self.assertEqual(self.service._sanitize_path(42), "42")

    # Regression — a Windows display-name account ("Jane Doe") used to leak its
    # first name because the old sanitizer stopped at the first whitespace.
    def test_sanitize_spaced_username_app_data_path(self) -> None:
        raw = r"C:\Users\Jane Doe\AppData\Roaming\ZDUltimateLegend\profile.json"
        sanitized = self.service._sanitize_path(raw)
        self.assertEqual(sanitized, f"{APP_DATA_PLACEHOLDER}/profile.json")
        self.assertNotIn("Jane", sanitized)
        self.assertNotIn("Doe", sanitized)

    def test_sanitize_spaced_username_outside_app_data(self) -> None:
        raw = r"C:\Users\Jane Doe\Documents\foo.txt"
        sanitized = self.service._sanitize_path(raw)
        self.assertEqual(sanitized, "foo.txt")
        self.assertNotIn("Jane", sanitized)
        self.assertNotIn("Doe", sanitized)
        self.assertNotIn(r"C:\Users", sanitized)

    def test_render_hardware_scrubs_path_bearing_identity(self) -> None:
        # Defensive: device-identity fields flow into the shareable Hardware
        # section. A path-bearing value must be scrubbed, and a normal value
        # must survive untouched.
        md = self.service.generate_markdown(
            device_identity={
                "product_string": r"C:\Users\Jane Doe\dev\ZD Stick",
                "firmware_version": "v1.18",
            }
        )
        self.assertNotIn("Jane", md)
        self.assertNotIn(r"C:\Users", md)
        self.assertIn("Firmware: v1.18", md)


class MarkdownDoesNotLeakAbsolutePathsTests(unittest.TestCase):
    """Service notes that include absolute paths must be sanitized in render."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = _frozen_clock(
            datetime(2026, 5, 26, 18, 0, 0, tzinfo=timezone.utc)
        )
        self.ledger = WearLedgerService(
            base_dir=self.root / "ledger", utc_now=self.clock
        )
        self.ledger.append(
            SERVICE_NOTE,
            summary="reset",
            details={
                "note": (
                    "Reset after loading "
                    "C:/Users/exampleuser/AppData/Roaming/ZDUltimateLegend/wrapper_profiles/x.json"
                ),
            },
        )
        self.service = DiagnosticBundleService(
            base_dir=self.root / "bundles",
            wear_ledger=self.ledger,
            app_data_dir=Path(
                "C:/Users/exampleuser/AppData/Roaming/ZDUltimateLegend"
            ),
            utc_now=self.clock,
        )

    def test_service_note_path_is_sanitized(self) -> None:
        md = self.service.generate_markdown()
        self.assertNotIn("exampleuser", md)
        self.assertIn(APP_DATA_PLACEHOLDER, md)


class MarkdownDoesNotLeakRootedHomePathTests(unittest.TestCase):
    """A drive-LESS rooted home path in a service note must not leak the name.

    Mirrors the GPT-5.5 probe: a Wear Ledger service note containing
    ``\\Users\\Jane Doe\\...`` (no drive letter — what ``%HOMEPATH%`` expands
    to) flowed through the bundle UNCHANGED on base because the shared scrubber
    only recognized drive-letter / POSIX / UNC home anchors. The bundle preview
    must carry neither the username's first nor last name.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = _frozen_clock(
            datetime(2026, 5, 26, 18, 0, 0, tzinfo=timezone.utc)
        )
        self.ledger = WearLedgerService(
            base_dir=self.root / "ledger", utc_now=self.clock
        )
        self.ledger.append(
            SERVICE_NOTE,
            summary="reset",
            details={
                "note": r"backup at \Users\Jane Doe\Documents\secret.json",
            },
        )
        self.service = DiagnosticBundleService(
            base_dir=self.root / "bundles",
            wear_ledger=self.ledger,
            utc_now=self.clock,
        )

    def test_rooted_home_path_in_note_is_scrubbed(self) -> None:
        md = self.service.generate_markdown()
        self.assertNotIn("Jane", md)
        self.assertNotIn("Doe", md)
        self.assertNotIn(r"\Users", md)


class ReportMarkdownEscapingTests(unittest.TestCase):
    """Freeform bundle values must not become active Markdown in report.md."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = _frozen_clock(
            datetime(2026, 5, 26, 18, 0, 0, tzinfo=timezone.utc)
        )
        self.ledger = WearLedgerService(
            base_dir=self.root / "ledger", utc_now=self.clock
        )
        self.ledger.append(
            SERVICE_NOTE,
            summary="serviced",
            details={
                "note": (
                    "Service note | [note](file:///note) `note` "
                    r"C:\Users\Jane Doe\AppData\Roaming\ZDUltimateLegend\note.txt"
                    "\n# note heading"
                ),
            },
        )
        self.health_dir = self.root / "health_reports"
        self.health_dir.mkdir(parents=True, exist_ok=True)
        stamp = "2026-05-26_180000"
        (self.health_dir / f"zd_health_report_{stamp}.md").write_text(
            "# Controller Health Report\n\nOverall: ready\n",
            encoding="utf-8",
        )
        (self.health_dir / f"zd_health_report_{stamp}.json").write_text(
            json.dumps(
                {
                    "overall_status": (
                        "ready | [overall](file:///overall) `overall`\n"
                        "# overall heading "
                        r"C:\Users\Jane Doe\AppData\Roaming\ZDUltimateLegend\overall.json"
                    ),
                    "captured_at_utc": (
                        "Captured | [captured](file:///captured) `captured`\n"
                        "# captured heading"
                    ),
                    "sample_count": 12000,
                    "device": {
                        "controller_name": (
                            "Pad | [controller](file:///controller) `controller`\n"
                            "# controller heading "
                            r"C:\Users\Jane Doe\AppData\Roaming\ZDUltimateLegend\controllers\pad.json"
                        ),
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self.service = DiagnosticBundleService(
            base_dir=self.root / "bundles",
            health_report_dir=self.health_dir,
            wear_ledger=self.ledger,
            app_data_dir=Path(
                r"C:\Users\Jane Doe\AppData\Roaming\ZDUltimateLegend"
            ),
            utc_now=self.clock,
        )

    def test_report_md_escapes_markdown_after_path_scrub(self) -> None:
        target = self.root / "out.zip"
        result = self.service.generate_bundle_zip(
            target,
            device_identity={
                "product_string": (
                    "ZD | [product](file:///product) `product`\n"
                    "# product heading "
                    r"C:\Users\Jane Doe\AppData\Roaming\ZDUltimateLegend\device\product.json"
                ),
                "firmware_version": (
                    "v1.18 | [firmware](file:///firmware) `firmware`\n"
                    "# firmware heading"
                ),
                "connection": (
                    "USB | [connection](file:///connection) `connection`\n"
                    "# connection heading"
                ),
            },
        )
        self.assertEqual(result, target)

        with zipfile.ZipFile(target, "r") as zf:
            report_md = zf.read("report.md").decode("utf-8")
            wear_summary = json.loads(
                zf.read("wear_ledger_summary.json").decode("utf-8")
            )

        for escaped in (
            r"ZD \| \[product\]",
            r"v1.18 \| \[firmware\]",
            r"USB \| \[connection\]",
            r"Captured \| \[captured\]",
            r"Pad \| \[controller\]",
            r"ready \| \[overall\]",
            r"Service note \| \[note\]",
        ):
            self.assertIn(escaped, report_md)
        for raw_link in (
            "[product](file:///product)",
            "[firmware](file:///firmware)",
            "[connection](file:///connection)",
            "[captured](file:///captured)",
            "[controller](file:///controller)",
            "[overall](file:///overall)",
            "[note](file:///note)",
            "[product](file:/product)",
            "[firmware](file:/firmware)",
            "[connection](file:/connection)",
            "[captured](file:/captured)",
            "[controller](file:/controller)",
            "[overall](file:/overall)",
            "[note](file:/note)",
        ):
            self.assertNotIn(raw_link, report_md)
        for raw_code in (
            "`product`",
            "`firmware`",
            "`connection`",
            "`captured`",
            "`controller`",
            "`overall`",
            "`note`",
        ):
            self.assertNotIn(raw_code, report_md)
        for heading in (
            "\n# product heading",
            "\n# firmware heading",
            "\n# connection heading",
            "\n# captured heading",
            "\n# controller heading",
            "\n# overall heading",
            "\n# note heading",
        ):
            self.assertNotIn(heading, report_md)

        self.assertIn(f"{APP_DATA_PLACEHOLDER}/device/product.json", report_md)
        self.assertIn(f"{APP_DATA_PLACEHOLDER}/controllers/pad.json", report_md)
        self.assertIn(f"{APP_DATA_PLACEHOLDER}/overall.json", report_md)
        self.assertIn(f"{APP_DATA_PLACEHOLDER}/note.txt", report_md)
        # JSON members stay path-scrubbed, not Markdown-escaped.
        preview = wear_summary["recent_service_notes"][0]["preview"]
        self.assertIn("[note]", preview)
        self.assertNotIn(r"\[note\]", preview)


# ---------------------------------------------------------------------------
# ZIP composition
# ---------------------------------------------------------------------------


class GenerateBundleZipTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = _frozen_clock(
            datetime(2026, 5, 26, 18, 0, 0, tzinfo=timezone.utc)
        )
        self.ledger = WearLedgerService(
            base_dir=self.root / "ledger", utc_now=self.clock
        )
        self.passports = ModulePassportService(
            base_dir=self.root / "passports",
            wear_ledger=self.ledger,
            utc_now=self.clock,
        )
        self.passports.assign(SIDE_LEFT, "STOCK_LEFT")
        self.passports.append_fingerprint(SIDE_LEFT, _fingerprint())
        # Force one archived passport.
        self.passports.assign(SIDE_LEFT, "STOCK_LEFT_V2")
        self.passports.assign(SIDE_RIGHT, "STOCK_RIGHT")
        self.health_dir = self.root / "health_reports"
        _seed_health_report(self.health_dir, stamp="2026-05-26_180000")
        self.service = DiagnosticBundleService(
            base_dir=self.root / "bundles",
            module_passport_service=self.passports,
            health_report_dir=self.health_dir,
            wear_ledger=self.ledger,
            app_data_dir=self.root,
            utc_now=self.clock,
        )

    def test_zip_contains_report_and_passport_entries(self) -> None:
        target = self.root / "out.zip"
        result = self.service.generate_bundle_zip(target)
        self.assertEqual(result, target)
        self.assertTrue(target.exists())
        with zipfile.ZipFile(target, "r") as zf:
            names = set(zf.namelist())
        self.assertIn("report.md", names)
        self.assertIn("module_passports/left.json", names)
        self.assertIn("module_passports/right.json", names)
        # Archived passport is in the archive subfolder.
        archive_names = [n for n in names if n.startswith("module_passports/archive/")]
        self.assertEqual(len(archive_names), 1)
        # Health report MD + JSON siblings landed.
        self.assertIn(
            "health_reports/zd_health_report_2026-05-26_180000.md", names
        )
        self.assertIn(
            "health_reports/zd_health_report_2026-05-26_180000.json", names
        )
        # Wear-ledger summary entry exists and is valid JSON.
        self.assertIn("wear_ledger_summary.json", names)
        with zipfile.ZipFile(target, "r") as zf:
            summary = json.loads(
                zf.read("wear_ledger_summary.json").decode("utf-8")
            )
        self.assertIn("counts", summary)
        self.assertIn("recent_service_notes", summary)
        # The raw events.jsonl must NOT be in the bundle.
        self.assertNotIn("wear_ledger/events.jsonl", names)
        self.assertNotIn("events.jsonl", names)

    def test_preview_manifest_matches_zip_members_and_counts(self) -> None:
        manifest = self.service.preview_bundle_manifest()
        target = self.root / "out.zip"
        self.service.generate_bundle_zip(target)

        with zipfile.ZipFile(target, "r") as zf:
            names = set(zf.namelist())

        self.assertEqual(set(manifest.member_paths), names)
        items = {item.key: item for item in manifest.items}
        self.assertEqual(items["report"].count, 1)
        self.assertEqual(items["report"].member_paths, ("report.md",))
        self.assertEqual(items["module_passports"].count, 3)
        self.assertEqual(
            items["module_passports"].metadata["active_count"], 2
        )
        self.assertEqual(
            items["module_passports"].metadata["archived_count"], 1
        )
        self.assertEqual(items["health_reports"].count, 1)
        self.assertEqual(items["health_reports"].member_count, 2)
        self.assertEqual(
            items["health_reports"].metadata["markdown_count"], 1
        )
        self.assertEqual(items["health_reports"].metadata["json_count"], 1)
        self.assertEqual(items["wear_ledger"].count, 1)
        excluded = {item.key: item for item in manifest.excluded_items}
        self.assertEqual(excluded["raw_event_log"].count, 0)
        self.assertEqual(excluded["raw_app_logs"].count, 0)

    def test_zip_excludes_archive_when_include_archived_false(self) -> None:
        target = self.root / "out_no_archive.zip"
        self.service.generate_bundle_zip(target, include_archived=False)
        with zipfile.ZipFile(target, "r") as zf:
            names = set(zf.namelist())
        self.assertFalse(
            any(n.startswith("module_passports/archive/") for n in names),
            f"archive entries leaked: {[n for n in names if 'archive' in n]}",
        )

        manifest = self.service.preview_bundle_manifest(include_archived=False)
        items = {item.key: item for item in manifest.items}
        self.assertEqual(
            items["module_passports"].metadata["archived_count"], 0
        )
        self.assertFalse(
            any(
                name.startswith("module_passports/archive/")
                for name in manifest.member_paths
            )
        )

    def test_zip_report_md_round_trips_through_bundle(self) -> None:
        target = self.root / "out.zip"
        self.service.generate_bundle_zip(target)
        with zipfile.ZipFile(target, "r") as zf:
            report_md = zf.read("report.md").decode("utf-8")
        # Same heading set as generate_markdown directly.
        self.assertIn("# ZD Ultimate Legend - Diagnostic Bundle", report_md)
        self.assertIn(CLAIM_BOUNDARY_PARAGRAPH, report_md)

    def test_zip_write_into_bad_target_returns_none(self) -> None:
        # Writing into a non-existent disk produces an OSError. Target a
        # path whose parent can't be created.
        target = Path("Z:/non_existent_drive/diagnostic_bundle.zip")
        result = self.service.generate_bundle_zip(target)
        self.assertIsNone(result)


class BundlePreviewEmptySourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.service = DiagnosticBundleService(base_dir=self.root / "bundles")

    def test_preview_manifest_keeps_zero_count_classes_visible(self) -> None:
        manifest = self.service.preview_bundle_manifest()
        self.assertEqual(
            set(manifest.member_paths),
            {"report.md", "wear_ledger_summary.json"},
        )
        items = {item.key: item for item in manifest.items}
        self.assertEqual(items["report"].count, 1)
        self.assertEqual(items["module_passports"].count, 0)
        self.assertEqual(items["health_reports"].count, 0)
        self.assertEqual(items["wear_ledger"].count, 1)
        self.assertEqual(items["wear_ledger"].metadata["event_count"], 0)
        self.assertEqual(
            {item.key for item in manifest.excluded_items},
            {"raw_event_log", "raw_app_logs"},
        )


# ---------------------------------------------------------------------------
# Wear-ledger event emission
# ---------------------------------------------------------------------------


class EmitGeneratedEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = _frozen_clock(
            datetime(2026, 5, 26, 18, 0, 0, tzinfo=timezone.utc)
        )
        self.ledger = WearLedgerService(
            base_dir=self.root / "ledger", utc_now=self.clock
        )
        self.service = DiagnosticBundleService(
            base_dir=self.root / "bundles",
            wear_ledger=self.ledger,
            utc_now=self.clock,
        )

    def test_emit_generated_event_records_to_ledger(self) -> None:
        self.service.emit_generated_event(
            output_filename="diagnostic_bundle_2026-05-26_180000.md",
            bundle_format="md",
            include_archived=True,
            health_report_limit=5,
            wear_ledger_days=90,
        )
        events = self.ledger.read_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.event_type, DIAGNOSTIC_BUNDLE_GENERATED)
        self.assertEqual(event.details["format"], "md")
        self.assertEqual(
            event.details["output_filename"],
            "diagnostic_bundle_2026-05-26_180000.md",
        )
        # No absolute path in the recorded details.
        self.assertNotIn("/", event.details["output_filename"])
        self.assertNotIn("\\", event.details["output_filename"])

    def test_emit_with_no_ledger_is_noop(self) -> None:
        service = DiagnosticBundleService(
            base_dir=self.root / "bundles2",
            utc_now=self.clock,
        )
        # Should not raise; nothing observable to assert beyond that.
        service.emit_generated_event(
            output_filename="x.md",
            bundle_format="md",
            include_archived=False,
            health_report_limit=1,
            wear_ledger_days=30,
        )


# ---------------------------------------------------------------------------
# Lifecycle: combined generation + event
# ---------------------------------------------------------------------------


class FullGenerationLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = _frozen_clock(
            datetime(2026, 5, 26, 18, 0, 0, tzinfo=timezone.utc)
        )
        self.ledger = WearLedgerService(
            base_dir=self.root / "ledger", utc_now=self.clock
        )
        self.passports = ModulePassportService(
            base_dir=self.root / "passports",
            wear_ledger=self.ledger,
            utc_now=self.clock,
        )
        self.passports.assign(SIDE_LEFT, "M_LIFE")
        self.passports.append_fingerprint(
            SIDE_LEFT,
            _fingerprint(overall_status=STATUS_WEAR_OBSERVED),
        )
        self.service = DiagnosticBundleService(
            base_dir=self.root / "bundles",
            module_passport_service=self.passports,
            wear_ledger=self.ledger,
            app_data_dir=self.root,
            utc_now=self.clock,
        )

    def test_zip_generation_followed_by_event_lands_two_records(self) -> None:
        target = self.root / "out.zip"
        result = self.service.generate_bundle_zip(target)
        self.assertIsNotNone(result)
        assert result is not None
        self.service.emit_generated_event(
            output_filename=result.name,
            bundle_format="zip",
            include_archived=True,
            health_report_limit=5,
            wear_ledger_days=90,
        )
        events = self.ledger.read_events()
        bundle_events = [
            e for e in events if e.event_type == DIAGNOSTIC_BUNDLE_GENERATED
        ]
        self.assertEqual(len(bundle_events), 1)
        self.assertEqual(bundle_events[0].details["format"], "zip")


class PassportPathSanitizationTests(unittest.TestCase):
    """A1: operator-assignable ``module_id`` + ``notes`` are path-scrubbed in
    report.md and the module_passports JSON — for the active AND archived
    passports, in both the JSON body and the archive entry filename — so a
    username/home path typed into a note never ships verbatim in the shareable
    diagnostic bundle.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = _frozen_clock(
            datetime(2026, 5, 26, 18, 0, 0, tzinfo=timezone.utc)
        )
        self.ledger = WearLedgerService(
            base_dir=self.root / "ledger", utc_now=self.clock
        )
        self.passports = ModulePassportService(
            base_dir=self.root / "passports",
            wear_ledger=self.ledger,
            utc_now=self.clock,
        )
        # A path-bearing note + a module_id carrying a real-looking home path.
        # Both are operator freeform fields that historically shipped verbatim.
        self.passports.assign(
            SIDE_LEFT,
            r"C:\Users\secretuser\modules\left.bin",
            notes=r"backup saved to C:\Users\secretuser\Documents\profile.json",
        )
        self.passports.append_fingerprint(SIDE_LEFT, _fingerprint())
        self.passports.assign(SIDE_RIGHT, "STOCK_RIGHT")
        self.health_dir = self.root / "health_reports"
        _seed_health_report(self.health_dir, stamp="2026-05-26_180000")
        self.service = DiagnosticBundleService(
            base_dir=self.root / "bundles",
            module_passport_service=self.passports,
            health_report_dir=self.health_dir,
            wear_ledger=self.ledger,
            app_data_dir=self.root,
            utc_now=self.clock,
        )

    def test_report_md_scrubs_username_path_from_module_id_and_notes(self) -> None:
        md = self.service.generate_markdown()
        self.assertNotIn("secretuser", md)
        # The sanitized basenames still convey the content.
        self.assertIn("profile.json", md)
        self.assertIn("left.bin", md)

    def test_passport_json_scrubs_username_path(self) -> None:
        target = self.root / "out.zip"
        self.service.generate_bundle_zip(target)
        with zipfile.ZipFile(target, "r") as zf:
            left_json = zf.read("module_passports/left.json").decode("utf-8")
        self.assertNotIn("secretuser", left_json)
        self.assertIn("profile.json", left_json)
        self.assertIn("left.bin", left_json)

    def test_archived_passport_json_and_filename_scrub_username(self) -> None:
        # Reassigning archives the path-bearing passport; the same shareable ZIP
        # must scrub both the archived JSON body AND the archive entry filename
        # (module_id is path-bearing, so the filename segment leaked it before).
        self.passports.assign(SIDE_LEFT, "STOCK_LEFT_CLEAN")
        target = self.root / "out_archive.zip"
        self.service.generate_bundle_zip(target, include_archived=True)
        with zipfile.ZipFile(target, "r") as zf:
            archive_names = [
                n for n in zf.namelist()
                if n.startswith("module_passports/archive/")
            ]
            self.assertTrue(archive_names, "expected an archived passport entry")
            for name in archive_names:
                self.assertNotIn("secretuser", name)
                body = zf.read(name).decode("utf-8")
                self.assertNotIn("secretuser", body)
                # Sanitized basename still conveys the module.
                self.assertIn("left.bin", body)


class HealthReportPathSanitizationTests(unittest.TestCase):
    """A1: health-report artifacts in the shareable bundle are path-scrubbed —
    the verbatim ``health_reports/*.md`` + ``*.json`` entries AND the report.md
    summary that echoes their JSON fields — so a home/username path inside a
    health report never ships in the diagnostic bundle.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = _frozen_clock(
            datetime(2026, 5, 26, 18, 0, 0, tzinfo=timezone.utc)
        )
        self.ledger = WearLedgerService(
            base_dir=self.root / "ledger", utc_now=self.clock
        )
        self.passports = ModulePassportService(
            base_dir=self.root / "passports",
            wear_ledger=self.ledger,
            utc_now=self.clock,
        )
        # Clean passports — this test isolates the health-report leak.
        self.passports.assign(SIDE_LEFT, "STOCK_LEFT")
        self.passports.assign(SIDE_RIGHT, "STOCK_RIGHT")
        self.health_dir = self.root / "health_reports"
        self.health_dir.mkdir(parents=True, exist_ok=True)
        stamp = "2026-05-26_180000"
        # A health report whose body + JSON carry an absolute home path.
        (self.health_dir / f"zd_health_report_{stamp}.md").write_text(
            "# Controller Health Report\n\n"
            r"Saved from C:\Users\secretuser\AppData\Roaming\ZDUltimateLegend\hr.md"
            "\n\nOverall: ready\n",
            encoding="utf-8",
        )
        (self.health_dir / f"zd_health_report_{stamp}.json").write_text(
            json.dumps(
                {
                    "overall_status": "ready",
                    "captured_at_utc": "2026-05-26T18:00:00Z",
                    "sample_count": 12000,
                    "device": {"controller_name": r"C:\Users\secretuser\dev\ZD"},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self.service = DiagnosticBundleService(
            base_dir=self.root / "bundles",
            module_passport_service=self.passports,
            health_report_dir=self.health_dir,
            wear_ledger=self.ledger,
            app_data_dir=self.root,
            utc_now=self.clock,
        )

    def test_bundle_scrubs_username_from_all_health_report_artifacts(self) -> None:
        target = self.root / "out.zip"
        self.service.generate_bundle_zip(target)
        with zipfile.ZipFile(target, "r") as zf:
            names = zf.namelist()
            for name in names:
                body = zf.read(name).decode("utf-8", errors="replace")
                self.assertNotIn(
                    "secretuser", body, f"username leaked in bundle entry {name}"
                )
            # Content is preserved (sanitized), not dropped.
            md_entry = f"health_reports/zd_health_report_2026-05-26_180000.md"
            self.assertIn(md_entry, names)
            self.assertIn("Overall: ready", zf.read(md_entry).decode("utf-8"))


class BundleScrubsSpacedDisplayNameAcrossAllMembersTests(unittest.TestCase):
    """P4 (round-3): every copied bundle member is scrubbed on write, even for a
    SPACED display-name account (``C:\\Users\\Jane Doe\\...``) — the dangerous
    leak class where a stop-at-first-whitespace regex truncated the path and
    shipped the first name ("Jane") verbatim. A single bundle carries the
    home path in the passport ``module_id`` + ``notes``, a legacy health-report
    ``.md`` + ``.json`` pair, and a service-note preview; NO ZIP member may
    contain "Jane", ``C:\\Users``, or ``C:/Users``.

    Passes today — ``generate_bundle_zip`` scrubs every copied member on write
    (health reports via ``_sanitize_path(read_text())``, passports via the
    sanitized passport dict, the wear summary's note previews). This pins that
    contract against a future copy-without-scrub regression, across all members
    at once and for the spaced-username case the earlier per-member tests
    (which use a space-free ``secretuser``) don't cover.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = _frozen_clock(
            datetime(2026, 5, 26, 18, 0, 0, tzinfo=timezone.utc)
        )
        self.ledger = WearLedgerService(
            base_dir=self.root / "ledger", utc_now=self.clock
        )
        self.passports = ModulePassportService(
            base_dir=self.root / "passports", wear_ledger=self.ledger,
            utc_now=self.clock,
        )
        # Passport module_id + notes carry a spaced-display-name home path.
        self.passports.assign(
            SIDE_LEFT,
            r"C:\Users\Jane Doe\modules\left.bin",
            notes=r"backup saved to C:\Users\Jane Doe\Documents\profile.json",
        )
        self.passports.append_fingerprint(SIDE_LEFT, _fingerprint())
        self.passports.assign(SIDE_RIGHT, "STOCK_RIGHT")
        # A legacy health report whose .md body AND .json fields carry the path.
        self.health_dir = self.root / "health_reports"
        self.health_dir.mkdir(parents=True, exist_ok=True)
        self.stamp = "2026-05-26_180000"
        (self.health_dir / f"zd_health_report_{self.stamp}.md").write_text(
            "# Controller Health Report\n\n"
            r"Saved from C:\Users\Jane Doe\AppData\Roaming\ZDUltimateLegend\hr.md"
            "\n\nOverall: ready\n",
            encoding="utf-8",
        )
        (self.health_dir / f"zd_health_report_{self.stamp}.json").write_text(
            json.dumps(
                {
                    "overall_status": "ready",
                    "captured_at_utc": "2026-05-26T18:00:00Z",
                    "sample_count": 12000,
                    "device": {"controller_name": r"C:\Users\Jane Doe\dev\ZD"},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        # A service note whose preview surfaces in wear_ledger_summary.json.
        self.ledger.append(
            SERVICE_NOTE,
            summary="serviced",
            details={"note": r"swapped stick; see C:\Users\Jane Doe\notes.txt"},
        )
        self.service = DiagnosticBundleService(
            base_dir=self.root / "bundles",
            module_passport_service=self.passports,
            health_report_dir=self.health_dir,
            wear_ledger=self.ledger,
            app_data_dir=self.root,
            utc_now=self.clock,
        )

    def test_no_bundle_member_contains_username_or_home_path(self) -> None:
        target = self.root / "out.zip"
        self.assertIsNotNone(self.service.generate_bundle_zip(target))

        forbidden = ("Jane", r"C:\Users", "C:/Users")
        md_member = f"health_reports/zd_health_report_{self.stamp}.md"
        json_member = f"health_reports/zd_health_report_{self.stamp}.json"
        with zipfile.ZipFile(target, "r") as zf:
            names = zf.namelist()
            # Every member the spec enumerates is actually present (so the scrub
            # is genuinely exercised, not vacuously passing on absent members).
            for expected in (
                "report.md",
                "module_passports/left.json",
                "module_passports/right.json",
                md_member,
                json_member,
                "wear_ledger_summary.json",
            ):
                self.assertIn(expected, names, f"bundle missing {expected}")
            # ...and NONE leaks the username or the home-path prefix.
            for name in names:
                body = zf.read(name).decode("utf-8", errors="replace")
                for needle in forbidden:
                    self.assertNotIn(
                        needle, body, f"{needle!r} leaked in bundle member {name}"
                    )
            # Content is preserved as sanitized basenames, not dropped.
            left_json = zf.read("module_passports/left.json").decode("utf-8")
            self.assertIn("left.bin", left_json)
            self.assertIn("profile.json", left_json)
            self.assertIn("Overall: ready", zf.read(md_member).decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
