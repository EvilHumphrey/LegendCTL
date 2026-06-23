"""Hardware-free tests for the v1 standalone trigger service.

All Win32 calls are routed through dependency-injected seams on
``fire_trigger_sequence``; tests substitute fakes that record call order
and return controlled results. No real controller, no real ctypes calls.
Tests run on any platform.
"""

from __future__ import annotations

import threading
import unittest
from typing import Optional
from unittest import mock

from zd_app.services.standalone_trigger_service import (
    DEFAULT_FINAL_RETRY_PAUSE_S,
    DEFAULT_INTER_ATTEMPT_PAUSE_S,
    DEFAULT_INTER_TRIGGER_GAP_S,
    DEFAULT_MAX_RETRIES,
    GET_INFORMATION_OUT_LEN,
    GET_LED_INPUT,
    GET_LED_OUT_LEN,
    GET_STATE_INPUT,
    GET_STATE_OUT_LEN,
    HID_COLLECTION_DESCRIPTOR_OUT_LEN,
    HID_COLLECTION_INFORMATION_OUT_LEN,
    HID_READ_OPEN_DESIRED_ACCESS,
    HID_READ_OPEN_FLAGS_AND_ATTRIBUTES,
    HID_REPORT_SIZE,
    HID_WRITE_OPEN_DESIRED_ACCESS,
    HID_WRITE_OPEN_FLAGS_AND_ATTRIBUTES,
    IOCTL_GET_INFORMATION,
    IOCTL_GET_LED,
    IOCTL_GET_STATE,
    IOCTL_HID_GET_COLLECTION_DESCRIPTOR,
    IOCTL_HID_GET_COLLECTION_INFORMATION,
    IOCTL_SET_STATE,
    KEEPALIVE_WRITE_A_PAYLOAD,
    KEEPALIVE_WRITE_B_PAYLOAD,
    SET_STATE_PREAMBLE_INPUT,
    SINGLETON_MUTEX_NAME,
    TRIGGER_PAYLOAD_1,
    TRIGGER_PAYLOAD_2,
    TRIGGER_PAYLOAD_3,
    EnsureDualModeResult,
    EnsureDualModeStatus,
    HidKeepaliveIo,
    HidSessionResult,
    HidSessionStatus,
    IoctlAttempt,
    KeepaliveLoop,
    KeepaliveSnapshot,
    OverlappedReadContext,
    StandaloneServiceSnapshot,
    StandaloneStartResult,
    StandaloneStartStatus,
    StandaloneTriggerService,
    TriggerOutcome,
    TriggerResult,
    _choose_hidden_path,
    _choose_xusb_path,
    ensure_dual_mode,
    fire_trigger_sequence,
    open_hid_session,
)


_DIRECT_HANDLE = 0x1111
_DUP_HANDLE = 0x2222
_FAKE_PATH = r"\\?\usb#vid_413d&pid_2104&mi_00#7&fake#{ec87f1e3-c13b-4100-b5f7-8b84d54260cb}"


class _Recorder:
    """Records DI seam calls so tests can assert ordering + arguments."""

    def __init__(
        self,
        *,
        xusb_paths: list[str] | None = None,
        open_result: tuple[int | None, int] = (_DIRECT_HANDLE, 0),
        duplicate_result: tuple[int | None, int] = (_DUP_HANDLE, 0),
        # ioctl_results indexed by step (1..7); each entry is
        # (ok, last_error, bytes_returned, out_hex). Default: all success.
        ioctl_results: dict[int, tuple[bool, int, int, str]] | None = None,
        hidden_responses: list[bool] | None = None,
        clock_ticks: list[float] | None = None,
    ):
        self.xusb_paths = (
            xusb_paths if xusb_paths is not None else [_FAKE_PATH]
        )
        self.open_result = open_result
        self.duplicate_result = duplicate_result
        self.ioctl_results = ioctl_results or {}
        # Distinguish None (default-to-[True]) from [] (explicit always-False).
        self.hidden_responses = (
            list(hidden_responses) if hidden_responses is not None else [True]
        )
        self.clock_ticks = list(clock_ticks) if clock_ticks else None

        self.events: list[tuple] = []
        self._ioctl_step = 0
        self._clock_idx = 0
        self._clock_t = 0.0

    # --- seam implementations ---

    def enumerate_xusb_paths(self) -> list[str]:
        self.events.append(("enumerate_xusb_paths",))
        return list(self.xusb_paths)

    def open_xusb_handle(self, path: str) -> tuple[int | None, int]:
        self.events.append(("open_xusb_handle", path))
        return self.open_result

    def duplicate_handle(self, source_handle: int) -> tuple[int | None, int]:
        self.events.append(("duplicate_handle", source_handle))
        return self.duplicate_result

    def device_io_control(
        self, handle: int, ioctl: int, in_bytes: bytes, out_len: int,
    ) -> tuple[bool, int, int, str]:
        self._ioctl_step += 1
        self.events.append(
            ("device_io_control", self._ioctl_step, handle, ioctl, in_bytes, out_len)
        )
        if self._ioctl_step in self.ioctl_results:
            return self.ioctl_results[self._ioctl_step]
        # Default: success with reasonable return shapes
        if ioctl == IOCTL_GET_INFORMATION:
            return True, 0, GET_INFORMATION_OUT_LEN, "03010100000000003d410421"
        if ioctl == IOCTL_GET_STATE:
            return True, 0, GET_STATE_OUT_LEN, "00" * GET_STATE_OUT_LEN
        if ioctl == IOCTL_GET_LED:
            return True, 0, GET_LED_OUT_LEN, "030106"
        if ioctl == IOCTL_SET_STATE:
            return True, 0, 0, ""
        return True, 0, 0, ""

    def close_handle(self, handle: int) -> bool:
        self.events.append(("close_handle", handle))
        return True

    def check_hidden_visible(self) -> bool:
        self.events.append(("check_hidden_visible",))
        if self.hidden_responses:
            return self.hidden_responses.pop(0)
        return False

    def sleep(self, s: float) -> None:
        self.events.append(("sleep", s))

    def clock(self) -> float:
        if self.clock_ticks is not None:
            if self._clock_idx < len(self.clock_ticks):
                t = self.clock_ticks[self._clock_idx]
                self._clock_idx += 1
                return t
            return self.clock_ticks[-1]
        # Synthetic monotonic clock that auto-advances each call by 0.001
        self._clock_t += 0.001
        return self._clock_t


def _kwargs_from(rec: _Recorder, **overrides) -> dict:
    base = {
        "enumerate_xusb_paths": rec.enumerate_xusb_paths,
        "open_xusb_handle": rec.open_xusb_handle,
        "duplicate_handle": rec.duplicate_handle,
        "device_io_control": rec.device_io_control,
        "close_handle": rec.close_handle,
        "check_hidden_visible": rec.check_hidden_visible,
        "clock": rec.clock,
        "sleep": rec.sleep,
        # Tight defaults so timeout-driven tests don't run long even
        # under accident.
        "poll_timeout_s": 0.5,
        "poll_interval_s": 0.0,
    }
    base.update(overrides)
    return base


class TestHappyPath(unittest.TestCase):
    def test_full_sequence_succeeds(self) -> None:
        rec = _Recorder()
        result = fire_trigger_sequence(**_kwargs_from(rec))

        self.assertEqual(result.outcome, TriggerOutcome.SUCCESS)
        self.assertEqual(result.target_path, _FAKE_PATH)
        self.assertTrue(result.open_ok)
        self.assertTrue(result.duplicate_ok)
        self.assertIsNotNone(result.visible_after_ms)
        self.assertIsNone(result.error_message)

    def test_records_seven_ioctl_attempts_in_order(self) -> None:
        rec = _Recorder()
        result = fire_trigger_sequence(**_kwargs_from(rec))

        attempts = result.ioctl_attempts
        self.assertEqual(len(attempts), 7)

        # Step + ioctl + label expectations
        expected = [
            (1, "identify", IOCTL_GET_INFORMATION, b""),
            (2, "get_state", IOCTL_GET_STATE, GET_STATE_INPUT),
            (3, "get_led", IOCTL_GET_LED, GET_LED_INPUT),
            (4, "set_state_preamble", IOCTL_SET_STATE, SET_STATE_PREAMBLE_INPUT),
            (5, "trigger_1", IOCTL_SET_STATE, TRIGGER_PAYLOAD_1),
            (6, "trigger_2", IOCTL_SET_STATE, TRIGGER_PAYLOAD_2),
            (7, "trigger_3", IOCTL_SET_STATE, TRIGGER_PAYLOAD_3),
        ]
        for attempt, (step, label, ioctl, payload) in zip(attempts, expected):
            self.assertEqual(attempt.step, step)
            self.assertEqual(attempt.label, label)
            self.assertEqual(attempt.ioctl, ioctl)
            self.assertEqual(attempt.in_hex, payload.hex())
            self.assertTrue(attempt.ok)
            self.assertEqual(attempt.last_error, 0)

    def test_identify_runs_on_direct_handle_remaining_run_on_dup(self) -> None:
        rec = _Recorder()
        fire_trigger_sequence(**_kwargs_from(rec))

        ioctl_calls = [e for e in rec.events if e[0] == "device_io_control"]
        self.assertEqual(len(ioctl_calls), 7)
        # Step 1 (identify) on direct
        self.assertEqual(ioctl_calls[0][2], _DIRECT_HANDLE)
        # Steps 2..7 on duplicate
        for call in ioctl_calls[1:]:
            self.assertEqual(call[2], _DUP_HANDLE)

    def test_two_inter_trigger_sleeps(self) -> None:
        # Gaps: trigger_1 -> trigger_2 (1 sleep), trigger_2 -> trigger_3 (1 sleep).
        # Preamble -> trigger_1 has NO sleep; identify, get_state, get_led
        # are also gap-free. Total sleeps from gaps = 2. Polling sleep is
        # configured to 0.0 in the test kwargs, so polling adds no sleep
        # entries on a happy-path immediate-visible.
        rec = _Recorder()
        fire_trigger_sequence(**_kwargs_from(rec))

        sleeps = [e for e in rec.events if e[0] == "sleep"]
        # On first hidden_visible() call returning True, polling exits
        # before any poll-interval sleep, so total sleeps = 2 inter-trigger
        # gaps.
        self.assertEqual(len(sleeps), 2)
        self.assertAlmostEqual(sleeps[0][1], DEFAULT_INTER_TRIGGER_GAP_S)
        self.assertAlmostEqual(sleeps[1][1], DEFAULT_INTER_TRIGGER_GAP_S)

    def test_cleans_up_both_handles(self) -> None:
        rec = _Recorder()
        fire_trigger_sequence(**_kwargs_from(rec))

        closes = [e for e in rec.events if e[0] == "close_handle"]
        self.assertEqual(len(closes), 2)
        # Direct is closed early (between step 4 preamble and step 5 trigger 1)
        # to match the vendor-observed pattern. Dup is closed in finally afterwards.
        self.assertEqual(closes[0][1], _DIRECT_HANDLE)
        self.assertEqual(closes[1][1], _DUP_HANDLE)

    def test_direct_handle_closed_before_trigger_packets(self) -> None:
        # The early-close-direct redirect: direct must be closed AFTER step 4
        # (set_state_preamble) and BEFORE step 5 (trigger_1). Every successful
        # probe (4, v5, H1 lifecycle) used this ordering.
        rec = _Recorder()
        fire_trigger_sequence(**_kwargs_from(rec))

        # Find the indices of: preamble ioctl (step 4), trigger_1 ioctl
        # (step 5), and the direct-close call.
        def _ioctl_idx(step_num: int) -> int:
            for i, e in enumerate(rec.events):
                if e[0] == "device_io_control" and e[1] == step_num:
                    return i
            raise AssertionError(f"no device_io_control event with step={step_num}")

        def _direct_close_idx() -> int:
            for i, e in enumerate(rec.events):
                if e[0] == "close_handle" and e[1] == _DIRECT_HANDLE:
                    return i
            raise AssertionError("no close_handle event for direct handle")

        preamble_idx = _ioctl_idx(4)
        trigger_1_idx = _ioctl_idx(5)
        direct_close_idx = _direct_close_idx()

        self.assertLess(
            preamble_idx, direct_close_idx,
            "direct must be closed after set_state_preamble",
        )
        self.assertLess(
            direct_close_idx, trigger_1_idx,
            "direct must be closed before trigger_1",
        )

    def test_direct_handle_only_closed_once_on_happy_path(self) -> None:
        # Defensive: the finally block must not double-close direct after the
        # early-close path took ownership. Recorder records every close call;
        # exactly one should target _DIRECT_HANDLE.
        rec = _Recorder()
        fire_trigger_sequence(**_kwargs_from(rec))

        direct_closes = [
            e for e in rec.events
            if e[0] == "close_handle" and e[1] == _DIRECT_HANDLE
        ]
        self.assertEqual(len(direct_closes), 1)

    def test_explicit_path_skips_enumeration(self) -> None:
        rec = _Recorder()
        custom_path = r"\\?\usb#vid_413d&pid_2104&mi_00#7&custom#{...}"
        result = fire_trigger_sequence(
            **_kwargs_from(rec, explicit_path=custom_path)
        )

        self.assertEqual(result.outcome, TriggerOutcome.SUCCESS)
        self.assertEqual(result.target_path, custom_path)
        # enumerate_xusb_paths was never called
        self.assertEqual(
            [e for e in rec.events if e[0] == "enumerate_xusb_paths"], []
        )

    def test_polling_sleeps_until_visible(self) -> None:
        # Hidden becomes visible only on the 3rd check; verify polling
        # sleeps in between, then exits.
        rec = _Recorder(hidden_responses=[False, False, True])
        result = fire_trigger_sequence(
            **_kwargs_from(rec, poll_interval_s=0.05, poll_timeout_s=5.0)
        )

        self.assertEqual(result.outcome, TriggerOutcome.SUCCESS)
        # 3 hidden checks, 2 poll sleeps between them
        hidden_calls = [e for e in rec.events if e[0] == "check_hidden_visible"]
        self.assertEqual(len(hidden_calls), 3)
        post_ioctl_sleeps = [
            e for e in rec.events if e[0] == "sleep" and abs(e[1] - 0.05) < 1e-9
        ]
        self.assertEqual(len(post_ioctl_sleeps), 2)


