"""Tests for the Diagnostics Trust Self-Check evidence artifact."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from zd_app import i18n
from zd_app.services.model_fingerprint import InterfaceInventory, ModelFingerprint
from zd_app.services import trust_self_check
from zd_app.services.share_card import build_share_card


_LOCALE_DIR = Path("zd_app/i18n/locales")
_NOW = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def _row(
    result: trust_self_check.TrustSelfCheckResult,
    key: str,
) -> trust_self_check.TrustSelfCheckRow:
    return next(row for row in result.rows if row.key == key)


class TrustSelfCheckEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_claim_wording_and_boundary_are_present(self) -> None:
        result = trust_self_check.build_trust_self_check(now=_NOW)
        text = result.to_text()

        self.assertIn(
            "No network: this build imports no networking modules.",
            text,
        )
        self.assertIn(
            "Observed for THIS process THIS session - not a system-wide audit.",
            text,
        )

    def test_static_no_network_scan_is_clean_and_webbrowser_is_separate(self) -> None:
        result = trust_self_check.build_trust_self_check(now=_NOW)

        self.assertEqual(result.network_import_findings, ())
        self.assertTrue(
            any(handoff.call == "webbrowser.open" for handoff in result.browser_handoffs),
            "About screen browser handoff should be named separately.",
        )
        markdown = result.to_markdown()
        self.assertIn("Static scan of zd_app + main_zd.py", markdown)
        self.assertIn("webbrowser.open", markdown)
        self.assertIn("not LegendCTL telemetry", markdown)

    def test_unreadable_root_fails_closed_without_clean_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = trust_self_check.build_trust_self_check(
                package_root=Path(tmp) / "does-not-exist",
                now=_NOW,
            )

        self.assertEqual(
            result.scan_integrity,
            trust_self_check.ScanIntegrity(
                readable=False,
                python_file_count=0,
                parse_failures=(),
                entry_module_scanned=False,
            ),
        )
        self.assertEqual(
            _row(result, "network").claim,
            "Network scan did not run: no networking claim is made for this run.",
        )
        self.assertEqual(
            _row(result, "drivers").claim,
            "Driver/virtual-device scan did not run: no footprint claim is made for this run.",
        )
        rendered = result.to_text() + result.to_markdown()
        self.assertIn("the package tree could not be read", rendered)
        for clean_string in (
            "No network: this build imports no networking modules.",
            "No drivers / virtual devices: this shipped app footprint contains no driver or virtual-device package artifacts.",
            "Static scan of zd_app + main_zd.py",
            "0 driver/virtual-device artifacts across",
        ):
            with self.subTest(clean_string=clean_string):
                self.assertNotIn(clean_string, rendered)

    def test_empty_readable_root_fails_closed_as_no_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp) / "zd_app"
            package_root.mkdir()
            result = trust_self_check.build_trust_self_check(
                package_root=package_root,
                now=_NOW,
            )

        self.assertEqual(
            result.scan_integrity,
            trust_self_check.ScanIntegrity(
                readable=True,
                python_file_count=0,
                parse_failures=(),
                entry_module_scanned=False,
            ),
        )
        self.assertIn(
            "no scannable package files were found",
            _row(result, "network").evidence,
        )
        self.assertEqual(
            _row(result, "network").claim,
            "Network scan did not run: no networking claim is made for this run.",
        )
        self.assertEqual(
            _row(result, "drivers").claim,
            "Driver/virtual-device scan did not run: no footprint claim is made for this run.",
        )

    def test_parse_failures_fail_closed_but_findings_win(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp) / "zd_app"
            package_root.mkdir()
            (package_root / "valid.py").write_text("import socket\n", encoding="utf-8")
            (package_root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
            result = trust_self_check.build_trust_self_check(
                package_root=package_root,
                now=_NOW,
            )

        self.assertEqual(result.scan_integrity.readable, True)
        self.assertEqual(result.scan_integrity.python_file_count, 1)
        self.assertEqual(result.scan_integrity.parse_failures, ("broken.py",))
        self.assertEqual(
            result.network_import_findings,
            (
                trust_self_check.StaticImportFinding(
                    relative_path="valid.py",
                    line=1,
                    module="socket",
                ),
            ),
        )
        self.assertIn("valid.py:1 imports socket", _row(result, "network").evidence)
        self.assertNotIn("scan did not run", _row(result, "network").claim)
        self.assertIn(
            "some package files could not be parsed",
            _row(result, "drivers").evidence,
        )
        self.assertIn("broken.py", _row(result, "drivers").evidence)
        self.assertEqual(
            _row(result, "drivers").claim,
            "Driver/virtual-device scan did not run: no footprint claim is made for this run.",
        )

    def test_normal_tree_clean_rows_use_parsed_python_count(self) -> None:
        result = trust_self_check.build_trust_self_check(now=_NOW)

        self.assertTrue(result.scan_integrity.readable)
        self.assertGreater(result.scan_integrity.python_file_count, 0)
        self.assertEqual(result.scan_integrity.parse_failures, ())
        self.assertTrue(result.scan_integrity.entry_module_scanned)
        self.assertEqual(
            result.package_file_count,
            result.scan_integrity.python_file_count,
        )
        self.assertIn(
            f"({result.scan_integrity.python_file_count} Python files)",
            _row(result, "network").evidence,
        )
        self.assertIn(
            f"across {result.scan_integrity.python_file_count} package file(s)",
            _row(result, "drivers").evidence,
        )

    def test_entry_module_is_scanned_when_present_and_optional_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            package_root = parent / "zd_app"
            package_root.mkdir()
            (package_root / "__init__.py").write_text("", encoding="utf-8")
            (parent / "main_zd.py").write_text("import socket\n", encoding="utf-8")

            included = trust_self_check.build_trust_self_check(
                package_root=package_root,
                now=_NOW,
            )

        self.assertIn(
            trust_self_check.StaticImportFinding(
                relative_path="main_zd.py",
                line=1,
                module="socket",
            ),
            included.network_import_findings,
        )
        self.assertEqual(included.scan_integrity.python_file_count, 2)
        self.assertTrue(included.scan_integrity.entry_module_scanned)

        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp) / "zd_app"
            package_root.mkdir()
            (package_root / "__init__.py").write_text("", encoding="utf-8")
            absent = trust_self_check.build_trust_self_check(
                package_root=package_root,
                now=_NOW,
            )

        self.assertTrue(absent.scan_integrity.readable)
        self.assertEqual(absent.scan_integrity.python_file_count, 1)
        self.assertEqual(absent.scan_integrity.parse_failures, ())
        self.assertFalse(absent.scan_integrity.entry_module_scanned)
        self.assertEqual(
            _row(absent, "network").evidence,
            "Static scan of zd_app (1 Python files) found 0 imports of "
            "socket/http/urllib/requests/ssl. The entry module was not present "
            "as a source file in this build and was not scanned. No webbrowser.open "
            "handoff was found in zd_app.",
        )
        self.assertNotIn(
            "main_zd.py",
            _row(absent, "network").evidence,
        )

    def test_path_output_uses_env_placeholders_without_raw_home_path(self) -> None:
        env = {
            "APPDATA": r"C:\Users\Avery Stone\AppData\Roaming",
            "LOCALAPPDATA": r"C:\Users\Avery Stone\AppData\Local",
        }
        with patch.dict(os.environ, env, clear=False):
            result = trust_self_check.build_trust_self_check(
                executable_path=(
                    r"C:\Users\Avery Stone\AppData\Local\Programs"
                    r"\LegendCTL\ZD Ultimate Legend.exe"
                ),
                user_data_dir=(
                    r"C:\Users\Avery Stone\AppData\Roaming\ZDUltimateLegend"
                ),
                frozen=True,
                now=_NOW,
            )

        combined = result.to_text() + result.to_markdown()
        self.assertNotIn("Avery Stone", combined)
        self.assertIn(r"%APPDATA%\ZDUltimateLegend", combined)
        self.assertIn(
            "%LOCALAPPDATA%\\\u2026\\ZD Ultimate Legend.exe",
            combined,
        )
        self.assertNotIn(r"%LOCALAPPDATA%\Programs\LegendCTL", combined)

    def test_deep_home_rooted_display_path_collapses_intermediate_segments(self) -> None:
        env = {"USERPROFILE": r"C:\Users\Avery Stone"}
        with patch.dict(os.environ, env, clear=False):
            display = trust_self_check._display_path(
                r"C:\Users\Avery Stone\Documents\claude code"
                r"\legendctl-cut-2026-06-30\local-install\ZD Ultimate Legend.exe"
            )

        self.assertEqual(
            display,
            "%USERPROFILE%\\\u2026\\ZD Ultimate Legend.exe",
        )
        self.assertNotIn("Avery Stone", display)
        self.assertNotIn("Documents", display)
        self.assertNotIn("claude code", display)
        self.assertNotIn("legendctl-cut-2026-06-30", display)
        self.assertNotIn("local-install", display)

    def test_appdata_single_leaf_display_path_is_unchanged(self) -> None:
        env = {"APPDATA": r"C:\Users\Avery Stone\AppData\Roaming"}
        with patch.dict(os.environ, env, clear=False):
            display = trust_self_check._display_path(
                r"C:\Users\Avery Stone\AppData\Roaming\ZDUltimateLegend"
            )

        self.assertEqual(display, r"%APPDATA%\ZDUltimateLegend")

    def test_non_env_program_files_display_path_uses_scrub_fallback(self) -> None:
        env = {
            "APPDATA": r"C:\Users\Avery Stone\AppData\Roaming",
            "LOCALAPPDATA": r"C:\Users\Avery Stone\AppData\Local",
            "USERPROFILE": r"C:\Users\Avery Stone",
        }
        with patch.dict(os.environ, env, clear=False):
            path = r"C:\Program Files\LegendCTL\ZD Ultimate Legend.exe"
            display = trust_self_check._display_path(path)

        self.assertEqual(display, trust_self_check.scrub_paths(path))
        self.assertNotIn("\u2026", display)

    def test_forbidden_overclaim_phrases_are_absent(self) -> None:
        result = trust_self_check.build_trust_self_check(now=_NOW)
        lowered = result.to_text().lower() + result.to_markdown().lower()

        for phrase in (
            "guaranteed",
            "malware",
            "anti-cheat",
            "system clean",
            "pii-free",
            "safe for every game",
        ):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, lowered)

    def test_markdown_renderer_escapes_interpolated_metacharacters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp) / "zd_app"
            package_root.mkdir()
            (package_root / "__init__.py").write_text("", encoding="utf-8")
            with patch.object(
                trust_self_check.app_version,
                "__version__",
                "2|[demo](x)`",
            ), patch.object(
                trust_self_check.app_version,
                "__build_commit__",
                "abc|[sha](x)",
            ):
                result = trust_self_check.build_trust_self_check(
                    package_root=package_root,
                    executable_path=r"C:\Users\Avery Stone\Legend|CTL.exe",
                    user_data_dir=r"C:\Users\Avery Stone\AppData\Roaming\ZDUltimateLegend",
                    now=_NOW,
                )

        markdown = result.to_markdown()
        self.assertIn(r"2\|\[demo\]\(x\)\`", markdown)
        self.assertIn(r"abc\|\[sha\]\(x\)", markdown)
        self.assertIn(r"Legend\|CTL.exe", markdown)
        self.assertNotIn("Avery Stone", markdown)
        self.assertNotIn("2|[demo](x)`", markdown)

    def test_copy_export_markdown_snapshot_stays_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp) / "zd_app"
            package_root.mkdir()
            (package_root / "__init__.py").write_text("", encoding="utf-8")
            (Path(tmp) / "main_zd.py").write_text("", encoding="utf-8")
            env = {
                "APPDATA": r"C:\Users\Avery Stone\AppData\Roaming",
                "USERPROFILE": r"C:\Users\Avery Stone",
            }
            with patch.dict(os.environ, env, clear=False), patch.object(
                trust_self_check.app_version,
                "__version__",
                "2.3.1",
            ), patch.object(
                trust_self_check.app_version,
                "__build_commit__",
                "abc123",
            ), patch.object(
                trust_self_check.app_version,
                "__build_date__",
                "2026-07-01",
            ), patch.object(os, "getpid", return_value=4242):
                result = trust_self_check.build_trust_self_check(
                    package_root=package_root,
                    executable_path=(
                        r"C:\Users\Avery Stone\LegendCTL\python.exe"
                    ),
                    user_data_dir=(
                        r"C:\Users\Avery Stone\AppData\Roaming\ZDUltimateLegend"
                    ),
                    frozen=False,
                    now=_NOW,
                )

        self.assertTrue(result.scan_integrity.entry_module_scanned)
        expected = r"""# Trust Self-Check

