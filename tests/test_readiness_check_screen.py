"""DPG screen-build tests for the Readiness Check (quick mode) screen.

Mirrors ``tests/test_health_report_screen.py``: patches dearpygui symbols on
the screen module so ``build`` runs without a real DPG context, then asserts
the right tags + callbacks are wired for each lifecycle state.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from zd_app.services.health_report import (
    QuickCheckPhase,
    QuickCheckState,
    ReadinessStatus,
    ReadinessVerdict,
)
from zd_app.services.health_report.quick_check import QuickCheckProgress
from zd_app.ui.screens import readiness_check as screen


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeService:
    """Drop-in stand-in for QuickCheckService. Records calls."""

    def __init__(
        self,
        state: QuickCheckState,
        verdict: ReadinessVerdict | None = None,
        progress: QuickCheckProgress | None = None,
    ) -> None:
        self.state = state
        self.verdict = verdict
        self.calls: list[str] = []
        # Derive a sensible default progress for sampling states.
        if progress is None:
            phase_map = {
                QuickCheckState.REST: QuickCheckPhase.REST,
                QuickCheckState.RANGE: QuickCheckPhase.RANGE,
                QuickCheckState.TRIGGER: QuickCheckPhase.TRIGGER,
            }
            self._progress = QuickCheckProgress(
                state=state,
                phase=phase_map.get(state),
                elapsed_s=2.5,
                total_budget_s=20.0,
                samples_collected=420,
            )
        else:
            self._progress = progress

    def progress(self) -> QuickCheckProgress:
        return self._progress

    def start(self) -> None:
        self.calls.append("start")
        self.state = QuickCheckState.REST

    def cancel(self) -> None:
        self.calls.append("cancel")
        self.state = QuickCheckState.CANCELLED

    def reset(self) -> None:
        self.calls.append("reset")
        self.state = QuickCheckState.IDLE
        self.verdict = None

    def tick(self) -> None:
        self.calls.append("tick")


class _ShellWithSwitch:
    """Shell stub that captures switch_screen calls so the "Open full
    Health Check" button test can verify navigation routing."""

    def __init__(self, service) -> None:
        self.quick_check_service = service
        self.COLORS = {
            "accent": (1, 1, 1, 1),
            "muted": (2, 2, 2, 2),
            "good": (3, 3, 3, 3),
            "warn": (4, 4, 4, 4),
        }
        self.rebuild_current_screen = MagicMock()
        self.switch_screen = MagicMock()
        self.current_screen = "readiness_check"


def _shell_with(service: _FakeService) -> _ShellWithSwitch:
    return _ShellWithSwitch(service)


class _FakeContextManager:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _PatchedScreen:
    """Context manager that holds DPG patches active across build + callbacks.

    Use as::

        with _PatchedScreen(shell) as ps:
            ps.build()
            ps.buttons[TAG].callback()
            assert ps.values[TAG] == ...
    """

    def __init__(self, shell) -> None:
        self.shell = shell
        self.values: dict[str, object] = {}
        self.calls: list[tuple[str, tuple, dict]] = []
        self.buttons: dict[str, callable] = {}
        self.child_windows: list[dict] = []
        self._cm = None

    def __enter__(self) -> "_PatchedScreen":
        def record(name):
            def _fn(*args, **kw):
                self.calls.append((name, args, kw))
                if name == "add_button" and "tag" in kw and "callback" in kw:
                    self.buttons[kw["tag"]] = kw["callback"]
                return kw.get("tag", name)

            return _fn

        def record_child_window(*args, **kw):
            self.calls.append(("child_window", args, kw))
            self.child_windows.append(kw)
            return _FakeContextManager()

        def set_value(tag, value):
            self.values[tag] = value

        self._cm = patch.multiple(
            "zd_app.ui.screens.readiness_check.dpg",
            add_text=record("add_text"),
            add_spacer=record("add_spacer"),
            add_button=record("add_button"),
            add_progress_bar=record("add_progress_bar"),
            child_window=record_child_window,
            group=lambda *a, **kw: _FakeContextManager(),
            set_value=set_value,
            does_item_exist=lambda *_args, **_kw: True,
            get_frame_count=lambda: 0,
            set_frame_callback=record("set_frame_callback"),
        )
        self._cm.__enter__()
        return self

    def __exit__(self, *exc):
        if self._cm is not None:
            self._cm.__exit__(*exc)
        return False

    def build(self) -> None:
        screen.build(self.shell, parent="content_region")


