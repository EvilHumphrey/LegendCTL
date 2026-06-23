"""Tests for the vendored XInput ctypes bindings (``zd_app/services/xinput.py``).

B3 robustness hardening: the module is imported unconditionally at startup
(``main_zd.py`` -> ``device_service.py``). On a system where none of the XInput
DLLs load — or off-Windows, where ``ctypes`` has no ``windll`` — the load chain
must degrade to a ``None`` sentinel instead of aborting launch with a raw
traceback, and the public functions must no-op gracefully. (Never reached on the
shipped Win8+ target since ``xinput1_4.dll`` ships in-box — robustness only.)
"""

from __future__ import annotations

import unittest
from unittest import mock

from zd_app.services import xinput


class _RaisingWinDLL:
    """Stand-in for ``ctypes.windll`` where every DLL attribute fails to load."""

    def __getattr__(self, name):
        raise OSError(f"{name}.dll not found")


class XInputDllLoadFallbackTests(unittest.TestCase):
    def test_load_returns_none_when_windll_absent(self) -> None:
        """Off-Windows ``ctypes`` has no ``windll``; load degrades to None."""
        fake_ctypes = mock.Mock(spec=[])  # no ``windll`` attribute at all
        with mock.patch.object(xinput, "ctypes", fake_ctypes):
            self.assertIsNone(xinput._load_xinput())

    def test_load_returns_none_when_all_dlls_absent(self) -> None:
        fake_ctypes = mock.Mock()
        fake_ctypes.windll = _RaisingWinDLL()
        with mock.patch.object(xinput, "ctypes", fake_ctypes):
            self.assertIsNone(xinput._load_xinput())


class XInputDegradedSentinelTests(unittest.TestCase):
    """When no DLL loaded (``_xinput is None``), public calls degrade to
    ERROR_DEVICE_NOT_CONNECTED instead of raising AttributeError on a None
    dereference (the pre-fix import-time crash analogue)."""

    def setUp(self) -> None:
        self._saved = xinput._xinput
        xinput._xinput = None

    def tearDown(self) -> None:
        xinput._xinput = self._saved

    def test_get_state_returns_disconnected(self) -> None:
        code, state = xinput.get_state(0)
        self.assertEqual(code, xinput.ERROR_DEVICE_NOT_CONNECTED)
        self.assertIsInstance(state, xinput.XINPUT_STATE)

    def test_get_capabilities_returns_disconnected(self) -> None:
        code, caps = xinput.get_capabilities(0)
        self.assertEqual(code, xinput.ERROR_DEVICE_NOT_CONNECTED)
        self.assertIsInstance(caps, xinput.XINPUT_CAPABILITIES)

    def test_set_vibration_returns_disconnected(self) -> None:
        self.assertEqual(
            xinput.set_vibration(0, 100, 100), xinput.ERROR_DEVICE_NOT_CONNECTED
        )

    def test_get_connected_controllers_empty(self) -> None:
        self.assertEqual(xinput.get_connected_controllers(), [])

    def test_get_battery_information_returns_disconnected(self) -> None:
        code, battery = xinput.get_battery_information(0)
        self.assertEqual(code, xinput.ERROR_DEVICE_NOT_CONNECTED)
        self.assertIsNone(battery)

    def test_describe_battery_level_unknown(self) -> None:
        self.assertEqual(xinput.describe_battery_level(0), "Unknown")

    def test_decode_buttons_still_pure(self) -> None:
        # decode_buttons never touches the DLL; it must keep working regardless.
        self.assertEqual(
            xinput.decode_buttons(xinput.XINPUT_GAMEPAD_A), ["A"]
        )


if __name__ == "__main__":
    unittest.main()
