"""Worker-thread phase 1 — executor-seam tests for the long HID flows.

The seam is :meth:`AppShell._run_hid_job`: with ``hid_executor=None`` (the
default every other test file exercises) the DPG-free job and the DPG on_done
run inline, in today's statement order; with a threaded executor the job runs
off the render thread, its completion is queued, and ``AppShell._tick`` drains
it on the render thread.

Coverage map (worker-thread phase 1):

- W1 — sync default: refresh / apply / restore behave identically.
- W2 — threaded executor: busy flag set during the job, cleared after drain.
- W3 — second flow refused while busy: status-bar message set, HID-flow
  buttons disabled for the duration and re-enabled on completion.
- W4 — a raising job propagates through on_done into today's failure handling.
- W5 — ``_tick`` is the drain point.
- Nested composition — a threaded profile apply runs its post-apply read
  inline on the same worker (one busy window) and both on_done halves drain
  in order.

Stub services use ``threading.Event`` gates for latency control — never
sleeps-as-synchronization.
"""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.r2_shell_test_helpers import empty_snapshot
from zd_app import i18n
from zd_app.models import AppSettings, DeviceState
from zd_app.services.settings_apply_coordinator import ApplyFailure
from zd_app.services.settings_service import (
    BackPaddleBinding,
    MacroSlot,
    PollingRate,
    SetPollingRateOutcome,
    SettingsServiceError,
)
from zd_app.ui.app_shell import (
    _HID_FLOW_BUTTON_TAGS,
    HID_JOB_STALL_WARN_S,
    AppShell,
    threaded_hid_executor,
)
from zd_app.ui.screens import restore_points as rp_screen


_GATE_TIMEOUT_S = 5.0


def _make_shell(
    settings_service=None,
    *,
    hid_executor=None,
    restore_point_service=None,
) -> AppShell:
    settings_store = MagicMock()
    settings_store.load.return_value = AppSettings()
    device_service = MagicMock()
    device_service.state = DeviceState()
    device_service.recent_events.return_value = []
    device_service.summary_source_summary.return_value = "Not verified"
    device_service.last_read_duration_ms = None
    device_service.last_write_duration_ms = None
    profile_service = MagicMock()
    profile_service.pending_changes_count.return_value = 0
    wrapper_profile_store = MagicMock()
    wrapper_profile_store.list_profiles.return_value = []
    shell = AppShell(
        device_service=device_service,
        profile_service=profile_service,
        diagnostics_service=MagicMock(),
        settings_store=settings_store,
        preflight_service=MagicMock(),
        settings_service=settings_service,
        wrapper_profile_store=wrapper_profile_store,
        restore_point_service=restore_point_service,
        hid_executor=hid_executor,
    )
    # Suppress the default-built RestorePointService unless a test passes its
    # own — keeps the refresh job's first-connect capture a deterministic
    # no-op (no fresh-read against the gated stub, no on-disk RP writes).
    if restore_point_service is None:
        shell.restore_point_service = None
    shell.refresh_shell = lambda: None
    shell.rebuild_current_screen = lambda: None
    return shell


class LastBackPaddleBindingsThreadSafetyTests(unittest.TestCase):
    """P2f: ``_last_back_paddle_bindings`` is read/copied on the HID worker
    thread (read job) and mutated on the render thread (Save As /
    _remember_back_paddle_binding). The lock serializes those so a concurrent
    in-place write can't tear a cache copy. (CPython's GIL makes ``dict()``
    atomic, so this exercises the locked paths and guards against a future
    refactor — or a free-threaded build — where the race would become real.)"""

    def test_concurrent_cache_access_raises_nothing(self) -> None:
        shell = _make_shell()
        snap = empty_snapshot()  # empty back_paddle_bindings -> copy-from-cache path
        # Seed the cache so readers take the copy path, not the early return.
        shell._remember_back_paddle_binding(MacroSlot.M1, BackPaddleBinding(None))
        errors: list[BaseException] = []

        def reader() -> None:
            for _ in range(3000):
                try:
                    shell._with_last_back_paddle_bindings(snap)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

        def writer(base: int) -> None:
            for i in range(3000):
                try:
                    shell._remember_back_paddle_binding(
                        MacroSlot((base + i) % len(MacroSlot)), BackPaddleBinding(None)
                    )
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        threads += [threading.Thread(target=writer, args=(b,)) for b in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_GATE_TIMEOUT_S)

        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_lock_is_held_during_cache_methods(self) -> None:
        # The cache methods must actually take the lock (guards against a future
        # edit that drops the guard).
        shell = _make_shell()
        snap = empty_snapshot()
        observed: list[bool] = []
        real_lock = shell._last_back_paddle_bindings_lock

        class _ObservingLock:
            def __enter__(self):
                observed.append(True)
                return real_lock.__enter__()

            def __exit__(self, *args):
                return real_lock.__exit__(*args)

        shell._last_back_paddle_bindings_lock = _ObservingLock()
        shell._with_last_back_paddle_bindings(snap)
        shell._remember_back_paddle_binding(MacroSlot.M1, BackPaddleBinding(None))
        self.assertEqual(len(observed), 2)

    def test_snapshot_writes_serialized_under_lock(self) -> None:
        """P4 [D1]: ``last_controller_snapshot`` is written from BOTH threads —
        the render thread (refresh on_done / Save-As, now via
        ``_set_last_controller_snapshot``) and the HID worker thread
        (``_remember_back_paddle_binding``'s read-modify-write of the back-paddle
        bindings). Both writes must happen while the back-paddle lock is held by
        the writing thread, so the RMW is atomic against a plain overwrite and
        neither update is lost.

        Two real threads sharing a barrier hammer both write paths. A lock
        wrapper records which thread currently holds it; an instrumented
        ``__setattr__`` flags any ``last_controller_snapshot`` write made by a
        thread that does NOT hold the lock. The race itself is GIL-timing
        dependent (a stale read-modify-write rarely interleaves under natural
        scheduling, and the rememberer's own reads stay monotonic because it
        preserves-then-reads), so we assert the lock discipline that *prevents*
        the lost update instead — which fails deterministically on base, where
        the RMW snapshot write and the site overwrites run unlocked."""

        shell = _make_shell()
        real_lock = shell._last_back_paddle_bindings_lock

        class _HolderLock:
            """Wraps the real lock and records the holding thread's ident."""

            def __init__(self) -> None:
                self.holder: int | None = None

            def __enter__(self):
                real_lock.__enter__()
                self.holder = threading.get_ident()
                return self

            def __exit__(self, *args):
                self.holder = None
                return real_lock.__exit__(*args)

        holder_lock = _HolderLock()
        violations: list[str] = []

        class _TrackingShell(shell.__class__):
            def __setattr__(self, name, value):
                if name == "last_controller_snapshot":
                    lock = self.__dict__.get("_last_back_paddle_bindings_lock")
                    if (
                        lock is not None
                        and getattr(lock, "holder", None) != threading.get_ident()
                    ):
                        violations.append(name)
                super().__setattr__(name, value)

        shell.__class__ = _TrackingShell
        shell._last_back_paddle_bindings_lock = holder_lock
        shell._set_last_controller_snapshot(empty_snapshot(step_size=0))

        iterations = 2000
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def setter() -> None:
            try:
                barrier.wait(timeout=_GATE_TIMEOUT_S)
                for i in range(1, iterations + 1):
                    shell._set_last_controller_snapshot(empty_snapshot(step_size=i))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def rememberer() -> None:
            try:
                barrier.wait(timeout=_GATE_TIMEOUT_S)
                for _ in range(iterations):
                    shell._remember_back_paddle_binding(
                        MacroSlot.M1, BackPaddleBinding(None)
                    )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=setter),
            threading.Thread(target=rememberer),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_GATE_TIMEOUT_S * 4)

        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(
            violations,
            [],
            f"{len(violations)} last_controller_snapshot writes made without the "
            f"back-paddle lock held (lost-update race)",
        )