Copyable evidence for this run. Observed for THIS process THIS session - not a system-wide audit.

- Generated: 2026-06-30T12:00:00+00:00
- Version: 2.3.1
- Build commit: abc123
- Build date: 2026-07-01
- Run mode: source run \(not frozen\)

| Claim | Evidence | Boundary |
| --- | --- | --- |
| No network: this build imports no networking modules. | Static scan of zd_app + main_zd.py \(2 Python files\) found 0 imports of socket/http/urllib/requests/ssl. No webbrowser.open handoff was found in zd_app. | Observed for THIS process THIS session - not a system-wide audit. |
| No drivers / virtual devices: this shipped app footprint contains no driver or virtual-device package artifacts. | Static scan of zd_app found 0 driver/virtual-device artifacts across 2 package file\(s\). | Observed for THIS process THIS session - not a system-wide audit. This is an app-footprint check, not a whole-PC or game-compatibility clearance. |
| No background service / autostart: this app installs no service and registers nothing to start with Windows; closing the window stops this process. | Design property of this build - the app ships no service or autostart installer \(see the import/packaging gates\). This session: source run \(not frozen\); process id 4242; executable path \(scrubbed\): %USERPROFILE%\\LegendCTL\\python.exe. Windows services, scheduled tasks, and Run keys are not inspected. | Observed for THIS process THIS session - not a system-wide audit. |
| Local data location + scrub posture: app data stays local and displayed paths are scrubbed to placeholders. | Default data directory \(scrubbed\): %APPDATA%\\ZDUltimateLegend. Copy/export text uses path scrubbing and Markdown escaping before it is shareable. | Observed for THIS process THIS session - not a system-wide audit. |
| Build identity: report what this process can observe. | Version 2.3.1; build commit abc123; build date 2026-07-01; run mode source run \(not frozen\). | Observed for THIS process THIS session - not a system-wide audit. |
"""
        self.assertEqual(result.to_markdown(), expected)

    def test_copy_export_text_snapshot_stays_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp) / "zd_app"
            package_root.mkdir()
            (package_root / "__init__.py").write_text("", encoding="utf-8")
            (Path(tmp) / "main_zd.py").write_text("", encoding="utf-8")
            env = {
                "APPDATA": r"C:\Users\Avery Stone\AppData\Roaming",
                "USERPROFILE": r"C:\Users\Avery Stone",
            }
            with patch.dict(os.environ, env, clear=False), patch.object(
                trust_self_check.app_version,
                "__version__",
                "2.3.1",
            ), patch.object(
                trust_self_check.app_version,
                "__build_commit__",
                "abc123",
            ), patch.object(
                trust_self_check.app_version,
                "__build_date__",
                "2026-07-01",
            ), patch.object(os, "getpid", return_value=4242):
                result = trust_self_check.build_trust_self_check(
                    package_root=package_root,
                    executable_path=(
                        r"C:\Users\Avery Stone\LegendCTL\python.exe"
                    ),
                    user_data_dir=(
                        r"C:\Users\Avery Stone\AppData\Roaming\ZDUltimateLegend"
                    ),
                    frozen=False,
                    now=_NOW,
                )

        self.assertTrue(result.scan_integrity.entry_module_scanned)
        expected = r"""Trust Self-Check
