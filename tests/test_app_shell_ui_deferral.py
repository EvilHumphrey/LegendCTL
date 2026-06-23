"""Deferred-UI / modal-swap seam tests.

The seam is :meth:`AppShell._defer_ui_call` (paced queue, one drain pass
per rendered frame) plus :meth:`AppShell._defer_modal_swap` (teardown
pass → rendered frame → create pass). It encodes DPG's empirically
benched modal law — thread-INDEPENDENT: a modal created in the same pass
that another modal was showing (deleted, hidden, or left up) exists but
never becomes visible, and one rendered frame between teardown and
create cures it. Matrix: tools/diag_dpg_modal_thread_visibility.py.

Coverage map (this file pins the seam itself; the Safe Import hops that
ride it are pinned in test_safe_import.py::ModalThreadDeferralTests):

- D1 — unarmed (no live render loop: every sync/headless path, the whole
  existing suite) executes inline on the caller's thread, exceptions
  propagating exactly like the direct call it replaces.
- D2 — armed: calls queue (nothing runs at enqueue time) and the drain
  executes them in FIFO order.
- D3 — drained-on-render-thread semantics: a call enqueued from a worker
  thread runs on whichever thread drains (the render thread in
  production), asserted via a thread-identity sentinel.
- D4 — exception containment: a throwing deferred call is logged, does
  not propagate out of the drain / ``_tick``, and does not starve the
  calls queued behind it.
- D5 — ``_tick`` is the drain point.
- D6 — the drain is bounded per pass: a call enqueued DURING a drain runs
  on the NEXT pass. This is the load-bearing frame gap between a modal
  swap's teardown half and create half.
- D7 — ``_defer_modal_swap``: inline when unarmed; armed it tears down
  (delete and hide) one pass and opens the next; ``key`` coalesces
  re-entrant requests and is released after the open (or a teardown
  failure) so the hop stays reachable.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from tests.r2_shell_test_helpers import make_shell


def _make_shell():
    shell = make_shell(settings_service=MagicMock())
    # Same determinism trims as test_app_shell_worker_thread: no real
    # screen rebuilds, no first-connect RP capture against the mock.
    shell.restore_point_service = None
    shell.refresh_shell = lambda: None
    shell.rebuild_current_screen = lambda: None
    return shell


def _capture_widget_state(callback):
    """Run ``callback`` under patched DPG (shape shared with the sibling
    app-shell test files): _tick's widget writes become recorded no-ops."""

    values = {}
    config = {}

    def set_value(tag, value):
        values[tag] = value

    def configure_item(tag, **kwargs):
        config.setdefault(tag, {}).update(kwargs)

    with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True), patch(
        "zd_app.ui.app_shell.dpg.set_value",
        side_effect=set_value,
    ), patch(
        "zd_app.ui.app_shell.dpg.configure_item",
        side_effect=configure_item,
    ):
        callback()
    return values, config


class _SwapRecorder:
    """Patch app_shell.dpg so a modal swap's teardown is observable.

    Records ("delete", tag) / ("hide", tag) / custom open events into one
    ordered list, so tests can assert the teardown→open sequencing.
    """

    def __init__(self):
        self.events: list = []
        self._patchers = [
            patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True),
            patch(
                "zd_app.ui.app_shell.dpg.delete_item",
                side_effect=lambda tag, **kw: self.events.append(("delete", tag)),
            ),
            patch(
                "zd_app.ui.app_shell.dpg.configure_item",
                side_effect=lambda tag, **kw: self.events.append(
                    ("hide", tag) if kw.get("show") is False else ("configure", tag)
                ),
            ),
        ]

    def __enter__(self):
        for p in self._patchers:
            p.start()
        return self

    def __exit__(self, *exc_info):
        for p in self._patchers:
            p.stop()
        return False


class UnarmedInlineTests(unittest.TestCase):
    """D1 — the unarmed seam is a transparent inline call."""

    def test_unarmed_by_default_and_runs_inline_on_caller_thread(self) -> None:
        shell = _make_shell()
        self.assertFalse(shell._defer_ui_armed)

        ran: list[threading.Thread] = []
        shell._defer_ui_call(lambda: ran.append(threading.current_thread()))

        self.assertEqual(ran, [threading.current_thread()])
        self.assertTrue(shell._deferred_ui_calls.empty())

    def test_unarmed_inline_call_propagates_exceptions(self) -> None:
        # Inline mode must behave exactly like the direct call it replaces:
        # the existing suite (and any sync path) sees the raise, not a log.
        shell = _make_shell()

        def boom() -> None:
            raise RuntimeError("inline failure")

        with self.assertRaises(RuntimeError):
            shell._defer_ui_call(boom)