class BackPaddleSaveAsMergeStoreGapTests(unittest.TestCase):
    """P2 (round-3): the Save-As back-paddle merge->store sequence is two
    SEPARATELY-locked ops — ``_with_last_back_paddle_bindings`` (merge cache into
    the snapshot) then ``_set_last_controller_snapshot`` (store it). A
    worker-thread ``_remember_back_paddle_binding`` (the apply-coordinator's
    ``on_back_paddle_apply``) landing in the gap added its paddle to the cache
    and to ``last_controller_snapshot`` under the lock, but the store then
    overwrote ``last_controller_snapshot`` with a snapshot built BEFORE that
    paddle existed — the just-added paddle was lost. The store now folds the
    current cache back in, so the concurrently-added paddle survives.

    Round-4 follow-up: memory was correct after the round-3 fix, but Save-As still
    built the saved *profile* from its pre-store local snapshot, so the racing
    paddle reached ``last_controller_snapshot`` yet was dropped from the file on
    disk. ``_set_last_controller_snapshot`` now returns the folded snapshot and
    Save-As persists that, so the binding lands in the saved profile too.
    """

    def test_save_as_keeps_paddle_set_in_merge_store_gap(self) -> None:
        from zd_app.services.settings_service import ControllerButtonTarget

        shell = _make_shell()
        m1, a = MacroSlot.M1, BackPaddleBinding(ControllerButtonTarget.X)
        m2, b = MacroSlot.M2, BackPaddleBinding(ControllerButtonTarget.Y)

        # State right before a Save-As: the live snapshot carries no back-paddle
        # bindings yet, but the cache holds {M1: A} (so the merge will fold it
        # into the snapshot the store receives).
        shell.last_controller_snapshot = empty_snapshot(back_paddle_bindings={})
        shell._last_back_paddle_bindings = {m1: a}

        # Simulate the worker callback (M2 -> B) landing in the merge->store gap:
        # patch the store so its FIRST invocation fires the rememberer once, then
        # delegates to the real store. On base the real store overwrites with the
        # pre-merge snapshot and M2 is lost; after the fix the store folds the
        # cache (now {M1: A, M2: B}) back in.
        real_set = shell._set_last_controller_snapshot
        fired = {"once": False}

        def set_with_injected_worker(snap):
            if not fired["once"]:
                fired["once"] = True
                shell._remember_back_paddle_binding(m2, b)
            return real_set(snap)

        shell._set_last_controller_snapshot = set_with_injected_worker

        shell.save_current_as_named_wrapper_profile("Race")

        # The profile saved by THIS Save-As (the one the worker raced) must itself
        # carry M2: Save-As builds the profile from the snapshot the store folded
        # the late paddle into, not its stale pre-store local. Fail-on-base: the
        # "Race" profile persisted only {M1}, dropping M2 even though memory kept it.
        race_profile = shell.wrapper_profile_store.save.call_args.args[0]
        self.assertEqual(
            race_profile.snapshot.back_paddle_bindings,
            {m1: a, m2: b},
            "Save-As must persist a back-paddle that landed in the merge->store gap",
        )

        # In-memory snapshot must carry BOTH paddles (fail-on-base: M2 dropped).
        self.assertEqual(
            shell.last_controller_snapshot.back_paddle_bindings,
            {m1: a, m2: b},
        )

        # And a follow-up Save-As (no injection) persists M2 to the stored
        # profile — confirming the binding is durably recovered, not just present
        # transiently.
        shell._set_last_controller_snapshot = real_set
        shell.save_current_as_named_wrapper_profile("Follow")
        saved_profile = shell.wrapper_profile_store.save.call_args.args[0]
        self.assertEqual(saved_profile.snapshot.back_paddle_bindings.get(m2), b)