class TestFailurePaths(unittest.TestCase):
    def test_no_xusb_path_short_circuits(self) -> None:
        rec = _Recorder(xusb_paths=[])
        result = fire_trigger_sequence(**_kwargs_from(rec))

        self.assertEqual(result.outcome, TriggerOutcome.NO_XUSB_PATH)
        self.assertIsNone(result.target_path)
        self.assertIn("VID_413D", result.error_message or "")
        # No open / dup / ioctl / close calls
        forbidden = {"open_xusb_handle", "duplicate_handle", "device_io_control", "close_handle"}
        for event in rec.events:
            self.assertNotIn(event[0], forbidden)

    def test_open_failed(self) -> None:
        ERROR_FILE_NOT_FOUND = 2
        rec = _Recorder(open_result=(None, ERROR_FILE_NOT_FOUND))
        result = fire_trigger_sequence(**_kwargs_from(rec))

        self.assertEqual(result.outcome, TriggerOutcome.OPEN_FAILED)
        self.assertFalse(result.open_ok)
        self.assertEqual(result.open_last_error, ERROR_FILE_NOT_FOUND)
        self.assertEqual(result.target_path, _FAKE_PATH)
        # No dup / ioctl calls; no close (we only close handles we got)
        forbidden = {"duplicate_handle", "device_io_control", "close_handle"}
        for event in rec.events:
            self.assertNotIn(event[0], forbidden)

    def test_identify_ioctl_failure_skips_dup_and_remaining(self) -> None:
        ERROR_INVALID_FUNCTION = 1
        rec = _Recorder(
            ioctl_results={1: (False, ERROR_INVALID_FUNCTION, 0, "")},
        )
        result = fire_trigger_sequence(**_kwargs_from(rec))

        self.assertEqual(result.outcome, TriggerOutcome.IOCTL_FAILED)
        self.assertEqual(len(result.ioctl_attempts), 1)
        self.assertEqual(result.ioctl_attempts[0].step, 1)
        self.assertFalse(result.ioctl_attempts[0].ok)
        self.assertEqual(
            result.ioctl_attempts[0].last_error, ERROR_INVALID_FUNCTION
        )
        # Direct handle closed; dup never created -> only one close
        closes = [e for e in rec.events if e[0] == "close_handle"]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0][1], _DIRECT_HANDLE)
        # Duplicate never called
        self.assertEqual(
            [e for e in rec.events if e[0] == "duplicate_handle"], []
        )

    def test_duplicate_failure_after_identify(self) -> None:
        ERROR_ACCESS_DENIED = 5
        rec = _Recorder(duplicate_result=(None, ERROR_ACCESS_DENIED))
        result = fire_trigger_sequence(**_kwargs_from(rec))

        self.assertEqual(result.outcome, TriggerOutcome.DUPLICATE_FAILED)
        self.assertFalse(result.duplicate_ok)
        self.assertEqual(result.duplicate_last_error, ERROR_ACCESS_DENIED)
        # Identify (step 1) was attempted; no further IOCTLs
        self.assertEqual(len(result.ioctl_attempts), 1)
        self.assertEqual(result.ioctl_attempts[0].step, 1)
        # Direct handle closed; dup never created
        closes = [e for e in rec.events if e[0] == "close_handle"]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0][1], _DIRECT_HANDLE)

    def test_trigger_2_ioctl_failure_at_step_6(self) -> None:
        ERROR_GEN_FAILURE = 31
        rec = _Recorder(
            ioctl_results={6: (False, ERROR_GEN_FAILURE, 0, "")},
        )
        result = fire_trigger_sequence(**_kwargs_from(rec))

        self.assertEqual(result.outcome, TriggerOutcome.IOCTL_FAILED)
        # Steps 1..6 were attempted, 7 was skipped after step 6 failed
        self.assertEqual(len(result.ioctl_attempts), 6)
        self.assertEqual(result.ioctl_attempts[-1].step, 6)
        self.assertEqual(result.ioctl_attempts[-1].label, "trigger_2")
        self.assertFalse(result.ioctl_attempts[-1].ok)
        # Both handles still closed via finally block
        closes = [e for e in rec.events if e[0] == "close_handle"]
        self.assertEqual(len(closes), 2)
        # Polling never started
        self.assertEqual(
            [e for e in rec.events if e[0] == "check_hidden_visible"], []
        )

    def test_hidden_never_visible_yields_distinct_outcome(self) -> None:
        # hidden_responses=[] -> returns False on every poll until timeout
        rec = _Recorder(hidden_responses=[])
        # Use a fake clock that advances past the deadline after one poll
        ticks = iter([0.0, 0.001, 0.002, 0.003, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])

        def fake_clock():
            try:
                return next(ticks)
            except StopIteration:
                return 100.0

        result = fire_trigger_sequence(
            **_kwargs_from(
                rec,
                clock=fake_clock,
                poll_timeout_s=0.1,
                poll_interval_s=0.01,
            )
        )

        self.assertEqual(result.outcome, TriggerOutcome.HIDDEN_NOT_VISIBLE)
        self.assertEqual(len(result.ioctl_attempts), 7)
        self.assertTrue(all(a.ok for a in result.ioctl_attempts))
        self.assertIsNone(result.visible_after_ms)
        self.assertIn("did not", (result.error_message or "").lower())
        # Both handles closed
        closes = [e for e in rec.events if e[0] == "close_handle"]
        self.assertEqual(len(closes), 2)


class TestChooseXusbPath(unittest.TestCase):
    def test_prefers_usb_bus_prefix(self) -> None:
        usb_path = r"\\?\usb#vid_413d&pid_2104&mi_00#7&abc#{guid}"
        composite = r"\\?\hid#vid_413d&pid_2104&mi_00#7&abc#{guid}"
        # Composite first; choose still picks usb#
        chosen = _choose_xusb_path([composite, usb_path])
        self.assertEqual(chosen, usb_path)

    def test_returns_none_for_empty(self) -> None:
        self.assertIsNone(_choose_xusb_path([]))

    def test_single_path_passthrough(self) -> None:
        only = r"\\?\hid#vid_413d&pid_2104&mi_00#abc#{guid}"
        self.assertEqual(_choose_xusb_path([only]), only)


class TestPayloadConstants(unittest.TestCase):
    """Lock the captured payload bytes so a future refactor can't silently
    change them. XX bytes 01/AA/23 are required-specific."""

    def test_trigger_payloads_are_5_bytes_each(self) -> None:
        for payload in (TRIGGER_PAYLOAD_1, TRIGGER_PAYLOAD_2, TRIGGER_PAYLOAD_3):
            self.assertEqual(len(payload), 5)

    def test_trigger_payload_xx_bytes_are_01_AA_23(self) -> None:
        # The XX byte is the 4th byte (0-indexed: position 3).
        self.assertEqual(TRIGGER_PAYLOAD_1[3], 0x01)
        self.assertEqual(TRIGGER_PAYLOAD_2[3], 0xAA)
        self.assertEqual(TRIGGER_PAYLOAD_3[3], 0x23)

    def test_preamble_input_is_0006000001(self) -> None:
        self.assertEqual(SET_STATE_PREAMBLE_INPUT.hex(), "0006000001")

    def test_get_state_input_is_010100(self) -> None:
        self.assertEqual(GET_STATE_INPUT.hex(), "010100")

    def test_get_led_input_is_010100(self) -> None:
        self.assertEqual(GET_LED_INPUT.hex(), "010100")


class TestIoctlAttemptDataclass(unittest.TestCase):
    def test_attempt_records_in_hex_correctly(self) -> None:
        rec = _Recorder()
        result = fire_trigger_sequence(**_kwargs_from(rec))
        # Step 1 input is empty bytes -> empty hex string
        self.assertEqual(result.ioctl_attempts[0].in_hex, "")
        # Trigger packets carry the captured XX bytes
        self.assertEqual(result.ioctl_attempts[4].in_hex, "0000550102")
        self.assertEqual(result.ioctl_attempts[5].in_hex, "000055aa02")
        self.assertEqual(result.ioctl_attempts[6].in_hex, "0000552302")

    def test_attempt_records_out_hex_for_metadata_ioctls(self) -> None:
        rec = _Recorder()
        result = fire_trigger_sequence(**_kwargs_from(rec))
        # identify -> 12-byte response from default fake
        self.assertTrue(result.ioctl_attempts[0].out_hex)
        self.assertEqual(result.ioctl_attempts[0].bytes_returned, GET_INFORMATION_OUT_LEN)
        # set_state has out_len=0 -> empty
        self.assertEqual(result.ioctl_attempts[3].out_hex, "")
        self.assertEqual(result.ioctl_attempts[3].bytes_returned, 0)


