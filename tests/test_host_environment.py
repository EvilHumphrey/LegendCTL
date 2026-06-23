"""Unit tests for host-environment heuristics (VMware detection)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from zd_app.services.host_environment import (
    _default_process_lister,
    is_vmware_workstation_running,
    warn_if_vmware_usb_redirect_active,
)


class TestIsVmwareWorkstationRunning(unittest.TestCase):
    def test_returns_true_when_vmware_vmx_in_process_list(self) -> None:
        listing = ["chrome.exe", "vmware-vmx.exe", "explorer.exe"]
        self.assertTrue(is_vmware_workstation_running(process_lister=lambda: listing))

    def test_case_insensitive_match(self) -> None:
        listing = ["VMWARE-VMX.EXE"]
        self.assertTrue(is_vmware_workstation_running(process_lister=lambda: listing))

    def test_returns_false_when_vmware_vmx_absent(self) -> None:
        listing = ["chrome.exe", "explorer.exe"]
        self.assertFalse(is_vmware_workstation_running(process_lister=lambda: listing))

    def test_other_vmware_processes_alone_dont_trigger(self) -> None:
        # VMware Workstation has many helper processes; only vmware-vmx.exe
        # indicates an active running VM.
        listing = ["vmware.exe", "vmware-tray.exe", "vmware-authd.exe"]
        self.assertFalse(is_vmware_workstation_running(process_lister=lambda: listing))

    def test_returns_false_when_lister_raises(self) -> None:
        def explode():
            raise OSError("tasklist not on PATH")

        self.assertFalse(is_vmware_workstation_running(process_lister=explode))

    def test_default_process_lister_uses_silent_subprocess(self) -> None:
        result = type("Result", (), {"stdout": '"vmware-vmx.exe","123","Console","1","1 K"\n'})()

        with patch("zd_app.services.host_environment.silent_run", return_value=result) as run_mock:
            names = _default_process_lister()

        self.assertEqual(names, ["vmware-vmx.exe"])
        run_mock.assert_called_once()


class TestWarnIfVmwareUsbRedirectActive(unittest.TestCase):
    def test_emits_warning_when_vmware_running(self) -> None:
        captured: list[str] = []
        result = warn_if_vmware_usb_redirect_active(
            process_lister=lambda: ["vmware-vmx.exe"],
            write_warning=captured.append,
        )
        self.assertTrue(result)
        self.assertEqual(len(captured), 1)
        self.assertIn("VMware Workstation", captured[0])

    def test_silent_when_vmware_not_running(self) -> None:
        captured: list[str] = []
        result = warn_if_vmware_usb_redirect_active(
            process_lister=lambda: ["chrome.exe"],
            write_warning=captured.append,
        )
        self.assertFalse(result)
        self.assertEqual(captured, [])


if __name__ == "__main__":
    unittest.main()
