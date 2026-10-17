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
  re-entrant requests and is released when the create pass begins (or on
  a teardown failure) so the hop stays reachable.
- D8 — modal-swap ARBITRATION. Pacing plus per-key coalescing is not
  enough: two different sites passing two different keys used to have
  their teardowns drain together and then create two modals in one pass,
  which is the very shape the law forbids. One transition therefore owns
  the create slot from request until its create pass begins; a same-key
  duplicate is dropped, a different-key (or unkeyed) request supersedes,
  and the superseded transition's queued closures abort on a stale
  generation token. ``owner_screen`` additionally abandons the create if
  the user navigated away while the swap was pending.
- D9 — supersession is bounded by the TEARDOWN, not by the create.
  Superseding after the owner's teardown ran abandons the only closure
  that can undo it, which strands the flow (Save + Apply hides the Safe
  Import preview; only its own create puts a confirm in front of it). So
  a different-key request arriving after the teardown pass is dropped
  instead of superseding, and the owner's create is allowed to land. The
  cost is one rendered frame in which last-request-wins does not hold.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n


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

    def test_cross_key_requests_supersede_instead_of_running_together(self) -> None:
        # CONTRACT CHANGE (2026-07-27). This test previously asserted the
        # opposite -- that independent keys all run and unkeyed swaps never
        # coalesce. That contract is what let Notes and Export (or a click
        # and an async job completion) drain their teardowns together and
        # then create two modals in ONE pass: the exact shape the seam
        # exists to prevent. Only one transition may own the create slot, so
        # a different key supersedes the pending one, last request wins, and
        # unkeyed requests are ordinary transitions with no identity of
        # their own -- they supersede and are superseded like any other.
        # All four requests here land BEFORE the first drain, which is the
        # only window supersession is still allowed in (see D9 below).
        shell = _make_shell()
        shell._defer_ui_armed = True

        opened: list[str] = []
        with _SwapRecorder():
            shell._defer_modal_swap(lambda: opened.append("a"), key="ka")
            shell._defer_modal_swap(lambda: opened.append("b"), key="kb")
            shell._defer_modal_swap(lambda: opened.append("u1"))
            shell._defer_modal_swap(lambda: opened.append("u2"))

            # Only the last request owns the slot; the three superseded
            # teardown closures are still queued but hold a stale token.
            self.assertTrue(shell._modal_swap_pending)

            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

        self.assertEqual(opened, ["u2"])  # exactly one create, the LAST request
        self.assertEqual(shell._pending_modal_swap_keys, set())
        self.assertTrue(shell._deferred_ui_calls.empty())

    def test_superseded_keys_are_released_and_reusable(self) -> None:
        # A superseded request must not leave its key held, or that hop would
        # refuse every later request for the rest of the session.
        shell = _make_shell()
        shell._defer_ui_armed = True

        opened: list[str] = []
        with _SwapRecorder():
            shell._defer_modal_swap(lambda: opened.append("a"), key="ka")
            shell._defer_modal_swap(lambda: opened.append("b"), key="kb")
            self.assertNotIn("ka", shell._pending_modal_swap_keys)
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()
            self.assertEqual(opened, ["b"])

            shell._defer_modal_swap(lambda: opened.append("a again"), key="ka")
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

        self.assertEqual(opened, ["b", "a again"])

    def test_no_drain_pass_ever_runs_two_modal_creates(self) -> None:
        # The invariant in its most direct form, over the two sequences the
        # review named: two keys requested before the first drain, and a
        # second key arriving between a teardown pass and its create pass.
        # The two sequences resolve DIFFERENTLY, which is D9: before the
        # teardown the newcomer wins, after it the newcomer is dropped so the
        # owner's create can undo its own teardown.
        shell = _make_shell()
        shell._defer_ui_armed = True

        per_pass: list[list[str]] = []
        current: list[str] = []

        def _drain() -> None:
            nonlocal current
            current = []
            per_pass.append(current)
            shell._drain_deferred_ui_calls()

        with _SwapRecorder():
            # Sequence 1 — both before drain 1.
            shell._defer_modal_swap(lambda: current.append("notes"), key="notes")
            shell._defer_modal_swap(lambda: current.append("export"), key="export")
            _drain()
            _drain()

            # Sequence 2 — the second request lands after the first's
            # teardown ran but before its create. The newcomer is dropped
            # here, so the guide (whose teardown already ran) still opens.
            shell._defer_modal_swap(lambda: current.append("guide"), key="guide")
            _drain()  # guide teardown
            shell._defer_modal_swap(lambda: current.append("save_as"), key="save_as")
            _drain()
            _drain()

        self.assertTrue(
            all(len(creates) <= 1 for creates in per_pass),
            f"a drain pass created more than one modal: {per_pass}",
        )
        flattened = [name for creates in per_pass for name in creates]
        self.assertEqual(flattened, ["export", "guide"])

    def test_a_new_transition_is_accepted_once_the_create_pass_began(self) -> None:
        # The slot is released when the create STARTS, so the deliberate
        # modal-to-modal hop (a button inside modal A opening modal B) still
        # works and nothing is wedged behind a finished transition.
        shell = _make_shell()
        shell._defer_ui_armed = True

        opened: list[str] = []
        with _SwapRecorder():
            shell._defer_modal_swap(lambda: opened.append("a"), key="ka")
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()
            self.assertEqual(opened, ["a"])

            shell._defer_modal_swap(lambda: opened.append("b"), key="kb")
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

        self.assertEqual(opened, ["a", "b"])
        self.assertFalse(shell._modal_swap_pending)

    def test_a_request_after_the_teardown_pass_is_dropped_not_superseded(
        self,
    ) -> None:
        # D9. Superseding here would abandon the only closure that can undo
        # the teardown that already ran. Concretely: Save + Apply HIDES the
        # Safe Import preview in its teardown pass and its create is what
        # puts the confirm modal in front of it -- supersede that and the
        # preview is left hidden with no confirm and no way back. So the
        # newcomer is dropped and the owner's create lands.
        shell = _make_shell()
        shell._defer_ui_armed = True

        opened: list[str] = []
        with _SwapRecorder() as rec:
            shell._defer_modal_swap(
                lambda: opened.append("confirm"),
                hide_tags=("preview",),
                key="confirm",
            )
            shell._drain_deferred_ui_calls()  # teardown pass: preview hidden
            self.assertEqual(rec.events, [("hide", "preview")])

            shell._defer_modal_swap(lambda: opened.append("save_as"), key="save_as")
            # Dropped means not even scheduled: only the owner's create is
            # queued behind the teardown.
            self.assertEqual(shell._deferred_ui_calls.qsize(), 1)

            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

        self.assertEqual(
            opened,
            ["confirm"],
            "the transition whose teardown already ran must be the one that "
            "opens; superseding it strands whatever the teardown hid",
        )
        self.assertFalse(shell._modal_swap_pending)
        self.assertFalse(shell._modal_swap_teardown_done)

    def test_a_dropped_post_teardown_request_is_logged(self) -> None:
        # Dropping a user action silently is not acceptable; the log line is
        # how a runtime-log read explains a click that did nothing.
        shell = _make_shell()
        shell._defer_ui_armed = True

        with _SwapRecorder():
            shell._defer_modal_swap(lambda: None, hide_tags=("preview",), key="confirm")
            shell._drain_deferred_ui_calls()  # teardown pass
            with self.assertLogs("zd_app.ui.app_shell", level="INFO") as logs:
                shell._defer_modal_swap(lambda: None, key="save_as")

        self.assertTrue(
            any("save_as" in line for line in logs.output),
            f"the dropped request must name itself in the log: {logs.output}",
        )

    def test_a_dropped_user_request_says_so_in_the_footer(self) -> None:
        # The log line alone left the user with nothing on screen when the
        # winning transition's own create bailed. A user-click request now
        # gets a neutral footer line (success=None: not an error).
        shell = _make_shell()
        shell._defer_ui_armed = True
        shell._update_apply_status = MagicMock()

        with _SwapRecorder():
            shell._defer_modal_swap(lambda: None, hide_tags=("preview",), key="confirm")
            shell._drain_deferred_ui_calls()  # teardown pass
            shell._defer_modal_swap(lambda: None, key="save_as", notify_if_dropped=True)

        shell._update_apply_status.assert_called_once_with(
            i18n.t("shell.modal_request_dropped"), None
        )

    def test_a_dropped_async_request_leaves_the_jobs_result_line_alone(self) -> None:
        # A job's on_done writes its own result ("applied 3 of 5, 2 failed")
        # and THEN requests its modal, in the same tick. If a user click holds
        # the slot past its teardown, that request is dropped -- and a "try
        # again" over the failure report would invite a repeat write. So an
        # async request (the default) drops with the log line only.
        shell = _make_shell()
        shell._defer_ui_armed = True
        shell._update_apply_status = MagicMock()

        opened: list[str] = []
        with _SwapRecorder():
            shell._defer_modal_swap(
                lambda: opened.append("save_as"), key="wrapper_profile_save_as"
            )
            shell._drain_deferred_ui_calls()  # the click's teardown ran
            shell._defer_modal_swap(
                lambda: opened.append("details"), key="profile_apply_details"
            )
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

        self.assertEqual(opened, ["save_as"])  # the async request WAS dropped
        shell._update_apply_status.assert_not_called()

    def test_coalesced_and_superseding_requests_say_nothing(self) -> None:
        # Anti-noise for the test above: only the post-teardown drop loses the
        # request. A same-key duplicate is already pending and will open; a
        # pre-teardown newcomer supersedes and opens itself.
        shell = _make_shell()
        shell._defer_ui_armed = True
        shell._update_apply_status = MagicMock()

        with _SwapRecorder():
            shell._defer_modal_swap(lambda: None, key="confirm")
            shell._defer_modal_swap(
                lambda: None, key="confirm", notify_if_dropped=True
            )  # coalesced
            shell._defer_modal_swap(
                lambda: None, key="save_as", notify_if_dropped=True
            )  # supersedes
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

        shell._update_apply_status.assert_not_called()

    def test_a_request_before_the_teardown_pass_still_supersedes(self) -> None:
        # Anti-vacuity for the test above: the drop rule must be scoped to
        # the post-teardown window, not applied to every pending transition.
        # Nothing is stranded here because the teardown never ran -- the
        # preview was never hidden.
        shell = _make_shell()
        shell._defer_ui_armed = True

        opened: list[str] = []
        with _SwapRecorder() as rec:
            shell._defer_modal_swap(
                lambda: opened.append("confirm"),
                hide_tags=("preview",),
                key="confirm",
            )
            shell._defer_modal_swap(lambda: opened.append("save_as"), key="save_as")
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

        self.assertEqual(opened, ["save_as"])
        self.assertNotIn(("hide", "preview"), rec.events)

    def test_a_dropped_request_can_simply_be_made_again(self) -> None:
        # Anti-wedge: the drop must cost one request, not the hop. Once the
        # owner's create has run the slot is free and the same key gets in.
        shell = _make_shell()
        shell._defer_ui_armed = True

        opened: list[str] = []
        with _SwapRecorder():
            shell._defer_modal_swap(lambda: opened.append("confirm"), key="confirm")
            shell._drain_deferred_ui_calls()  # teardown pass
            shell._defer_modal_swap(lambda: opened.append("dropped"), key="save_as")
            shell._drain_deferred_ui_calls()  # confirm's create
            self.assertEqual(opened, ["confirm"])

            shell._defer_modal_swap(lambda: opened.append("retried"), key="save_as")
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

        self.assertEqual(opened, ["confirm", "retried"])

    def test_a_teardown_that_raises_reopens_the_slot_to_newcomers(self) -> None:
        # The latch must not outlive its transition. A teardown that raises
        # releases the slot, which clears the latch -- otherwise one contained
        # error would leave the seam refusing every cross-key request until
        # the next successful transition.
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
                shell._drain_deferred_ui_calls()

        self.assertFalse(shell._modal_swap_teardown_done)
        with _SwapRecorder():
            shell._defer_modal_swap(lambda: opened.append("other"), key="other")
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()
        self.assertEqual(opened, ["other"])

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
        # ...and the slot is free, so the next request still gets through.
        self.assertFalse(shell._modal_swap_pending)
        with _SwapRecorder():
            shell._defer_modal_swap(
                lambda: opened.append("after"), delete_tags=("m",), key="k"
            )
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()
        self.assertEqual(opened, ["after"])