class BackPaddleReadOnDoneMergeStoreGapTests(unittest.TestCase):
    """P2 (round-4 symmetry): the refresh read-on-done store site is the twin of
    the Save-As one. The round-3 fix made ``_set_last_controller_snapshot`` fold
    a racing back-paddle back into memory; the round-4 fix made Save-As *persist*
    the folded snapshot rather than its pre-store local. This store site was left
    hydrating the UI from its pre-fold local: a worker-thread
    ``_remember_back_paddle_binding`` (the apply-coordinator's
    ``on_back_paddle_apply``) landing in the merge->store gap reached
    ``last_controller_snapshot`` but NOT the on_done local, so the just-added
    paddle was absent from the hydrated back-paddle combo until the next refresh.
    on_done now captures the store's return and hydrates from THAT.

    Fail-on-base: the back-paddle hydration received the pre-fold local (only
    ``{M1}``), dropping the racing ``M2`` from the rendered combo; after the fix
    it receives the folded ``{M1, M2}``. (``missing`` is unaffected — the fold
    only adds back-paddle bindings, which the hydrate consumes without
    contributing to the readable-field set.)
    """

    def test_read_on_done_hydrates_paddle_set_in_merge_store_gap(self) -> None:
        from zd_app.services.settings_service import ControllerButtonTarget

        shell = _make_shell()
        m1, a = MacroSlot.M1, BackPaddleBinding(ControllerButtonTarget.X)
        m2, b = MacroSlot.M2, BackPaddleBinding(ControllerButtonTarget.Y)

        # State the on_done half receives: the snapshot the job produced already
        # carries {M1: A} (the job folded the cache in), and the cache matches.
        snapshot = empty_snapshot(back_paddle_bindings={m1: a})
        shell.last_controller_snapshot = snapshot
        shell._last_back_paddle_bindings = {m1: a}

        # Capture exactly which bindings the back-paddle hydration leaf is handed.
        hydrated: dict = {}

        def record_back_paddle(bindings):
            hydrated["bindings"] = dict(bindings or {})

        shell._hydrate_back_paddle_bindings = record_back_paddle

        # Inject the worker callback (M2 -> B) into the merge->store gap: the
        # store's FIRST invocation fires the rememberer once, then delegates to
        # the real store (which folds the now-{M1,M2} cache back in).
        real_set = shell._set_last_controller_snapshot
        fired = {"once": False}

        def set_with_injected_worker(snap):
            if not fired["once"]:
                fired["once"] = True
                shell._remember_back_paddle_binding(m2, b)
            return real_set(snap)

        shell._set_last_controller_snapshot = set_with_injected_worker

        _capture_widget_state(
            lambda: shell._refresh_read_on_done((snapshot, None), include_device=True)
        )

        # The back-paddle combo hydrated from the folded snapshot, so the late M2
        # is present. Fail-on-base: only {M1} (the pre-fold local) reached hydrate.
        self.assertEqual(hydrated["bindings"], {m1: a, m2: b})
        # And memory carries both as well (true since round-3; invariant guard).
        self.assertEqual(
            shell.last_controller_snapshot.back_paddle_bindings, {m1: a, m2: b}
        )


def _capture_widget_state(callback):
    """Run ``callback`` under patched DPG, capturing set_value + configure_item.

    Same shape as the helper in test_app_shell_settings_integration: returns
    ``(values, config)`` where ``values`` maps tag -> last set_value and
    ``config`` maps tag -> merged configure_item kwargs.
    """

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


class _GatedService:
    """Settings-service stub whose calls are gated on threading.Events.

    ``read_gate`` / ``write_gate``: when set on the constructor, the matching
    call signals ``started`` then blocks until ``release`` is set — letting a
    test hold a worker mid-job deterministically. Ungated calls return
    immediately. ``read_raises`` makes get_all_settings raise (after the gate,
    if any), storing the instance on ``self.raised`` for exact-message
    assertions.
    """

    def __init__(
        self,
        snapshot,
        *,
        gate_read=False,
        gate_write=False,
        read_raises=None,
    ):
        self._snapshot = snapshot
        self._gate_read = gate_read
        self._gate_write = gate_write
        self._read_raises = read_raises
        self.started = threading.Event()
        self.release = threading.Event()
        self.read_calls = 0
        self.write_calls = 0
        self.raised = None

    def _pass_gate(self, gated: bool) -> None:
        if not gated:
            return
        self.started.set()
        if not self.release.wait(timeout=_GATE_TIMEOUT_S):
            raise AssertionError("test gate never released")

    def get_all_settings(self):
        self.read_calls += 1
        self._pass_gate(self._gate_read)
        if self._read_raises is not None:
            self.raised = self._read_raises
            raise self._read_raises
        return self._snapshot

    def set_polling_rate(self, rate):
        self.write_calls += 1
        self._pass_gate(self._gate_write)
        return SimpleNamespace(outcome=SetPollingRateOutcome.OK, error_code=None)