# ===========================================================================
# ensure_dual_mode retry wrapper tests
# ===========================================================================


class _StubFireTrigger:
    """Records each fire_trigger call; returns canned outcomes in sequence."""

    def __init__(self, outcomes: list[TriggerOutcome]):
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> TriggerResult:
        self.calls.append(kwargs)
        if not self._outcomes:
            raise AssertionError(
                "fire_trigger called more times than the test expected"
            )
        outcome = self._outcomes.pop(0)
        # Build a minimal TriggerResult — the wrapper inspects outcome,
        # target_path, visible_after_ms, error_message.
        kwargs_path = kwargs.get("explicit_path") or _FAKE_PATH
        return TriggerResult(
            outcome=outcome,
            target_path=kwargs_path,
            open_ok=outcome != TriggerOutcome.OPEN_FAILED,
            duplicate_ok=outcome
            not in (
                TriggerOutcome.OPEN_FAILED,
                TriggerOutcome.DUPLICATE_FAILED,
                TriggerOutcome.NO_XUSB_PATH,
            ),
            visible_after_ms=391 if outcome == TriggerOutcome.SUCCESS else None,
            error_message=(
                None
                if outcome == TriggerOutcome.SUCCESS
                else f"stub error for {outcome.value}"
            ),
        )


class _SleepRecorder:
    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, s: float) -> None:
        self.calls.append(s)


class TestEnsureDualModeHappyPath(unittest.TestCase):
    def test_first_attempt_success_no_retry(self) -> None:
        fire = _StubFireTrigger([TriggerOutcome.SUCCESS])
        sleep = _SleepRecorder()
        result = ensure_dual_mode(fire_trigger=fire, sleep=sleep)

        self.assertEqual(result.status, EnsureDualModeStatus.ESTABLISHED)
        self.assertEqual(result.target_path, _FAKE_PATH)
        self.assertEqual(result.visible_after_ms, 391)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(len(fire.calls), 1)
        self.assertEqual(len(sleep.calls), 0, "no retry sleeps on first-attempt success")
        self.assertIsNone(result.error_message)


class TestEnsureDualModeRetry(unittest.TestCase):
    def test_immediate_retry_succeeds_no_wait(self) -> None:
        # First attempt HIDDEN_NOT_VISIBLE, second SUCCESS. The retry
        # before attempt 2 uses inter_attempt_pause_s (default 0.0 — Probe 4
        # showed zero-wait recovers ~100%).
        fire = _StubFireTrigger(
            [TriggerOutcome.HIDDEN_NOT_VISIBLE, TriggerOutcome.SUCCESS]
        )
        sleep = _SleepRecorder()
        result = ensure_dual_mode(fire_trigger=fire, sleep=sleep)

        self.assertEqual(result.status, EnsureDualModeStatus.ESTABLISHED)
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(len(fire.calls), 2)
        # Exactly one sleep call (between attempts 1 and 2), and it's 0s
        # by default per inter_attempt_pause_s.
        self.assertEqual(sleep.calls, [DEFAULT_INTER_ATTEMPT_PAUSE_S])

    def test_final_retry_succeeds_after_30s_pause(self) -> None:
        # First two attempts HIDDEN_NOT_VISIBLE, third SUCCESS. The retry
        # before the LAST attempt uses final_retry_pause_s (default 30s).
        fire = _StubFireTrigger(
            [
                TriggerOutcome.HIDDEN_NOT_VISIBLE,
                TriggerOutcome.HIDDEN_NOT_VISIBLE,
                TriggerOutcome.SUCCESS,
            ]
        )
        sleep = _SleepRecorder()
        result = ensure_dual_mode(fire_trigger=fire, sleep=sleep)

        self.assertEqual(result.status, EnsureDualModeStatus.ESTABLISHED)
        self.assertEqual(len(result.attempts), 3)
        # Sleeps: before attempt 2 (inter_attempt_pause_s=0), before
        # attempt 3 (final_retry_pause_s=30).
        self.assertEqual(
            sleep.calls,
            [DEFAULT_INTER_ATTEMPT_PAUSE_S, DEFAULT_FINAL_RETRY_PAUSE_S],
        )

    def test_all_attempts_fail_yields_exhausted_retries(self) -> None:
        fire = _StubFireTrigger(
            [
                TriggerOutcome.HIDDEN_NOT_VISIBLE,
                TriggerOutcome.HIDDEN_NOT_VISIBLE,
                TriggerOutcome.HIDDEN_NOT_VISIBLE,
            ]
        )
        sleep = _SleepRecorder()
        result = ensure_dual_mode(fire_trigger=fire, sleep=sleep)

        self.assertEqual(result.status, EnsureDualModeStatus.EXHAUSTED_RETRIES)
        self.assertEqual(len(result.attempts), 3)
        self.assertIsNone(result.visible_after_ms)
        self.assertIn("unplugging", (result.error_message or "").lower())
        self.assertIn("3 trigger attempts", result.error_message or "")
        # Two sleeps: 0s before attempt 2, 30s before attempt 3
        self.assertEqual(
            sleep.calls,
            [DEFAULT_INTER_ATTEMPT_PAUSE_S, DEFAULT_FINAL_RETRY_PAUSE_S],
        )

    def test_max_retries_zero_runs_one_attempt(self) -> None:
        # max_retries=0 means total_attempts=1; on HIDDEN_NOT_VISIBLE we
        # should immediately surface EXHAUSTED_RETRIES with no retry sleep.
        fire = _StubFireTrigger([TriggerOutcome.HIDDEN_NOT_VISIBLE])
        sleep = _SleepRecorder()
        result = ensure_dual_mode(fire_trigger=fire, sleep=sleep, max_retries=0)

        self.assertEqual(result.status, EnsureDualModeStatus.EXHAUSTED_RETRIES)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(len(sleep.calls), 0)

    def test_default_max_retries_is_two(self) -> None:
        # Lock the production default: 2 retries on top of initial = 3
        # total. Probe 4 data point.
        self.assertEqual(DEFAULT_MAX_RETRIES, 2)

    def test_custom_pause_values_propagate(self) -> None:
        # Override pauses; verify they're used verbatim.
        fire = _StubFireTrigger(
            [
                TriggerOutcome.HIDDEN_NOT_VISIBLE,
                TriggerOutcome.HIDDEN_NOT_VISIBLE,
                TriggerOutcome.SUCCESS,
            ]
        )
        sleep = _SleepRecorder()
        ensure_dual_mode(
            fire_trigger=fire,
            sleep=sleep,
            inter_attempt_pause_s=1.5,
            final_retry_pause_s=99.0,
        )
        self.assertEqual(sleep.calls, [1.5, 99.0])


class TestEnsureDualModeHardFailures(unittest.TestCase):
    def test_no_xusb_path_short_circuits(self) -> None:
        fire = _StubFireTrigger([TriggerOutcome.NO_XUSB_PATH])
        sleep = _SleepRecorder()
        result = ensure_dual_mode(fire_trigger=fire, sleep=sleep)

        self.assertEqual(result.status, EnsureDualModeStatus.NO_XUSB_PATH)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(len(fire.calls), 1, "no retry on hard failure")
        self.assertEqual(len(sleep.calls), 0)
        self.assertIn("NO_XUSB_PATH".lower(), (result.error_message or "").lower())

    def test_open_failed_short_circuits(self) -> None:
        fire = _StubFireTrigger([TriggerOutcome.OPEN_FAILED])
        sleep = _SleepRecorder()
        result = ensure_dual_mode(fire_trigger=fire, sleep=sleep)

        self.assertEqual(result.status, EnsureDualModeStatus.OPEN_FAILED)
        self.assertEqual(len(fire.calls), 1)
        self.assertEqual(len(sleep.calls), 0)

    def test_duplicate_failed_short_circuits(self) -> None:
        fire = _StubFireTrigger([TriggerOutcome.DUPLICATE_FAILED])
        sleep = _SleepRecorder()
        result = ensure_dual_mode(fire_trigger=fire, sleep=sleep)

        self.assertEqual(result.status, EnsureDualModeStatus.DUPLICATE_FAILED)
        self.assertEqual(len(fire.calls), 1)
        self.assertEqual(len(sleep.calls), 0)

    def test_ioctl_failed_short_circuits(self) -> None:
        fire = _StubFireTrigger([TriggerOutcome.IOCTL_FAILED])
        sleep = _SleepRecorder()
        result = ensure_dual_mode(fire_trigger=fire, sleep=sleep)

        self.assertEqual(result.status, EnsureDualModeStatus.IOCTL_FAILED)
        self.assertEqual(len(fire.calls), 1)
        self.assertEqual(len(sleep.calls), 0)

    def test_hard_failure_after_one_transient_does_not_continue(self) -> None:
        # First attempt transient, second attempt hard failure.
        # The wrapper must STOP at attempt 2 and propagate the hard
        # failure status — not keep retrying.
        fire = _StubFireTrigger(
            [TriggerOutcome.HIDDEN_NOT_VISIBLE, TriggerOutcome.IOCTL_FAILED]
        )
        sleep = _SleepRecorder()
        result = ensure_dual_mode(fire_trigger=fire, sleep=sleep)

        self.assertEqual(result.status, EnsureDualModeStatus.IOCTL_FAILED)
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(len(fire.calls), 2)
        # Only the inter-attempt sleep before attempt 2.
        self.assertEqual(sleep.calls, [DEFAULT_INTER_ATTEMPT_PAUSE_S])


class TestEnsureDualModeKwargForwarding(unittest.TestCase):
    def test_trigger_kwargs_forwarded_to_fire_trigger(self) -> None:
        # Verify that any keyword args inside trigger_kwargs propagate to
        # each fire_trigger call. Used by the end-to-end tests to inject DI seams.
        fire = _StubFireTrigger([TriggerOutcome.SUCCESS])
        sleep = _SleepRecorder()
        ensure_dual_mode(
            fire_trigger=fire,
            sleep=sleep,
            trigger_kwargs={"explicit_path": "custom_path", "poll_timeout_s": 3.0},
        )
        self.assertEqual(len(fire.calls), 1)
        self.assertEqual(fire.calls[0]["explicit_path"], "custom_path")
        self.assertEqual(fire.calls[0]["poll_timeout_s"], 3.0)

    def test_trigger_kwargs_none_default_passes_no_kwargs(self) -> None:
        fire = _StubFireTrigger([TriggerOutcome.SUCCESS])
        sleep = _SleepRecorder()
        ensure_dual_mode(fire_trigger=fire, sleep=sleep)
        self.assertEqual(fire.calls[0], {})


