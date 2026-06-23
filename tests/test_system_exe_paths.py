"""Audit M7 — system executables are invoked by absolute System32 path.

Bare-name ``powershell`` / ``tasklist`` / ``pnputil`` are vulnerable to PATH
search-order hijacking (a planted exe earlier on PATH). Each call site now
anchors on ``%SystemRoot%\\System32``. These tests pin that invariant on the
module-level constants, so a regression back to a bare name is caught.

Windows-path assertions use ``ntpath`` so the test is correct on any platform
(this suite runs hardware-free on non-Windows too).
"""

from __future__ import annotations

import ntpath
import unittest

from zd_app.protocol import preflight_visibility
from zd_app.services import (
    device_service,
    host_environment,
    official_app_summary_service,
)


def _is_under_system32(path: str) -> bool:
    parts = [p.lower() for p in ntpath.normpath(path).split("\\")]
    return "system32" in parts


class SystemExePathTests(unittest.TestCase):
    def _assert_system32_exe(self, path: str, exe_name: str) -> None:
        self.assertTrue(ntpath.isabs(path), f"{path!r} is not an absolute path")
        self.assertTrue(_is_under_system32(path), f"{path!r} is not under System32")
        self.assertEqual(ntpath.basename(path).lower(), exe_name.lower())

    def test_tasklist_paths_absolute_under_system32(self) -> None:
        self._assert_system32_exe(host_environment._TASKLIST_EXE, "tasklist.exe")

    def test_pnputil_path_absolute_under_system32(self) -> None:
        self._assert_system32_exe(device_service._PNPUTIL_EXE, "pnputil.exe")

    def test_powershell_paths_absolute_under_system32(self) -> None:
        self._assert_system32_exe(preflight_visibility._POWERSHELL_EXE, "powershell.exe")
        self._assert_system32_exe(
            official_app_summary_service._POWERSHELL_EXE, "powershell.exe"
        )


if __name__ == "__main__":
    unittest.main()