class _StubRestoreService:
    """RestorePointService stub for the restore flow: gated or raising."""

    def __init__(self, result=None, *, gate=False, raises=None):
        self.result = result if result is not None else SimpleNamespace(label="ok")
        self._gate = gate
        self._raises = raises
        self.started = threading.Event()
        self.release = threading.Event()
        self.restore_calls = 0

    def restore(self, rp_id):
        self.restore_calls += 1
        if self._gate:
            self.started.set()
            if not self.release.wait(timeout=_GATE_TIMEOUT_S):
                raise AssertionError("test gate never released")
        if self._raises is not None:
            raise self._raises
        return self.result


def _drain_queued_completions(shell, expected: int):
    """Block until ``expected`` completions are queued, then re-queue them.

    Deterministic join point for threaded tests: ``queue.get(timeout=...)``
    returns the moment the worker delivers, no polling sleeps. Entries are
    put back in arrival order so a subsequent drain processes them exactly
    as ``_tick`` would. Returns the entries for flag assertions.
    """

    entries = [
        shell._hid_job_completions.get(timeout=_GATE_TIMEOUT_S)
        for _ in range(expected)
    ]
    for entry in entries:
        shell._hid_job_completions.put(entry)
    return entries


# ---------------------------------------------------------------------------
# W1 — sync default (hid_executor=None) behaves exactly like the old inline
# flows.
# ---------------------------------------------------------------------------


class SyncDefaultBehaviorTests(unittest.TestCase):
    def test_refresh_runs_inline_and_hydrates(self) -> None:
        snapshot = empty_snapshot(polling_rate=PollingRate.HZ_8000)
        service = _GatedService(snapshot)
        shell = _make_shell(service)

        values, _ = _capture_widget_state(shell.refresh_from_controller)

        self.assertEqual(service.read_calls, 1)
        self.assertIs(shell.last_controller_snapshot, snapshot)
        self.assertIsNotNone(shell.last_snapshot_ts)
        self.assertFalse(shell._hid_job_in_flight)
        self.assertTrue(shell._hid_job_completions.empty())
        # Status text landed during the call (inline on_done), not later.
        self.assertIn("footer_status_text", values)

    def test_refresh_failure_runs_inline_failure_handling(self) -> None:
        exc = SettingsServiceError("read exploded")
        service = _GatedService(empty_snapshot(), read_raises=exc)
        shell = _make_shell(service)

        _capture_widget_state(shell.refresh_from_controller)

        self.assertIsNone(shell.last_controller_snapshot)
        self.assertIsNone(shell.last_snapshot_ts)
        self.assertEqual(
            shell.last_snapshot_status,
            i18n.t("apply.read.failed", reason=exc),
        )
        self.assertFalse(shell._hid_job_in_flight)

    def test_apply_runs_inline_records_result_and_reads_back(self) -> None:
        snapshot = empty_snapshot(polling_rate=PollingRate.HZ_8000)
        service = _GatedService(snapshot)
        shell = _make_shell(service)

        with patch("zd_app.ui.app_shell.time.sleep"):
            _capture_widget_state(
                lambda: shell._apply_wrapper_profile_snapshot(
                    "profile-w1", snapshot, include_device=True
                )
            )

        self.assertEqual(service.write_calls, 1)
        self.assertEqual(service.read_calls, 1)  # post-apply read ran
        self.assertIsNotNone(shell._last_apply_result)
        self.assertEqual(shell._last_apply_result.total_attempted, 1)
        recorded = shell.device_service.record_apply_result.call_args
        self.assertIs(recorded.args[0], True)
        self.assertIs(shell.last_controller_snapshot, snapshot)
        self.assertFalse(shell._hid_job_in_flight)

    def test_restore_runs_inline_to_result_view(self) -> None:
        service = _StubRestoreService()
        shell = _make_shell(MagicMock(), restore_point_service=service)
        shell.restore_points_screen_state = rp_screen.RestorePointsScreenState(
            view=rp_screen.VIEW_IN_PROGRESS, selected_rp_id="rp1"
        )

        rp_screen._execute_restore(shell)

        state = shell.restore_points_screen_state
        self.assertEqual(state.view, rp_screen.VIEW_RESULT)
        self.assertIs(state.result, service.result)
        self.assertFalse(shell._hid_job_in_flight)

    def test_restore_failure_runs_inline_to_list_view(self) -> None:
        service = _StubRestoreService(raises=RuntimeError("HID timeout"))
        shell = _make_shell(MagicMock(), restore_point_service=service)
        shell.restore_points_screen_state = rp_screen.RestorePointsScreenState(
            view=rp_screen.VIEW_IN_PROGRESS, selected_rp_id="rp1"
        )

        rp_screen._execute_restore(shell)

        state = shell.restore_points_screen_state
        self.assertEqual(state.view, rp_screen.VIEW_LIST)
        self.assertIn("HID timeout", state.status_text)
        self.assertEqual(state.status_kind, "warn")
        self.assertIsNone(state.result)

    def test_restore_supports_stub_shells_without_runner(self) -> None:
        # restore_points tests drive the state machine with SimpleNamespace
        # shells; the seam must keep that contract.
        service = _StubRestoreService()
        state = rp_screen.RestorePointsScreenState(
            view=rp_screen.VIEW_IN_PROGRESS, selected_rp_id="rp1"
        )
        shell = SimpleNamespace(
            restore_point_service=service,
            restore_points_screen_state=state,
            rebuild_current_screen=MagicMock(),
        )

        rp_screen._execute_restore(shell)

        self.assertEqual(state.view, rp_screen.VIEW_RESULT)
        self.assertIs(state.result, service.result)
        shell.rebuild_current_screen.assert_called_once()


# ---------------------------------------------------------------------------
# W2 — threaded executor lifecycle: busy during the job, cleared after drain.
# ---------------------------------------------------------------------------