class TestEnsureDualModeAttemptList(unittest.TestCase):
    def test_attempts_list_records_each_trigger_result_in_order(self) -> None:
        fire = _StubFireTrigger(
            [
                TriggerOutcome.HIDDEN_NOT_VISIBLE,
                TriggerOutcome.HIDDEN_NOT_VISIBLE,
                TriggerOutcome.SUCCESS,
            ]
        )
        sleep = _SleepRecorder()
        result = ensure_dual_mode(fire_trigger=fire, sleep=sleep)

        self.assertEqual(len(result.attempts), 3)
        self.assertEqual(result.attempts[0].outcome, TriggerOutcome.HIDDEN_NOT_VISIBLE)
        self.assertEqual(result.attempts[1].outcome, TriggerOutcome.HIDDEN_NOT_VISIBLE)
        self.assertEqual(result.attempts[2].outcome, TriggerOutcome.SUCCESS)


# ===========================================================================
# open_hid_session tests
# ===========================================================================


_HID_FAKE_PATH = r"\\?\hid#vid_20bc&pid_5080#7&fake#{4d1e55b2-f16f-11cf-88cb-001111000030}"
_WRITE_HANDLE = 0x3333
_READ_HANDLE = 0x4444


class _HidRecorder:
    """Records HID-side seam calls for open_hid_session tests."""

    def __init__(
        self,
        *,
        hidden_paths: list[str] | None = None,
        write_open_result: tuple[int | None, int] = (_WRITE_HANDLE, 0),
        read_open_result: tuple[int | None, int] = (_READ_HANDLE, 0),
        descriptor_results: dict[int, tuple[bool, int, int, str]] | None = None,
    ):
        self.hidden_paths = (
            hidden_paths if hidden_paths is not None else [_HID_FAKE_PATH]
        )
        self.write_open_result = write_open_result
        self.read_open_result = read_open_result
        self.descriptor_results = descriptor_results or {}
        self.events: list[tuple] = []
        self._descriptor_step = 0

    def enumerate_hidden_paths(self) -> list[str]:
        self.events.append(("enumerate_hidden_paths",))
        return list(self.hidden_paths)

    def open_hid_write_handle(self, path: str) -> tuple[int | None, int]:
        self.events.append(("open_hid_write_handle", path))
        return self.write_open_result

    def open_hid_read_handle(self, path: str) -> tuple[int | None, int]:
        self.events.append(("open_hid_read_handle", path))
        return self.read_open_result

    def device_io_control(
        self, handle: int, ioctl: int, in_bytes: bytes, out_len: int,
    ) -> tuple[bool, int, int, str]:
        self._descriptor_step += 1
        self.events.append(
            ("device_io_control", self._descriptor_step, handle, ioctl, in_bytes, out_len)
        )
        if self._descriptor_step in self.descriptor_results:
            return self.descriptor_results[self._descriptor_step]
        # Default: success with vendor-shaped output
        if ioctl == IOCTL_HID_GET_COLLECTION_INFORMATION:
            # 12-byte response carrying VID/PID, matching the vendor's xusb22 IOCTL
            return True, 0, HID_COLLECTION_INFORMATION_OUT_LEN, "640600000000bc208050000a"
        if ioctl == IOCTL_HID_GET_COLLECTION_DESCRIPTOR:
            return True, 0, HID_COLLECTION_DESCRIPTOR_OUT_LEN, "00" * HID_COLLECTION_DESCRIPTOR_OUT_LEN
        return True, 0, 0, ""

    def close_handle(self, handle: int) -> bool:
        self.events.append(("close_handle", handle))
        return True


def _hid_kwargs(rec: _HidRecorder, **overrides) -> dict:
    base = {
        "enumerate_hidden_paths": rec.enumerate_hidden_paths,
        "open_hid_write_handle": rec.open_hid_write_handle,
        "open_hid_read_handle": rec.open_hid_read_handle,
        "device_io_control": rec.device_io_control,
        "close_handle": rec.close_handle,
    }
    base.update(overrides)
    return base


class TestOpenHidSessionHappyPath(unittest.TestCase):
    def test_opens_both_handles_and_issues_descriptor_ioctls(self) -> None:
        rec = _HidRecorder()
        result = open_hid_session(**_hid_kwargs(rec))

        self.assertEqual(result.status, HidSessionStatus.OPENED)
        self.assertEqual(result.target_path, _HID_FAKE_PATH)
        self.assertEqual(result.write_handle, _WRITE_HANDLE)
        self.assertEqual(result.read_handle, _READ_HANDLE)
        self.assertIsNone(result.error_message)

    def test_write_open_recorded_with_vendor_shape(self) -> None:
        rec = _HidRecorder()
        result = open_hid_session(**_hid_kwargs(rec))
        wo = result.write_open
        self.assertIsNotNone(wo)
        self.assertEqual(wo.role, "write")
        self.assertEqual(wo.path, _HID_FAKE_PATH)
        self.assertTrue(wo.ok)
        self.assertEqual(wo.last_error, 0)
        # Vendor's write-handle shape
        self.assertEqual(wo.desired_access, HID_WRITE_OPEN_DESIRED_ACCESS)
        self.assertEqual(wo.flags_and_attributes, HID_WRITE_OPEN_FLAGS_AND_ATTRIBUTES)

    def test_read_open_recorded_with_overlapped_shape(self) -> None:
        rec = _HidRecorder()
        result = open_hid_session(**_hid_kwargs(rec))
        ro = result.read_open
        self.assertIsNotNone(ro)
        self.assertEqual(ro.role, "read")
        self.assertTrue(ro.ok)
        self.assertEqual(ro.desired_access, HID_READ_OPEN_DESIRED_ACCESS)
        self.assertEqual(ro.flags_and_attributes, HID_READ_OPEN_FLAGS_AND_ATTRIBUTES)
        # Read handle uses overlapped flag (0x40000000)
        self.assertEqual(ro.flags_and_attributes & 0x40000000, 0x40000000)

    def test_descriptor_ioctls_issued_on_write_handle(self) -> None:
        rec = _HidRecorder()
        result = open_hid_session(**_hid_kwargs(rec))

        attempts = result.descriptor_attempts
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].label, "hid_get_collection_information")
        self.assertEqual(attempts[0].ioctl, IOCTL_HID_GET_COLLECTION_INFORMATION)
        self.assertEqual(attempts[1].label, "hid_get_collection_descriptor")
        self.assertEqual(attempts[1].ioctl, IOCTL_HID_GET_COLLECTION_DESCRIPTOR)
        self.assertTrue(all(a.ok for a in attempts))

        ioctl_calls = [e for e in rec.events if e[0] == "device_io_control"]
        self.assertEqual(len(ioctl_calls), 2)
        # Both descriptor IOCTLs target the write handle, not the read handle
        for call in ioctl_calls:
            self.assertEqual(call[2], _WRITE_HANDLE)

    def test_explicit_path_skips_enumeration(self) -> None:
        rec = _HidRecorder()
        custom = r"\\?\hid#vid_20bc&pid_5080#7&custom#{guid}"
        result = open_hid_session(**_hid_kwargs(rec, explicit_path=custom))

        self.assertEqual(result.status, HidSessionStatus.OPENED)
        self.assertEqual(result.target_path, custom)
        self.assertEqual(
            [e for e in rec.events if e[0] == "enumerate_hidden_paths"], []
        )

    def test_no_handles_closed_on_happy_path(self) -> None:
        # Caller takes ownership on OPENED — neither handle is closed by us.
        rec = _HidRecorder()
        open_hid_session(**_hid_kwargs(rec))
        closes = [e for e in rec.events if e[0] == "close_handle"]
        self.assertEqual(len(closes), 0)


class TestOpenHidSessionFailurePaths(unittest.TestCase):
    def test_no_hidden_path_short_circuits(self) -> None:
        rec = _HidRecorder(hidden_paths=[])
        result = open_hid_session(**_hid_kwargs(rec))

        self.assertEqual(result.status, HidSessionStatus.NO_HIDDEN_PATH)
        self.assertIsNone(result.target_path)
        self.assertIsNone(result.write_handle)
        self.assertIsNone(result.read_handle)
        self.assertIn("VID_20BC", result.error_message or "")
        # No open / ioctl / close calls
        forbidden = {
            "open_hid_write_handle", "open_hid_read_handle",
            "device_io_control", "close_handle",
        }
        for event in rec.events:
            self.assertNotIn(event[0], forbidden)

    def test_write_open_failure(self) -> None:
        ERROR_ACCESS_DENIED = 5
        rec = _HidRecorder(write_open_result=(None, ERROR_ACCESS_DENIED))
        result = open_hid_session(**_hid_kwargs(rec))

        self.assertEqual(result.status, HidSessionStatus.WRITE_HANDLE_OPEN_FAILED)
        self.assertIsNone(result.write_handle)
        self.assertIsNone(result.read_handle)
        self.assertEqual(result.write_open.last_error, ERROR_ACCESS_DENIED)
        self.assertFalse(result.write_open.ok)
        self.assertIsNone(result.read_open)  # never attempted
        # No handles to close
        closes = [e for e in rec.events if e[0] == "close_handle"]
        self.assertEqual(len(closes), 0)

    def test_read_open_failure_cleans_up_write(self) -> None:
        # Write opened successfully; read open failed. The write handle
        # MUST be closed before return so we don't leak it.
        ERROR_INVALID_PARAMETER = 87
        rec = _HidRecorder(read_open_result=(None, ERROR_INVALID_PARAMETER))
        result = open_hid_session(**_hid_kwargs(rec))

        self.assertEqual(result.status, HidSessionStatus.READ_HANDLE_OPEN_FAILED)
        self.assertIsNone(result.write_handle, "write handle must be cleared on failure")
        self.assertIsNone(result.read_handle)
        self.assertTrue(result.write_open.ok)
        self.assertFalse(result.read_open.ok)
        self.assertEqual(result.read_open.last_error, ERROR_INVALID_PARAMETER)
        # Write handle was opened then closed (cleanup); read never opened
        closes = [e for e in rec.events if e[0] == "close_handle"]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0][1], _WRITE_HANDLE)
        # No descriptor IOCTLs were issued (we returned before that step)
        ioctl_calls = [e for e in rec.events if e[0] == "device_io_control"]
        self.assertEqual(len(ioctl_calls), 0)

    def test_descriptor_ioctl_failure_does_not_invalidate_session(self) -> None:
        # A minimal-lifecycle run sustained 30s without these IOCTLs — they're
        # for vendor parity, not load-bearing. Failures must NOT
        # invalidate the session; both handles must still be returned.
        ERROR_NOT_SUPPORTED = 50
        rec = _HidRecorder(
            descriptor_results={
                1: (False, ERROR_NOT_SUPPORTED, 0, ""),
                2: (False, ERROR_NOT_SUPPORTED, 0, ""),
            }
        )
        result = open_hid_session(**_hid_kwargs(rec))

        self.assertEqual(result.status, HidSessionStatus.OPENED)
        self.assertEqual(result.write_handle, _WRITE_HANDLE)
        self.assertEqual(result.read_handle, _READ_HANDLE)
        self.assertEqual(len(result.descriptor_attempts), 2)
        self.assertFalse(any(a.ok for a in result.descriptor_attempts))
        # Both descriptor failures are recorded; handles still owned by caller
        closes = [e for e in rec.events if e[0] == "close_handle"]
        self.assertEqual(len(closes), 0)


