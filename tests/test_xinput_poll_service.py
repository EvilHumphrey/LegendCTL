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
    _MODE_AUTO,
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


class _MultiSlotFakeDLL:
    """XInput DLL stand-in driven by a mutable set of connected slots.

    Returns ``ERROR_SUCCESS`` (0) and writes a populated state for any slot in
    ``connected`` (with a per-slot packet number so a test can confirm WHICH
    slot's data was decoded); ``ERROR_DEVICE_NOT_CONNECTED`` (1167) otherwise.
    ``calls`` records every polled user index, so a test can assert the scan
    pattern (a sticky selection re-reads ONE slot, not all four).
    """

    def __init__(self, connected) -> None:
        self.connected = set(connected)
        self.calls: list[int] = []

    def XInputGetState(self, user_index, state_byref):  # noqa: N802 — Win32 name
        self.calls.append(user_index)
        if user_index in self.connected:
            state = ctypes.cast(state_byref, ctypes.POINTER(_XINPUT_STATE)).contents
            state.dwPacketNumber = 1000 + user_index
            return 0  # ERROR_SUCCESS
        return 1167  # ERROR_DEVICE_NOT_CONNECTED


class XInputMultiSlotSelectionTests(unittest.TestCase):
    """Headless proof of the multi-slot fix: the service scans XInput indices
    0-3, auto-selects the FIRST connected pad, stays sticky, honours a manual
    pin, and re-scans on disconnect. Drives ``_poll_once`` directly with a
    mocked DLL — no hardware, no worker thread — mirroring the existing
    ``_PopulatingFakeDLL`` / ``_poll_once`` decode-test style above.
    """

    def _service(self, connected):
        fake = _MultiSlotFakeDLL(connected)
        return XInputPollService(dll_loader=lambda: fake), fake

    def test_auto_selects_the_only_connected_slot(self) -> None:
        # (a) When ONLY slot 2 is connected, the service selects slot 2 — the
        # exact reviewer multi-pad-bench case the hardcoded index 0 missed.
        service, fake = self._service({2})
        snap = service._poll_once(_XINPUT_STATE())
        self.assertTrue(snap.connected)
        self.assertEqual(snap.slot, 2)
        self.assertEqual(snap.packet_number, 1002)  # decoded slot 2's own data
        self.assertEqual(service.active_slot, 2)
        self.assertEqual(service.selection_mode, "auto")
        # Scan stopped at the first connected slot (polled 0,1,2 — never 3).
        self.assertEqual(fake.calls, [0, 1, 2])

    def test_auto_selection_is_sticky_across_polls(self) -> None:
        # (b) With two pads live (1 and 3), auto takes the first (1) and stays
        # there; a still-connected selection is re-read with ONE call, never
        # re-scanned, so it cannot flip to 3 mid-session.
        service, fake = self._service({1, 3})
        state = _XINPUT_STATE()
        self.assertEqual(service._poll_once(state).slot, 1)
        fake.calls.clear()
        self.assertEqual(service._poll_once(state).slot, 1)
        self.assertEqual(fake.calls, [1])  # sticky == single re-read, no re-scan
        self.assertEqual(service._poll_once(state).slot, 1)

    def test_manual_override_pins_chosen_slot(self) -> None:
        # (c) A manual pin selects exactly that slot and polls only it, even
        # when a lower-numbered pad (0) is also connected.
        service, fake = self._service({0, 2})
        state = _XINPUT_STATE()
        service.select_slot(2)
        self.assertEqual(service.selection_mode, "manual")
        snap = service._poll_once(state)
        self.assertEqual(snap.slot, 2)  # pinned to 2 despite 0 being live + lower
        self.assertEqual(service.active_slot, 2)
        fake.calls.clear()
        service._poll_once(state)
        self.assertEqual(fake.calls, [2])  # polls ONLY the pinned slot

    def test_disconnect_rescans_and_picks_next_connected(self) -> None:
        # (d) When the selected slot disconnects, the worker re-scans and
        # re-selects the next connected slot; with none left it exposes a clean
        # no-pad state (no crash, slot None).
        service, fake = self._service({1})
        state = _XINPUT_STATE()
        self.assertEqual(service._poll_once(state).slot, 1)
        fake.connected = {3}  # 1 drops, 3 appears
        second = service._poll_once(state)
        self.assertEqual(second.slot, 3)
        self.assertEqual(service.active_slot, 3)
        fake.connected = set()  # everything gone
        third = service._poll_once(state)
        self.assertFalse(third.connected)
        self.assertIsNone(third.slot)
        self.assertIsNone(service.active_slot)

    def test_manual_pin_survives_its_slot_disconnecting(self) -> None:
        # A manually-pinned slot keeps its identity while disconnected (no
        # auto-flip), so the UI keeps showing the operator's explicit Player N.
        service, _fake = self._service({0})
        state = _XINPUT_STATE()
        service.select_slot(3)  # pin an empty slot
        snap = service._poll_once(state)
        self.assertFalse(snap.connected)
        self.assertEqual(snap.slot, 3)  # still the pin
        self.assertEqual(service.active_slot, 3)
        self.assertEqual(service.selection_mode, "manual")

    def test_select_auto_resumes_scan_from_a_manual_pin(self) -> None:
        service, _fake = self._service({0, 2})
        state = _XINPUT_STATE()
        service.select_slot(2)
        self.assertEqual(service._poll_once(state).slot, 2)
        service.select_auto()
        self.assertEqual(service.selection_mode, "auto")
        self.assertEqual(service._poll_once(state).slot, 0)  # fresh scan -> first
        self.assertEqual(service.active_slot, 0)

    def test_no_pad_connected_is_clean_disconnected_state(self) -> None:
        service, fake = self._service(set())
        snap = service._poll_once(_XINPUT_STATE())
        self.assertFalse(snap.connected)
        self.assertTrue(snap.dll_available)
        self.assertIsNone(snap.slot)
        self.assertEqual(fake.calls, [0, 1, 2, 3])  # scanned all four, then gave up

    def test_select_slot_rejects_out_of_range(self) -> None:
        service, _fake = self._service({0})
        for bad in (-1, 4, 99):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    service.select_slot(bad)

    def test_select_slot_rejects_non_canonical_int(self) -> None:
        # Audit #1 regression: bool/float pass a bare numeric range check (bool is
        # an int subclass; float compares numerically) but are not valid slots and
        # must not ride onto snapshot.slot. Reject them at the API boundary.
        service, _fake = self._service({0})
        for bad in (True, False, 2.5, 1.0):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    service.select_slot(bad)

    def test_auto_writeback_does_not_clobber_concurrent_select_auto(self) -> None:
        # Audit #2 regression: a select_auto() that lands DURING the worker's
        # (unlocked) scan must win over the stale AUTO write-back. Sticky on slot
        # 2; the fake DLL fires select_auto() on the first XInputGetState call
        # (the UI thread, mid-scan). The worker must respect the cleared pin
        # (None), not re-sticky slot 2 — else the operator's Auto re-scan is lost.
        calls: list[int] = []
        holder: dict = {}

        class _RaceDLL:
            def XInputGetState(self, user_index, state_byref):  # noqa: N802
                calls.append(user_index)
                if len(calls) == 1:
                    holder["svc"].select_auto()
                return 0 if user_index == 2 else 1167

        service = XInputPollService(dll_loader=lambda: _RaceDLL())
        holder["svc"] = service
        service._mode = _MODE_AUTO
        service._selected_slot = 2  # sticky on slot 2 (connected)
        service._poll_once(_XINPUT_STATE())
        self.assertIsNone(service._selected_slot)  # cleared pin survives
        self.assertEqual(service.selection_mode, "auto")


if __name__ == "__main__":
    unittest.main()