class ThreadedExecutorLifecycleTests(unittest.TestCase):
    def test_busy_during_job_then_cleared_and_hydrated_after_drain(self) -> None:
        snapshot = empty_snapshot(polling_rate=PollingRate.HZ_8000)
        service = _GatedService(snapshot, gate_read=True)
        shell = _make_shell(service, hid_executor=threaded_hid_executor)

        shell.refresh_from_controller()

        self.assertTrue(service.started.wait(_GATE_TIMEOUT_S))
        self.assertTrue(shell._hid_job_in_flight)
        # on_done has not run: no snapshot bookkeeping yet.
        self.assertIsNone(shell.last_controller_snapshot)

        service.release.set()
        entries = _drain_queued_completions(shell, 1)
        self.assertTrue(entries[0][2])  # outermost completion clears the flag
        # Delivery alone must not flip the flag — only the render-thread drain.
        self.assertTrue(shell._hid_job_in_flight)

        _capture_widget_state(shell._drain_hid_job_completions)

        self.assertFalse(shell._hid_job_in_flight)
        self.assertIs(shell.last_controller_snapshot, snapshot)
        self.assertIsNotNone(shell.last_snapshot_ts)

    def test_threaded_restore_animates_state_machine(self) -> None:
        service = _StubRestoreService(gate=True)
        shell = _make_shell(
            MagicMock(),
            hid_executor=threaded_hid_executor,
            restore_point_service=service,
        )
        rebuilds = MagicMock()
        shell.rebuild_current_screen = rebuilds
        shell.restore_points_screen_state = rp_screen.RestorePointsScreenState(
            view=rp_screen.VIEW_IN_PROGRESS, selected_rp_id="rp1"
        )

        rp_screen._execute_restore(shell)

        self.assertTrue(service.started.wait(_GATE_TIMEOUT_S))
        # Job in flight: still IN_PROGRESS (the progress bar's frame),
        # busy flag guards other flows.
        self.assertTrue(shell._hid_job_in_flight)
        state = shell.restore_points_screen_state
        self.assertEqual(state.view, rp_screen.VIEW_IN_PROGRESS)

        service.release.set()
        _drain_queued_completions(shell, 1)
        _capture_widget_state(shell._drain_hid_job_completions)

        self.assertEqual(state.view, rp_screen.VIEW_RESULT)
        self.assertIs(state.result, service.result)
        self.assertFalse(shell._hid_job_in_flight)
        rebuilds.assert_called_once()


# ---------------------------------------------------------------------------
# W3 — re-entrancy: second flow refused while busy, buttons disabled then
# re-enabled.
# ---------------------------------------------------------------------------


class BusyRefusalTests(unittest.TestCase):
    def _gated_busy_shell(self):
        snapshot = empty_snapshot(polling_rate=PollingRate.HZ_8000)
        service = _GatedService(snapshot, gate_read=True)
        shell = _make_shell(service, hid_executor=threaded_hid_executor)
        shell._dpg_context_ready = True
        return shell, service

    def test_second_flow_refused_with_status_message(self) -> None:
        shell, service = self._gated_busy_shell()
        _capture_widget_state(shell.refresh_from_controller)
        self.assertTrue(service.started.wait(_GATE_TIMEOUT_S))

        values, _ = _capture_widget_state(shell.refresh_from_controller)

        self.assertEqual(service.read_calls, 1)  # second read never started
        busy_banner = i18n.t(
            "apply.status.failure_prefix", message=i18n.t("apply.busy")
        )
        self.assertEqual(values["footer_status_text"], busy_banner)
        self.assertEqual(values["settings_v2_status_text"], busy_banner)

        service.release.set()
        _drain_queued_completions(shell, 1)
        _capture_widget_state(shell._drain_hid_job_completions)
        self.assertFalse(shell._hid_job_in_flight)

    def test_apply_refused_while_busy_writes_nothing(self) -> None:
        shell, service = self._gated_busy_shell()
        _capture_widget_state(shell.refresh_from_controller)
        self.assertTrue(service.started.wait(_GATE_TIMEOUT_S))

        _capture_widget_state(
            lambda: shell._apply_wrapper_profile_snapshot(
                "profile-w3",
                empty_snapshot(polling_rate=PollingRate.HZ_8000),
                include_device=True,
            )
        )

        self.assertEqual(service.write_calls, 0)
        shell.device_service.record_apply_result.assert_not_called()

        service.release.set()
        _drain_queued_completions(shell, 1)
        _capture_widget_state(shell._drain_hid_job_completions)

    def test_confirm_restore_refused_while_busy_stays_on_confirm(self) -> None:
        shell, service = self._gated_busy_shell()
        shell.restore_points_screen_state = rp_screen.RestorePointsScreenState(
            view=rp_screen.VIEW_CONFIRM, selected_rp_id="rp1"
        )
        _capture_widget_state(shell.refresh_from_controller)
        self.assertTrue(service.started.wait(_GATE_TIMEOUT_S))

        rp_screen._on_confirm_restore(shell)

        state = shell.restore_points_screen_state
        self.assertEqual(state.view, rp_screen.VIEW_CONFIRM)
        self.assertEqual(state.status_text, i18n.t("apply.busy"))
        self.assertEqual(state.status_kind, "warn")

        service.release.set()
        _drain_queued_completions(shell, 1)
        _capture_widget_state(shell._drain_hid_job_completions)

    def test_hid_flow_buttons_disabled_then_reenabled(self) -> None:
        shell, service = self._gated_busy_shell()

        _, spawn_config = _capture_widget_state(shell.refresh_from_controller)
        self.assertTrue(service.started.wait(_GATE_TIMEOUT_S))
        for tag in _HID_FLOW_BUTTON_TAGS:
            self.assertIs(spawn_config[tag]["enabled"], False, tag)

        service.release.set()
        _drain_queued_completions(shell, 1)
        _, drain_config = _capture_widget_state(shell._drain_hid_job_completions)
        for tag in _HID_FLOW_BUTTON_TAGS:
            self.assertIs(drain_config[tag]["enabled"], True, tag)

    def test_hydration_request_deferred_not_consumed_while_busy(self) -> None:
        shell, service = self._gated_busy_shell()
        _capture_widget_state(shell.refresh_from_controller)
        self.assertTrue(service.started.wait(_GATE_TIMEOUT_S))

        shell.request_settings_service_hydration()
        _capture_widget_state(
            lambda: shell._tick_settings_service_tasks(time.time())
        )

        # Still pending (not dropped via a refused refresh), no second read.
        self.assertTrue(shell._needs_hydration)
        self.assertEqual(service.read_calls, 1)

        service.release.set()
        _drain_queued_completions(shell, 1)
        _capture_widget_state(shell._drain_hid_job_completions)


