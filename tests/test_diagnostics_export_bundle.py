"""Privacy/hardening tests for the diagnostics export bundle."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zd_app.models import DeviceState
from zd_app.services.diagnostics_service import DiagnosticsService, _redact_instance_id


class RedactInstanceIdTests(unittest.TestCase):
    def test_drops_unit_specific_tail_keeps_vid_pid(self) -> None:
        self.assertEqual(
            _redact_instance_id("USB\\VID_413D&PID_2104\\ABC123DEF456"),
            "USB\\VID_413D&PID_2104",
        )
        self.assertEqual(
            _redact_instance_id("HID\\VID_2DC8&PID_3106&MI_02\\7&1a2b3c4d&0&0000"),
            "HID\\VID_2DC8&PID_3106",
        )

    def test_non_pnp_identifiers_pass_through(self) -> None:
        self.assertEqual(_redact_instance_id("xinput-slot-0"), "xinput-slot-0")
        self.assertEqual(_redact_instance_id("unknown"), "unknown")
        self.assertEqual(_redact_instance_id(""), "")
        self.assertEqual(_redact_instance_id(None), "")


class ExportBundlePrivacyTests(unittest.TestCase):
    def test_bundle_redacts_id_and_uses_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as data_root:
            with patch.dict(os.environ, {"ZDUL_DATA_DIR": data_root}):
                state = DeviceState(
                    stable_identifier="USB\\VID_413D&PID_2104\\ABC123DEF456",
                )
                # A field that is NOT on the bundle allowlist must not leak even
                # though it lives in device_state.__dict__.
                state.injected_secret = "ZZZ_SHOULD_NOT_LEAK"  # type: ignore[attr-defined]

                service = DiagnosticsService()
                bundle_path = service.export_bundle(state, 1.0, 2.0, data_root)

                raw = bundle_path.read_text(encoding="utf-8")
                bundle = json.loads(raw)

        # The unit-specific instance-ID tail never reaches the file...
        self.assertNotIn("ABC123DEF456", raw)
        # ...and a non-allowlisted field never reaches the file.
        self.assertNotIn("ZZZ_SHOULD_NOT_LEAK", raw)
        self.assertNotIn("injected_secret", raw)

        # Both ID surfaces (device_state + snapshot.device_id) are redacted to
        # the model-level VID/PID prefix.
        self.assertEqual(
            bundle["device_state"]["stable_identifier"], "USB\\VID_413D&PID_2104"
        )
        self.assertEqual(bundle["snapshot"]["device_id"], "USB\\VID_413D&PID_2104")

        # The bundle is built from the explicit allowlist, not __dict__.
        self.assertEqual(
            set(bundle["device_state"]),
            {
                "product_name",
                "stable_identifier",
                "connection_mode",
                "firmware_version",
                "battery_level",
                "sleep_setting",
                "active_onboard_profile",
                "sync_status",
                "connection_state",
                "data_freshness",
                "supported_capabilities",
                "summary_sources",
                "xinput_slot",
                "last_read_time",
                "last_apply_time",
            },
        )


class ExportBundlePathConstraintTests(unittest.TestCase):
    def test_dir_inside_user_data_root_is_honored(self) -> None:
        with tempfile.TemporaryDirectory() as data_root:
            with patch.dict(os.environ, {"ZDUL_DATA_DIR": data_root}):
                target = str(Path(data_root) / "diagnostics")
                service = DiagnosticsService()
                bundle_path = service.export_bundle(DeviceState(), None, None, target)

            self.assertEqual(bundle_path.parent.resolve(), Path(target).resolve())
            self.assertTrue(bundle_path.exists())

    def test_dir_outside_user_data_root_is_constrained_to_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as data_root, tempfile.TemporaryDirectory() as escape_root:
            with patch.dict(os.environ, {"ZDUL_DATA_DIR": data_root}):
                service = DiagnosticsService()
                bundle_path = service.export_bundle(DeviceState(), None, None, escape_root)

            # Nothing was written to the attacker-controlled location...
            self.assertEqual(list(Path(escape_root).iterdir()), [])
            # ...the bundle landed in the safe default subdir under the root.
            self.assertEqual(
                bundle_path.parent.resolve(),
                (Path(data_root) / "diagnostics").resolve(),
            )
            self.assertTrue(bundle_path.exists())


class ExportBundleEventLogRedactionTests(unittest.TestCase):
    def test_event_log_paths_are_redacted(self) -> None:
        # Event-log entries routinely carry absolute paths (config file paths,
        # and the app logs the exported-bundle path on every export). The OS
        # username / home dir must not ship in the shareable bundle's event_log.
        with tempfile.TemporaryDirectory() as data_root:
            with patch.dict(os.environ, {"ZDUL_DATA_DIR": data_root}):
                service = DiagnosticsService()
                service.log_event(
                    r"Attempted path: C:\Users\secretuser\AppData\Roaming\ZDUltimateLegend\hidden.json"
                )
                service.log_i18n_event(
                    "log.diagnostics.exported_bundle",
                    path=r"C:\Users\secretuser\diagnostics\prev.json",
                )
                bundle_path = service.export_bundle(DeviceState(), 1.0, 2.0, data_root)
                raw = bundle_path.read_text(encoding="utf-8")
                bundle = json.loads(raw)

        self.assertNotIn("secretuser", raw)
        joined = "\n".join(bundle["snapshot"]["event_log"])
        self.assertNotIn("secretuser", joined)
        # Redaction keeps the basename so the log stays diagnostically useful.
        self.assertIn("hidden.json", joined)

    def test_event_log_spaced_username_is_redacted(self) -> None:
        # Regression: a display-name account ("Jane Doe") used to leak its first
        # name because the event-log redactor stopped at the first whitespace.
        with tempfile.TemporaryDirectory() as data_root:
            with patch.dict(os.environ, {"ZDUL_DATA_DIR": data_root}):
                service = DiagnosticsService()
                service.log_event(
                    r"Attempted path: C:\Users\Jane Doe\Documents\config.json"
                )
                bundle_path = service.export_bundle(DeviceState(), 1.0, 2.0, data_root)
                raw = bundle_path.read_text(encoding="utf-8")
                joined = "\n".join(json.loads(raw)["snapshot"]["event_log"])

        self.assertNotIn("Jane", raw)
        self.assertNotIn("Doe", raw)
        self.assertNotIn("Jane", joined)
        # Basename still kept.
        self.assertIn("config.json", joined)


if __name__ == "__main__":
    unittest.main()