# ---------------------------------------------------------------------------
# INIT state
# ---------------------------------------------------------------------------


class InitStateTests(unittest.TestCase):
    def test_init_renders_run_button(self) -> None:
        service = _FakeService(state=QuickCheckState.IDLE)
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            button_tags = list(ps.buttons.keys())

        self.assertIn(screen.TAG_RUN_BUTTON, button_tags)

    def test_init_renders_title_and_body_tags(self) -> None:
        service = _FakeService(state=QuickCheckState.IDLE)
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            text_tags = [
                kw.get("tag") for fn, _, kw in ps.calls if fn == "add_text"
            ]

        self.assertIn(screen.TAG_INIT_TITLE, text_tags)
        self.assertIn(screen.TAG_INIT_BODY, text_tags)

    def test_run_button_starts_service_and_rebuilds(self) -> None:
        service = _FakeService(state=QuickCheckState.IDLE)
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            ps.buttons[screen.TAG_RUN_BUTTON]()

        self.assertEqual(service.calls, ["start"])
        self.assertEqual(service.state, QuickCheckState.REST)
        shell.rebuild_current_screen.assert_called_once()


# ---------------------------------------------------------------------------
# RUNNING state
# ---------------------------------------------------------------------------


class RunningStateTests(unittest.TestCase):
    def _all_running_states(self) -> tuple[QuickCheckState, ...]:
        return (
            QuickCheckState.REST,
            QuickCheckState.RANGE,
            QuickCheckState.TRIGGER,
        )

    def test_every_running_state_renders_progress_bar_and_cancel(self) -> None:
        for state in self._all_running_states():
            with self.subTest(state=state):
                service = _FakeService(state=state)
                shell = _shell_with(service)

                with _PatchedScreen(shell) as ps:
                    ps.build()
                    progress_tags = [
                        kw.get("tag")
                        for fn, _, kw in ps.calls
                        if fn == "add_progress_bar"
                    ]
                    buttons = list(ps.buttons.keys())

                self.assertIn(screen.TAG_RUNNING_PROGRESS_BAR, progress_tags)
                self.assertIn(screen.TAG_RUNNING_CANCEL_BUTTON, buttons)

    def test_running_state_renders_phase_label(self) -> None:
        service = _FakeService(state=QuickCheckState.RANGE)
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            text_tags = [
                kw.get("tag") for fn, _, kw in ps.calls if fn == "add_text"
            ]

        self.assertIn(screen.TAG_RUNNING_PHASE, text_tags)
        self.assertIn(screen.TAG_RUNNING_REMAINING, text_tags)
        self.assertIn(screen.TAG_RUNNING_SAMPLES, text_tags)

    def test_cancel_button_invokes_service_cancel(self) -> None:
        service = _FakeService(state=QuickCheckState.RANGE)
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            ps.buttons[screen.TAG_RUNNING_CANCEL_BUTTON]()

        self.assertEqual(service.calls, ["cancel"])
        self.assertEqual(service.state, QuickCheckState.CANCELLED)


# ---------------------------------------------------------------------------
# DONE state
# ---------------------------------------------------------------------------


def _fixture_verdict(status: ReadinessStatus = ReadinessStatus.GREEN) -> ReadinessVerdict:
    return ReadinessVerdict(
        status=status,
        observations=(
            "readiness_check.obs.rest_clean",
            "readiness_check.obs.range_good",
            "readiness_check.obs.trigger_smooth",
            "readiness_check.obs.cadence_consistent",
        ),
    )