class TestChooseHiddenPath(unittest.TestCase):
    def test_prefers_hid_class_prefix(self) -> None:
        hid_path = r"\\?\hid#vid_20bc&pid_5080#7&abc#{guid}"
        usb_path = r"\\?\usb#vid_20bc&pid_5080#7&abc#{guid}"
        chosen = _choose_hidden_path([usb_path, hid_path])
        self.assertEqual(chosen, hid_path)

    def test_returns_none_for_empty(self) -> None:
        self.assertIsNone(_choose_hidden_path([]))


class TestHidConstants(unittest.TestCase):
    """Lock the HID-side open shapes + IOCTL codes against silent regression."""

    def test_write_handle_is_generic_write_synchronous(self) -> None:
        # GENERIC_WRITE = 0x40000000; flags=0 means synchronous (no overlapped)
        self.assertEqual(HID_WRITE_OPEN_DESIRED_ACCESS, 0x40000000)
        self.assertEqual(HID_WRITE_OPEN_FLAGS_AND_ATTRIBUTES, 0)

    def test_read_handle_is_generic_rw_overlapped(self) -> None:
        # GENERIC_READ|GENERIC_WRITE = 0xc0000000; FILE_FLAG_OVERLAPPED = 0x40000000
        self.assertEqual(HID_READ_OPEN_DESIRED_ACCESS, 0xC0000000)
        self.assertEqual(HID_READ_OPEN_FLAGS_AND_ATTRIBUTES, 0x40000000)

    def test_descriptor_ioctl_codes(self) -> None:
        self.assertEqual(IOCTL_HID_GET_COLLECTION_INFORMATION, 0x000B01A8)
        self.assertEqual(IOCTL_HID_GET_COLLECTION_DESCRIPTOR, 0x000B0193)

    def test_descriptor_response_lengths(self) -> None:
        self.assertEqual(HID_COLLECTION_INFORMATION_OUT_LEN, 12)
        self.assertEqual(HID_COLLECTION_DESCRIPTOR_OUT_LEN, 1636)


# ===========================================================================
# KeepaliveLoop tests
# ===========================================================================


_KEEP_WRITE_HANDLE = 0x5555
_KEEP_READ_HANDLE = 0x6666


class _FakeKeepaliveIo:
    """Records keepalive seam invocations + returns canned outcomes.

    Designed for use with real threading: each call is independently
    thread-safe (single-list appends). Supports per-call result overrides
    via constructor args.
    """

    def __init__(
        self,
        *,
        write_results: Optional[list[tuple[bool, int, int]]] = None,
        post_read_results: Optional[list[tuple[bool, int]]] = None,
        wait_read_results: Optional[list[tuple[bool, int, int, str]]] = None,
        cancel_read_result: tuple[bool, int] = (True, 0),
    ):
        self.write_results = list(write_results) if write_results else None
        self.post_read_results = list(post_read_results) if post_read_results else None
        self.wait_read_results = list(wait_read_results) if wait_read_results else None
        self.cancel_read_result = cancel_read_result

        self.write_calls: list[tuple[int, bytes]] = []
        self.create_overlapped_calls = 0
        self.post_read_calls: list[tuple[int, int]] = []  # (handle, sequence_num)
        self.wait_read_calls: list[tuple[int, int]] = []  # (sequence_num, timeout_ms)
        self.cancel_read_calls: list[tuple[int, int]] = []  # (handle, sequence_num)
        self.destroy_overlapped_calls: list[int] = []  # sequence_nums

        # Used by tests to wait until N cycles have run before stop()
        self.wait_read_completion_event = threading.Event()
        self._wait_read_target_count: Optional[int] = None
        self._lock = threading.Lock()

    def expect_n_wait_reads(self, n: int) -> None:
        """Set a wait_read target count; the completion event fires once
        ``wait_read`` has been invoked at least ``n`` times.
        """
        self._wait_read_target_count = n

    def write(self, handle: int, payload: bytes) -> tuple[bool, int, int]:
        with self._lock:
            self.write_calls.append((handle, bytes(payload)))
        if self.write_results is not None and self.write_results:
            return self.write_results.pop(0)
        return True, 0, HID_REPORT_SIZE

    def create_overlapped(self) -> OverlappedReadContext:
        with self._lock:
            self.create_overlapped_calls += 1
            seq = self.create_overlapped_calls
        ctx = OverlappedReadContext()
        ctx.sequence_num = seq
        return ctx

    def post_read(
        self, handle: int, ctx: OverlappedReadContext,
    ) -> tuple[bool, int]:
        with self._lock:
            self.post_read_calls.append((handle, ctx.sequence_num))
        if self.post_read_results is not None and self.post_read_results:
            return self.post_read_results.pop(0)
        return True, 0

    def wait_read(
        self, ctx: OverlappedReadContext, timeout_ms: int,
    ) -> tuple[bool, int, int, str]:
        with self._lock:
            self.wait_read_calls.append((ctx.sequence_num, timeout_ms))
            n = len(self.wait_read_calls)
            target = self._wait_read_target_count
        ctx.completed = True
        if self.wait_read_results is not None and self.wait_read_results:
            result = self.wait_read_results.pop(0)
        else:
            # Default: success with vendor-shaped response
            result = (True, 0, HID_REPORT_SIZE,
                      "3055aad00f0001000000" + "00" * (HID_REPORT_SIZE - 10))
        if target is not None and n >= target:
            self.wait_read_completion_event.set()
        return result

    def cancel_read(
        self, handle: int, ctx: OverlappedReadContext,
    ) -> tuple[bool, int]:
        with self._lock:
            self.cancel_read_calls.append((handle, ctx.sequence_num))
        return self.cancel_read_result

    def destroy_overlapped(self, ctx: OverlappedReadContext) -> None:
        with self._lock:
            self.destroy_overlapped_calls.append(ctx.sequence_num)

    def as_io(self) -> HidKeepaliveIo:
        return HidKeepaliveIo(
            write=self.write,
            create_overlapped=self.create_overlapped,
            post_read=self.post_read,
            wait_read=self.wait_read,
            cancel_read=self.cancel_read,
            destroy_overlapped=self.destroy_overlapped,
        )


def _make_loop(io: HidKeepaliveIo, **overrides) -> KeepaliveLoop:
    """KeepaliveLoop with test-friendly defaults: tiny intervals, no inter-write sleep."""
    base = {
        "io": io,
        "cadence_s": 0.001,
        "inter_write_delay_s": 0.0,
        "read_timeout_ms": 50,
        "initial_cycle_delay_s": 0.0,
        "sleep": lambda s: None,
    }
    base.update(overrides)
    return KeepaliveLoop(**base)


class TestKeepaliveLoopLifecycle(unittest.TestCase):
    def test_initial_overlapped_read_posted_before_first_cycle(self) -> None:
        # The kickoff requires posting an overlapped ReadFile before the
        # loop's first write/wait cycle so the first wait_read has
        # something pending.
        fake = _FakeKeepaliveIo()
        fake.expect_n_wait_reads(1)
        loop = _make_loop(fake.as_io())
        loop.start(write_handle=_KEEP_WRITE_HANDLE, read_handle=_KEEP_READ_HANDLE)
        try:
            # Wait briefly for the first cycle to run a wait_read; if the
            # initial post wasn't issued, wait_read would never be called
            # (loop has no pending read to wait on... actually it would
            # call wait_read on whatever pending it has — let's just
            # confirm at least one post_read happened before any wait_read).
            self.assertTrue(
                fake.wait_read_completion_event.wait(timeout=2.0),
                "first wait_read never fired",
            )
            with fake._lock:
                first_wait_seq = fake.wait_read_calls[0][0]
                first_post_seq = fake.post_read_calls[0][1]
            self.assertEqual(first_post_seq, 1, "initial post should be sequence 1")
            self.assertEqual(
                first_wait_seq, 1,
                "first wait_read should target the initial-posted ctx",
            )
        finally:
            loop.stop()

    def test_runs_three_cycles_with_proper_per_cycle_sequence(self) -> None:
        fake = _FakeKeepaliveIo()
        fake.expect_n_wait_reads(3)
        loop = _make_loop(fake.as_io())
        loop.start(write_handle=_KEEP_WRITE_HANDLE, read_handle=_KEEP_READ_HANDLE)
        try:
            self.assertTrue(
                fake.wait_read_completion_event.wait(timeout=2.0),
                "did not complete 3 cycles within timeout",
            )
        finally:
            loop.stop()

        # Each cycle: 2 writes (A then B). At least 3 cycles complete.
        self.assertGreaterEqual(len(fake.write_calls), 6)
        # Verify alternation A/B/A/B/A/B in the first 6 calls
        for i in range(0, 6, 2):
            self.assertEqual(fake.write_calls[i][1], KEEPALIVE_WRITE_A_PAYLOAD)
            self.assertEqual(fake.write_calls[i + 1][1], KEEPALIVE_WRITE_B_PAYLOAD)
        # All writes target the write handle
        for handle, _ in fake.write_calls:
            self.assertEqual(handle, _KEEP_WRITE_HANDLE)
        # Initial post + 3 reposts = 4 posts (or more if cycles overshot)
        self.assertGreaterEqual(len(fake.post_read_calls), 4)
        # All posts target the read handle
        for handle, _ in fake.post_read_calls:
            self.assertEqual(handle, _KEEP_READ_HANDLE)
        # Wait_reads ≥ 3
        self.assertGreaterEqual(len(fake.wait_read_calls), 3)

    def test_cycle_records_capture_per_cycle_telemetry(self) -> None:
        fake = _FakeKeepaliveIo()
        fake.expect_n_wait_reads(2)
        loop = _make_loop(fake.as_io())
        loop.start(write_handle=_KEEP_WRITE_HANDLE, read_handle=_KEEP_READ_HANDLE)
        try:
            self.assertTrue(fake.wait_read_completion_event.wait(timeout=2.0))
        finally:
            loop.stop()

        cycles = loop.cycles
        self.assertGreaterEqual(len(cycles), 2)
        for c in cycles[:2]:
            self.assertTrue(c.write_a_ok)
            self.assertTrue(c.write_b_ok)
            self.assertEqual(c.write_a_bytes, HID_REPORT_SIZE)
            self.assertEqual(c.write_b_bytes, HID_REPORT_SIZE)
            self.assertTrue(c.read_ok)
            self.assertTrue(c.read_reposted)
        # cycle_num is 1-indexed
        self.assertEqual(cycles[0].cycle_num, 1)
        self.assertEqual(cycles[1].cycle_num, 2)


