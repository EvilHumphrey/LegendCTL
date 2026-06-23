"""Readiness Check (quick mode) screen.

UI surface for the 20-second pre-match ritual. Three rendered states:

    INIT     -> intro card + "Run readiness check (20s)" button.
    RUNNING  -> progress bar + "Phase X of 3" label + sample counter.
    DONE     -> verdict card (status, observations, "Run again" + link to
                full Health Report).

The screen reuses the same self-rescheduling frame-callback pattern as the
full Health Report screen (``shell.quick_check_service.tick()`` from a DPG
frame timer; rebuild on state change).
"""

from __future__ import annotations

import logging
from typing import Callable

import dearpygui.dearpygui as dpg

from zd_app.i18n import t
from zd_app.services.health_report import (
    QuickCheckPhase,
    QuickCheckService,
    QuickCheckState,
    ReadinessStatus,
    ReadinessVerdict,
)
from zd_app.ui.typography import screen_title


logger = logging.getLogger(__name__)


# UI tags — flat namespace so refresh / tests can address them by name.
TAG_ROOT_CONTAINER = "readiness_check_root"
TAG_INIT_TITLE = "readiness_check_init_title"
TAG_INIT_BODY = "readiness_check_init_body"
TAG_RUN_BUTTON = "readiness_check_run_button"
TAG_RUNNING_TITLE = "readiness_check_running_title"
TAG_RUNNING_PHASE = "readiness_check_running_phase"
TAG_RUNNING_PROGRESS_BAR = "readiness_check_running_progress_bar"
TAG_RUNNING_REMAINING = "readiness_check_running_remaining"
TAG_RUNNING_SAMPLES = "readiness_check_running_samples"
TAG_RUNNING_CANCEL_BUTTON = "readiness_check_running_cancel_button"
TAG_DONE_STATUS = "readiness_check_done_status"
TAG_DONE_VERDICT_CARD = "readiness_check_done_verdict_card"
TAG_DONE_RUN_AGAIN_BUTTON = "readiness_check_done_run_again_button"
TAG_DONE_OPEN_FULL_BUTTON = "readiness_check_done_open_full_button"
TAG_DONE_FOOTER = "readiness_check_done_footer"
TAG_CANCELLED_TITLE = "readiness_check_cancelled_title"
TAG_CANCELLED_RESET_BUTTON = "readiness_check_cancelled_reset_button"


# Frame-tick cadence; mirrors the full Health Report screen (~30 Hz on 60 fps).
_REFRESH_FRAME_INTERVAL = 2


_STATUS_I18N = {
    ReadinessStatus.GREEN: "readiness_check.status.green",
    ReadinessStatus.YELLOW: "readiness_check.status.yellow",
    ReadinessStatus.RED: "readiness_check.status.red",
}

_PHASE_I18N = {
    QuickCheckPhase.REST: "readiness_check.phase.rest",
    QuickCheckPhase.RANGE: "readiness_check.phase.range",
    QuickCheckPhase.TRIGGER: "readiness_check.phase.trigger",
}

# Phase number for the "Phase {n} of 3 — {label}" line.
_PHASE_NUMBER = {
    QuickCheckPhase.REST: 1,
    QuickCheckPhase.RANGE: 2,
    QuickCheckPhase.TRIGGER: 3,
}


# ---------------------------------------------------------------------------
# Top-level build
# ---------------------------------------------------------------------------


def build(shell, parent: str) -> None:
    """Render the readiness-check screen for the current service state."""

    service: QuickCheckService = shell.quick_check_service
    state = service.state

    with dpg.child_window(
        parent=parent,
        tag=TAG_ROOT_CONTAINER,
        autosize_x=True,
        autosize_y=True,
        border=False,
    ):
        if state == QuickCheckState.IDLE:
            _build_init(shell, service)
        elif state in (
            QuickCheckState.REST,
            QuickCheckState.RANGE,
            QuickCheckState.TRIGGER,
        ):
            _build_running(shell, service)
        elif state == QuickCheckState.COMPLETE:
            _build_done(shell, service)
        elif state == QuickCheckState.CANCELLED:
            _build_cancelled(shell, service)
        else:
            dpg.add_text(f"Unexpected readiness check state: {state.value}")

    _schedule_next_refresh(shell)