Copyable evidence for this run. Observed for THIS process THIS session - not a system-wide audit.

Generated: 2026-06-30T12:00:00+00:00
Version: 2.3.1
Build commit: abc123
Build date: 2026-07-01
Run mode: source run (not frozen)

No network: this build imports no networking modules.
  Static scan of zd_app + main_zd.py (2 Python files) found 0 imports of socket/http/urllib/requests/ssl. No webbrowser.open handoff was found in zd_app.
  Observed for THIS process THIS session - not a system-wide audit.

No drivers / virtual devices: this shipped app footprint contains no driver or virtual-device package artifacts.
  Static scan of zd_app found 0 driver/virtual-device artifacts across 2 package file(s).
  Observed for THIS process THIS session - not a system-wide audit. This is an app-footprint check, not a whole-PC or game-compatibility clearance.

No background service / autostart: this app installs no service and registers nothing to start with Windows; closing the window stops this process.
  Design property of this build - the app ships no service or autostart installer (see the import/packaging gates). This session: source run (not frozen); process id 4242; executable path (scrubbed): %USERPROFILE%\LegendCTL\python.exe. Windows services, scheduled tasks, and Run keys are not inspected.
  Observed for THIS process THIS session - not a system-wide audit.

Local data location + scrub posture: app data stays local and displayed paths are scrubbed to placeholders.
  Default data directory (scrubbed): %APPDATA%\ZDUltimateLegend. Copy/export text uses path scrubbing and Markdown escaping before it is shareable.
  Observed for THIS process THIS session - not a system-wide audit.