class TestKeepaliveLoopShutdown(unittest.TestCase):
    def test_stop_cancels_pending_overlapped_read(self) -> None:
        fake = _FakeKeepaliveIo()
        fake.expect_n_wait_reads(1)
        loop = _make_loop(fake.as_io())
        loop.start(write_handle=_KEEP_WRITE_HANDLE, read_handle=_KEEP_READ_HANDLE)
        try:
            self.assertTrue(fake.wait_read_completion_event.wait(timeout=2.0))
        finally:
            loop.stop()

        # cancel_read fires once on shutdown for the most-recently-posted ctx.
        # NB: if the loop happens to land its final repost just as stop is
        # signaled, cancel_read may run on that newly-posted ctx; either
        # way it fires at LEAST once.
        self.assertGreaterEqual(len(fake.cancel_read_calls), 1)
        # Cancel targets the read handle
        self.assertEqual(fake.cancel_read_calls[0][0], _KEEP_READ_HANDLE)

    def test_stop_destroys_overlapped_contexts(self) -> None:
        fake = _FakeKeepaliveIo()
        fake.expect_n_wait_reads(1)
        loop = _make_loop(fake.as_io())
        loop.start(write_handle=_KEEP_WRITE_HANDLE, read_handle=_KEEP_READ_HANDLE)
        try:
            self.assertTrue(fake.wait_read_completion_event.wait(timeout=2.0))
        finally:
            loop.stop()

        # Each cycle destroys the prior ctx before posting a new one;
        # plus stop() destroys the final pending ctx. So destroys ≥ 1.
        self.assertGreaterEqual(len(fake.destroy_overlapped_calls), 1)

    def test_stop_is_idempotent(self) -> None:
        fake = _FakeKeepaliveIo()
        loop = _make_loop(fake.as_io())
        loop.start(write_handle=_KEEP_WRITE_HANDLE, read_handle=_KEEP_READ_HANDLE)
        loop.stop()
        loop.stop()  # must not raise or double-cancel
        loop.stop()  # idempotent

    def test_stop_without_start_is_safe(self) -> None:
        # Edge case: someone calls stop() before start() (e.g., service
        # bails during setup). Must not crash.
        fake = _FakeKeepaliveIo()
        loop = _make_loop(fake.as_io())
        loop.stop()  # no thread to join, no pending read

    def test_can_start_again_after_stop(self) -> None:
        fake = _FakeKeepaliveIo()
        fake.expect_n_wait_reads(1)
        loop = _make_loop(fake.as_io())
        loop.start(write_handle=_KEEP_WRITE_HANDLE, read_handle=_KEEP_READ_HANDLE)
        try:
            self.assertTrue(fake.wait_read_completion_event.wait(timeout=2.0))
        finally:
            loop.stop()

        # Reset event + counters for second run
        fake.wait_read_completion_event.clear()
        fake.expect_n_wait_reads(len(fake.wait_read_calls) + 1)
        loop.start(write_handle=_KEEP_WRITE_HANDLE, read_handle=_KEEP_READ_HANDLE)
        try:
            self.assertTrue(fake.wait_read_completion_event.wait(timeout=2.0))
        finally:
            loop.stop()


class TestKeepaliveLoopHealthSnapshot(unittest.TestCase):
    def test_snapshot_reports_running_then_stopped(self) -> None:
        fake = _FakeKeepaliveIo()
        fake.expect_n_wait_reads(1)
        loop = _make_loop(fake.as_io())
        # Before start: not running
        snap_before = loop.health_snapshot()
        self.assertFalse(snap_before.is_running)
        self.assertEqual(snap_before.cycle_count, 0)

        loop.start(write_handle=_KEEP_WRITE_HANDLE, read_handle=_KEEP_READ_HANDLE)
        try:
            self.assertTrue(fake.wait_read_completion_event.wait(timeout=2.0))
            snap_running = loop.health_snapshot()
            # is_running may already be False if stop happened to land
            # between assertion and snapshot, but cycle_count should be ≥ 1
            self.assertGreaterEqual(snap_running.cycle_count, 1)
        finally:
            loop.stop()

        snap_after = loop.health_snapshot()
        self.assertFalse(snap_after.is_running)
        self.assertGreaterEqual(snap_after.cycle_count, 1)
        self.assertGreaterEqual(snap_after.successful_write_pairs, 1)
        self.assertGreaterEqual(snap_after.successful_reads, 1)


class TestKeepaliveLoopErrorHandling(unittest.TestCase):
    def test_write_error_increments_counter_but_loop_continues(self) -> None:
        # First cycle's write_a fails; subsequent cycles succeed.
        # The loop must NOT bail on write errors — it just records them.
        fake = _FakeKeepaliveIo(
            write_results=[
                (False, 6, 0),   # cycle 1 write_a (ERROR_INVALID_HANDLE)
                (True, 0, HID_REPORT_SIZE),  # cycle 1 write_b
                (True, 0, HID_REPORT_SIZE),  # cycle 2 write_a
                (True, 0, HID_REPORT_SIZE),  # cycle 2 write_b
            ]
        )
        fake.expect_n_wait_reads(2)
        loop = _make_loop(fake.as_io())
        loop.start(write_handle=_KEEP_WRITE_HANDLE, read_handle=_KEEP_READ_HANDLE)
        try:
            self.assertTrue(fake.wait_read_completion_event.wait(timeout=2.0))
        finally:
            loop.stop()

        snap = loop.health_snapshot()
        self.assertGreaterEqual(snap.write_errors, 1)
        # Loop reached cycle 2 (the recovered one), so write_pairs >= 1
        self.assertGreaterEqual(snap.cycle_count, 2)
        self.assertGreaterEqual(snap.successful_write_pairs, 1)

    def test_read_error_increments_counter_loop_continues(self) -> None:
        # First wait_read times out; subsequent succeed.
        fake = _FakeKeepaliveIo(
            wait_read_results=[
                (False, 0x102, 0, ""),  # WAIT_TIMEOUT
                # subsequent are default success (handled by fallback)
            ]
        )
        fake.expect_n_wait_reads(2)
        loop = _make_loop(fake.as_io())
        loop.start(write_handle=_KEEP_WRITE_HANDLE, read_handle=_KEEP_READ_HANDLE)
        try:
            self.assertTrue(fake.wait_read_completion_event.wait(timeout=2.0))
        finally:
            loop.stop()

        snap = loop.health_snapshot()
        self.assertGreaterEqual(snap.read_errors, 1)
        # Loop continued to cycle 2 even after the read failure
        self.assertGreaterEqual(snap.cycle_count, 2)


class TestKeepaliveLoopCrashVisibility(unittest.TestCase):
    """Thread crashes must surface via health_snapshot.

    Wrap _run_loop in try/except, capture ``last_error``
    string, surface through ``KeepaliveSnapshot.last_error``. No
    auto-restart — caller (main_zd's monitor) decides what to do.
    """

    def test_loop_exception_captured_in_health_snapshot(self) -> None:
        # A misbehaving DI seam raises mid-loop. The thread should die
        # cleanly with last_error populated (no propagation into main).
        fake = _FakeKeepaliveIo()

        def crashing_wait_read(ctx, timeout_ms):
            raise RuntimeError("synthetic seam crash")

        io = HidKeepaliveIo(
            write=fake.write,
            create_overlapped=fake.create_overlapped,
            post_read=fake.post_read,
            wait_read=crashing_wait_read,
            cancel_read=fake.cancel_read,
            destroy_overlapped=fake.destroy_overlapped,
        )
        loop = _make_loop(io)
        loop.start(write_handle=_KEEP_WRITE_HANDLE, read_handle=_KEEP_READ_HANDLE)
        try:
            # Wait for the thread to die (it will, after the first
            # wait_read invocation)
            self.assertIsNotNone(loop._thread)
            loop._thread.join(timeout=2.0)
            self.assertFalse(loop._thread.is_alive(), "loop thread should have died")
        finally:
            loop.stop()

        snap = loop.health_snapshot()
        self.assertFalse(snap.is_running)
        self.assertIsNotNone(snap.last_error)
        self.assertIn("RuntimeError", snap.last_error)
        self.assertIn("synthetic seam crash", snap.last_error)

    def test_last_error_none_in_normal_operation(self) -> None:
        # Sanity: no exceptions → last_error stays None across normal cycles.
        fake = _FakeKeepaliveIo()
        fake.expect_n_wait_reads(2)
        loop = _make_loop(fake.as_io())
        loop.start(write_handle=_KEEP_WRITE_HANDLE, read_handle=_KEEP_READ_HANDLE)
        try:
            self.assertTrue(fake.wait_read_completion_event.wait(timeout=2.0))
        finally:
            loop.stop()

        snap = loop.health_snapshot()
        self.assertIsNone(snap.last_error)

    def test_last_error_resets_on_restart(self) -> None:
        # After a crashing run, calling start() again should clear
        # last_error so a fresh run reports clean state.
        fake = _FakeKeepaliveIo()
        crash_calls = {"n": 0}

        def crashing_wait_read(ctx, timeout_ms):
            crash_calls["n"] += 1
            raise ValueError("boom")

        io_crash = HidKeepaliveIo(
            write=fake.write,
            create_overlapped=fake.create_overlapped,
            post_read=fake.post_read,
            wait_read=crashing_wait_read,
            cancel_read=fake.cancel_read,
            destroy_overlapped=fake.destroy_overlapped,
        )
        loop = _make_loop(io_crash)
        loop.start(write_handle=_KEEP_WRITE_HANDLE, read_handle=_KEEP_READ_HANDLE)
        try:
            self.assertIsNotNone(loop._thread)
            loop._thread.join(timeout=2.0)
        finally:
            loop.stop()
        self.assertIsNotNone(loop.health_snapshot().last_error)

        # Now restart with a non-crashing IO; last_error should clear.
        fake2 = _FakeKeepaliveIo()
        fake2.expect_n_wait_reads(1)
        loop._io = fake2.as_io()  # swap io for the restart
        loop.start(write_handle=_KEEP_WRITE_HANDLE, read_handle=_KEEP_READ_HANDLE)
        try:
            self.assertTrue(fake2.wait_read_completion_event.wait(timeout=2.0))
        finally:
            loop.stop()
        # Restart cleared last_error before any new exception could populate it
        self.assertIsNone(loop.health_snapshot().last_error)


class TestKeepaliveConstants(unittest.TestCase):
    def test_keepalive_payloads_are_64_bytes_with_correct_prefixes(self) -> None:
        self.assertEqual(len(KEEPALIVE_WRITE_A_PAYLOAD), HID_REPORT_SIZE)
        self.assertEqual(len(KEEPALIVE_WRITE_B_PAYLOAD), HID_REPORT_SIZE)
        # First 5 bytes are the captured magic prefixes
        self.assertEqual(KEEPALIVE_WRITE_A_PAYLOAD[:5].hex(), "1055aa5001")
        self.assertEqual(KEEPALIVE_WRITE_B_PAYLOAD[:5].hex(), "1055aa500f")
        # Remaining 59 bytes are zeros (verified zero-padded sustains 30s)
        self.assertEqual(KEEPALIVE_WRITE_A_PAYLOAD[5:], bytes(HID_REPORT_SIZE - 5))
        self.assertEqual(KEEPALIVE_WRITE_B_PAYLOAD[5:], bytes(HID_REPORT_SIZE - 5))

    def test_default_io_uses_real_helpers(self) -> None:
        # Smoke check: default-constructed HidKeepaliveIo has all 6 callables.
        io = HidKeepaliveIo()
        self.assertTrue(callable(io.write))
        self.assertTrue(callable(io.create_overlapped))
        self.assertTrue(callable(io.post_read))
        self.assertTrue(callable(io.wait_read))
        self.assertTrue(callable(io.cancel_read))
        self.assertTrue(callable(io.destroy_overlapped))


# ===========================================================================
# StandaloneTriggerService end-to-end tests
# ===========================================================================


