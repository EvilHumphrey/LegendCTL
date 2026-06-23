from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from zd_app.protocol.preflight_visibility import (
    APP_PATH_ENV_VAR,
    OfficialUiProbe,
    run_official_ui_probe,
    summarize_preflight,
)
from zd_app.protocol.trigger_interface import PublicProbeResult


class PreflightTransportVisibilityTests(unittest.TestCase):
    def test_summarize_ready_for_settings(self) -> None:
        result = summarize_preflight(
            launch_official=False,
            official_ui=OfficialUiProbe(
                app_running=True,
                launched=False,
                main_window="ZDGame",
                settings_window=None,
                device_settings_button=True,
                no_device_connected=False,
                connect_via_usb=False,
                visible_names=("Device Settings",),
            ),
            public_paths=["public"],
            public_probe=PublicProbeResult(
                path="public",
                attempted=True,
                ok=True,
                last_error=0,
                access_mode="public_identify",
                out_hex="03010100000000003d410421",
                matched_expected=True,
            ),
            hidden_visible_paths=[],
        )

        self.assertEqual(result.state, "ready_for_settings")

    def test_summarize_device_missing(self) -> None:
        result = summarize_preflight(
            launch_official=True,
            official_ui=OfficialUiProbe(
                app_running=True,
                launched=True,
                main_window="ZDGame",
                settings_window=None,
                device_settings_button=False,
                no_device_connected=True,
                connect_via_usb=True,
                visible_names=("No ZD device connected", "Please select a device and connect via USB"),
            ),
            public_paths=[],
            public_probe=PublicProbeResult(
                path=None,
                attempted=False,
                ok=False,
                last_error=0,
                access_mode=None,
                out_hex="",
                matched_expected=False,
            ),
            hidden_visible_paths=[],
        )

        self.assertEqual(result.state, "device_missing")

    def test_summarize_public_visible_ui_mismatch(self) -> None:
        result = summarize_preflight(
            launch_official=True,
            official_ui=OfficialUiProbe(
                app_running=True,
                launched=False,
                main_window="ZDGame",
                settings_window=None,
                device_settings_button=False,
                no_device_connected=True,
                connect_via_usb=True,
                visible_names=("No ZD device connected",),
            ),
            public_paths=["public"],
            public_probe=PublicProbeResult(
                path="public",
                attempted=True,
                ok=True,
                last_error=0,
                access_mode="public_identify",
                out_hex="03010100000000003d410421",
                matched_expected=True,
            ),
            hidden_visible_paths=[],
        )

        self.assertEqual(result.state, "public_visible_ui_mismatch")


def _stub_completed_process(payload: str = "{}") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=payload,
        stderr="",
    )


class RunOfficialUiProbeAppPathTests(unittest.TestCase):
    """DEFAULT_APP is configurable; resolution prefers arg > env > None."""

    def test_no_app_path_omits_apppath_arg(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch(
                "zd_app.protocol.preflight_visibility.subprocess.run",
                return_value=_stub_completed_process(),
            ) as run_mock:
                run_official_ui_probe(launch_official=False)

        argv = run_mock.call_args.args[0]
        self.assertNotIn("-AppPath", argv)
        self.assertNotIn("-LaunchIfMissing", argv)

    def test_app_path_from_env_var(self) -> None:
        with mock.patch.dict(os.environ, {APP_PATH_ENV_VAR: "C:\\test-env.exe"}, clear=True):
            with mock.patch(
                "zd_app.protocol.preflight_visibility.subprocess.run",
                return_value=_stub_completed_process(),
            ) as run_mock:
                run_official_ui_probe(launch_official=False)

        argv = run_mock.call_args.args[0]
        self.assertIn("-AppPath", argv)
        self.assertEqual(argv[argv.index("-AppPath") + 1], "C:\\test-env.exe")

    def test_arg_overrides_env_var(self) -> None:
        with mock.patch.dict(os.environ, {APP_PATH_ENV_VAR: "C:\\env.exe"}, clear=True):
            with mock.patch(
                "zd_app.protocol.preflight_visibility.subprocess.run",
                return_value=_stub_completed_process(),
            ) as run_mock:
                run_official_ui_probe(launch_official=False, app_path=Path("C:\\arg.exe"))

        argv = run_mock.call_args.args[0]
        self.assertIn("-AppPath", argv)
        self.assertEqual(argv[argv.index("-AppPath") + 1], "C:\\arg.exe")

    def test_launch_official_without_app_path_raises(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                run_official_ui_probe(launch_official=True)


if __name__ == "__main__":
    unittest.main()