class ScreenScopedSwapTests(unittest.TestCase):
    """D8 — a screen-scoped swap must not open over the destination screen."""

    def _armed_shell(self):
        shell = _make_shell()
        shell._defer_ui_armed = True
        shell.current_screen = "modules"
        return shell

    def test_navigation_between_the_two_passes_abandons_the_create(self) -> None:
        shell = self._armed_shell()

        opened: list[str] = []
        with _SwapRecorder():
            shell._defer_modal_swap(
                lambda: opened.append("notes"),
                delete_tags=("notes_modal",),
                key="modules_notes_modal",
                owner_screen="modules",
            )
            shell._drain_deferred_ui_calls()  # teardown pass
            shell.switch_screen("home")
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

        self.assertEqual(opened, [])
        self.assertFalse(shell._modal_swap_pending)  # cancelling frees the slot
        self.assertEqual(shell._pending_modal_swap_keys, set())

    def test_navigation_before_the_teardown_pass_abandons_the_create(self) -> None:
        shell = self._armed_shell()

        opened: list[str] = []
        with _SwapRecorder() as rec:
            shell._defer_modal_swap(
                lambda: opened.append("notes"),
                delete_tags=("notes_modal",),
                key="modules_notes_modal",
                owner_screen="modules",
            )
            shell.switch_screen("home")
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

        self.assertEqual(opened, [])
        self.assertEqual(rec.events, [])  # the teardown was abandoned too

    def test_a_screen_scoped_swap_still_opens_when_the_screen_is_unchanged(
        self,
    ) -> None:
        # Anti-vacuity for the two above: the guard must not simply refuse.
        shell = self._armed_shell()

        opened: list[str] = []
        with _SwapRecorder():
            shell._defer_modal_swap(
                lambda: opened.append("notes"),
                delete_tags=("notes_modal",),
                key="modules_notes_modal",
                owner_screen="modules",
            )
            shell._drain_deferred_ui_calls()
            shell._drain_deferred_ui_calls()

        self.assertEqual(opened, ["notes"])

    def test_a_footer_global_swap_survives_navigation(self) -> None:
        # Save As is requested from the footer and captures no screen state,
        # so it passes no owner_screen and navigation must not cancel it.
        shell = self._armed_shell()

        opened: list[str] = []
        with _SwapRecorder():
            shell._defer_modal_swap(
                lambda: opened.append("save_as"),
                delete_tags=("save_as_modal",),
                key="wrapper_profile_save_as",
            )
            shell._drain_deferred_ui_calls()
            shell.switch_screen("home")
            shell._drain_deferred_ui_calls()

        self.assertEqual(opened, ["save_as"])


if __name__ == "__main__":
    unittest.main()