_SVC_XUSB_PATH = r"\\?\usb#vid_413d&pid_2104&mi_00#7&svc#{guid}"
_SVC_HID_PATH = r"\\?\hid#vid_20bc&pid_5080#7&svc#{guid}"
_SVC_WRITE_HANDLE = 0xAAAA
_SVC_READ_HANDLE = 0xBBBB


def _make_ensure_result(
    status: EnsureDualModeStatus = EnsureDualModeStatus.ESTABLISHED,
    *,
    target_path: str = _SVC_XUSB_PATH,
    error_message: Optional[str] = None,
) -> EnsureDualModeResult:
    return EnsureDualModeResult(
        status=status,
        target_path=target_path,
        attempts=[TriggerResult(outcome=TriggerOutcome.SUCCESS, target_path=target_path)],
        visible_after_ms=391 if status == EnsureDualModeStatus.ESTABLISHED else None,
        error_message=error_message,
    )


def _make_session_result(
    status: HidSessionStatus = HidSessionStatus.OPENED,
    *,
    target_path: str = _SVC_HID_PATH,
    write_handle: Optional[int] = _SVC_WRITE_HANDLE,
    read_handle: Optional[int] = _SVC_READ_HANDLE,
    error_message: Optional[str] = None,
) -> HidSessionResult:
    return HidSessionResult(
        status=status,
        target_path=target_path,
        write_handle=write_handle if status == HidSessionStatus.OPENED else None,
        read_handle=read_handle if status == HidSessionStatus.OPENED else None,
        error_message=error_message,
    )


class _FakeKeepaliveLoop:
    """Stand-in for KeepaliveLoop in service-level tests."""

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.start_calls: list[tuple[int, int]] = []
        self.stop_calls = 0
        self._is_running = False
        self.cycles = 0

    def start(self, write_handle: int, read_handle: int) -> None:
        self.start_calls.append((write_handle, read_handle))
        self._is_running = True

    def stop(self) -> None:
        self.stop_calls += 1
        self._is_running = False

    def health_snapshot(self) -> KeepaliveSnapshot:
        return KeepaliveSnapshot(
            is_running=self._is_running,
            cycle_count=self.cycles,
            successful_write_pairs=self.cycles,
            successful_reads=self.cycles,
            write_errors=0,
            read_errors=0,
            last_cycle_started_ts=None,
        )


class _FakeFactory:
    """Records keepalive_loop_factory invocations + returns FakeKeepaliveLoop."""

    def __init__(self):
        self.calls: list[dict] = []
        self.last_loop: Optional[_FakeKeepaliveLoop] = None

    def __call__(self, **kwargs) -> _FakeKeepaliveLoop:
        self.calls.append(kwargs)
        loop = _FakeKeepaliveLoop(**kwargs)
        self.last_loop = loop
        return loop


def _make_service(
    *,
    ensure_result: Optional[EnsureDualModeResult] = None,
    session_result: Optional[HidSessionResult] = None,
    factory: Optional[_FakeFactory] = None,
    enable_singleton_mutex: bool = False,  # tests don't touch real Win32 mutex
    close_handle_recorder: Optional[list] = None,
    # Default check_hidden_visible to False so the trigger flow runs in
    # tests (existing behavior). The skip-trigger tests override to True.
    check_hidden_visible=lambda: False,
    **service_kwargs,
):
    fake_factory = factory if factory is not None else _FakeFactory()
    er = ensure_result if ensure_result is not None else _make_ensure_result()
    sr = session_result if session_result is not None else _make_session_result()
    closes: list[int] = close_handle_recorder if close_handle_recorder is not None else []

    def fake_close_handle(handle: int) -> bool:
        closes.append(handle)
        return True

    # Track ensure_dual_mode_fn invocations so the skip-trigger tests can assert it was
    # NOT called when the trigger is skipped. We capture the call via a
    # wrapper rather than mutating the closure each test.
    ensure_calls: list[dict] = []

    def fake_ensure(**kwargs):
        ensure_calls.append(kwargs)
        return er

    service = StandaloneTriggerService(
        ensure_dual_mode_fn=fake_ensure,
        open_hid_session_fn=lambda **kwargs: sr,
        keepalive_loop_factory=fake_factory,
        close_handle=fake_close_handle,
        check_hidden_visible=check_hidden_visible,
        enable_singleton_mutex=enable_singleton_mutex,
        **service_kwargs,
    )
    # Stash ensure_calls on the factory for the few tests that inspect it.
    fake_factory.ensure_calls = ensure_calls  # type: ignore[attr-defined]
    return service, fake_factory, closes


class TestStandaloneTriggerServiceStart(unittest.TestCase):
    def test_start_success_drives_full_pipeline(self) -> None:
        service, factory, closes = _make_service()
        result = service.start()

        self.assertEqual(result.status, StandaloneStartStatus.STARTED)
        self.assertEqual(result.target_xusb_path, _SVC_XUSB_PATH)
        self.assertEqual(result.target_hid_path, _SVC_HID_PATH)
        # Keepalive loop was constructed and started with the right handles
        self.assertEqual(len(factory.calls), 1)
        self.assertIsNotNone(factory.last_loop)
        self.assertEqual(
            factory.last_loop.start_calls,
            [(_SVC_WRITE_HANDLE, _SVC_READ_HANDLE)],
        )
        # No handles closed yet (start success — handles remain open for keepalive)
        self.assertEqual(closes, [])

    def test_start_returns_singleton_conflict_when_mutex_held(self) -> None:
        # Force singleton check failure by making acquire_singleton return False
        service, factory, closes = _make_service(enable_singleton_mutex=False)
        # Override acquire_singleton to simulate conflict
        service.acquire_singleton = lambda: False  # type: ignore[method-assign]
        # Re-enable the singleton check path
        service._enable_singleton_mutex = True

        result = service.start()
        self.assertEqual(result.status, StandaloneStartStatus.SINGLETON_CONFLICT)
        self.assertIn("already running", result.error_message or "")
        # No keepalive started, no handles touched
        self.assertEqual(len(factory.calls), 0)
        self.assertEqual(closes, [])

    def test_trigger_no_xusb_path_propagates_status(self) -> None:
        service, factory, closes = _make_service(
            ensure_result=_make_ensure_result(
                EnsureDualModeStatus.NO_XUSB_PATH,
                error_message="No VID_413D MI_00 path",
            )
        )
        result = service.start()
        self.assertEqual(result.status, StandaloneStartStatus.NO_XUSB_PATH)
        self.assertEqual(result.error_message, "No VID_413D MI_00 path")
        # No HID session opened, no keepalive, no handles closed
        self.assertEqual(len(factory.calls), 0)
        self.assertEqual(closes, [])

    def test_trigger_exhausted_retries_propagates(self) -> None:
        service, factory, closes = _make_service(
            ensure_result=_make_ensure_result(
                EnsureDualModeStatus.EXHAUSTED_RETRIES,
                error_message="3 attempts; unplug and replug",
            )
        )
        result = service.start()
        self.assertEqual(result.status, StandaloneStartStatus.EXHAUSTED_RETRIES)
        self.assertEqual(len(factory.calls), 0)

    def test_trigger_open_failed_propagates(self) -> None:
        service, factory, closes = _make_service(
            ensure_result=_make_ensure_result(EnsureDualModeStatus.OPEN_FAILED)
        )
        self.assertEqual(service.start().status, StandaloneStartStatus.OPEN_FAILED)

    def test_trigger_duplicate_failed_propagates(self) -> None:
        service, factory, closes = _make_service(
            ensure_result=_make_ensure_result(EnsureDualModeStatus.DUPLICATE_FAILED)
        )
        self.assertEqual(service.start().status, StandaloneStartStatus.DUPLICATE_FAILED)

    def test_trigger_ioctl_failed_propagates(self) -> None:
        service, factory, closes = _make_service(
            ensure_result=_make_ensure_result(EnsureDualModeStatus.IOCTL_FAILED)
        )
        self.assertEqual(service.start().status, StandaloneStartStatus.IOCTL_FAILED)

    def test_hid_session_open_failed_yields_distinct_status(self) -> None:
        service, factory, closes = _make_service(
            session_result=_make_session_result(
                HidSessionStatus.WRITE_HANDLE_OPEN_FAILED,
                write_handle=None,
                read_handle=None,
                error_message="ACCESS_DENIED on HID write open",
            )
        )
        result = service.start()
        self.assertEqual(result.status, StandaloneStartStatus.HID_SESSION_OPEN_FAILED)
        self.assertEqual(result.error_message, "ACCESS_DENIED on HID write open")
        # Trigger ran (we saw target_xusb_path) but HID failed; no keepalive
        self.assertEqual(result.target_xusb_path, _SVC_XUSB_PATH)
        self.assertEqual(len(factory.calls), 0)
        self.assertEqual(closes, [])


class TestStandaloneTriggerServiceStop(unittest.TestCase):
    def test_stop_after_success_closes_handles_stops_loop(self) -> None:
        service, factory, closes = _make_service()
        result = service.start()
        self.assertEqual(result.status, StandaloneStartStatus.STARTED)

        service.stop()

        # Loop's stop() called exactly once
        self.assertEqual(factory.last_loop.stop_calls, 1)
        # Both handles closed (write + read)
        self.assertEqual(set(closes), {_SVC_WRITE_HANDLE, _SVC_READ_HANDLE})
        # Service state cleared
        self.assertFalse(service._is_started)
        self.assertIsNone(service._keepalive_loop)
        self.assertIsNone(service._write_handle)
        self.assertIsNone(service._read_handle)

    def test_stop_idempotent(self) -> None:
        service, factory, closes = _make_service()
        service.start()
        service.stop()
        service.stop()  # must not raise; must not double-close

        # Loop's stop called exactly once across both stops
        self.assertEqual(factory.last_loop.stop_calls, 1)
        # Each handle closed exactly once
        self.assertEqual(closes.count(_SVC_WRITE_HANDLE), 1)
        self.assertEqual(closes.count(_SVC_READ_HANDLE), 1)

    def test_stop_without_start_safe(self) -> None:
        service, factory, closes = _make_service()
        service.stop()  # must not raise
        # No handle closes, no loop calls
        self.assertEqual(closes, [])
        self.assertEqual(len(factory.calls), 0)

    def test_failed_start_releases_singleton_and_leaves_no_state(self) -> None:
        # Singleton was acquired in start(); after a failed trigger, it
        # must be released so a subsequent start can proceed (or another
        # process can take over).
        service, factory, closes = _make_service(
            ensure_result=_make_ensure_result(
                EnsureDualModeStatus.EXHAUSTED_RETRIES,
                error_message="exhausted",
            ),
            enable_singleton_mutex=False,  # don't touch real mutex
        )
        # Tracking acquire/release manually since singleton is disabled
        acquired = []
        released = []
        service.acquire_singleton = lambda: acquired.append(True) or True  # type: ignore[method-assign]
        service.release_singleton = lambda: released.append(True)  # type: ignore[method-assign]
        # Re-enable singleton path so start() actually calls acquire/release
        service._enable_singleton_mutex = True

        result = service.start()
        self.assertEqual(result.status, StandaloneStartStatus.EXHAUSTED_RETRIES)
        self.assertEqual(len(acquired), 1, "acquire should have been attempted")
        self.assertEqual(len(released), 1, "release must run on failed start")