class ArmedQueueTests(unittest.TestCase):
    """D2/D3/D6 — armed pacing: FIFO, drain-thread execution, pass bounds."""

    def test_armed_calls_do_not_run_at_enqueue_and_drain_fifo(self) -> None:
        shell = _make_shell()
        shell._defer_ui_armed = True

        ran: list[int] = []
        for i in (1, 2, 3):
            shell._defer_ui_call(lambda i=i: ran.append(i))

        self.assertEqual(ran, [])  # nothing runs on the enqueueing pass
        shell._drain_deferred_ui_calls()
        self.assertEqual(ran, [1, 2, 3])  # FIFO
        self.assertTrue(shell._deferred_ui_calls.empty())

        # Draining again is a no-op: each call ran exactly once.
        shell._drain_deferred_ui_calls()
        self.assertEqual(ran, [1, 2, 3])

    def test_worker_enqueue_runs_on_draining_thread(self) -> None:
        shell = _make_shell()
        shell._defer_ui_armed = True

        seen: list[threading.Thread] = []
        worker = threading.Thread(
            target=lambda: shell._defer_ui_call(
                lambda: seen.append(threading.current_thread())
            ),
            name="fake-dpg-callback",
        )
        worker.start()
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(seen, [])  # queued, not run on the callback thread

        shell._drain_deferred_ui_calls()  # this thread plays render thread
        self.assertEqual(seen, [threading.current_thread()])

    def test_call_enqueued_during_drain_runs_next_pass(self) -> None:
        # D6 — the frame gap. A modal swap's teardown half re-enqueues its
        # create half; the bounded drain must hold it for the next pass
        # (in production: the next frame), never run it in the same pass.
        shell = _make_shell()
        shell._defer_ui_armed = True

        ran: list[str] = []

        def first() -> None:
            ran.append("first")
            shell._defer_ui_call(lambda: ran.append("second"))

        shell._defer_ui_call(first)

        shell._drain_deferred_ui_calls()
        self.assertEqual(ran, ["first"])  # second held for the next pass

        shell._drain_deferred_ui_calls()
        self.assertEqual(ran, ["first", "second"])
        self.assertTrue(shell._deferred_ui_calls.empty())


class DrainContainmentTests(unittest.TestCase):
    """D4/D5 — _tick drains; a throwing deferred call cannot kill it."""

    def _lean_tick_shell(self):
        shell = _make_shell()
        shell._defer_ui_armed = True
        # Keep the tick to the drains: skip the presence poll and the
        # shell-refresh branch (same trim as TickDrainTests).
        shell._last_presence_poll = time.time()
        shell._last_tick = time.time()
        return shell

    def test_throwing_deferred_call_logged_and_rest_of_queue_drains(self) -> None:
        shell = _make_shell()
        shell._defer_ui_armed = True

        ran: list[str] = []

        def boom() -> None:
            raise RuntimeError("deferred failure")

        shell._defer_ui_call(boom)
        shell._defer_ui_call(lambda: ran.append("after"))

        with self.assertLogs("zd_app.ui.app_shell", level="ERROR") as logs:
            shell._drain_deferred_ui_calls()  # must not raise

        self.assertEqual(ran, ["after"])  # the thrower didn't starve the queue
        self.assertTrue(
            any("Deferred UI call failed" in line for line in logs.output)
        )
        self.assertTrue(shell._deferred_ui_calls.empty())

    def test_tick_drains_deferred_ui_calls_and_survives_thrower(self) -> None:
        shell = self._lean_tick_shell()

        ran: list[str] = []

        def boom() -> None:
            raise RuntimeError("deferred failure in tick")

        shell._defer_ui_call(boom)
        shell._defer_ui_call(lambda: ran.append("ran"))

        with self.assertLogs("zd_app.ui.app_shell", level="ERROR"):
            _capture_widget_state(shell._tick)  # must not raise

        self.assertEqual(ran, ["ran"])
        self.assertTrue(shell._deferred_ui_calls.empty())


