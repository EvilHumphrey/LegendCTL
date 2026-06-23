"""DEFAULT_UI_PROBE path resolution.

The preflight_visibility module lives in zd_app/protocol/, and the .ps1 it
expects beside SCRIPT_DIR must be packaged alongside the module (and added to
the PyInstaller datas). This smoke test pins both invariants so the dev-mode
regression cannot return.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zd_app.protocol import preflight_visibility


class PreflightProbePathResolutionTests(unittest.TestCase):
    def test_default_ui_probe_path_exists(self) -> None:
        self.assertTrue(
            preflight_visibility.DEFAULT_UI_PROBE.exists(),
            f"DEFAULT_UI_PROBE does not exist at {preflight_visibility.DEFAULT_UI_PROBE}",
        )

    def test_default_ui_probe_path_is_in_protocol_package(self) -> None:
        expected_dir = Path(preflight_visibility.__file__).resolve().parent
        self.assertEqual(preflight_visibility.DEFAULT_UI_PROBE.parent, expected_dir)

    def test_script_dir_uses_file_in_dev_mode(self) -> None:
        # Not frozen (a normal test run): resolve next to the module source.
        self.assertEqual(
            preflight_visibility._script_dir(),
            Path(preflight_visibility.__file__).resolve().parent,
        )

    def test_script_dir_uses_meipass_when_frozen(self) -> None:
        # Under PyInstaller the .ps1 is collected to _MEIPASS/zd_app/protocol
        # — the resolver must point there, not at __file__.
        fake_meipass = Path(tempfile.gettempdir()) / "zd_fake_meipass"
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", str(fake_meipass), create=True):
            resolved = preflight_visibility._script_dir()
        self.assertEqual(resolved, fake_meipass / "zd_app" / "protocol")


if __name__ == "__main__":
    unittest.main()