class TestStandaloneTriggerServiceHealthSnapshot(unittest.TestCase):
    def test_snapshot_pre_start_is_inactive(self) -> None:
        service, factory, closes = _make_service()
        snap = service.health_snapshot()
        self.assertFalse(snap.is_started)
        self.assertFalse(snap.is_keepalive_running)
        self.assertIsNone(snap.keepalive)
        self.assertIsNone(snap.started_xusb_path)
        self.assertIsNone(snap.started_hid_path)

    def test_snapshot_after_start_reflects_running_state(self) -> None:
        service, factory, closes = _make_service()
        service.start()

        snap = service.health_snapshot()
        self.assertTrue(snap.is_started)
        self.assertTrue(snap.is_keepalive_running)
        self.assertIsNotNone(snap.keepalive)
        self.assertEqual(snap.started_xusb_path, _SVC_XUSB_PATH)
        self.assertEqual(snap.started_hid_path, _SVC_HID_PATH)

    def test_snapshot_after_stop_reflects_inactive(self) -> None:
        service, factory, closes = _make_service()
        service.start()
        service.stop()

        snap = service.health_snapshot()
        self.assertFalse(snap.is_started)
        self.assertFalse(snap.is_keepalive_running)
        self.assertIsNone(snap.keepalive)
        self.assertIsNone(snap.started_xusb_path)
        self.assertIsNone(snap.started_hid_path)


class TestStandaloneTriggerServiceConfigForwarding(unittest.TestCase):
    def test_keepalive_factory_receives_configured_intervals(self) -> None:
        service, factory, closes = _make_service(
            keepalive_cadence_s=1.5,
            keepalive_inter_write_delay_s=0.005,
            keepalive_read_timeout_ms=250,
            keepalive_initial_cycle_delay_s=0.1,
        )
        service.start()

        self.assertEqual(len(factory.calls), 1)
        kw = factory.calls[0]
        self.assertEqual(kw["cadence_s"], 1.5)
        self.assertEqual(kw["inter_write_delay_s"], 0.005)
        self.assertEqual(kw["read_timeout_ms"], 250)
        self.assertEqual(kw["initial_cycle_delay_s"], 0.1)
        self.assertIsInstance(kw["io"], HidKeepaliveIo)

    def test_keepalive_io_can_be_overridden(self) -> None:
        # Tests should be able to inject a custom HidKeepaliveIo
        # (e.g., to use mocked write/read for an integration-style test).
        custom_io = HidKeepaliveIo()
        service, factory, closes = _make_service(keepalive_io=custom_io)
        service.start()
        self.assertIs(factory.calls[0]["io"], custom_io)


class TestStandaloneTriggerServiceTriggerSkip(unittest.TestCase):
    """Pre-trigger visibility check + trigger-skip path.

    If VID_20BC is already enumerated when start() is
    called (e.g., a prior wrapper crashed and the OS held VID_20BC
    enumerated), skip the trigger sequence and go straight to opening
    the HID session. This is the crash-recovery path — without it, a
    wrapper restart after a crash would fail NO_XUSB_PATH because
    VID_413D MI_00 isn't enumerated while in dual-mode.
    """

    def test_skip_trigger_when_vid_20bc_already_visible(self) -> None:
        # check_hidden_visible returns True at start. ensure_dual_mode
        # MUST NOT be called; HID session opens directly; keepalive starts.
        service, factory, closes = _make_service(check_hidden_visible=lambda: True)
        result = service.start()

        self.assertEqual(result.status, StandaloneStartStatus.STARTED)
        self.assertEqual(result.target_hid_path, _SVC_HID_PATH)
        self.assertIsNone(
            result.target_xusb_path,
            "trigger skipped → no XUSB path was enumerated",
        )
        self.assertEqual(
            result.trigger_attempts, [],
            "trigger skipped → no attempts recorded",
        )
        # ensure_dual_mode_fn must NOT have been called
        self.assertEqual(
            factory.ensure_calls, [],  # type: ignore[attr-defined]
            "ensure_dual_mode must not run when VID_20BC already visible",
        )
        # HID session opened + keepalive started
        self.assertEqual(len(factory.calls), 1)
        self.assertEqual(
            factory.last_loop.start_calls,
            [(_SVC_WRITE_HANDLE, _SVC_READ_HANDLE)],
        )

    def test_runs_trigger_when_vid_20bc_not_visible(self) -> None:
        # check_hidden_visible returns False at start (default). The
        # existing flow runs — ensure_dual_mode is invoked exactly once.
        service, factory, closes = _make_service(check_hidden_visible=lambda: False)
        result = service.start()

        self.assertEqual(result.status, StandaloneStartStatus.STARTED)
        self.assertEqual(len(factory.ensure_calls), 1)  # type: ignore[attr-defined]
        # Trigger attempts list reflects ensure_dual_mode's contribution
        self.assertEqual(len(result.trigger_attempts), 1)
        self.assertEqual(result.target_xusb_path, _SVC_XUSB_PATH)

    def test_skip_then_hid_session_failure_propagates(self) -> None:
        # Pre-trigger says skip. open_hid_session fails. Result must
        # surface as HID_SESSION_OPEN_FAILED with target_xusb_path=None
        # (no trigger ran) and target_hid_path from the failed session.
        service, factory, closes = _make_service(
            check_hidden_visible=lambda: True,
            session_result=_make_session_result(
                HidSessionStatus.WRITE_HANDLE_OPEN_FAILED,
                write_handle=None,
                read_handle=None,
                error_message="HID write open failed",
            ),
        )
        result = service.start()

        self.assertEqual(result.status, StandaloneStartStatus.HID_SESSION_OPEN_FAILED)
        self.assertEqual(result.error_message, "HID write open failed")
        self.assertIsNone(result.target_xusb_path)
        self.assertEqual(result.trigger_attempts, [])
        self.assertEqual(result.target_hid_path, _SVC_HID_PATH)
        # Singleton released on failed start
        # (no direct way to assert without the singleton path enabled;
        # the test_failed_start_releases_singleton test already covers
        # the singleton-release contract for failed starts)
        # No keepalive loop was constructed
        self.assertEqual(len(factory.calls), 0)

    def test_skip_branch_returns_started_with_zero_trigger_attempts(self) -> None:
        # Regression: the STARTED result's trigger_attempts list MUST be
        # empty when trigger was skipped. Callers can use len(attempts) == 0
        # to detect "skipped path" in telemetry.
        service, factory, closes = _make_service(check_hidden_visible=lambda: True)
        result = service.start()
        self.assertEqual(result.status, StandaloneStartStatus.STARTED)
        self.assertEqual(len(result.trigger_attempts), 0)

    def test_check_hidden_visible_called_exactly_once_per_start(self) -> None:
        # The pre-trigger check fires once. It does NOT poll repeatedly.
        call_count = {"n": 0}

        def counting_check():
            call_count["n"] += 1
            return False

        service, factory, closes = _make_service(check_hidden_visible=counting_check)
        service.start()
        self.assertEqual(call_count["n"], 1)


class TestStandaloneTriggerServiceSingletonName(unittest.TestCase):
    def test_singleton_mutex_name_is_stable(self) -> None:
        # The singleton mutex name is a stable literal so a single wrapper
        # instance (regardless of v0/v1 selection in main_zd's config) runs
        # at a time.
        self.assertEqual(
            StandaloneTriggerService.SINGLETON_MUTEX_NAME,
            SINGLETON_MUTEX_NAME,
        )
        self.assertEqual(SINGLETON_MUTEX_NAME, r"Local\ZDUltimateLegendWrapper")


class _OrderRecordingIo:
    """Keepalive IO seam that records the order of cancel/destroy and leaves
    reads UNcompleted (mirrors the real WAIT_TIMEOUT path, which never sets
    ``ctx.completed``)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, int]] = []
        self._seq = 0

    def write(self, handle, payload):
        return True, 0, HID_REPORT_SIZE

    def create_overlapped(self) -> OverlappedReadContext:
        self._seq += 1
        ctx = OverlappedReadContext()
        ctx.sequence_num = self._seq
        return ctx

    def post_read(self, handle, ctx):
        self.events.append(("post", ctx.sequence_num))
        return True, 0

    def wait_read(self, ctx, timeout_ms):
        # WAIT_TIMEOUT: read does NOT complete (ctx.completed stays False).
        self.events.append(("wait", ctx.sequence_num))
        return False, 0x102, 0, ""

    def cancel_read(self, handle, ctx):
        self.events.append(("cancel", ctx.sequence_num))
        return True, 0

    def destroy_overlapped(self, ctx):
        self.events.append(("destroy", ctx.sequence_num))

    def as_io(self) -> HidKeepaliveIo:
        return HidKeepaliveIo(
            write=self.write,
            create_overlapped=self.create_overlapped,
            post_read=self.post_read,
            wait_read=self.wait_read,
            cancel_read=self.cancel_read,
            destroy_overlapped=self.destroy_overlapped,
        )


class TestKeepaliveRepostUseAfterFree(unittest.TestCase):
    """A8: on the per-cycle repost, a read that did NOT complete must be
    cancelled + drained BEFORE its OVERLAPPED context is freed/overwritten —
    otherwise the kernel may still complete into the freed struct/buffer
    (use-after-free)."""

    def test_uncompleted_read_cancelled_before_freed_on_repost(self) -> None:
        io = _OrderRecordingIo()
        loop = KeepaliveLoop(io=io.as_io(), sleep=lambda s: None, read_timeout_ms=10)
        # Stand in for what start() does, without launching the loop thread.
        ctx = io.create_overlapped()  # sequence 1 -> the pending read
        loop._write_handle = _KEEP_WRITE_HANDLE
        loop._read_handle = _KEEP_READ_HANDLE
        loop._pending_read = ctx
        loop._next_read_seq = 2

        loop._run_one_cycle()

        self.assertIn(
            ("cancel", 1), io.events,
            "uncompleted pending read was never cancelled before free",
        )
        self.assertIn(("destroy", 1), io.events)
        self.assertLess(
            io.events.index(("cancel", 1)),
            io.events.index(("destroy", 1)),
            "cancel_read must precede destroy_overlapped (free) for an "
            "uncompleted read",
        )


class TestStandaloneSingletonConflictRelease(unittest.TestCase):
    """A9: a SINGLETON_CONFLICT start() must release the mutex handle that
    CreateMutexW returns even on conflict — otherwise a kernel handle leaks
    per conflicting start() (no stop() will ever run to release it)."""

    def test_conflict_releases_mutex_handle(self) -> None:
        released: list[Optional[int]] = []
        with mock.patch(
            "zd_app.services.standalone_trigger_service._try_acquire_singleton_mutex",
            return_value=(0xABCD, False),
        ), mock.patch(
            "zd_app.services.standalone_trigger_service._release_singleton_mutex",
            side_effect=lambda h: released.append(h),
        ):
            service = StandaloneTriggerService(enable_singleton_mutex=True)
            result = service.start()

        self.assertEqual(result.status, StandaloneStartStatus.SINGLETON_CONFLICT)
        self.assertEqual(released, [0xABCD])
        self.assertIsNone(service._mutex_handle)


if __name__ == "__main__":
    unittest.main()
