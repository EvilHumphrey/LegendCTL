"""Tests for XInputPollService."""

from __future__ import annotations

import ctypes
import threading
import time
import unittest

from zd_app.services.settings_service import ControllerButtonTarget
from zd_app.services.xinput_poll_service import (
    XInputPollService,
    XInputSnapshot,
    _XINPUT_STATE,
    buttons_from_mask,
)


class _FakeDLL:
    """In-memory stand-in for the XInput DLL.

    Returns the configured ``rc`` from every ``XInputGetState`` call. Tests
    that need to inspect the polled state can read ``call_count``. When ``rc``
    is the device-not-connected code (1167), the service treats it as a
    disconnected controller without populating the state struct, which is
    exactly what we want for lifecycle / threading tests.
    """

    def __init__(self, *, rc: int) -> None:
        self.rc = rc
        self.call_count = 0
        self._lock = threading.Lock()

    def XInputGetState(self, user_index, state_byref):  # noqa: N802 — Win32 name
        with self._lock:
            self.call_count += 1
        return self.rc


class XInputButtonMaskTests(unittest.TestCase):
    def test_button_mask_translation_singletons(self) -> None:
        self.assertEqual(buttons_from_mask(0x1000), frozenset({ControllerButtonTarget.A}))
        self.assertEqual(buttons_from_mask(0x2000), frozenset({ControllerButtonTarget.B}))
        self.assertEqual(buttons_from_mask(0x4000), frozenset({ControllerButtonTarget.X}))
        self.assertEqual(buttons_from_mask(0x8000), frozenset({ControllerButtonTarget.Y}))
        self.assertEqual(buttons_from_mask(0x0001), frozenset({ControllerButtonTarget.UP}))

    def test_button_mask_translation_combined(self) -> None:
        # A + Y + LB pressed simultaneously
        mask = 0x1000 | 0x8000 | 0x0100
        self.assertEqual(
            buttons_from_mask(mask),
            frozenset({
                ControllerButtonTarget.A,
                ControllerButtonTarget.Y,
                ControllerButtonTarget.LB,
            }),
        )

    def test_button_mask_translation_empty(self) -> None:
        self.assertEqual(buttons_from_mask(0), frozenset())


class XInputServiceLifecycleTests(unittest.TestCase):
    def test_disconnected_initial_state(self) -> None:
        service = XInputPollService(dll_loader=lambda: _FakeDLL(rc=1167))
        snapshot = service.get_snapshot()
        self.assertFalse(snapshot.connected)
        self.assertTrue(snapshot.dll_available)

    def test_dll_unavailable_falls_through(self) -> None:
        service = XInputPollService(dll_loader=lambda: None)
        snapshot = service.get_snapshot()
        self.assertFalse(snapshot.connected)
        self.assertFalse(snapshot.dll_available)
        # start() is still callable but spawns no thread when DLL is missing.
        service.start()
        self.assertIsNone(service._thread)
        # stop() is also safe.
        service.stop()

    def test_thread_lifecycle(self) -> None:
        fake = _FakeDLL(rc=1167)
        service = XInputPollService(
            poll_interval_s=1 / 200,
            dll_loader=lambda: fake,
        )
        service.start()
        # Idempotent start
        service.start()
        # Let the worker run a few iterations
        time.sleep(0.05)
        self.assertGreater(fake.call_count, 0)
        service.stop()
        # Idempotent stop
        service.stop()
        self.assertIsNone(service._thread)

    def test_get_snapshot_thread_safe(self) -> None:
        fake = _FakeDLL(rc=1167)
        service = XInputPollService(
            poll_interval_s=1 / 500,
            dll_loader=lambda: fake,
        )
        service.start()
        try:
            errors: list[BaseException] = []

            def reader() -> None:
                try:
                    for _ in range(500):
                        snap = service.get_snapshot()
                        # snap is a frozen dataclass; basic attribute access
                        # must not race with the writer thread.
                        _ = snap.connected, snap.left_trigger, snap.buttons
                except BaseException as exc:  # pragma: no cover — failure path
                    errors.append(exc)

            threads = [threading.Thread(target=reader) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=2.0)
            self.assertFalse(errors, f"Concurrent reads raised: {errors!r}")
        finally:
            service.stop()


class XInputSnapshotShapeTests(unittest.TestCase):
    def test_disconnected_factory(self) -> None:
        snap = XInputSnapshot.disconnected()
        self.assertFalse(snap.connected)
        self.assertTrue(snap.dll_available)
        self.assertEqual(snap.buttons, frozenset())
        self.assertEqual(snap.left_trigger, 0)
        self.assertEqual(snap.left_stick_x, 0)

    def test_unavailable_factory(self) -> None:
        snap = XInputSnapshot.unavailable()
        self.assertFalse(snap.connected)
        self.assertFalse(snap.dll_available)


