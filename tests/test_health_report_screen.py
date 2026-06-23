"""DPG screen-build tests for the Controller Health Report wizard.

These tests patch ``dearpygui.dearpygui`` symbols on the screen module so
``build`` runs without a real DPG context. Each test verifies the right
container tags appear for the corresponding state and that callbacks wired
to buttons invoke the orchestrator correctly.

The orchestrator itself is stubbed (``_FakeService``) so test cases are
fully deterministic and independent of the real sample capture pipeline.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from zd_app.i18n import t
from zd_app.services.health_report import (
    DeviceContext,
    HealthReport,
    HealthReportState,
    OverallStatus,
    RangeCoverageLabel,
    RestNoiseLabel,
    SampleQuality,
    StickRange,
    StickRangeReport,
    StickRestNoise,
    StickRestNoiseReport,
    TriggerRange,
    TriggerRangeReport,
    TriggerSmoothnessLabel,
)
from zd_app.services.health_report.service import StepProgress
from zd_app.ui.screens import health_report as screen


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeService:
    """Drop-in stand-in for HealthReportService. Records calls."""

    def __init__(self, state: HealthReportState, report: HealthReport | None = None,
                 device: DeviceContext | None = None,
                 progress: StepProgress | None = None) -> None:
        self.state = state
        self.report = report
        self.calls: list[str] = []
        self._device = device or DeviceContext(
            controller_name="Test Controller",
            configured_polling_hz=1000,
            profile_name="Test Profile",
        )
        self._progress = progress or StepProgress(
            state=state, elapsed_s=1.5, duration_s=5.0, samples_collected=300,
        )

    def _device_context_provider(self) -> DeviceContext:
        return self._device

    def step_progress(self) -> StepProgress:
        return self._progress

    def begin(self) -> None:
        self.calls.append("begin")

    def start_test(self) -> None:
        self.calls.append("start_test")

    def proceed_to_step(self) -> None:
        self.calls.append("proceed_to_step")
        if self.state == HealthReportState.READY_REST:
            self.state = HealthReportState.REST
        elif self.state == HealthReportState.READY_ROTATION:
            self.state = HealthReportState.ROTATION
        elif self.state == HealthReportState.READY_TRIGGER:
            self.state = HealthReportState.TRIGGER

    def cancel(self) -> None:
        self.calls.append("cancel")
        self.state = HealthReportState.CANCELLED

    def reset(self) -> None:
        self.calls.append("reset")
        self.state = HealthReportState.IDLE

    def tick(self) -> None:
        self.calls.append("tick")


def _shell_with(service: _FakeService) -> SimpleNamespace:
    return SimpleNamespace(
        health_report_service=service,
        COLORS={"accent": (1, 1, 1, 1), "muted": (2, 2, 2, 2), "good": (3, 3, 3, 3),
                "warn": (4, 4, 4, 4), "bad": (5, 5, 5, 5), "error": (5, 5, 5, 5),
                "text": (6, 6, 6, 6)},
        rebuild_current_screen=MagicMock(),
    )


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

        def record_cm(name):
            # Recorder for context-manager widgets (table, table_row) that the
            # the summary section opens with ``with``: record the call, yield a fake
            # CM so the body runs without a real DPG container on the stack.
            def _fn(*args, **kw):
                self.calls.append((name, args, kw))
                return _FakeContextManager()
            return _fn

        def set_value(tag, value):
            self.values[tag] = value

        self._cm = patch.multiple(
            "zd_app.ui.screens.health_report.dpg",
            add_text=record("add_text"),
            add_spacer=record("add_spacer"),
            add_button=record("add_button"),
            add_progress_bar=record("add_progress_bar"),
            add_checkbox=record("add_checkbox"),
            child_window=record_child_window,
            table=record_cm("table"),
            table_row=record_cm("table_row"),
            add_table_column=record("add_table_column"),
            group=_FakeGroup,
            set_value=set_value,
            does_item_exist=lambda *_args, **_kw: True,
            configure_item=record("configure_item"),
            set_clipboard_text=record("set_clipboard_text"),
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


class _FakeContextManager:
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return False


def _FakeChildWindow(*args, **kw):
    return _FakeContextManager()


def _FakeGroup(*args, **kw):
    return _FakeContextManager()


# ---------------------------------------------------------------------------
# Fixture report
# ---------------------------------------------------------------------------


def _fixture_report() -> HealthReport:
    # Stick values are signed integer percent of full axis travel (matches
    # the Sticks-tab deadzone slider unit).
    rest = StickRestNoise(
        sample_count=512, duration_ms=5000.0,
        median_center_x=0.0, median_center_y=0.0,
        mean_center_x=0.0, mean_center_y=0.0,
        max_abs_x=1, max_abs_y=1,
        max_r=2, p95_r=1, p99_r=1,
        suggested_deadzone_from_sample=2,
        quality=SampleQuality.GOOD, label=RestNoiseLabel.GOOD,
    )
    range_side = StickRange(
        sample_count=800, duration_ms=10000.0,
        center_used_x=0.0, center_used_y=0.0,
        min_x=-85, max_x=85, min_y=-85, max_y=85,
        cardinal_reach_up=85, cardinal_reach_down=85,
        cardinal_reach_left=85, cardinal_reach_right=85,
        sectors=tuple(),
        sector_coverage_threshold_pct=0.9,
        sector_coverage_pct=0.97,
        weakest_sector_index=3, weakest_sector_pct_of_max=0.92,
        quality=SampleQuality.GOOD, label=RangeCoverageLabel.GOOD,
    )
    trig = TriggerRange(
        sample_count=150, duration_ms=6000.0,
        observed_min=1, observed_max=254, observed_travel=253,
        monotonicity_violations=0, largest_adjacent_delta=10,
        quality=SampleQuality.GOOD, label=TriggerSmoothnessLabel.SMOOTH,
    )
    return HealthReport(
        app_version="2.0.0",
        app_build_commit="abc1234",
        generated_at_local="2026-05-22T20:00:00-07:00",
        device=DeviceContext(controller_name="Test Controller",
                             configured_polling_hz=1000,
                             profile_name="Test Profile"),
        stick_rest_noise=StickRestNoiseReport(left=rest, right=rest),
        stick_range=StickRangeReport(left=range_side, right=range_side),
        trigger_range=TriggerRangeReport(left=trig, right=trig),
        overall_status=OverallStatus.NORMAL,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class PrefaceCardHeightTests(unittest.TestCase):
    """Regression for the DPG card-clip lane (2026-06-21).

    The device-info card holds 3 device-info labels + a spacer + a wrap=640
    local_only footer. It used to pin a hand-measured fixed height (160), which a
    real-viewport probe (tools/diag_dpg_card_clip.py) still found clipping the
    footer by 14px at the shipped fonts. It now fits its content via the DPG-2.x
    ``auto_resize_y`` flag (legacy fill flag suppressed), so it can't clip and
    carries no magic number. Mirrors how the Live Verify tests assert
    ``auto_resize_y``.
    """

    def test_preface_device_card_fits_content_no_fixed_height(self) -> None:
        service = _FakeService(state=HealthReportState.IDLE)
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()

        # No child_window in PREFACE state pins a fixed height anymore: the outer
        # preface card and the root container fill (legacy autosize_y) and the
        # device-info card fits its content (auto_resize_y).
        sized = [kw for kw in ps.child_windows if kw.get("height") is not None]
        self.assertEqual(
            sized, [],
            f"Preface should pin no fixed card heights now; got "
            f"{[kw.get('height') for kw in sized]}",
        )
        # The device-info card is the content-fit (auto_resize_y) child window.
        fit_cards = [
            kw for kw in ps.child_windows if kw.get("auto_resize_y") is True
        ]
        self.assertGreater(
            len(fit_cards), 0,
            "Expected a content-fit (auto_resize_y) device-info card in preface.",
        )
        for kw in fit_cards:
            self.assertFalse(
                kw.get("autosize_y", False),
                f"Device-info card must suppress the legacy fill flag: {kw}",
            )


class PrefaceRenderTests(unittest.TestCase):
    def test_preface_renders_begin_button(self) -> None:
        service = _FakeService(state=HealthReportState.IDLE)
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            button_tags = [
                kw.get("tag") for fn, _, kw in ps.calls if fn == "add_button"
            ]

        self.assertIn(screen.TAG_BEGIN_BUTTON, button_tags)

    def test_begin_button_callback_starts_test(self) -> None:
        service = _FakeService(state=HealthReportState.IDLE)
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            ps.buttons[screen.TAG_BEGIN_BUTTON]()

        self.assertEqual(service.calls, ["begin", "start_test"])
        shell.rebuild_current_screen.assert_called_once()


class ReadyForStepRenderTests(unittest.TestCase):
    """The READY_* gate renders the next step's title + instruction with a
    'Start step N' button so the user can read instructions before sampling
    begins.
    """

    def _ready_states(self) -> tuple[HealthReportState, ...]:
        return (
            HealthReportState.READY_REST,
            HealthReportState.READY_ROTATION,
            HealthReportState.READY_TRIGGER,
        )

    def test_every_ready_state_renders_start_and_cancel_buttons(self) -> None:
        for state in self._ready_states():
            with self.subTest(state=state):
                service = _FakeService(state=state)
                shell = _shell_with(service)

                with _PatchedScreen(shell) as ps:
                    ps.build()
                    buttons = list(ps.buttons.keys())
                    text_tags = [
                        kw.get("tag") for fn, _, kw in ps.calls if fn == "add_text"
                    ]

                self.assertIn(screen.TAG_READY_START_BUTTON, buttons)
                self.assertIn(screen.TAG_READY_CANCEL_BUTTON, buttons)
                self.assertIn(screen.TAG_READY_HEADING, text_tags)
                self.assertIn(screen.TAG_READY_TITLE, text_tags)
                self.assertIn(screen.TAG_READY_INSTRUCTION, text_tags)

    def test_start_button_invokes_proceed_to_step(self) -> None:
        service = _FakeService(state=HealthReportState.READY_REST)
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            ps.buttons[screen.TAG_READY_START_BUTTON]()

        self.assertEqual(service.calls, ["proceed_to_step"])
        self.assertEqual(service.state, HealthReportState.REST)
        shell.rebuild_current_screen.assert_called_once()

    def test_cancel_button_invokes_cancel(self) -> None:
        service = _FakeService(state=HealthReportState.READY_ROTATION)
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            ps.buttons[screen.TAG_READY_CANCEL_BUTTON]()

        self.assertEqual(service.calls, ["cancel"])
        self.assertEqual(service.state, HealthReportState.CANCELLED)


class StepRenderTests(unittest.TestCase):
    def test_rest_step_renders_progress_bar_and_cancel(self) -> None:
        service = _FakeService(state=HealthReportState.REST)
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            progress_tags = [
                kw.get("tag") for fn, _, kw in ps.calls if fn == "add_progress_bar"
            ]
            buttons = list(ps.buttons.keys())

        self.assertIn(screen.TAG_STEP_PROGRESS_BAR, progress_tags)
        self.assertIn(screen.TAG_STEP_CANCEL_BUTTON, buttons)

    def test_rotation_step_renders_step_title(self) -> None:
        service = _FakeService(state=HealthReportState.ROTATION)
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            text_tags = [
                kw.get("tag") for fn, _, kw in ps.calls if fn == "add_text"
            ]

        self.assertIn(screen.TAG_STEP_TITLE, text_tags)
        self.assertIn(screen.TAG_STEP_INSTRUCTION, text_tags)

    def test_trigger_step_cancel_button_invokes_cancel(self) -> None:
        service = _FakeService(state=HealthReportState.TRIGGER)
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            ps.buttons[screen.TAG_STEP_CANCEL_BUTTON]()

        self.assertEqual(service.calls, ["cancel"])
        shell.rebuild_current_screen.assert_called_once()


class SummaryRenderTests(unittest.TestCase):
    def test_summary_renders_three_tiles_and_buttons(self) -> None:
        service = _FakeService(state=HealthReportState.COMPLETE,
                                report=_fixture_report())
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            text_tags = [
                kw.get("tag") for fn, _, kw in ps.calls if fn == "add_text"
            ]
            button_tags = list(ps.buttons.keys())

        self.assertIn(screen.TAG_SUMMARY_STATUS, text_tags)
        self.assertIn(screen.TAG_SUMMARY_FOOTER, text_tags)
        for tag in (
            screen.TAG_SUMMARY_EXPORT_MD,
            screen.TAG_SUMMARY_EXPORT_JSON,
            screen.TAG_SUMMARY_COPY,
            screen.TAG_SUMMARY_RETEST,
        ):
            self.assertIn(tag, button_tags)

    def test_summary_export_markdown_writes_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _FakeService(state=HealthReportState.COMPLETE,
                                    report=_fixture_report())
            shell = _shell_with(service)
            shell.health_report_dir_override = temp_dir

            with _PatchedScreen(shell) as ps:
                ps.build()
                ps.buttons[screen.TAG_SUMMARY_EXPORT_MD]()

            files = list(Path(temp_dir).glob("*.md"))
            self.assertEqual(len(files), 1)
            text = files[0].read_text(encoding="utf-8")
            self.assertIn("Controller Health Report", text)

    def test_summary_export_json_writes_parseable_file(self) -> None:
        import json
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _FakeService(state=HealthReportState.COMPLETE,
                                    report=_fixture_report())
            shell = _shell_with(service)
            shell.health_report_dir_override = temp_dir

            with _PatchedScreen(shell) as ps:
                ps.build()
                ps.buttons[screen.TAG_SUMMARY_EXPORT_JSON]()

            files = list(Path(temp_dir).glob("*.json"))
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 2)

    def test_summary_retest_button_resets_service(self) -> None:
        service = _FakeService(state=HealthReportState.COMPLETE,
                                report=_fixture_report())
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            ps.buttons[screen.TAG_SUMMARY_RETEST]()

        self.assertIn("reset", service.calls)
        shell.rebuild_current_screen.assert_called_once()

    def test_summary_copy_button_writes_clipboard(self) -> None:
        service = _FakeService(state=HealthReportState.COMPLETE,
                                report=_fixture_report())
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            ps.buttons[screen.TAG_SUMMARY_COPY]()
            clipboard_calls = [c for c in ps.calls if c[0] == "set_clipboard_text"]

        self.assertEqual(len(clipboard_calls), 1)

    def test_summary_renders_three_comparison_tables(self) -> None:
        service = _FakeService(state=HealthReportState.COMPLETE,
                                report=_fixture_report())
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            table_calls = [c for c in ps.calls if c[0] == "table"]
            column_labels = [
                kw.get("label") for fn, _, kw in ps.calls if fn == "add_table_column"
            ]
            text_values = [a[0] for fn, a, _ in ps.calls if fn == "add_text" and a]

        # One Left/Right comparison table per metric card: rest / range / trigger.
        self.assertEqual(len(table_calls), 3)
        # Each table carries exactly the three comparison columns (3 × 3 = 9).
        self.assertEqual(len(column_labels), 9)
        self.assertEqual(column_labels.count(t("health_report.table.col.metric")), 3)
        self.assertEqual(column_labels.count(t("health_report.table.col.left")), 3)
        self.assertEqual(column_labels.count(t("health_report.table.col.right")), 3)
        # Fixture values flow into the cells (trigger observed max=254;
        # range sector coverage 0.97 -> "97.0%").
        self.assertIn("254", text_values)
        self.assertIn("97.0%", text_values)


class CancelledRenderTests(unittest.TestCase):
    def test_cancelled_state_renders_retest_button(self) -> None:
        service = _FakeService(state=HealthReportState.CANCELLED)
        shell = _shell_with(service)

        with _PatchedScreen(shell) as ps:
            ps.build()
            text_tags = [
                kw.get("tag") for fn, _, kw in ps.calls if fn == "add_text"
            ]
            buttons = list(ps.buttons.keys())

        self.assertIn(screen.TAG_CANCELLED_TITLE, text_tags)
        self.assertIn(screen.TAG_SUMMARY_RETEST, buttons)


if __name__ == "__main__":
    unittest.main()