# ---------------------------------------------------------------------------
# INIT
# ---------------------------------------------------------------------------


def _build_init(shell, service: QuickCheckService) -> None:
    screen_title(
        t("readiness_check.init.title"),
        tag=TAG_INIT_TITLE,
    )
    dpg.add_spacer(height=8)
    dpg.add_text(
        t("readiness_check.init.body"),
        wrap=900,
        tag=TAG_INIT_BODY,
    )
    dpg.add_spacer(height=14)
    dpg.add_button(
        label=t("readiness_check.init.run_button"),
        tag=TAG_RUN_BUTTON,
        width=300,
        height=40,
        callback=lambda: _on_run(shell, service),
    )


# ---------------------------------------------------------------------------
# RUNNING
# ---------------------------------------------------------------------------


def _build_running(shell, service: QuickCheckService) -> None:
    screen_title(
        t("readiness_check.running.title"),
        tag=TAG_RUNNING_TITLE,
    )
    dpg.add_spacer(height=10)

    progress = service.progress()
    phase = progress.phase
    phase_label = (
        t(
            "readiness_check.running.phase_label",
            n=_PHASE_NUMBER[phase],
            phase=t(_PHASE_I18N[phase]),
        )
        if phase is not None
        else ""
    )
    dpg.add_text(phase_label, tag=TAG_RUNNING_PHASE)
    dpg.add_spacer(height=8)

    dpg.add_progress_bar(
        default_value=progress.fraction,
        width=520,
        tag=TAG_RUNNING_PROGRESS_BAR,
    )
    dpg.add_spacer(height=6)
    dpg.add_text(
        t(
            "readiness_check.running.remaining_label",
            seconds=f"{progress.remaining_s:.1f}",
        ),
        color=shell.COLORS["muted"],
        tag=TAG_RUNNING_REMAINING,
    )
    dpg.add_text(
        t(
            "readiness_check.running.samples_label",
            n=progress.samples_collected,
        ),
        color=shell.COLORS["muted"],
        tag=TAG_RUNNING_SAMPLES,
    )
    dpg.add_spacer(height=14)
    dpg.add_button(
        label=t("readiness_check.running.cancel_button"),
        tag=TAG_RUNNING_CANCEL_BUTTON,
        width=200,
        callback=lambda: _on_cancel(shell, service),
    )


# ---------------------------------------------------------------------------
# DONE
# ---------------------------------------------------------------------------


def _build_done(shell, service: QuickCheckService) -> None:
    verdict = service.verdict
    if verdict is None:
        screen_title(t("readiness_check.cancelled.title"))
        return

    screen_title(
        t("readiness_check.done.title"),
    )
    dpg.add_spacer(height=8)

    _build_verdict_card(shell, verdict)

    dpg.add_spacer(height=14)
    with dpg.group(horizontal=True):
        dpg.add_button(
            label=t("readiness_check.done.run_again_button"),
            tag=TAG_DONE_RUN_AGAIN_BUTTON,
            width=200,
            callback=lambda: _on_run_again(shell, service),
        )
        dpg.add_button(
            label=t("readiness_check.done.open_full_button"),
            tag=TAG_DONE_OPEN_FULL_BUTTON,
            width=240,
            callback=lambda: _on_open_full_health_report(shell, service),
        )
    dpg.add_spacer(height=12)
    dpg.add_text(
        t("readiness_check.done.footer"),
        tag=TAG_DONE_FOOTER,
        color=shell.COLORS["muted"],
        wrap=900,
    )


def _build_verdict_card(shell, verdict: ReadinessVerdict) -> None:
    # Fit to content height (DPG-2.x content-fit): the card holds a status line
    # plus up to 4 observation bullets (the cap in quick_check.py:584), each able
    # to wrap at the 480-wide budget. The old fixed 200px clipped the full
    # 4-bullet RED case, so shrink-to-fit instead. Keep the 520 width so the
    # bullet wrap budget is unchanged.
    with dpg.child_window(
        width=520,
        auto_resize_y=True,
        autosize_y=False,
        border=True,
        tag=TAG_DONE_VERDICT_CARD,
    ):
        dpg.add_text(
            t(_STATUS_I18N[verdict.status]),
            color=shell.COLORS["accent"],
            tag=TAG_DONE_STATUS,
        )
        dpg.add_spacer(height=8)
        for obs_key in verdict.observations:
            dpg.add_text(
                f"  - {t(obs_key)}",
                wrap=480,
            )