class XInputSnapshotNormalizedHelperTests(unittest.TestCase):
    def _snapshot(self, **overrides) -> XInputSnapshot:
        defaults = dict(
            connected=True,
            dll_available=True,
            packet_number=0,
            buttons=frozenset(),
            left_trigger=0,
            right_trigger=0,
            left_stick_x=0,
            left_stick_y=0,
            right_stick_x=0,
            right_stick_y=0,
        )
        defaults.update(overrides)
        return XInputSnapshot(**defaults)

    def test_zero_centers(self) -> None:
        snap = self._snapshot()
        self.assertEqual(snap.left_stick_normalized, (0.0, 0.0))
        self.assertEqual(snap.right_stick_normalized, (0.0, 0.0))

    def test_half_right_left_stick(self) -> None:
        snap = self._snapshot(left_stick_x=16384, left_stick_y=0)
        nx, ny = snap.left_stick_normalized
        self.assertAlmostEqual(nx, 0.5, places=4)
        self.assertAlmostEqual(ny, 0.0, places=4)

    def test_full_range_within_unit_interval(self) -> None:
        # XInput SHORT positive max 32767 / 32768 = 0.99997...
        max_pos = self._snapshot(left_stick_x=32767, left_stick_y=32767)
        nx, ny = max_pos.left_stick_normalized
        self.assertGreaterEqual(nx, 0.0)
        self.assertLessEqual(nx, 1.0)
        self.assertGreaterEqual(ny, 0.0)
        self.assertLessEqual(ny, 1.0)
        # Negative max is the asymmetric end (-32768 / 32768 = -1.0 exactly)
        max_neg = self._snapshot(left_stick_x=-32768, left_stick_y=-32768)
        nx, ny = max_neg.left_stick_normalized
        self.assertEqual(nx, -1.0)
        self.assertEqual(ny, -1.0)

    def test_right_stick_uses_right_fields(self) -> None:
        snap = self._snapshot(left_stick_x=0, right_stick_x=-16384, right_stick_y=8192)
        nx, ny = snap.right_stick_normalized
        self.assertAlmostEqual(nx, -0.5, places=4)
        self.assertAlmostEqual(ny, 0.25, places=4)


class _PopulatingFakeDLL:
    """Fake DLL whose ``XInputGetState`` writes a populated state through the byref.

    Closes the gap left by ``_FakeDLL`` (which only returns rc): this fake
    exercises ``_poll_once``'s ctypes pointer-decode path so the
    ``packet_number`` / ``buttons`` / trigger / stick struct reads are tested.
    """

    def __init__(
        self,
        *,
        packet: int = 42,
        buttons: int = 0x1000,
        left_trigger: int = 200,
        right_trigger: int = 100,
        left_stick_x: int = 1234,
        left_stick_y: int = -5678,
        right_stick_x: int = -100,
        right_stick_y: int = 999,
    ) -> None:
        self.packet = packet
        self.buttons = buttons
        self.left_trigger = left_trigger
        self.right_trigger = right_trigger
        self.left_stick_x = left_stick_x
        self.left_stick_y = left_stick_y
        self.right_stick_x = right_stick_x
        self.right_stick_y = right_stick_y
        self.call_count = 0

    def XInputGetState(self, user_index, state_byref):  # noqa: N802 - Win32 name
        self.call_count += 1
        state = ctypes.cast(state_byref, ctypes.POINTER(_XINPUT_STATE)).contents
        state.dwPacketNumber = self.packet
        state.Gamepad.wButtons = self.buttons
        state.Gamepad.bLeftTrigger = self.left_trigger
        state.Gamepad.bRightTrigger = self.right_trigger
        state.Gamepad.sThumbLX = self.left_stick_x
        state.Gamepad.sThumbLY = self.left_stick_y
        state.Gamepad.sThumbRX = self.right_stick_x
        state.Gamepad.sThumbRY = self.right_stick_y
        return 0


class XInputPointerDecodeTests(unittest.TestCase):
    def test_poll_once_decodes_populated_state(self) -> None:
        fake = _PopulatingFakeDLL()
        service = XInputPollService(dll_loader=lambda: fake)
        state = _XINPUT_STATE()

        snapshot = service._poll_once(state)

        self.assertTrue(snapshot.connected)
        self.assertTrue(snapshot.dll_available)
        self.assertEqual(snapshot.packet_number, 42)
        self.assertEqual(snapshot.buttons, frozenset({ControllerButtonTarget.A}))
        self.assertEqual(snapshot.left_trigger, 200)
        self.assertEqual(snapshot.right_trigger, 100)
        self.assertEqual(snapshot.left_stick_x, 1234)
        self.assertEqual(snapshot.left_stick_y, -5678)
        self.assertEqual(snapshot.right_stick_x, -100)
        self.assertEqual(snapshot.right_stick_y, 999)

    def test_poll_once_decodes_combined_button_mask(self) -> None:
        # A + Y + LB pressed simultaneously
        fake = _PopulatingFakeDLL(buttons=0x1000 | 0x8000 | 0x0100)
        service = XInputPollService(dll_loader=lambda: fake)
        state = _XINPUT_STATE()

        snapshot = service._poll_once(state)

        self.assertEqual(
            snapshot.buttons,
            frozenset({
                ControllerButtonTarget.A,
                ControllerButtonTarget.Y,
                ControllerButtonTarget.LB,
            }),
        )

    def test_poll_once_negative_stick_axes_round_trip(self) -> None:
        """SHORT range -32768 should round-trip cleanly without truncation."""
        fake = _PopulatingFakeDLL(
            left_stick_x=-32768, left_stick_y=-32768,
            right_stick_x=-32768, right_stick_y=-32768,
        )
        service = XInputPollService(dll_loader=lambda: fake)
        state = _XINPUT_STATE()

        snapshot = service._poll_once(state)

        self.assertEqual(snapshot.left_stick_x, -32768)
        self.assertEqual(snapshot.left_stick_y, -32768)
        self.assertEqual(snapshot.right_stick_x, -32768)
        self.assertEqual(snapshot.right_stick_y, -32768)


if __name__ == "__main__":
    unittest.main()