class ModalSwapTests(unittest.TestCase):
    """D7 — _defer_modal_swap sequencing, hide support, key coalescing."""

    def test_unarmed_swap_runs_teardown_then_open_inline(self) -> None:
        shell = _make_shell()

        with _SwapRecorder() as rec:
            shell._defer_modal_swap(
                lambda: rec.events.append("open"),
                delete_tags=("modal_x",),
                hide_tags=("modal_y",),
                key="k",
            )

        self.assertEqual(
            rec.events, [("delete", "modal_x"), ("hide", "modal_y"), "open"]
        )
        self.assertTrue(shell._deferred_ui_calls.empty())
        self.assertNotIn("k", shell._pending_modal_swap_keys)  # released

    def test_armed_swap_tears_down_one_pass_and_opens_the_next(self) -> None:
        shell = _make_shell()
        shell._defer_ui_armed = True

        with _SwapRecorder() as rec:
            shell._defer_modal_swap(
                lambda: rec.events.append("open"),
                delete_tags=("modal_x",),
                hide_tags=("modal_y",),
                key="k",
            )
            self.assertEqual(rec.events, [])  # nothing on the enqueue pass

            shell._drain_deferred_ui_calls()  # pass 1: teardown only
            self.assertEqual(
                rec.events, [("delete", "modal_x"), ("hide", "modal_y")]
            )
            self.assertIn("k", shell._pending_modal_swap_keys)  # still pending

            shell._drain_deferred_ui_calls()  # pass 2: create, after a frame
            self.assertEqual(
                rec.events,
                [("delete", "modal_x"), ("hide", "modal_y"), "open"],
            )

        self.assertNotIn("k", shell._pending_modal_swap_keys)
        self.assertTrue(shell._deferred_ui_calls.empty())

    def test_key_coalesces_reentrant_requests_until_open_ran(self) -> None:
        # A double-fired callback must not queue a second delete+create of
        # the same modal (the second create would share a pass with the
        # first modal's teardown — the poison).
        shell = _make_shell()
        shell._defer_ui_armed = True

        opened: list[str] = []
        with _SwapRecorder():
            shell._defer_modal_swap(
                lambda: opened.append("first"), delete_tags=("m",), key="k"
            )
            shell._defer_modal_swap(
                lambda: opened.append("dropped"), delete_tags=("m",), key="k"
            )
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

            self.assertEqual(opened, ["first"])  # re-entrant request dropped

            # Key released after the open: the hop is usable again.
            shell._defer_modal_swap(
                lambda: opened.append("second"), delete_tags=("m",), key="k"
            )
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

        self.assertEqual(opened, ["first", "second"])

    def test_keys_are_independent_and_unkeyed_swaps_never_coalesce(self) -> None:
        shell = _make_shell()
        shell._defer_ui_armed = True

        opened: list[str] = []
        with _SwapRecorder():
            shell._defer_modal_swap(lambda: opened.append("a"), key="ka")
            shell._defer_modal_swap(lambda: opened.append("b"), key="kb")
            shell._defer_modal_swap(lambda: opened.append("u1"))
            shell._defer_modal_swap(lambda: opened.append("u2"))
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

        self.assertEqual(sorted(opened), ["a", "b", "u1", "u2"])

    def test_open_failure_is_contained_and_releases_key(self) -> None:
        shell = _make_shell()
        shell._defer_ui_armed = True

        def boom() -> None:
            raise RuntimeError("open failed")

        with _SwapRecorder():
            shell._defer_modal_swap(boom, delete_tags=("m",), key="k")
            shell._drain_deferred_ui_calls()  # teardown pass
            with self.assertLogs("zd_app.ui.app_shell", level="ERROR"):
                shell._drain_deferred_ui_calls()  # open pass raises, contained

        self.assertNotIn("k", shell._pending_modal_swap_keys)

    def test_teardown_failure_is_contained_and_releases_key(self) -> None:
        # A teardown that raises must not wedge the coalescing key, or the
        # hop would refuse every later request until app restart.
        shell = _make_shell()
        shell._defer_ui_armed = True

        opened: list[str] = []
        with patch(
            "zd_app.ui.app_shell.dpg.does_item_exist", return_value=True
        ), patch(
            "zd_app.ui.app_shell.dpg.delete_item",
            side_effect=RuntimeError("delete exploded"),
        ):
            shell._defer_modal_swap(
                lambda: opened.append("never"), delete_tags=("m",), key="k"
            )
            with self.assertLogs("zd_app.ui.app_shell", level="ERROR"):
                shell._drain_deferred_ui_calls()  # teardown raises, contained

        self.assertEqual(opened, [])  # create half was never queued
        self.assertNotIn("k", shell._pending_modal_swap_keys)
        self.assertTrue(shell._deferred_ui_calls.empty())


if __name__ == "__main__":
    unittest.main()
