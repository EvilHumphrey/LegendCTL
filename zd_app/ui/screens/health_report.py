"""Controller Health Check screen.

Wizard flow (the recommended 3-step wizard):

    PREFACE  -> describes the check, lists controller + configured polling.
    REST     -> step 1, 5 s untouched controller.
    ROTATION -> step 2, 10 s stick rotation.
    TRIGGER  -> step 3, 6 s trigger pull/release.
    COMPLETE -> 4 summary tiles + export buttons + retest.

A self-rescheduling DPG frame callback drives ``service.tick()`` and refreshes
the live sample counter. The callback throttles itself to
``_REFRESH_FRAME_INTERVAL`` frames (~30 Hz) so the UI doesn't fight the
sample collector for cycles.

The build function is intentionally re-buildable on every state transition:
once the orchestrator moves from PREFACE to REST (or any other transition
that changes the layout), the shell tears down the screen contents and calls
``build`` again. That keeps the rendering logic state-pure rather than
juggling show/hide on a static layout.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import dearpygui.dearpygui as dpg

from zd_app.i18n import t
from zd_app.services.health_report import (
    HealthReport,
    HealthReportService,
    HealthReportState,
    OverallStatus,
    RangeCoverageLabel,
    RestNoiseLabel,
    TriggerSmoothnessLabel,
    to_json,
    to_markdown,
)
from zd_app.ui.components import Column, card, metric, right_cell, section, table
from zd_app.ui.themes import section_gap
from zd_app.ui.typography import helper_text, screen_title, section_title


logger = logging.getLogger(__name__)


# UI tags — flat namespace so refresh / tests can address them by name.
TAG_BEGIN_BUTTON = "health_report_begin_button"
TAG_READY_HEADING = "health_report_ready_heading"
TAG_READY_TITLE = "health_report_ready_title"
TAG_READY_INSTRUCTION = "health_report_ready_instruction"
TAG_READY_START_BUTTON = "health_report_ready_start_button"
TAG_READY_CANCEL_BUTTON = "health_report_ready_cancel_button"
TAG_STEP_TITLE = "health_report_step_title"
TAG_STEP_INSTRUCTION = "health_report_step_instruction"
TAG_STEP_SAMPLES = "health_report_step_samples"
TAG_STEP_REMAINING = "health_report_step_remaining"
TAG_STEP_PROGRESS_BAR = "health_report_step_progress_bar"
TAG_STEP_CANCEL_BUTTON = "health_report_step_cancel_button"
TAG_SUMMARY_STATUS = "health_report_summary_status"
TAG_SUMMARY_TILE_REST = "health_report_summary_tile_rest"
TAG_SUMMARY_TILE_RANGE = "health_report_summary_tile_range"
TAG_SUMMARY_TILE_TRIGGER = "health_report_summary_tile_trigger"
TAG_SUMMARY_EXPORT_MD = "health_report_summary_export_md"
TAG_SUMMARY_EXPORT_JSON = "health_report_summary_export_json"
TAG_SUMMARY_COPY = "health_report_summary_copy"
TAG_SUMMARY_RETEST = "health_report_summary_retest"
TAG_SUMMARY_FOOTER = "health_report_summary_footer"
TAG_EXPORT_STATUS = "health_report_export_status"
TAG_CANCELLED_TITLE = "health_report_cancelled_title"
TAG_ROOT_CONTAINER = "health_report_root"


# Frame-tick cadence for state-machine refresh. DPG's frame_callback fires
# at the requested frame; we re-register every N frames. 2 keeps the refresh
# rate near 30 Hz on a 60 fps render target.
_REFRESH_FRAME_INTERVAL = 2

# Max width for the sparse entry views (preface / ready / cancelled). The
# summary view intentionally stretches its comparison tables full-width, so
# this only bounds the text-and-button entry states, where the content would
# otherwise strand in the wide content region. 720 fits the 1180px min-window
# content area. The wrapping card carries no fixed height, so it does not count
# against the preface's "exactly one sized child_window" invariant.
_CONTENT_MAX_WIDTH = 720


# ---------------------------------------------------------------------------
# Top-level build
# ---------------------------------------------------------------------------


def build(shell, parent: str) -> None:
    """Render the health-check screen for ``shell.health_report_service.state``.

    The shell is responsible for tearing down the previous render before
    calling ``build`` again on a state change. The build function therefore
    only handles the *current* state's layout.
    """

    service: HealthReportService = shell.health_report_service
    state = service.state

    # autosize_y but NOT autosize_x: the container fills the content region's
    # width so the summary's width=-1 cards/tables stretch to fill (and shrink
    # with the window) instead of collapsing against a shrink-to-fit parent.
    # Mirrors the Home screen's flex layout.
    with dpg.child_window(
        parent=parent,
        tag=TAG_ROOT_CONTAINER,
        autosize_y=True,
        border=False,
    ):
        if state == HealthReportState.IDLE or state == HealthReportState.PREFACE:
            _build_preface(shell, service)
        elif state in _READY_NEXT_STEP:
            _build_ready_for_step(shell, service, _READY_NEXT_STEP[state])
        elif state in (
            HealthReportState.REST,
            HealthReportState.ROTATION,
            HealthReportState.TRIGGER,
        ):
            _build_step(shell, service, state)
        elif state == HealthReportState.COMPLETE:
            _build_summary(shell, service)
        elif state == HealthReportState.CANCELLED:
            _build_cancelled(shell, service)
        else:
            dpg.add_text(f"Unexpected health report state: {state.value}")

    # Live refresh: tick the service on a frame timer so the state machine
    # progresses without the user clicking anything. No-op when state has
    # no duration (IDLE / PREFACE / COMPLETE / CANCELLED).
    _schedule_next_refresh(shell)


# ---------------------------------------------------------------------------
# Preface
# ---------------------------------------------------------------------------


def _build_preface(shell, service: HealthReportService) -> None:
    device = service._device_context_provider()
    screen_title(t("health_report.preface.title"))
    dpg.add_spacer(height=8)

    controller_label = device.controller_name or t("health_report.preface.unknown_value")
    polling_label = (
        f"{device.configured_polling_hz} Hz"
        if device.configured_polling_hz
        else t("health_report.preface.unknown_value")
    )
    profile_label = device.profile_name or t("health_report.preface.no_profile")

    # Bound the entry content in a max-width card so the body / device-info /
    # begin button form an intentional panel instead of stranding in the wide
    # content region. The card carries no fixed height (autosize_y), so the
    # device-info child below stays the only height-sized child_window.
    with card(width=_CONTENT_MAX_WIDTH):
        dpg.add_text(t("health_report.preface.body"), wrap=660)
        dpg.add_spacer(height=10)

        # Device-info card fills the bounded panel width. 3 device labels + a
        # spacer + the local_only footer (wrap=640). Fit to content (DPG-2.x
        # auto_resize_y, legacy fill flag suppressed) rather than a fixed height:
        # the prior 160 still clipped the footer by 14px at the shipped fonts
        # (tools/diag_dpg_card_clip.py). Fit can't clip and needs no magic number.
        with dpg.child_window(
            width=-1, border=True, auto_resize_y=True, autosize_y=False
        ):
            dpg.add_text(
                f"{t('health_report.preface.label.controller')} {controller_label}"
            )
            dpg.add_text(
                f"{t('health_report.preface.label.polling')} {polling_label}"
            )
            dpg.add_text(
                f"{t('health_report.preface.label.profile')} {profile_label}"
            )
            dpg.add_spacer(height=6)
            dpg.add_text(
                t("health_report.preface.label.local_only"),
                color=shell.COLORS["muted"],
                wrap=640,
            )

        dpg.add_spacer(height=12)
        dpg.add_button(
            label=t("health_report.preface.begin_button"),
            tag=TAG_BEGIN_BUTTON,
            width=240,
            height=36,
            callback=lambda: _on_begin(shell, service),
        )


# ---------------------------------------------------------------------------
# Ready-for-step gate (READY_REST / READY_ROTATION / READY_TRIGGER)
# ---------------------------------------------------------------------------


_READY_NEXT_STEP: dict[HealthReportState, HealthReportState] = {
    HealthReportState.READY_REST: HealthReportState.REST,
    HealthReportState.READY_ROTATION: HealthReportState.ROTATION,
    HealthReportState.READY_TRIGGER: HealthReportState.TRIGGER,
}

# Per-step number used in the "Start step {step_n}" button label.
_STEP_NUMBER: dict[HealthReportState, int] = {
    HealthReportState.REST: 1,
    HealthReportState.ROTATION: 2,
    HealthReportState.TRIGGER: 3,
}


def _build_ready_for_step(
    shell, service: HealthReportService, next_step: HealthReportState
) -> None:
    """Render the user-paced gate that precedes each sampling step.

    The screen reuses the existing per-step title + instruction copy so the
    user sees the same instructions they'll be following once sampling
    starts; the only addition is a "ready when you are" heading and a
    "Start step N" button that calls :meth:`proceed_to_step`.
    """

    section_title(
        t("health_report.ready.heading"),
        tag=TAG_READY_HEADING,
    )
    dpg.add_spacer(height=6)
    section_title(
        t(_STEP_TITLE_KEYS[next_step]),
        tag=TAG_READY_TITLE,
    )
    dpg.add_spacer(height=8)
    dpg.add_text(
        t(_STEP_INSTRUCTION_KEYS[next_step]),
        wrap=900,
        tag=TAG_READY_INSTRUCTION,
    )
    dpg.add_spacer(height=14)
    dpg.add_button(
        label=t(
            "health_report.ready.start_button",
            step_n=_STEP_NUMBER[next_step],
        ),
        tag=TAG_READY_START_BUTTON,
        width=240,
        height=36,
        callback=lambda: _on_proceed_to_step(shell, service),
    )
    dpg.add_spacer(height=10)
    dpg.add_button(
        label=t("health_report.step.cancel_button"),
        tag=TAG_READY_CANCEL_BUTTON,
        width=200,
        callback=lambda: _on_cancel(shell, service),
    )


# ---------------------------------------------------------------------------
# Step (REST / ROTATION / TRIGGER)
# ---------------------------------------------------------------------------


_STEP_TITLE_KEYS = {
    HealthReportState.REST: "health_report.step.rest.title",
    HealthReportState.ROTATION: "health_report.step.rotation.title",
    HealthReportState.TRIGGER: "health_report.step.trigger.title",
}

_STEP_INSTRUCTION_KEYS = {
    HealthReportState.REST: "health_report.step.rest.instruction",
    HealthReportState.ROTATION: "health_report.step.rotation.instruction",
    HealthReportState.TRIGGER: "health_report.step.trigger.instruction",
}


def _build_step(shell, service: HealthReportService, state: HealthReportState) -> None:
    section_title(t(_STEP_TITLE_KEYS[state]), tag=TAG_STEP_TITLE)
    dpg.add_spacer(height=8)
    dpg.add_text(t(_STEP_INSTRUCTION_KEYS[state]), wrap=900, tag=TAG_STEP_INSTRUCTION)
    dpg.add_spacer(height=14)

    progress = service.step_progress()
    dpg.add_progress_bar(
        default_value=progress.fraction,
        width=520,
        tag=TAG_STEP_PROGRESS_BAR,
    )
    dpg.add_spacer(height=6)
    dpg.add_text(
        t("health_report.step.remaining_label", seconds=f"{progress.remaining_s:.1f}"),
        color=shell.COLORS["muted"],
        tag=TAG_STEP_REMAINING,
    )
    dpg.add_text(
        t("health_report.step.progress_label", n=progress.samples_collected),
        color=shell.COLORS["muted"],
        tag=TAG_STEP_SAMPLES,
    )
    dpg.add_spacer(height=14)
    dpg.add_button(
        label=t("health_report.step.cancel_button"),
        tag=TAG_STEP_CANCEL_BUTTON,
        width=200,
        callback=lambda: _on_cancel(shell, service),
    )


# ---------------------------------------------------------------------------
# Summary (COMPLETE)
# ---------------------------------------------------------------------------


_OVERALL_STATUS_I18N = {
    OverallStatus.NORMAL: "health_report.summary.overall_status.normal",
    OverallStatus.TUNING_SUGGESTED: "health_report.summary.overall_status.tuning_suggested",
    OverallStatus.RETEST_RECOMMENDED: "health_report.summary.overall_status.retest_recommended",
    OverallStatus.POSSIBLE_ISSUE: "health_report.summary.overall_status.possible_issue",
}

# Semantic status color for the overall-status line (a status indicator, not
# the actionable accent green — that stays reserved for buttons). Keys index
# shell.COLORS (legacy aliases); a missing key degrades to the default text
# color rather than raising.
_OVERALL_STATUS_COLOR_KEY = {
    OverallStatus.NORMAL: "good",
    OverallStatus.TUNING_SUGGESTED: "warn",
    OverallStatus.RETEST_RECOMMENDED: "warn",
    OverallStatus.POSSIBLE_ISSUE: "bad",
}

_REST_LABEL_I18N = {
    RestNoiseLabel.GOOD: "health_report.label.rest.good",
    RestNoiseLabel.NOTICEABLE: "health_report.label.rest.noticeable",
    RestNoiseLabel.HIGH: "health_report.label.rest.high",
    RestNoiseLabel.INSUFFICIENT: "health_report.label.rest.insufficient",
}

_RANGE_LABEL_I18N = {
    RangeCoverageLabel.GOOD: "health_report.label.range.good",
    RangeCoverageLabel.LIMITED: "health_report.label.range.limited",
    RangeCoverageLabel.UNEVEN: "health_report.label.range.uneven",
    RangeCoverageLabel.RETEST: "health_report.label.range.retest",
    RangeCoverageLabel.INSUFFICIENT: "health_report.label.range.insufficient",
}

_TRIGGER_LABEL_I18N = {
    TriggerSmoothnessLabel.SMOOTH: "health_report.label.trigger.smooth",
    TriggerSmoothnessLabel.STEPPED: "health_report.label.trigger.stepped",
    TriggerSmoothnessLabel.NOISY: "health_report.label.trigger.noisy",
    TriggerSmoothnessLabel.LIMITED: "health_report.label.trigger.limited",
    TriggerSmoothnessLabel.INSUFFICIENT: "health_report.label.trigger.insufficient",
}


def _build_summary(shell, service: HealthReportService) -> None:
    report = service.report
    if report is None:
        screen_title(t("health_report.cancelled.title"))
        return

    screen_title(t("health_report.summary.title"))
    dpg.add_spacer(height=6)
    _build_overall_status(shell, report)
    section_gap()

    # Three result cards, each a section() heading over a Left-vs-Right
    # comparison table. The report has exactly these three sections — rest
    # noise, range coverage, trigger travel — and no cadence.
    _build_rest_card(shell, report)
    section_gap()
    _build_range_card(shell, report)
    section_gap()
    _build_trigger_card(shell, report)

    section_gap()
    with dpg.group(horizontal=True):
        dpg.add_button(
            label=t("health_report.summary.export.markdown"),
            tag=TAG_SUMMARY_EXPORT_MD,
            width=210,
            callback=lambda: _on_export_markdown(shell, service),
        )
        dpg.add_button(
            label=t("health_report.summary.export.json"),
            tag=TAG_SUMMARY_EXPORT_JSON,
            width=210,
            callback=lambda: _on_export_json(shell, service),
        )
        dpg.add_button(
            label=t("health_report.summary.export.copy_summary"),
            tag=TAG_SUMMARY_COPY,
            width=180,
            callback=lambda: _on_copy_summary(shell, service),
        )
        dpg.add_button(
            label=t("health_report.summary.retest_button"),
            tag=TAG_SUMMARY_RETEST,
            width=200,
            callback=lambda: _on_retest(shell, service),
        )

    dpg.add_spacer(height=6)
    dpg.add_text("", tag=TAG_EXPORT_STATUS, color=shell.COLORS["muted"])
    dpg.add_spacer(height=12)
    # Load-bearing honesty footer — kept verbatim (i18n key unchanged).
    dpg.add_text(
        t("health_report.summary.footer_caveat"),
        tag=TAG_SUMMARY_FOOTER,
        color=shell.COLORS["muted"],
        wrap=900,
    )


def _build_overall_status(shell, report: HealthReport) -> None:
    """Overall-status headline.

    The text is the load-bearing honesty verdict and is kept verbatim via its
    i18n key; only a semantic status color is layered on so the verdict is
    scannable at a glance. Accent green stays reserved for buttons.
    """

    text_kwargs: dict = {"tag": TAG_SUMMARY_STATUS, "wrap": 900}
    color_key = _OVERALL_STATUS_COLOR_KEY.get(report.overall_status)
    color = shell.COLORS.get(color_key) if color_key else None
    if color is not None:
        text_kwargs["color"] = color
    dpg.add_text(t(_OVERALL_STATUS_I18N[report.overall_status]), **text_kwargs)


def _build_rest_card(shell, report: HealthReport) -> None:
    left = report.stick_rest_noise.left
    right = report.stick_rest_noise.right
    with section(
        t("health_report.summary.tile.rest_noise.title"),
        card=True,
        tag=TAG_SUMMARY_TILE_REST,
    ):
        _comparison_table(
            shell,
            (
                (
                    t("health_report.table.assessment"),
                    t(_REST_LABEL_I18N[left.label]),
                    t(_REST_LABEL_I18N[right.label]),
                ),
                (
                    t("health_report.table.rest.suggested_deadzone"),
                    str(left.suggested_deadzone_from_sample),
                    str(right.suggested_deadzone_from_sample),
                ),
                (
                    t("health_report.table.rest.center_offset"),
                    _format_xy(left.median_center_x, left.median_center_y),
                    _format_xy(right.median_center_x, right.median_center_y),
                ),
                (
                    t("health_report.table.rest.noise_p95"),
                    f"{left.p95_r:.1f}",
                    f"{right.p95_r:.1f}",
                ),
                (
                    t("health_report.table.rest.noise_max"),
                    f"{left.max_r:.1f}",
                    f"{right.max_r:.1f}",
                ),
            ),
        )
        _build_samples_note(shell, left.sample_count)
        helper_text(t("health_report.table.rest.caption"), wrap=700)


def _build_range_card(shell, report: HealthReport) -> None:
    left = report.stick_range.left
    right = report.stick_range.right
    with section(
        t("health_report.summary.tile.range.title"),
        card=True,
        tag=TAG_SUMMARY_TILE_RANGE,
    ):
        _comparison_table(
            shell,
            (
                (
                    t("health_report.table.assessment"),
                    t(_RANGE_LABEL_I18N[left.label]),
                    t(_RANGE_LABEL_I18N[right.label]),
                ),
                (
                    t("health_report.table.range.sector_coverage"),
                    _format_pct(left.sector_coverage_pct),
                    _format_pct(right.sector_coverage_pct),
                ),
                (
                    t("health_report.table.range.weakest_sector"),
                    _format_weakest_sector(left),
                    _format_weakest_sector(right),
                ),
                (
                    t("health_report.table.range.range_x"),
                    f"{left.min_x} / {left.max_x}",
                    f"{right.min_x} / {right.max_x}",
                ),
                (
                    t("health_report.table.range.range_y"),
                    f"{left.min_y} / {left.max_y}",
                    f"{right.min_y} / {right.max_y}",
                ),
            ),
        )
        _build_samples_note(shell, left.sample_count)
        helper_text(t("health_report.summary.tile.range.caption"), wrap=700)


def _build_trigger_card(shell, report: HealthReport) -> None:
    left = report.trigger_range.left
    right = report.trigger_range.right
    with section(
        t("health_report.summary.tile.trigger.title"),
        card=True,
        tag=TAG_SUMMARY_TILE_TRIGGER,
    ):
        _comparison_table(
            shell,
            (
                (
                    t("health_report.table.assessment"),
                    t(_TRIGGER_LABEL_I18N[left.label]),
                    t(_TRIGGER_LABEL_I18N[right.label]),
                ),
                (
                    t("health_report.table.trigger.observed_min"),
                    str(left.observed_min),
                    str(right.observed_min),
                ),
                (
                    t("health_report.table.trigger.observed_max"),
                    str(left.observed_max),
                    str(right.observed_max),
                ),
                (
                    t("health_report.table.trigger.travel"),
                    str(left.observed_travel),
                    str(right.observed_travel),
                ),
                (
                    t("health_report.table.trigger.largest_delta"),
                    str(left.largest_adjacent_delta),
                    str(right.largest_adjacent_delta),
                ),
                (
                    t("health_report.table.trigger.monotonicity"),
                    str(left.monotonicity_violations),
                    str(right.monotonicity_violations),
                ),
            ),
        )
        _build_samples_note(shell, left.sample_count)
        helper_text(t("health_report.summary.tile.trigger.caption"), wrap=700)


def _comparison_table(shell, rows) -> None:
    """Render a Metric | Left | Right comparison table.

    Routed through the shared :func:`~zd_app.ui.components.table` builder so the
    report tables pick up the app's standard header / zebra / padding chrome.
    The metric-name column is muted; the two value columns are numeric, rendered
    through :func:`~zd_app.ui.components.right_cell` (primary text) so the
    left-vs-right comparison reads cleanly. ``rows`` is an iterable of
    ``(metric_label, left_value, right_value)`` string tuples.
    """

    with table(
        [
            Column(t("health_report.table.col.metric"), weight=2.0),
            Column(t("health_report.table.col.left"), weight=1.0, numeric=True),
            Column(t("health_report.table.col.right"), weight=1.0, numeric=True),
        ]
    ):
        for metric_label, left_value, right_value in rows:
            with dpg.table_row():
                dpg.add_text(metric_label, color=shell.COLORS["muted"])
                right_cell(left_value)
                right_cell(right_value)


def _build_samples_note(shell, sample_count: int) -> None:
    """Muted sample-backing line — how much data backs the table above.

    Left and right are computed from the same captured window, so the count is
    shared. Rendered with :func:`metric` so it picks up the standard
    label/value styling.
    """

    metric(
        t("health_report.table.samples"),
        sample_count,
        value_color=shell.COLORS["muted"],
    )


def _format_xy(x: float, y: float) -> str:
    return f"({x:.1f}, {y:.1f})"


def _format_pct(fraction: float) -> str:
    return f"{fraction * 100:.1f}%"


def _format_weakest_sector(side) -> str:
    if side.weakest_sector_index is None or side.weakest_sector_pct_of_max is None:
        return "-"
    return f"#{side.weakest_sector_index} ({side.weakest_sector_pct_of_max * 100:.0f}%)"


# ---------------------------------------------------------------------------
# Cancelled
# ---------------------------------------------------------------------------


def _build_cancelled(shell, service: HealthReportService) -> None:
    screen_title(
        t("health_report.cancelled.title"),
        tag=TAG_CANCELLED_TITLE,
    )
    dpg.add_spacer(height=8)
    dpg.add_text(t("health_report.cancelled.body"), wrap=900)
    dpg.add_spacer(height=14)
    dpg.add_button(
        label=t("health_report.summary.retest_button"),
        tag=TAG_SUMMARY_RETEST,
        width=240,
        callback=lambda: _on_retest(shell, service),
    )


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


def _on_begin(shell, service: HealthReportService) -> None:
    if service.state == HealthReportState.IDLE:
        service.begin()
        service.start_test()
        shell.rebuild_current_screen()
    elif service.state == HealthReportState.PREFACE:
        service.start_test()
        shell.rebuild_current_screen()


def _on_proceed_to_step(shell, service: HealthReportService) -> None:
    service.proceed_to_step()
    shell.rebuild_current_screen()


def _on_cancel(shell, service: HealthReportService) -> None:
    service.cancel()
    shell.rebuild_current_screen()


def _on_retest(shell, service: HealthReportService) -> None:
    service.reset()
    shell.rebuild_current_screen()


def _on_export_markdown(shell, service: HealthReportService) -> None:
    report = service.report
    if report is None:
        return
    try:
        path = _save_report(shell, report, suffix=".md", content=to_markdown(report))
        dpg.set_value(TAG_EXPORT_STATUS, t("health_report.summary.export.success", path=str(path)))
    except OSError as exc:
        logger.exception("Health Report Markdown export failed")
        dpg.set_value(TAG_EXPORT_STATUS, t("health_report.summary.export.failure", reason=str(exc)))


def _on_export_json(shell, service: HealthReportService) -> None:
    report = service.report
    if report is None:
        return
    try:
        path = _save_report(shell, report, suffix=".json", content=to_json(report))
        dpg.set_value(TAG_EXPORT_STATUS, t("health_report.summary.export.success", path=str(path)))
    except OSError as exc:
        logger.exception("Health Report JSON export failed")
        dpg.set_value(TAG_EXPORT_STATUS, t("health_report.summary.export.failure", reason=str(exc)))


def _on_copy_summary(shell, service: HealthReportService) -> None:
    report = service.report
    if report is None:
        return
    try:
        dpg.set_clipboard_text(to_markdown(report))
        dpg.set_value(TAG_EXPORT_STATUS, t("health_report.summary.copy_success"))
    except Exception as exc:  # noqa: BLE001 — clipboard backends differ
        logger.exception("Health Report copy-summary failed")
        dpg.set_value(TAG_EXPORT_STATUS, t("health_report.summary.export.failure", reason=str(exc)))


def _save_report(shell, report, *, suffix: str, content: str) -> Path:
    base_dir = _user_health_report_dir(shell)
    base_dir.mkdir(parents=True, exist_ok=True)
    filename = f"zd_health_report_{time.strftime('%Y-%m-%d_%H%M%S')}{suffix}"
    target = base_dir / filename
    target.write_text(content, encoding="utf-8")
    return target


def _user_health_report_dir(shell) -> Path:
    """Resolve the per-user directory where Health Reports go.

    Reuses ``initialize_user_data_dir`` so this lives next to the existing
    ``crashes/`` and ``settings.json``. If the shell exposes a different
    explicit override, prefer it (test hook).
    """

    override = getattr(shell, "health_report_dir_override", None)
    if override:
        return Path(override)
    # Imported here so the screen module stays light at import time.
    from zd_app.storage.settings_store import _default_user_data_dir
    return _default_user_data_dir() / "health_reports"


# ---------------------------------------------------------------------------
# Frame refresh
# ---------------------------------------------------------------------------


def _schedule_next_refresh(shell) -> None:
    """Register a frame callback that ticks the service + refreshes labels.

    Mirrors the diagnostics XInput panel pattern: chain self-rescheduling
    frame callbacks until the screen widgets are torn down (does_item_exist
    returns False on the root container), then stop.
    """

    try:
        next_frame = dpg.get_frame_count() + _REFRESH_FRAME_INTERVAL
    except SystemError:
        return

    def _tick(*_args) -> None:
        if not dpg.does_item_exist(TAG_ROOT_CONTAINER):
            return  # screen torn down; stop the chain
        service: HealthReportService = shell.health_report_service
        prior_state = service.state
        try:
            service.tick()
        except Exception:  # noqa: BLE001 — never crash the frame
            logger.exception("HealthReportService.tick() raised")

        if service.state != prior_state:
            # State transition: rebuild the screen so the new layout shows.
            shell.rebuild_current_screen()
            return

        _refresh_step_widgets(service)
        _schedule_next_refresh(shell)

    try:
        dpg.set_frame_callback(next_frame, _tick)
    except SystemError:
        return


def _refresh_step_widgets(service: HealthReportService) -> None:
    """Update the active step's progress widgets without rebuilding the screen.

    No-op when the current state isn't a step (PREFACE / COMPLETE / etc.)
    or when the screen has been torn down (``does_item_exist`` False).
    """

    if service.state not in (
        HealthReportState.REST,
        HealthReportState.ROTATION,
        HealthReportState.TRIGGER,
    ):
        return
    if not dpg.does_item_exist(TAG_STEP_PROGRESS_BAR):
        return
    progress = service.step_progress()
    dpg.set_value(TAG_STEP_PROGRESS_BAR, progress.fraction)
    if dpg.does_item_exist(TAG_STEP_REMAINING):
        dpg.set_value(
            TAG_STEP_REMAINING,
            t("health_report.step.remaining_label", seconds=f"{progress.remaining_s:.1f}"),
        )
    if dpg.does_item_exist(TAG_STEP_SAMPLES):
        dpg.set_value(
            TAG_STEP_SAMPLES,
            t("health_report.step.progress_label", n=progress.samples_collected),
        )


__all__ = ["build"]