# ---------------------------------------------------------------------------
# CANCELLED
# ---------------------------------------------------------------------------


def _build_cancelled(shell, service: QuickCheckService) -> None:
    screen_title(
        t("readiness_check.cancelled.title"),
        tag=TAG_CANCELLED_TITLE,
    )
    dpg.add_spacer(height=8)
    dpg.add_text(t("readiness_check.cancelled.body"), wrap=900)
    dpg.add_spacer(height=14)
    dpg.add_button(
        label=t("readiness_check.cancelled.reset_button"),
        tag=TAG_CANCELLED_RESET_BUTTON,
        width=240,
        callback=lambda: _on_run_again(shell, service),
    )


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


def _on_run(shell, service: QuickCheckService) -> None:
    service.start()
    shell.rebuild_current_screen()


def _on_cancel(shell, service: QuickCheckService) -> None:
    service.cancel()
    shell.rebuild_current_screen()


def _on_run_again(shell, service: QuickCheckService) -> None:
    service.reset()
    shell.rebuild_current_screen()


def _on_open_full_health_report(shell, service: QuickCheckService) -> None:
    # Don't reset the quick-check state — the user may want to come back to
    # the verdict. The shell's switch_screen handles the actual navigation.
    try:
        shell.switch_screen("health_report")
    except Exception:  # noqa: BLE001 — nav must never crash the screen
        logger.exception("readiness_check: switch_screen('health_report') failed")


# ---------------------------------------------------------------------------
# Frame refresh
# ---------------------------------------------------------------------------


def _schedule_next_refresh(shell) -> None:
    """Register a frame callback that ticks the service + refreshes labels.

    Mirrors the full Health Report screen pattern: chain self-rescheduling
    frame callbacks until the screen widgets are torn down, then stop.
    """

    try:
        next_frame = dpg.get_frame_count() + _REFRESH_FRAME_INTERVAL
    except SystemError:
        return

    def _tick(*_args) -> None:
        if not dpg.does_item_exist(TAG_ROOT_CONTAINER):
            return  # screen torn down; stop the chain
        service: QuickCheckService = shell.quick_check_service
        prior_state = service.state
        try:
            service.tick()
        except Exception:  # noqa: BLE001 — never crash the frame
            logger.exception("QuickCheckService.tick() raised")

        if service.state != prior_state:
            shell.rebuild_current_screen()
            return

        _refresh_running_widgets(service)
        _schedule_next_refresh(shell)

    try:
        dpg.set_frame_callback(next_frame, _tick)
    except SystemError:
        return


def _refresh_running_widgets(service: QuickCheckService) -> None:
    """Update the active phase's progress widgets without rebuilding."""

    if service.state not in (
        QuickCheckState.REST,
        QuickCheckState.RANGE,
        QuickCheckState.TRIGGER,
    ):
        return
    if not dpg.does_item_exist(TAG_RUNNING_PROGRESS_BAR):
        return
    progress = service.progress()
    dpg.set_value(TAG_RUNNING_PROGRESS_BAR, progress.fraction)
    if dpg.does_item_exist(TAG_RUNNING_REMAINING):
        dpg.set_value(
            TAG_RUNNING_REMAINING,
            t(
                "readiness_check.running.remaining_label",
                seconds=f"{progress.remaining_s:.1f}",
            ),
        )
    if dpg.does_item_exist(TAG_RUNNING_SAMPLES):
        dpg.set_value(
            TAG_RUNNING_SAMPLES,
            t(
                "readiness_check.running.samples_label",
                n=progress.samples_collected,
            ),
        )
    if dpg.does_item_exist(TAG_RUNNING_PHASE) and progress.phase is not None:
        dpg.set_value(
            TAG_RUNNING_PHASE,
            t(
                "readiness_check.running.phase_label",
                n=_PHASE_NUMBER[progress.phase],
                phase=t(_PHASE_I18N[progress.phase]),
            ),
        )


__all__ = ["build"]