# ---------------------------------------------------------------------------
# W4 — a raising job propagates through on_done into today's failure handling.
# ---------------------------------------------------------------------------


class ThreadedFailurePropagationTests(unittest.TestCase):
    def test_read_failure_lands_in_failure_status_after_drain(self) -> None:
        exc = SettingsServiceError("read exploded mid-job")
        service = _GatedService(empty_snapshot(), gate_read=True, read_raises=exc)
        shell = _make_shell(service, hid_executor=threaded_hid_executor)

        shell.refresh_from_controller()
        self.assertTrue(service.started.wait(_GATE_TIMEOUT_S))
        service.release.set()

        _drain_queued_completions(shell, 1)
        values, _ = _capture_widget_state(shell._drain_hid_job_completions)

        self.assertFalse(shell._hid_job_in_flight)
        self.assertIsNone(shell.last_controller_snapshot)
        self.assertEqual(
            shell.last_snapshot_status,
            i18n.t("apply.read.failed", reason=service.raised),
        )
        self.assertIn("read exploded mid-job", values["settings_v2_status_text"])
        shell.device_service.log_i18n_event.assert_called_with(
            "log.snapshot.refresh_failed", reason=service.raised
        )

    def test_restore_failure_lands_in_list_view_after_drain(self) -> None:
        service = _StubRestoreService(gate=True, raises=RuntimeError("HID wedge"))
        shell = _make_shell(
            MagicMock(),
            hid_executor=threaded_hid_executor,
            restore_point_service=service,
        )
        shell.restore_points_screen_state = rp_screen.RestorePointsScreenState(
            view=rp_screen.VIEW_IN_PROGRESS, selected_rp_id="rp1"
        )

        rp_screen._execute_restore(shell)
        self.assertTrue(service.started.wait(_GATE_TIMEOUT_S))
        service.release.set()

        _drain_queued_completions(shell, 1)
        _capture_widget_state(shell._drain_hid_job_completions)

        state = shell.restore_points_screen_state
        self.assertEqual(state.view, rp_screen.VIEW_LIST)
        self.assertIn("HID wedge", state.status_text)
        self.assertEqual(state.status_kind, "warn")
        self.assertFalse(shell._hid_job_in_flight)


# ---------------------------------------------------------------------------
# W5 — _tick drains completions on the render path.
# ---------------------------------------------------------------------------


class TickDrainTests(unittest.TestCase):
    def test_tick_drains_completion_and_clears_busy(self) -> None:
        snapshot = empty_snapshot(polling_rate=PollingRate.HZ_8000)
        service = _GatedService(snapshot, gate_read=True)
        shell = _make_shell(service, hid_executor=threaded_hid_executor)
        # Keep this tick to the drain: skip the presence poll and the 0.15 s
        # shell-refresh branch, both irrelevant here.
        shell._last_presence_poll = time.time()
        shell._last_tick = time.time()

        shell.refresh_from_controller()
        self.assertTrue(service.started.wait(_GATE_TIMEOUT_S))
        service.release.set()
        _drain_queued_completions(shell, 1)

        _capture_widget_state(shell._tick)

        self.assertFalse(shell._hid_job_in_flight)
        self.assertIs(shell.last_controller_snapshot, snapshot)


# ---------------------------------------------------------------------------
# Nested composition — threaded apply runs its post-apply read inline on the
# worker; both on_done halves drain in order under one busy window.
# ---------------------------------------------------------------------------


class ThreadedApplyCompositionTests(unittest.TestCase):
    def test_apply_job_nests_read_and_drains_both_completions(self) -> None:
        snapshot = empty_snapshot(polling_rate=PollingRate.HZ_8000)
        service = _GatedService(snapshot, gate_write=True)
        shell = _make_shell(service, hid_executor=threaded_hid_executor)
        shell._dpg_context_ready = True

        with patch("zd_app.ui.app_shell.time.sleep"):
            _capture_widget_state(
                lambda: shell._apply_wrapper_profile_snapshot(
                    "profile-nested", snapshot, include_device=True
                )
            )
            self.assertTrue(service.started.wait(_GATE_TIMEOUT_S))
            self.assertTrue(shell._hid_job_in_flight)
            self.assertEqual(service.read_calls, 0)

            service.release.set()
            entries = _drain_queued_completions(shell, 2)
            # The nested read's completion arrives first and must NOT clear
            # the busy flag; the outer apply completion does.
            self.assertFalse(entries[0][2])
            self.assertTrue(entries[1][2])

            values, _ = _capture_widget_state(shell._drain_hid_job_completions)

        self.assertEqual(service.write_calls, 1)
        self.assertEqual(service.read_calls, 1)  # nested read ran on the worker
        self.assertFalse(shell._hid_job_in_flight)
        self.assertIs(shell.last_controller_snapshot, snapshot)
        recorded = shell.device_service.record_apply_result.call_args
        self.assertIs(recorded.args[0], True)
        # The apply banner is the final footer text (read status drained
        # first, then the banner took the apply-active hold).
        self.assertTrue(values["footer_status_text"].startswith("OK"))


