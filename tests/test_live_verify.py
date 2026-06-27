"""Tests for the dedicated Live Verify screen (circularity + inline deadzone).

Covers:
* screen build smoke in all 3 XInput availability states (Part I.2);
* the self-rescheduling frame callback registers, re-arms, self-terminates on
  teardown (stopping the worker), and dies quietly when superseded (Part I.2);
* the no-nested-scroll-trap layout invariant (the reason for the move);
* the inline-deadzone live-write path: busy gate, hydration gate, full-frame
  build, throttle, and trailing read-back verify (Part I.3, shell-side);
* the F3 rich-bundle re-point (Part I.4);
* nav wiring (NAV_ITEMS + SCREEN_BUILDERS) + the Diagnostics "Open Live Verify"
  link, and en/zh parity for the keys (Part I.5).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n
from zd_app.models import AppSettings, DeviceState
from zd_app.services.diagnostic_bundle import DiagnosticBundleService
from zd_app.services.settings_service import ControllerButtonTarget, StickDeadzones
from zd_app.services.xinput_poll_service import XInputSnapshot
from zd_app.ui import components, typography
from zd_app.ui.app_shell import AppShell
from zd_app.ui.screens import diagnostics, live_verify


# ---------------------------------------------------------------------------
# Fakes / harness
# ---------------------------------------------------------------------------


class _FakeXInputService:
    """Deterministic stand-in for XInputPollService: serves a fixed snapshot
    and records start()/stop() so lifecycle assertions don't spawn a thread.

    Also mirrors the slot-selection surface (``select_slot`` / ``select_auto`` /
    ``active_slot`` / ``selection_mode``) so the live-verify player-override row
    can build and forward picks against the fake without a real worker."""

    def __init__(self, snapshot: XInputSnapshot) -> None:
        self._snapshot = snapshot
        self.started = 0
        self.stopped = 0
        self.selection_mode = "auto"
        self.active_slot = snapshot.slot
        self.selected: list = []  # records select_slot(i) / select_auto() picks

    def start(self) -> None:
        self.started += 1

    def stop(self, **_kw) -> None:
        self.stopped += 1

    def get_snapshot(self) -> XInputSnapshot:
        return self._snapshot

    @property
    def dll_available(self) -> bool:
        return self._snapshot.dll_available

    def select_slot(self, index: int) -> None:
        self.selection_mode = "manual"
        self.active_slot = index
        self.selected.append(index)

    def select_auto(self) -> None:
        self.selection_mode = "auto"
        self.active_slot = None
        self.selected.append("auto")


class _DeadzoneService:
    """Settings-service stub for the deadzone live-write path."""

    def __init__(self, current: StickDeadzones) -> None:
        self.readback = current
        self.writes: list[StickDeadzones] = []
        self.read_calls = 0

    def get_deadzones(self) -> StickDeadzones:
        self.read_calls += 1
        return self.readback

    def set_all_deadzones(self, deadzones: StickDeadzones):
        self.writes.append(deadzones)
        return SimpleNamespace(
            outcome=SimpleNamespace(name="OK", value="ok"), error_code=None
        )


def _live_snap(**over) -> XInputSnapshot:
    base = dict(
        connected=True,
        dll_available=True,
        packet_number=1,
        buttons=frozenset({ControllerButtonTarget.A}),
        left_trigger=128,
        right_trigger=0,
        left_stick_x=32767,
        left_stick_y=0,
        right_stick_x=0,
        right_stick_y=0,
    )
    base.update(over)
    return XInputSnapshot(**base)


def _make_write_shell(settings_service) -> AppShell:
    """Minimal shell for the write-path tests (mirrors the busy-guard harness:
    refresh/rebuild nulled so the device-touching method runs DPG-free)."""

    settings_store = MagicMock()
    settings_store.load.return_value = AppSettings()
    device_service = MagicMock()
    device_service.state = DeviceState()
    device_service.recent_events.return_value = []
    device_service.last_read_duration_ms = None
    device_service.last_write_duration_ms = None
    profile_service = MagicMock()
    profile_service.pending_changes_count.return_value = 0
    wrapper = MagicMock()
    wrapper.list_profiles.return_value = []
    shell = AppShell(
        device_service=device_service,
        profile_service=profile_service,
        diagnostics_service=MagicMock(),
        settings_store=settings_store,
        preflight_service=MagicMock(),
        settings_service=settings_service,
        wrapper_profile_store=wrapper,
        restore_point_service=None,
    )
    shell.restore_point_service = None
    shell.refresh_shell = lambda: None
    shell.refresh_current_screen = lambda: None
    shell.rebuild_current_screen = lambda: None
    return shell


def _capture(callback):
    """Run ``callback`` under patched DPG mutators (does_item_exist True)."""

    with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True), patch(
        "zd_app.ui.app_shell.dpg.set_value"
    ), patch("zd_app.ui.app_shell.dpg.configure_item"):
        return callback()


def _fake_dpg():
    fake = MagicMock()
    fake._registered = []
    fake._exists = {"v": True}
    fake.set_frame_callback.side_effect = lambda frame, cb: fake._registered.append(cb)
    fake.get_frame_count.return_value = 0
    fake.does_item_exist.side_effect = lambda *_a, **_k: fake._exists["v"]
    fake.get_value.return_value = 0
    return fake


def _last_set_value(fake, tag):
    """Most-recent ``dpg.set_value(tag, value)`` value recorded by ``fake``."""

    for call in reversed(fake.set_value.call_args_list):
        if call.args and call.args[0] == tag:
            return call.args[1]
    return None


def _last_configure(fake, tag):
    """Kwargs of the most-recent ``dpg.configure_item(tag, ...)`` on ``fake``."""

    for call in reversed(fake.configure_item.call_args_list):
        if call.args and call.args[0] == tag:
            return call.kwargs
    return None


class _TimeoutReadDeadzoneService(_DeadzoneService):
    """Deadzone stub whose read-back times out (the item-J smoke condition)."""

    def get_deadzones(self):
        self.read_calls += 1
        raise TimeoutError("HID read timed out after 1000ms")


# ---------------------------------------------------------------------------
# Part I.2 — screen build smoke in the 3 availability states
# ---------------------------------------------------------------------------


class LiveVerifyScreenBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def _build_full_screen(self, snapshot: XInputSnapshot) -> _FakeXInputService:
        shell = make_shell(settings_service=MagicMock())
        fake = _FakeXInputService(snapshot)
        shell._xinput_poll_service = fake
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass
        live_verify.build(shell, "content_region")
        return fake

    @staticmethod
    def _nested_child_windows(root_tag):
        """All child_window items under ``root_tag`` (root itself excluded)."""

        found = []

        def walk(item):
            for child in dpg.get_item_children(item, 1) or []:
                try:
                    itype = dpg.get_item_type(child)
                except Exception:
                    itype = ""
                if itype.endswith("mvChildWindow"):
                    found.append(child)
                walk(child)

        walk(root_tag)
        return found

    def _assert_core_tags(self) -> None:
        self.assertTrue(dpg.does_item_exist(live_verify.LIVE_VERIFY_ROOT_TAG))
        self.assertTrue(dpg.does_item_exist(live_verify.LIVE_VERIFY_AVAILABILITY_TAG))
        # Player-slot override row (multi-slot selection) builds in every state.
        self.assertTrue(dpg.does_item_exist(live_verify.PLAYER_SELECT_COMBO_TAG))
        self.assertTrue(dpg.does_item_exist(live_verify.PLAYER_ACTIVE_TAG))
        self.assertTrue(dpg.does_item_exist(live_verify._test_button_tag("left")))
        self.assertTrue(dpg.does_item_exist(live_verify._test_button_tag("right")))
        self.assertTrue(
            dpg.does_item_exist(live_verify._button_chip_tag(ControllerButtonTarget.A))
        )
        self.assertTrue(dpg.does_item_exist(live_verify.TRIGGER_LEFT_BAR_TAG))
        self.assertTrue(
            dpg.does_item_exist(live_verify._deadzone_slider_tag("left", "center"))
        )
        # Polish-pass additions: B center->dot radial line, N2 header avg-error
        # %, G label-above-slider items.
        for side in ("left", "right"):
            self.assertTrue(dpg.does_item_exist(live_verify._radial_tag(side)))
            self.assertTrue(dpg.does_item_exist(live_verify._header_error_tag(side)))
        for side, kind in (("left", "center"), ("right", "outer")):
            self.assertTrue(
                dpg.does_item_exist(live_verify._deadzone_label_tag(side, kind))
            )

    def test_envelope_polygon_is_filled(self) -> None:
        # Item C: the swept envelope carries a semi-transparent fill, not just
        # an outline, and it isn't clipped to the unit circle.
        dpg.create_context()
        try:
            self._build_full_screen(_live_snap())
            cfg = dpg.get_item_configuration(live_verify._envelope_tag("left"))
            fill = cfg.get("fill")
            self.assertIsNotNone(fill)
            self.assertGreater(fill[3], 0)  # non-zero alpha == shaded
            self.assertLess(fill[3], 255)  # semi-transparent, not opaque
        finally:
            dpg.destroy_context()

    def test_deadzone_label_is_separate_item_above_slider(self) -> None:
        # Item G: each label is its own item (so it can sit above the slider),
        # and the slider itself carries no inline (right-side) label.
        dpg.create_context()
        try:
            self._build_full_screen(_live_snap())
            for side, kind in (
                ("left", "center"),
                ("left", "outer"),
                ("right", "center"),
                ("right", "outer"),
            ):
                self.assertTrue(
                    dpg.does_item_exist(live_verify._deadzone_label_tag(side, kind))
                )
                slider_cfg = dpg.get_item_configuration(
                    live_verify._deadzone_slider_tag(side, kind)
                )
                self.assertFalse(slider_cfg.get("label"))
        finally:
            dpg.destroy_context()

    def test_no_nested_scroll_capturing_child_window(self) -> None:
        # HARD REQUIREMENT 1 — the whole reason for the move, and the fix for the
        # per-card scrollbars the operator saw. The screen root is the single
        # governing scroll: it KEEPS the legacy autosize_x + autosize_y (= ImGui
        # size==0 → FILL the content_region) so it spans the content area and owns
        # the one scrollbar (NOT no_scrollbar). Every nested card instead uses
        # _fit_card() (auto_resize_y = ImGui ChildFlags_AutoResizeY) so it FITS
        # its content height — the dearpygui-2.x content-fit flag. The shared
        # card()'s LEGACY autosize_y would FILL instead, ballooning a stacked card
        # to the bottom of the screen (the operator's screenshot; a real-DPG bench
        # measured the stick card at 625px before the fix, 338px after). So the
        # invariant is: nested cards carry auto_resize_y and do NOT carry the
        # legacy autosize_y fill flag, and none re-introduces no_scrollbar.
        dpg.create_context()
        try:
            self._build_full_screen(_live_snap())
            root_cfg = dpg.get_item_configuration(live_verify.LIVE_VERIFY_ROOT_TAG)
            self.assertTrue(root_cfg.get("autosize_x"))
            self.assertTrue(root_cfg.get("autosize_y"))
            self.assertFalse(root_cfg.get("no_scrollbar"))

            nested = self._nested_child_windows(live_verify.LIVE_VERIFY_ROOT_TAG)
            # 2 stick cards + buttons/triggers card + deadzone card.
            self.assertGreaterEqual(len(nested), 4)
            for item in nested:
                cfg = dpg.get_item_configuration(item)
                alias = dpg.get_item_alias(item)
                # The anti-balloon invariant: every nested card FITS its content
                # via auto_resize_y (the dearpygui-2.x content-fit flag) and does
                # NOT use the legacy autosize_y fill flag, and never no_scrollbar.
                self.assertTrue(
                    cfg.get("auto_resize_y"),
                    f"{alias} lacks auto_resize_y (would FILL/balloon, not fit)",
                )
                self.assertFalse(
                    cfg.get("autosize_y"),
                    f"{alias} uses the legacy autosize_y fill flag (balloons on "
                    "dearpygui 2.x)",
                )
                self.assertFalse(
                    cfg.get("no_scrollbar"),
                    f"{alias} re-introduces the no_scrollbar wheel-trap",
                )
        finally:
            dpg.destroy_context()

    def test_stick_plots_are_equal_size(self) -> None:
        # Work item 5: the two stick cards are exactly equal width and both FIT
        # their height via auto_resize_y (_fit_card) — with identical content they
        # fit to the SAME height (a real-DPG bench measured both at 338px) — and
        # hold equal _PLOT_SIZE drawlists, so the left/right reference circles
        # match exactly. (Headless can't measure rendered height; the equal-height
        # contract rests on identical width + the same content-fit flag + identical
        # content. The 338px figure is locked structurally below + by the bench.)
        dpg.create_context()
        try:
            self._build_full_screen(_live_snap())
            left = dpg.get_item_configuration(live_verify._stick_block_tag("left"))
            right = dpg.get_item_configuration(live_verify._stick_block_tag("right"))
            self.assertEqual(left.get("width"), right.get("width"))
            self.assertGreater(left.get("width"), 0)
            # Both FIT content (auto_resize_y), neither FILLS (legacy autosize_y);
            # symmetric flags + identical content => equal rendered height.
            self.assertTrue(left.get("auto_resize_y"))
            self.assertTrue(right.get("auto_resize_y"))
            self.assertFalse(left.get("autosize_y"))
            self.assertFalse(right.get("autosize_y"))
        finally:
            dpg.destroy_context()

    def test_fit_card_uses_content_fit_flag_not_legacy_fill(self) -> None:
        # Root-cause lock. On dearpygui 2.x the shared card()'s legacy autosize_y
        # means "fill the parent's available height" (ImGui size.y == 0), NOT
        # "shrink to content" — the content-fit behaviour was split into the
        # separate auto_resize_y (ImGui ChildFlags_AutoResizeY). So a legacy
        # autosize_y card stacked above other content BALLOONS to fill the screen
        # (a real-DPG bench measured the Live Verify stick card at 625px before
        # the fix, 338px after). _fit_card() must therefore open its card with
        # auto_resize_y=True AND suppress the legacy autosize_y fill flag — the
        # single invariant the whole stick-card trim rests on.
        dpg.create_context()
        try:
            with dpg.window():
                with live_verify._fit_card(tag="probe_fit_card", width=410):
                    dpg.add_text("content")
            cfg = dpg.get_item_configuration("probe_fit_card")
            self.assertTrue(
                cfg.get("auto_resize_y"),
                "_fit_card must use auto_resize_y so the card fits content",
            )
            self.assertFalse(
                cfg.get("autosize_y"),
                "_fit_card must NOT use the legacy autosize_y fill flag (balloons)",
            )
            self.assertEqual(cfg.get("width"), 410)
        finally:
            dpg.destroy_context()

    def test_builds_when_xinput_unavailable(self) -> None:
        dpg.create_context()
        try:
            fake = self._build_full_screen(XInputSnapshot.unavailable())
            self._assert_core_tags()
            self.assertGreaterEqual(fake.started, 1)  # service started in build
        finally:
            dpg.destroy_context()

    def test_builds_when_no_controller(self) -> None:
        dpg.create_context()
        try:
            fake = self._build_full_screen(XInputSnapshot.disconnected())
            self._assert_core_tags()
            self.assertGreaterEqual(fake.started, 1)
        finally:
            dpg.destroy_context()

    def test_builds_when_live(self) -> None:
        dpg.create_context()
        try:
            fake = self._build_full_screen(_live_snap())
            self._assert_core_tags()
            self.assertGreaterEqual(fake.started, 1)
        finally:
            dpg.destroy_context()


# ---------------------------------------------------------------------------
# Part I.2 — frame-callback lifecycle (patched DPG)
# ---------------------------------------------------------------------------


class LiveVerifyFrameCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def _build_with_fake(self, snapshot: XInputSnapshot):
        shell = _make_write_shell(settings_service=MagicMock())
        service = _FakeXInputService(snapshot)
        shell._xinput_poll_service = service
        fake = _fake_dpg()
        # components.dpg too: the cards now route through the card() helper,
        # which lives in zd_app.ui.components and uses that module's dpg — patch
        # it so card() builds against the fake (not the real, stack-less DPG).
        with patch.object(live_verify, "dpg", fake), patch.object(
            typography, "dpg", fake
        ), patch.object(components, "dpg", fake):
            live_verify.build(shell, "content_region")
        return shell, service, fake

    def _tick(self, fake) -> None:
        cb = fake._registered[-1]
        with patch.object(live_verify, "dpg", fake), patch.object(
            typography, "dpg", fake
        ), patch.object(components, "dpg", fake):
            cb()

    def test_build_registers_chain_and_starts_service(self) -> None:
        _shell, service, fake = self._build_with_fake(_live_snap())
        self.assertEqual(len(fake._registered), 1)
        self.assertGreaterEqual(service.started, 1)

    def test_tick_reschedules_while_root_exists(self) -> None:
        _shell, service, fake = self._build_with_fake(_live_snap())
        self._tick(fake)
        self.assertEqual(len(fake._registered), 2)  # re-armed
        self.assertEqual(service.stopped, 0)

    def test_tick_stops_service_on_teardown(self) -> None:
        _shell, service, fake = self._build_with_fake(_live_snap())
        # One live tick so the chain has SEEN the root (root_seen=True); only
        # after the screen has gone live does a missing root mean a genuine
        # nav-away teardown (item I).
        self._tick(fake)
        self.assertEqual(len(fake._registered), 2)  # re-armed while live
        fake._exists["v"] = False  # nav-away deleted the root
        self._tick(fake)
        self.assertEqual(service.stopped, 1)
        self.assertEqual(len(fake._registered), 2)  # NOT re-armed after teardown

    def test_not_ready_first_tick_keeps_chain_alive(self) -> None:
        # Item I: a missing root on an EARLY tick (before the screen root has
        # ever been seen) must NOT be mistaken for a teardown — the chain keeps
        # re-arming and the worker is left running until the root appears, so a
        # frame-count timing hiccup can't permanently freeze the screen.
        _shell, service, fake = self._build_with_fake(_live_snap())
        fake._exists["v"] = False  # root not committed yet on the first tick
        self._tick(fake)
        self.assertEqual(service.stopped, 0)  # worker NOT stopped
        self.assertEqual(len(fake._registered), 2)  # re-armed (chain alive)
        # Once the root appears, the chain goes live and keeps ticking.
        fake._exists["v"] = True
        self._tick(fake)
        self.assertEqual(service.stopped, 0)
        self.assertEqual(len(fake._registered), 3)

    def test_axis_readout_uses_five_decimals(self) -> None:
        # Item A: the live axis readout shows 5 decimals (resting-drift visible).
        _shell, _service, fake = self._build_with_fake(
            _live_snap(left_stick_x=32767, left_stick_y=0)
        )
        self._tick(fake)
        value = _last_set_value(fake, live_verify._axes_tag("left"))
        self.assertIsNotNone(value)
        self.assertRegex(value, r"[+-]\d+\.\d{5}\b.*[+-]\d+\.\d{5}\b")

    def test_header_error_updates_live_while_active(self) -> None:
        # Item N2: while a stick test is active, the header avg-error % is
        # updated every tick (not only on Stop), as a "X.X%" value.
        shell, _service, fake = self._build_with_fake(
            _live_snap(left_stick_x=32767, left_stick_y=0)
        )
        shell._live_verify_state.left_active = True
        self._tick(fake)
        value = _last_set_value(fake, live_verify._header_error_tag("left"))
        self.assertIsNotNone(value)
        self.assertRegex(value, r"\d+\.\d%")

    def test_header_error_not_churned_when_idle(self) -> None:
        # Symmetry with the live test: with no active test the header number is
        # left at its build-time "—%" (the gate lives on {side}_active), so the
        # refresh writes no new value to it.
        shell, _service, fake = self._build_with_fake(_live_snap())
        shell._live_verify_state.left_active = False
        self._tick(fake)
        self.assertIsNone(_last_set_value(fake, live_verify._header_error_tag("left")))

    def test_tick_dies_quietly_when_superseded(self) -> None:
        shell, service, fake = self._build_with_fake(_live_snap())
        shell._live_verify_state = object()  # a newer build replaced it
        self._tick(fake)
        self.assertEqual(service.stopped, 0)  # newer chain owns the worker
        self.assertEqual(len(fake._registered), 1)  # NOT re-armed

    def test_active_stick_feeds_circularity_sweep(self) -> None:
        shell, _service, fake = self._build_with_fake(
            _live_snap(left_stick_x=32767, left_stick_y=0)
        )
        shell._live_verify_state.left_active = True
        before = shell._live_verify_state.left_sweep.sample_count
        self._tick(fake)
        self.assertGreater(
            shell._live_verify_state.left_sweep.sample_count, before
        )


# ---------------------------------------------------------------------------
# Part I.3 — inline deadzone live-write (shell-side write safety / J / L)
# ---------------------------------------------------------------------------


class DiagnosticsDeadzoneWriteTests(unittest.TestCase):
    def _shell(self, current: StickDeadzones):
        service = _DeadzoneService(current)
        shell = _make_write_shell(settings_service=service)
        shell._diag_deadzone_hydrated = True
        return shell, service

    def test_busy_refuses_without_writing(self) -> None:
        shell, service = self._shell(StickDeadzones(5, 6, 7, 8))
        shell._hid_job_in_flight = True
        out = _capture(
            lambda: shell.apply_diagnostics_deadzone(StickDeadzones(1, 2, 3, 4))
        )
        self.assertIsNone(out)
        self.assertEqual(service.writes, [])

    def test_unhydrated_ignores_callback(self) -> None:
        shell, service = self._shell(StickDeadzones(0, 0, 0, 0))
        shell._diag_deadzone_hydrated = False
        _capture(lambda: shell.apply_diagnostics_deadzone(StickDeadzones(1, 2, 3, 4)))
        self.assertEqual(service.writes, [])

    def test_leading_edge_writes_full_frame_without_readback(self) -> None:
        shell, service = self._shell(StickDeadzones(0, 0, 0, 0))
        dz = StickDeadzones(left_center=10, right_center=20, left_outer=30, right_outer=40)
        _capture(lambda: shell.apply_diagnostics_deadzone(dz))
        # The whole StickDeadzones (all 4 fields) is written, not a partial.
        self.assertEqual(service.writes, [dz])
        # Read-back is deferred to the trailing flush, not the leading edge.
        self.assertEqual(service.read_calls, 0)
        self.assertEqual(shell._diag_deadzone_status_key, "sending")

    def test_throttle_suppresses_second_write_and_stores_pending(self) -> None:
        shell, service = self._shell(StickDeadzones(0, 0, 0, 0))
        _capture(lambda: shell.apply_diagnostics_deadzone(StickDeadzones(1, 1, 1, 1)))
        _capture(lambda: shell.apply_diagnostics_deadzone(StickDeadzones(2, 2, 2, 2)))
        self.assertEqual(len(service.writes), 1)  # second is throttled
        self.assertEqual(
            shell._slider_throttle._pending.get("deadzones"),
            StickDeadzones(2, 2, 2, 2),
        )

    def test_flush_writes_trailing_value_and_verifies(self) -> None:
        shell, service = self._shell(StickDeadzones(0, 0, 0, 0))
        dz = StickDeadzones(3, 3, 3, 3)
        service.readback = dz
        shell._slider_throttle._pending["deadzones"] = dz
        shell._slider_throttle._last_write_ts["deadzones"] = monotonic() - 60.0
        _capture(shell._flush_slider_throttle)
        self.assertIn(dz, service.writes)
        self.assertGreaterEqual(service.read_calls, 1)  # read-back on flush
        self.assertEqual(shell._diag_deadzone_status_key, "verified")

    def test_flush_mismatch_sets_mismatch_status(self) -> None:
        shell, service = self._shell(StickDeadzones(0, 0, 0, 0))
        dz = StickDeadzones(3, 3, 3, 3)
        service.readback = StickDeadzones(9, 9, 9, 9)  # firmware reports something else
        shell._slider_throttle._pending["deadzones"] = dz
        shell._slider_throttle._last_write_ts["deadzones"] = monotonic() - 60.0
        _capture(shell._flush_slider_throttle)
        self.assertEqual(shell._diag_deadzone_status_key, "mismatch")

    def test_flush_timeout_marks_sent_unverified(self) -> None:
        # Item J: a read-back TimeoutError after a SUCCESSFUL write reports
        # "sent, could not verify" — never a mismatch/failure — and never raises
        # on the render thread (the write already landed).
        service = _TimeoutReadDeadzoneService(StickDeadzones(0, 0, 0, 0))
        shell = _make_write_shell(settings_service=service)
        shell._diag_deadzone_hydrated = True
        dz = StickDeadzones(3, 3, 3, 3)
        shell._slider_throttle._pending["deadzones"] = dz
        shell._slider_throttle._last_write_ts["deadzones"] = monotonic() - 60.0
        _capture(shell._flush_slider_throttle)  # must not raise
        self.assertIn(dz, service.writes)  # the write landed
        self.assertEqual(shell._diag_deadzone_status_key, "sent_unverified")

    def test_verify_read_runs_off_the_render_thread(self) -> None:
        # Item J: the confirm read is handed to the HID executor (worker
        # thread), not run inline on the render thread; the status stays
        # "verifying" until the completion drains, then firms to "verified".
        service = _DeadzoneService(StickDeadzones(0, 0, 0, 0))
        shell = _make_write_shell(settings_service=service)
        shell._diag_deadzone_hydrated = True
        recorded: list = []
        shell._hid_executor = lambda job, deliver: recorded.append((job, deliver))
        dz = StickDeadzones(3, 3, 3, 3)
        service.readback = dz
        shell._slider_throttle._pending["deadzones"] = dz
        shell._slider_throttle._last_write_ts["deadzones"] = monotonic() - 60.0
        _capture(shell._flush_slider_throttle)
        # Write landed synchronously; the read is deferred to the worker.
        self.assertIn(dz, service.writes)
        self.assertEqual(service.read_calls, 0)  # NOT read on the render thread
        self.assertEqual(shell._diag_deadzone_status_key, "verifying")
        self.assertEqual(len(recorded), 1)
        # Run the worker job, deliver its result, then drain on the render
        # thread — only now does the status firm up.
        job, deliver = recorded[0]
        deliver(job())
        self.assertGreaterEqual(service.read_calls, 1)
        _capture(shell._drain_hid_job_completions)
        self.assertEqual(shell._diag_deadzone_status_key, "verified")

    def test_trailing_flush_during_in_flight_verify_reaches_final_state(self) -> None:
        # Item L: a deadzone trailing flush that comes due WHILE a prior
        # read-back verify still holds the gate must not strand the inline
        # status. The verify holds _hid_job_in_flight for ~1s (settle + read),
        # far longer than the throttle window, so a release mid-verify used to
        # land in the in-flight branch, which CONSUMED + dropped the pending —
        # leaving the status pinned at "sending" forever. The pending must be
        # re-queued and verify the moment the gate clears, so the status always
        # ends at a final state (never stuck at sending/verifying).
        service = _DeadzoneService(StickDeadzones(0, 0, 0, 0))
        shell = _make_write_shell(settings_service=service)
        shell._diag_deadzone_hydrated = True
        recorded: list = []
        shell._hid_executor = lambda job, deliver: recorded.append((job, deliver))
        throttle = shell._slider_throttle

        # Drag 1 commits + schedules an off-thread verify: the gate closes and
        # the status parks at "verifying" until the read-back drains.
        dz1 = StickDeadzones(3, 3, 3, 3)
        service.readback = dz1
        throttle._pending["deadzones"] = dz1
        throttle._last_write_ts["deadzones"] = monotonic() - 60.0
        _capture(shell._flush_slider_throttle)
        self.assertTrue(shell._hid_job_in_flight)  # the verify holds the gate
        self.assertEqual(shell._diag_deadzone_status_key, "verifying")

        # The user keeps dragging: a newer final value is pending while the
        # verify still holds the gate. The render tick fires the flush, but the
        # gate is in flight — the pending must be RE-QUEUED, not consumed.
        dz2 = StickDeadzones(7, 7, 7, 7)
        throttle._pending["deadzones"] = dz2
        throttle._last_write_ts["deadzones"] = monotonic() - 60.0
        _capture(shell._flush_slider_throttle)
        self.assertEqual(throttle._pending.get("deadzones"), dz2)  # not consumed
        self.assertEqual(shell._diag_deadzone_status_key, "verifying")

        # Verify 1 drains: the gate reliably clears and dz1's status firms up.
        job1, deliver1 = recorded[0]
        deliver1(job1())
        _capture(shell._drain_hid_job_completions)
        self.assertFalse(shell._hid_job_in_flight)  # gate clears after the drain

        # The very next flush (gate now open) fires the re-queued dz2 trailing
        # write + its own verify — no further user input required.
        service.readback = dz2
        _capture(shell._flush_slider_throttle)
        self.assertIn(dz2, service.writes)  # drag-2 final value actually landed
        self.assertEqual(shell._diag_deadzone_status_key, "verifying")
        job2, deliver2 = recorded[1]
        deliver2(job2())
        _capture(shell._drain_hid_job_completions)

        # Ends at a final state — never stranded at sending/verifying.
        self.assertIn(
            shell._diag_deadzone_status_key,
            {"verified", "mismatch", "sent_unverified"},
        )
        self.assertEqual(shell._diag_deadzone_status_key, "verified")

    def test_single_callback_change_reaches_final_status(self) -> None:
        # Item N3: a SINGLE apply_diagnostics_deadzone call (a quick flick / a
        # drag that ends on its leading edge) creates NO trailing pending, so the
        # multi-callback trailing flush never runs — the inline status used to
        # strand at "sending" forever. The leading-edge value must arm a deferred
        # verify that fires once the throttle quiet window elapses, so a single
        # change still resolves to a FINAL status (verified/sent_unverified/
        # mismatch), never sending/verifying.
        shell, service = self._shell(StickDeadzones(0, 0, 0, 0))
        dz = StickDeadzones(12, 12, 12, 12)
        service.readback = dz
        # One callback only — the leading-edge write; no follow-up drag tick, so
        # nothing lands in the throttle's pending map.
        _capture(lambda: shell.apply_diagnostics_deadzone(dz))
        self.assertEqual(service.writes, [dz])  # value committed once
        self.assertEqual(service.read_calls, 0)  # no read on the leading edge
        self.assertEqual(shell._diag_deadzone_status_key, "sending")  # before the window
        self.assertEqual(shell._slider_throttle._pending.get("deadzones"), None)  # no pending
        self.assertEqual(shell._deadzone_pending_verify, dz)  # deferred verify armed
        # Age the leading write past the throttle quiet window so the deferred
        # verify is due, then drain one render tick's flush (sync mode -> the
        # read-back verify runs inline and firms the status).
        shell._slider_throttle._last_write_ts["deadzones"] = monotonic() - 60.0
        _capture(shell._flush_slider_throttle)
        self.assertEqual(service.writes, [dz])  # the verify is a READ, not a re-write
        self.assertGreaterEqual(service.read_calls, 1)  # deferred read-back ran
        self.assertNotIn(shell._diag_deadzone_status_key, {"sending", "verifying"})
        self.assertEqual(shell._diag_deadzone_status_key, "verified")
        self.assertIsNone(shell._deadzone_pending_verify)  # consumed, won't re-fire


# ---------------------------------------------------------------------------
# Part I.4 — F3 rich diagnostic bundle re-point
# ---------------------------------------------------------------------------


class ExportRichBundleTests(unittest.TestCase):
    def _bundle(self, *, result):
        bundle = MagicMock(spec=DiagnosticBundleService)
        bundle.base_dir = Path(tempfile.gettempdir())
        bundle.generate_bundle_zip.return_value = result
        return bundle

    def test_export_calls_generate_bundle_zip_with_rich_args(self) -> None:
        shell = _make_write_shell(settings_service=MagicMock())
        result = Path(tempfile.gettempdir()) / "diagnostic_bundle_test.zip"
        bundle = self._bundle(result=result)
        shell.diagnostic_bundle_service = bundle
        with patch("zd_app.ui.app_shell.os.startfile", create=True):
            shell.export_rich_diagnostic_bundle()
        bundle.generate_bundle_zip.assert_called_once()
        _args, kwargs = bundle.generate_bundle_zip.call_args
        self.assertTrue(kwargs.get("include_archived"))
        self.assertEqual(kwargs.get("health_report_limit"), 5)
        self.assertEqual(kwargs.get("wear_ledger_days"), 90)
        bundle.emit_generated_event.assert_called_once()

    def test_export_guards_none_result(self) -> None:
        shell = _make_write_shell(settings_service=MagicMock())
        bundle = self._bundle(result=None)
        shell.diagnostic_bundle_service = bundle
        with patch("zd_app.ui.app_shell.os.startfile", create=True):
            shell.export_rich_diagnostic_bundle()
        bundle.emit_generated_event.assert_not_called()

    def test_export_guards_missing_service(self) -> None:
        shell = _make_write_shell(settings_service=MagicMock())
        shell.diagnostic_bundle_service = None
        # Must not raise even when no bundle service is wired.
        shell.export_rich_diagnostic_bundle()


# ---------------------------------------------------------------------------
# Part I.5 — nav wiring + Diagnostics link
# ---------------------------------------------------------------------------


class LiveVerifyNavWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_live_verify_registered_in_nav_after_diagnostics(self) -> None:
        self.assertIn("live_verify", AppShell.NAV_ITEMS)
        items = list(AppShell.NAV_ITEMS)
        self.assertEqual(items[items.index("diagnostics") + 1], "live_verify")

    def test_live_verify_screen_builder_points_to_module(self) -> None:
        self.assertIs(AppShell.SCREEN_BUILDERS.get("live_verify"), live_verify.build)

    def test_live_verify_canonicalizes_from_display_name(self) -> None:
        # The SCREEN_ALIASES entry lets a display-name caller resolve the screen.
        self.assertEqual(AppShell.SCREEN_ALIASES.get("Live Verify"), "live_verify")

    def test_diagnostics_has_open_live_verify_link_that_navigates(self) -> None:
        dpg.create_context()
        try:
            shell = make_shell(settings_service=MagicMock())
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            diagnostics.build(shell, "content_region")
            self.assertTrue(
                dpg.does_item_exist(diagnostics.OPEN_LIVE_VERIFY_BUTTON_TAG)
            )
            # Invoking the link navigates to the dedicated screen.
            recorded: list = []
            shell.switch_screen = lambda screen: recorded.append(screen)
            cfg = dpg.get_item_configuration(diagnostics.OPEN_LIVE_VERIFY_BUTTON_TAG)
            cfg["callback"]()
            self.assertEqual(recorded, ["live_verify"])
        finally:
            dpg.destroy_context()


# ---------------------------------------------------------------------------
# Part I.5 — en/zh parity for new + existing keys
# ---------------------------------------------------------------------------


class LiveVerifyI18nTests(unittest.TestCase):
    def _locales(self):
        locale_dir = Path("zd_app/i18n/locales")
        en = json.loads((locale_dir / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((locale_dir / "zh-CN.json").read_text(encoding="utf-8"))
        return en, zh

    def test_new_keys_present_and_nonempty_in_both_locales(self) -> None:
        en, zh = self._locales()
        required = [
            "nav.live_verify",
            "diagnostics.live_verify.open_button",
            "diagnostics.live_verify.buttons_triggers_title",
            "diagnostics.live_verify.title",
            "diagnostics.live_verify.intro",
            "diagnostics.live_verify.availability.unavailable",
            "diagnostics.live_verify.availability.no_controller",
            "diagnostics.live_verify.availability.live",
            "diagnostics.live_verify.run_stick_test",
            "diagnostics.live_verify.stop_stick_test",
            "diagnostics.live_verify.error",
            "diagnostics.live_verify.error_gated",
            "diagnostics.live_verify.coverage",
            "diagnostics.live_verify.circularity_help",
            "diagnostics.live_verify.buttons.title",
            "diagnostics.live_verify.triggers.title",
            "diagnostics.live_verify.deadzone.title",
            "diagnostics.live_verify.deadzone.note",
            "diagnostics.live_verify.deadzone.status.verified",
            "diagnostics.live_verify.deadzone.status.verifying",
            "diagnostics.live_verify.deadzone.status.mismatch",
            "diagnostics.live_verify.deadzone.status.sent_unverified",
            "diagnostics.trust_anchor_intro",
            "diagnostics.actions.bundle_note",
            "diagnostics.stale_warning.headline",
            "diagnostics.stale_warning.helper",
            "diagnostics.trust.body",
            "log.diagnostics.bundle_unavailable",
            "log.diagnostics.bundle_failed",
            # Player-slot override row (multi-slot selection).
            "diagnostics.live_verify.player.control_label",
            "diagnostics.live_verify.player.auto",
            "diagnostics.live_verify.player.slot",
            "diagnostics.live_verify.player.active",
            "diagnostics.live_verify.player.none",
        ]
        for key in required:
            with self.subTest(key=key):
                self.assertIn(key, en)
                self.assertIn(key, zh)
                self.assertTrue(en[key])
                self.assertTrue(zh[key])

    def test_placeholders_preserved_across_locales(self) -> None:
        en, zh = self._locales()
        for key in (
            "diagnostics.live_verify.error",
            "diagnostics.live_verify.error_gated",
            "diagnostics.live_verify.coverage",
        ):
            with self.subTest(key=key):
                self.assertIn("{pct}", en[key])
                self.assertIn("{pct}", zh[key])
        self.assertIn("{x}", en["diagnostics.live_verify.stick.axes"])
        self.assertIn("{y}", zh["diagnostics.live_verify.stick.axes"])

    def test_trust_body_keeps_honest_reported_as_sent_wording(self) -> None:
        en, _zh = self._locales()
        self.assertIn("reported as sent", en["diagnostics.trust.body"])

    def test_stale_warning_keys_match_pinned_constants(self) -> None:
        en, _zh = self._locales()
        self.assertEqual(
            en["diagnostics.stale_warning.headline"],
            diagnostics.STALE_WARNING_HEADLINE,
        )
        self.assertEqual(
            en["diagnostics.stale_warning.helper"],
            diagnostics.STALE_WARNING_HELPER,
        )

    def test_zh_values_are_real_hanzi(self) -> None:
        _en, zh = self._locales()
        for key in (
            "nav.live_verify",
            "diagnostics.live_verify.title",
            "diagnostics.live_verify.deadzone.title",
        ):
            with self.subTest(key=key):
                self.assertTrue(
                    any("一" <= ch <= "鿿" for ch in zh[key])
                )

    def test_en_live_verify_strings_avoid_unrenderable_glyphs(self) -> None:
        # Item H: the English Inter font has no glyph for U+2014 (em-dash),
        # U+2026 (ellipsis), or U+2713 (check mark) — they render as "?". Keep
        # the live_verify copy free of them so no "?" artifacts surface (the
        # Open Live Verify link uses an ASCII "->" arrow for the same reason).
        en, _zh = self._locales()
        unrenderable = ("—", "…", "✓")
        offenders = {
            key: value
            for key, value in en.items()
            if key.startswith("diagnostics.live_verify.")
            and any(ch in value for ch in unrenderable)
        }
        self.assertEqual(offenders, {})

    def test_header_idle_literal_avoids_unrenderable_glyphs(self) -> None:
        # The header avg-error idle readout (N2) is a CODE literal, not i18n, so
        # the test above can't see it. The English Inter font renders U+2014 /
        # U+2026 / U+2713 as "?", so it must use ASCII (the "--%" convention) and
        # not the em-dash the spec sketched with.
        for ch in ("—", "…", "✓"):
            self.assertNotIn(ch, live_verify._HEADER_PCT_IDLE)


class LiveVerifyPlayerSelectorTests(unittest.TestCase):
    """The player-slot override row: builds, shows the active 'Player N', and
    forwards Auto / Player 1-4 picks to the poll service (which owns the slot
    logic). Pairs with tests/test_xinput_poll_service.py, which proves the
    service-side multi-slot selection itself."""

    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def _build(self, fake) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell._xinput_poll_service = fake
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass
        live_verify.build(shell, "content_region")
        return shell

    def test_row_builds_with_combo_and_auto_player_items(self) -> None:
        dpg.create_context()
        try:
            self._build(_FakeXInputService(_live_snap(slot=1)))
            self.assertTrue(dpg.does_item_exist(live_verify.PLAYER_SELECT_COMBO_TAG))
            self.assertTrue(dpg.does_item_exist(live_verify.PLAYER_ACTIVE_TAG))
            items = dpg.get_item_configuration(
                live_verify.PLAYER_SELECT_COMBO_TAG
            )["items"]
            self.assertEqual(
                list(items),
                ["Auto", "Player 1", "Player 2", "Player 3", "Player 4"],
            )
        finally:
            dpg.destroy_context()

    def test_active_readout_shows_player_for_connected_slot(self) -> None:
        dpg.create_context()
        try:
            self._build(_FakeXInputService(_live_snap(slot=2)))
            # 1-based: snapshot slot 2 -> "Player 3".
            self.assertEqual(
                dpg.get_value(live_verify.PLAYER_ACTIVE_TAG), "Active: Player 3"
            )
        finally:
            dpg.destroy_context()

    def test_active_readout_reads_no_controller_when_disconnected(self) -> None:
        dpg.create_context()
        try:
            self._build(_FakeXInputService(XInputSnapshot.disconnected()))
            self.assertEqual(
                dpg.get_value(live_verify.PLAYER_ACTIVE_TAG),
                "Active: No controller",
            )
        finally:
            dpg.destroy_context()

    def test_override_forwards_player_pick_to_service(self) -> None:
        fake = _FakeXInputService(_live_snap(slot=0))
        live_verify._on_player_select(
            SimpleNamespace(xinput_poll_service=fake), "Player 3"
        )
        self.assertEqual(fake.selection_mode, "manual")
        self.assertEqual(fake.active_slot, 2)  # Player 3 -> zero-based slot 2
        self.assertEqual(fake.selected, [2])

    def test_override_auto_resumes_auto_selection(self) -> None:
        fake = _FakeXInputService(_live_snap(slot=0))
        fake.select_slot(1)  # start pinned
        live_verify._on_player_select(
            SimpleNamespace(xinput_poll_service=fake), "Auto"
        )
        self.assertEqual(fake.selection_mode, "auto")
        self.assertIsNone(fake.active_slot)

    def test_combo_default_reflects_manual_pin_then_auto(self) -> None:
        fake = _FakeXInputService(_live_snap(slot=2))
        fake.select_slot(2)
        self.assertEqual(live_verify._player_combo_default(fake), "Player 3")
        fake.select_auto()
        self.assertEqual(live_verify._player_combo_default(fake), "Auto")


class PollServiceConstructionTests(unittest.TestCase):
    """Item K: the lazy poll-service property is the sole construction site and
    memoizes, so the XInput DLL loads once and one worker is reused per run."""

    def test_poll_service_constructed_once_per_run(self) -> None:
        shell = _make_write_shell(settings_service=MagicMock())
        shell._xinput_poll_service = None
        count = {"n": 0}

        def _factory(*_a, **_k):
            count["n"] += 1
            return _FakeXInputService(XInputSnapshot.unavailable())

        with patch(
            "zd_app.ui.app_shell.XInputPollService", side_effect=_factory
        ):
            first = shell.xinput_poll_service
            again = shell.xinput_poll_service
            third = shell.xinput_poll_service
        self.assertIs(first, again)
        self.assertIs(again, third)
        self.assertEqual(count["n"], 1)  # constructed exactly once


if __name__ == "__main__":
    unittest.main()