class DoneStateTests(unittest.TestCase):
    def test_done_renders_verdict_card_status_and_buttons(self) -> None:
        service = _FakeService(
            state=QuickCheckState.COMPLETE,
            verdict=_fixture_verdict(ReadinessStatus.GREEN),
        )
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            text_tags = [
                kw.get("tag") for fn, _, kw in ps.calls if fn == "add_text"
            ]
            buttons = list(ps.buttons.keys())

        self.assertIn(screen.TAG_DONE_STATUS, text_tags)
        self.assertIn(screen.TAG_DONE_FOOTER, text_tags)
        self.assertIn(screen.TAG_DONE_RUN_AGAIN_BUTTON, buttons)
        self.assertIn(screen.TAG_DONE_OPEN_FULL_BUTTON, buttons)

    def test_done_renders_verdict_card_child_window(self) -> None:
        service = _FakeService(
            state=QuickCheckState.COMPLETE,
            verdict=_fixture_verdict(),
        )
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            card_tags = [
                kw.get("tag") for kw in ps.child_windows
            ]

        self.assertIn(screen.TAG_DONE_VERDICT_CARD, card_tags)

    def test_done_run_again_resets_to_idle(self) -> None:
        service = _FakeService(
            state=QuickCheckState.COMPLETE,
            verdict=_fixture_verdict(),
        )
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            ps.buttons[screen.TAG_DONE_RUN_AGAIN_BUTTON]()

        self.assertEqual(service.calls, ["reset"])
        self.assertEqual(service.state, QuickCheckState.IDLE)
        self.assertIsNone(service.verdict)
        shell.rebuild_current_screen.assert_called_once()

    def test_open_full_button_navigates_to_health_report(self) -> None:
        service = _FakeService(
            state=QuickCheckState.COMPLETE,
            verdict=_fixture_verdict(),
        )
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            ps.buttons[screen.TAG_DONE_OPEN_FULL_BUTTON]()

        shell.switch_screen.assert_called_once_with("health_report")
        # Don't reset state — the user may want to come back to the verdict.
        self.assertNotIn("reset", service.calls)
        self.assertEqual(service.state, QuickCheckState.COMPLETE)
        self.assertIsNotNone(service.verdict)

    def test_open_full_button_does_not_crash_if_switch_fails(self) -> None:
        service = _FakeService(
            state=QuickCheckState.COMPLETE,
            verdict=_fixture_verdict(),
        )
        shell = _shell_with(service)
        shell.switch_screen = MagicMock(side_effect=RuntimeError("nav fault"))

        with _PatchedScreen(shell) as ps:
            ps.build()
            # Should swallow the exception, not propagate.
            ps.buttons[screen.TAG_DONE_OPEN_FULL_BUTTON]()

        shell.switch_screen.assert_called_once()

    def test_done_verdict_card_is_content_fit(self) -> None:
        # The card holds a status line + up to 4 observation bullets (the cap in
        # quick_check.py:584), each able to wrap at the 480-wide budget. It must
        # stay CONTENT-FIT (DPG-2.x auto_resize_y, NO fixed height) so the full
        # RED 4-bullet case can't clip — the old fixed 200px did (real-viewport
        # probe: V=14px overshoot). See tools/diag_dpg_card_clip.py +
        # test_card_clip_regressions.FitContractTests.
        service = _FakeService(
            state=QuickCheckState.COMPLETE,
            verdict=_fixture_verdict(),
        )
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()

        cards = [
            kw for kw in ps.child_windows
            if kw.get("tag") == screen.TAG_DONE_VERDICT_CARD
        ]
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertTrue(
            card.get("auto_resize_y"),
            "verdict card must use DPG-2.x content-fit (auto_resize_y=True) so it "
            "can't clip the full 4-observation RED case",
        )
        self.assertIsNone(
            card.get("height"),
            "verdict card must NOT carry a fixed height — that reintroduces the clip",
        )
        # Width stays pinned: the bullets wrap at 480 inside a 520-wide card.
        self.assertEqual(card.get("width"), 520)


# ---------------------------------------------------------------------------
# CANCELLED state
# ---------------------------------------------------------------------------


class CancelledStateTests(unittest.TestCase):
    def test_cancelled_renders_reset_button(self) -> None:
        service = _FakeService(state=QuickCheckState.CANCELLED)
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            buttons = list(ps.buttons.keys())

        self.assertIn(screen.TAG_CANCELLED_RESET_BUTTON, buttons)

    def test_cancelled_reset_returns_to_idle(self) -> None:
        service = _FakeService(state=QuickCheckState.CANCELLED)
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            ps.buttons[screen.TAG_CANCELLED_RESET_BUTTON]()

        self.assertEqual(service.calls, ["reset"])
        self.assertEqual(service.state, QuickCheckState.IDLE)


if __name__ == "__main__":
    unittest.main()