# ---------------------------------------------------------------------------
# Reconnect stop() deferral — a USB bounce mid-job must not
# close the HID handles out from under the running worker: the reconnect
# branch flags _needs_service_restart instead, and the tick consumes it
# (stop + hydration request) only when no job is in flight.
# ---------------------------------------------------------------------------


class _StoppableGatedService(_GatedService):
    """_GatedService plus the stop() the reconnect branch calls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1


class ReconnectStopDeferralTests(unittest.TestCase):
    def _arm_reconnect(self, shell) -> None:
        """Make the next _tick observe a no_device -> connected transition."""

        shell.settings.auto_read_on_connect = False  # keep the tick to the branch under test
        shell._last_tick = time.time()  # skip the shell-refresh branch
        shell._last_connection_state = "no_device"
        shell.device_service.state.connection_state = "connected"
        shell._last_presence_poll = 0.0  # presence branch fires this tick

    def test_reconnect_mid_job_defers_stop_until_job_drains(self) -> None:
        snapshot = empty_snapshot(polling_rate=PollingRate.HZ_8000)
        service = _StoppableGatedService(snapshot, gate_read=True)
        shell = _make_shell(service, hid_executor=threaded_hid_executor)
        self._arm_reconnect(shell)

        _capture_widget_state(shell.refresh_from_controller)  # busy window
        self.assertTrue(service.started.wait(_GATE_TIMEOUT_S))

        _capture_widget_state(shell._tick)  # reconnect lands mid-job

        self.assertEqual(service.stop_calls, 0)  # deferred, not inline
        self.assertTrue(shell._needs_service_restart)
        # Hydration rides the restart's consumption, not the reconnect tick.
        self.assertFalse(shell._needs_hydration)

        # Further ticks while the job is still in flight keep deferring.
        shell._last_presence_poll = 0.0
        _capture_widget_state(shell._tick)
        self.assertEqual(service.stop_calls, 0)
        self.assertTrue(shell._needs_service_restart)

        service.release.set()
        _drain_queued_completions(shell, 1)
        # One tick: drains the completion, then consumes the restart —
        # exactly one stop() plus the hydration refresh (a fresh jobbed
        # read; the read gate is already open).
        _capture_widget_state(shell._tick)

        self.assertEqual(service.stop_calls, 1)
        self.assertFalse(shell._needs_service_restart)
        self.assertFalse(shell._needs_hydration)  # consumed on the same tick

        _drain_queued_completions(shell, 1)
        _capture_widget_state(shell._drain_hid_job_completions)
        self.assertEqual(service.read_calls, 2)  # hydration read ran jobbed
        self.assertFalse(shell._hid_job_in_flight)

    def test_reconnect_sync_mode_stops_inline_unchanged(self) -> None:
        service = _StoppableGatedService(
            empty_snapshot(polling_rate=PollingRate.HZ_8000)
        )
        shell = _make_shell(service)  # sync default: never busy
        self._arm_reconnect(shell)

        _capture_widget_state(shell._tick)

        self.assertEqual(service.stop_calls, 1)  # pre-seam inline behavior
        self.assertFalse(shell._needs_service_restart)
        self.assertTrue(shell._needs_hydration)  # consumed by a later tick


# ---------------------------------------------------------------------------
# Retry-failed-settings jobbing — the failure modal's Retry
# runs burst + settle as the job; status / modal re-show / chained refresh as
# on_done; refused outright (modal left open) while another job is in flight.
# ---------------------------------------------------------------------------


class RetryFailedSettingsJobbingTests(unittest.TestCase):
    def _gated_failure(self):
        """An ApplyFailure whose retry_fn is event-gated like _GatedService."""

        started = threading.Event()
        release = threading.Event()
        retry_calls = []

        def retry_fn():
            retry_calls.append("retry")
            started.set()
            if not release.wait(timeout=_GATE_TIMEOUT_S):
                raise AssertionError("test gate never released")
            return SimpleNamespace(
                outcome=SetPollingRateOutcome.OK, error_code=None
            )

        failure = ApplyFailure(
            setting_label="polling",
            error="transient write glitch",
            is_transient=True,
            retry_fn=retry_fn,
        )
        return failure, started, release, retry_calls

    def test_threaded_retry_runs_on_worker_then_chains_jobbed_refresh(self) -> None:
        snapshot = empty_snapshot(polling_rate=PollingRate.HZ_8000)
        # gate_read holds the CHAINED refresh mid-read so the second busy
        # window is observable deterministically (not raced by the drain).
        service = _GatedService(snapshot, gate_read=True)
        shell = _make_shell(service, hid_executor=threaded_hid_executor)
        shell._dpg_context_ready = True
        failure, started, release, retry_calls = self._gated_failure()

        with patch("zd_app.ui.app_shell.time.sleep"), patch(
            "zd_app.ui.app_shell.dpg.delete_item"
        ):
            returned: list = []
            _capture_widget_state(
                lambda: returned.append(shell._retry_failed_settings([failure]))
            )

            # Threaded mode reports via on_done, not the return value.
            self.assertIsNone(returned[0])
            self.assertTrue(started.wait(_GATE_TIMEOUT_S))
            self.assertTrue(shell._hid_job_in_flight)
            # on_done has not run: no status recorded, no chained refresh.
            shell.device_service.record_apply_result.assert_not_called()
            self.assertEqual(service.read_calls, 0)

            release.set()
            _drain_queued_completions(shell, 1)
            _capture_widget_state(shell._drain_hid_job_completions)

            # on_done recorded the success THEN chained the refresh as its
            # own jobbed read — a fresh busy window, not an inline call.
            self.assertEqual(retry_calls, ["retry"])
            recorded = shell.device_service.record_apply_result.call_args
            self.assertIs(recorded.args[0], True)
            self.assertTrue(service.started.wait(_GATE_TIMEOUT_S))
            self.assertTrue(shell._hid_job_in_flight)
            self.assertIsNone(shell.last_controller_snapshot)

            service.release.set()
            _drain_queued_completions(shell, 1)
            _capture_widget_state(shell._drain_hid_job_completions)

        self.assertEqual(service.read_calls, 1)  # chained refresh ran jobbed
        self.assertIs(shell.last_controller_snapshot, snapshot)
        self.assertFalse(shell._hid_job_in_flight)
        self.assertIsNotNone(shell._last_apply_result)
        self.assertEqual(shell._last_apply_result.succeeded, 1)

    def test_retry_refused_while_busy_keeps_modal_open(self) -> None:
        snapshot = empty_snapshot(polling_rate=PollingRate.HZ_8000)
        service = _GatedService(snapshot, gate_read=True)
        shell = _make_shell(service, hid_executor=threaded_hid_executor)
        shell._dpg_context_ready = True
        _capture_widget_state(shell.refresh_from_controller)
        self.assertTrue(service.started.wait(_GATE_TIMEOUT_S))

        failure, _started, _release, retry_calls = self._gated_failure()
        with patch("zd_app.ui.app_shell.dpg.delete_item") as delete_item:
            returned: list = []
            values, _ = _capture_widget_state(
                lambda: returned.append(shell._retry_failed_settings([failure]))
            )

        self.assertIsNone(returned[0])
        self.assertEqual(retry_calls, [])  # burst never started
        # The refusal happens BEFORE the modal teardown: the user can
        # re-click Retry once the busy window closes.
        delete_item.assert_not_called()
        busy_banner = i18n.t(
            "apply.status.failure_prefix", message=i18n.t("apply.busy")
        )
        self.assertEqual(values["footer_status_text"], busy_banner)
        shell.device_service.record_apply_result.assert_not_called()

        service.release.set()
        _drain_queued_completions(shell, 1)
        _capture_widget_state(shell._drain_hid_job_completions)
        self.assertFalse(shell._hid_job_in_flight)


# ---------------------------------------------------------------------------
# Busy-staleness surfacing — a job stuck past
# HID_JOB_STALL_WARN_S logs ONE warning and pins the footer to the
# apply.busy_long banner with a counting duration.
# ---------------------------------------------------------------------------


class HidJobStalenessTests(unittest.TestCase):
    def test_stale_job_warns_once_and_pins_busy_long_status(self) -> None:
        snapshot = empty_snapshot(polling_rate=PollingRate.HZ_8000)
        service = _GatedService(snapshot, gate_read=True)
        shell = _make_shell(service, hid_executor=threaded_hid_executor)
        shell._dpg_context_ready = True
        # Keep ticks to the staleness path: skip presence poll + refresh.
        shell._last_presence_poll = time.time()
        shell._last_tick = time.time()

        _capture_widget_state(shell.refresh_from_controller)
        self.assertTrue(service.started.wait(_GATE_TIMEOUT_S))
        self.assertIsNotNone(shell._hid_job_started_monotonic)

        # Young job: no warning, no status pin.
        values, _ = _capture_widget_state(shell._tick)
        self.assertNotIn("footer_status_text", values)

        # Age the job past the threshold by backdating its start marker.
        shell._hid_job_started_monotonic -= HID_JOB_STALL_WARN_S + 1.0

        with self.assertLogs("zd_app.ui.app_shell", level="WARNING") as logs:
            values, _ = _capture_widget_state(shell._tick)
            seconds = shell._hid_job_stall_last_seconds
            _capture_widget_state(shell._tick)  # same second: no re-warn

        stall_warnings = [
            line for line in logs.output if "still in flight" in line
        ]
        self.assertEqual(len(stall_warnings), 1)  # ONE warning per job
        self.assertGreaterEqual(seconds, int(HID_JOB_STALL_WARN_S) + 1)
        expected_banner = i18n.t(
            "apply.status.failure_prefix",
            message=i18n.t("apply.busy_long", seconds=seconds),
        )
        self.assertEqual(values["footer_status_text"], expected_banner)
        self.assertIn(f"({seconds}s)", values["footer_status_text"])

        service.release.set()
        _drain_queued_completions(shell, 1)
        _capture_widget_state(shell._drain_hid_job_completions)
        self.assertIsNone(shell._hid_job_started_monotonic)
        self.assertFalse(shell._hid_job_in_flight)

    def test_sync_mode_never_records_a_job_start(self) -> None:
        service = _GatedService(empty_snapshot(polling_rate=PollingRate.HZ_8000))
        shell = _make_shell(service)

        _capture_widget_state(shell.refresh_from_controller)

        self.assertIsNone(shell._hid_job_started_monotonic)


if __name__ == "__main__":
    unittest.main()