Build identity: report what this process can observe.
  Version 2.3.1; build commit abc123; build date 2026-07-01; run mode source run (not frozen).
  Observed for THIS process THIS session - not a system-wide audit.
"""
        self.assertEqual(result.to_text(), expected)

    def test_model_fingerprint_block_renders_fields_without_serial(self) -> None:
        fingerprint = ModelFingerprint(
            vid=0x413D,
            pid=0x2104,
            version_number=0x0124,
            product_string="ZD Ultimate Legend",
            manufacturer_string="ZD",
            usage_page=0xFF00,
            usage=0x0001,
            input_report_len=64,
            output_report_len=65,
            feature_report_len=17,
            button_caps_count=10,
            value_caps_count=6,
            interface_inventory=InterfaceInventory(count=3, mi_indices=(0, 1, 2)),
        )

        result = trust_self_check.build_trust_self_check(
            package_root=Path("zd_app"),
            model_fingerprint=fingerprint,
            now=_NOW,
        )
        text = result.to_text()
        markdown = result.to_markdown()

        self.assertIn("Model fingerprint", text)
        self.assertIn(fingerprint.short_digest or "", text)
        self.assertIn("VID: 0x413D", text)
        self.assertIn("MI_00, MI_01, MI_02", text)
        self.assertIn("Write validation basis: ZD Ultimate Legend (wired USB)", text)
        self.assertNotIn("serial", (text + markdown).lower())


class FrozenTrustManifestWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def _write_manifest(
        self,
        package_root: Path,
        *,
        executable: Path,
        payload: Path,
    ) -> None:
        payload_hash = hashlib.sha256(payload.read_bytes()).hexdigest()
        executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
        document = {
            "schema": 1,
            "version": "2.3.1",
            "build_commit": "abcdef0" + "1" * 33,
            "build_commit_short": "abcdef0",
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
                "browser_handoffs": [
                    {
                        "relative_path": "zd_app/ui/about.py",
                        "line": 12,
                        "call": "webbrowser.open",
                    }
                ],
                "driver_footprint_findings": [],
                "source_files": {
                    "main_zd.py": "0" * 64,
                    "zd_app/__init__.py": "1" * 64,
                },
            },
            "payload_files": {
                executable.name: executable_hash,
                "_internal/payload.dat": payload_hash,
            },
        }
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / trust_self_check.TRUST_MANIFEST_FILENAME).write_text(
            json.dumps(document), encoding="utf-8"
        )

    def _frozen_layout(self, temporary: str) -> tuple[Path, Path, Path]:
        dist_root = Path(temporary) / "ZDUltimateLegend"
        package_root = dist_root / "_internal" / "zd_app"
        executable = dist_root / "LegendCTL.exe"
        payload = dist_root / "_internal" / "payload.dat"
        payload.parent.mkdir(parents=True)
        executable.write_bytes(b"exe")
        payload.write_bytes(b"payload")
        self._write_manifest(package_root, executable=executable, payload=payload)
        return package_root, executable, payload

    def test_frozen_matching_manifest_uses_recorded_evidence_in_exports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            trust_self_check.app_version, "__build_commit__", "abcdef0"
        ):
            package_root, executable, _payload = self._frozen_layout(temporary)
            result = trust_self_check.build_trust_self_check(
                package_root=package_root,
                executable_path=executable,
                frozen=True,
                now=_NOW,
            )

        network = _row(result, "network")
        self.assertTrue(result.manifest_present)
        self.assertTrue(result.manifest_valid)
        self.assertIsNotNone(result.payload_verification)
        self.assertEqual(result.payload_verification.matched, 1)
        self.assertEqual(network.claim, "No network: this build imports no networking modules.")
        self.assertIn("Build-time scan", network.evidence)
        self.assertIn("webbrowser.open", network.evidence)
        self.assertEqual(network.boundary, i18n.t("trust_self_check.manifest.boundary"))
        self.assertIn("Build-recorded EXE SHA-256", result.to_markdown())
        card = build_share_card(trust_self_check=result, now=_NOW)
        self.assertIn("Build-time scan", card.to_markdown() + card.to_html())

    def test_skew_and_payload_mismatch_stay_on_unverified_warning_branches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package_root, executable, payload = self._frozen_layout(temporary)
            with patch.object(
                trust_self_check.app_version, "__build_commit__", "different"
            ):
                skew = trust_self_check.build_trust_self_check(
                    package_root=package_root,
                    executable_path=executable,
                    frozen=True,
                    now=_NOW,
                )
            self.assertIsNone(skew.payload_verification)
            self.assertEqual(
                _row(skew, "network").claim,
                "Network scan did not run: no networking claim is made for this run.",
            )
            self.assertIn("does not match this build", skew.to_text())

            payload.write_bytes(b"corrupted")
            with patch.object(
                trust_self_check.app_version, "__build_commit__", "abcdef0"
            ):
                mismatch = trust_self_check.build_trust_self_check(
                    package_root=package_root,
                    executable_path=executable,
                    frozen=True,
                    now=_NOW,
                )
            self.assertFalse(mismatch.payload_verification.clean)
            self.assertEqual(
                _row(mismatch, "drivers").claim,
                "Driver/virtual-device scan did not run: no footprint claim is made for this run.",
            )
            self.assertIn("do not match the build record", mismatch.to_markdown())

    def test_manifest_loader_is_unreachable_in_dev_and_live_scan_branches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package_root = Path(temporary) / "zd_app"
            package_root.mkdir()
            (package_root / "__init__.py").write_text("", encoding="utf-8")
            with patch.object(
                trust_self_check,
                "load_trust_manifest",
                side_effect=AssertionError("manifest loader must not run"),
            ):
                dev_result = trust_self_check.build_trust_self_check(
                    package_root=package_root,
                    frozen=False,
                    now=_NOW,
                )
                frozen_live_result = trust_self_check.build_trust_self_check(
                    package_root=package_root,
                    frozen=True,
                    now=_NOW,
                )

        self.assertIsNone(dev_result.manifest)
        self.assertIsNone(frozen_live_result.manifest)
        self.assertIn("Static scan of zd_app", _row(dev_result, "network").evidence)
        self.assertIn("Static scan of zd_app", _row(frozen_live_result, "network").evidence)

    def test_deep_frozen_manifest_degrades_to_invalid_diagnostics_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package_root = Path(temporary) / "zd_app"
            package_root.mkdir()
            depth = 1100
            (package_root / trust_self_check.TRUST_MANIFEST_FILENAME).write_text(
                "[" * depth + "0" + "]" * depth,
                encoding="utf-8",
            )

            result = trust_self_check.build_trust_self_check(
                package_root=package_root,
                executable_path=Path(temporary) / "LegendCTL.exe",
                frozen=True,
                now=_NOW,
            )

        self.assertTrue(result.manifest_present)
        self.assertFalse(result.manifest_valid)
        self.assertIsNone(result.payload_verification)
        self.assertEqual(
            _row(result, "network").claim,
            "Network scan did not run: no networking claim is made for this run.",
        )
        self.assertIn(
            "the build record is unreadable or invalid",
            _row(result, "network").evidence,
        )


class TrustSelfCheckI18nTests(unittest.TestCase):
    def test_en_zh_cn_have_matching_trust_self_check_keys(self) -> None:
        en = json.loads((_LOCALE_DIR / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((_LOCALE_DIR / "zh-CN.json").read_text(encoding="utf-8"))

        en_keys = {key for key in en if key.startswith("trust_self_check.")}
        zh_keys = {key for key in zh if key.startswith("trust_self_check.")}

        self.assertEqual(en_keys, zh_keys)
        self.assertGreater(len(en_keys), 20)

    def test_trust_self_check_locale_placeholders_match(self) -> None:
        en = json.loads((_LOCALE_DIR / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((_LOCALE_DIR / "zh-CN.json").read_text(encoding="utf-8"))

        for key in (k for k in en if k.startswith("trust_self_check.")):
            en_placeholders = set(_placeholders(en[key]))
            zh_placeholders = set(_placeholders(zh[key]))
            with self.subTest(key=key):
                self.assertEqual(en_placeholders, zh_placeholders)


def _placeholders(value: str) -> list[str]:
    parts = []
    cursor = 0
    while True:
        start = value.find("{", cursor)
        if start == -1:
            return parts
        end = value.find("}", start)
        if end == -1:
            return parts
        parts.append(value[start + 1:end])
        cursor = end + 1


_MANIFEST_COPY_PINNED = {
    "en": {
        "trust_self_check.manifest.evidence.network_clean": "Build-time scan of {py_count} source files at commit {commit}: no {modules} imports. This session: {matched} of {total} shipped files match the build record.",
        "trust_self_check.manifest.evidence.drivers_clean": "Build-time scan of {py_count} source files at commit {commit}: no driver/virtual-device artifacts. This session: {matched} of {total} shipped files match the build record.",
        "trust_self_check.manifest.evidence.payload_warning": "{bad_count} shipped files do not match the build record ({details}).",
        "trust_self_check.manifest.evidence.extras_note": "{extra_count} files present that the build record does not list.",
        "trust_self_check.manifest.boundary": "Scan results recorded at build time, not re-run this session. The app cannot verify its own EXE from inside; verify downloads externally against the release attestation and SHA256SUMS.",
        "trust_self_check.manifest.reason.skew": "the build record does not match this build",
        "trust_self_check.manifest.reason.invalid": "the build record is unreadable or invalid",
        "trust_self_check.manifest.exe_recorded": "Build-recorded EXE SHA-256: {hash} (verify from outside the app).",
    },
    "zh-CN": {
        "trust_self_check.manifest.evidence.network_clean": "构建时扫描了 {py_count} 个源文件（提交 {commit}）：未发现 {modules} 导入。本次会话：{matched}/{total} 个打包文件与构建记录一致。",
        "trust_self_check.manifest.evidence.drivers_clean": "构建时扫描了 {py_count} 个源文件（提交 {commit}）：未发现驱动或虚拟设备痕迹。本次会话：{matched}/{total} 个打包文件与构建记录一致。",
        "trust_self_check.manifest.evidence.payload_warning": "{bad_count} 个打包文件与构建记录不一致（{details}）。",
        "trust_self_check.manifest.evidence.extras_note": "存在 {extra_count} 个构建记录中未列出的文件。",
        "trust_self_check.manifest.boundary": "扫描结果记录于构建时，并非本次会话的实时扫描。应用无法从内部验证自身的 EXE；请通过发布证明（attestation）与 SHA256SUMS 在外部验证下载文件。",
        "trust_self_check.manifest.reason.skew": "构建记录与当前构建不匹配",
        "trust_self_check.manifest.reason.invalid": "构建记录无法读取或无效",
        "trust_self_check.manifest.exe_recorded": "构建记录的 EXE SHA-256：{hash}（请在应用之外验证）。",
    },
}


class ManifestCopyBytePinTests(unittest.TestCase):
    """Researcher-authored manifest copy is byte-pinned in BOTH locales."""

    def test_manifest_copy_is_byte_exact_in_both_locales(self) -> None:
        for locale, pinned in _MANIFEST_COPY_PINNED.items():
            with (_LOCALE_DIR / f"{locale}.json").open(encoding="utf-8") as handle:
                data = json.load(handle)
            for key, expected in pinned.items():
                self.assertEqual(
                    data.get(key),
                    expected,
                    f"{locale}:{key} drifted from the researcher-pinned copy",
                )


if __name__ == "__main__":
    unittest.main()
