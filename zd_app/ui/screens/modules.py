"""Modules screen — per-side stick-module passport list + detail + wizard.

Three top-level views:

- ``"list"``    — two cards (left + right) summarising each active passport.
                   This is the entry point.
- ``"detail"``  — full characterization history for one side, with the
                   verdict sparkline + per-fingerprint expand rows.
- ``"wizard"``  — the 60-second characterization runner, READY-gated per
                   phase so the operator has time to position the controller
                   between sampling steps.

The Assign / Edit Notes / Swap / Export modals are built into DPG on
demand and torn down when closed. The wizard's state lives on a
:class:`CharacterizationOrchestrator` stored on
``shell.modules_screen_state.orchestrator``.

State-pure dispatcher: :func:`build` reads ``shell.modules_screen_state``
and routes to one of ``_build_list`` / ``_build_detail`` / ``_build_wizard``.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import dearpygui.dearpygui as dpg

from zd_app.i18n import t
from zd_app.services.diagnostic_bundle import DiagnosticBundleService
from zd_app.services.health_report.sample_capture import make_xinput_sample_provider
from zd_app.services.module_passport import (
    ATTENTION_AGE_DAYS,
    CharacterizationOrchestrator,
    CharacterizationPhase,
    CharacterizationState,
    METRIC_ORDER,
    MetricTrend,
    ModulePassportService,
    PassportTrendSummary,
    TREND_STATUS_DRIFTING,
    TREND_STATUS_INSUFFICIENT,
    TREND_STATUS_INVESTIGATE,
    TREND_STATUS_STABLE,
)
from zd_app.storage.module_passport_models import (
    ModuleFingerprint,
    ModulePassport,
    SIDES,
    SIDE_LEFT,
    SIDE_RIGHT,
    STATUS_GOOD,
    STATUS_WATCH,
    STATUS_WEAR_OBSERVED,
)
from zd_app.ui import diagnostic_bundle_preview
from zd_app.ui.typography import helper_text, screen_title, section_title


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DPG tags
# ---------------------------------------------------------------------------


TAG_ROOT_CONTAINER = "modules_root"
TAG_STATUS_TEXT = "modules_status_text"

TAG_ASSIGN_MODAL = "modules_assign_modal"
TAG_ASSIGN_INPUT_ID = "modules_assign_input_id"
TAG_ASSIGN_INPUT_NOTES = "modules_assign_input_notes"
TAG_ASSIGN_SAVE = "modules_assign_save"
TAG_ASSIGN_CANCEL = "modules_assign_cancel"

TAG_NOTES_MODAL = "modules_notes_modal"
TAG_NOTES_INPUT = "modules_notes_input"
TAG_NOTES_SAVE = "modules_notes_save"
TAG_NOTES_CANCEL = "modules_notes_cancel"

TAG_SWAP_MODAL = "modules_swap_modal"
TAG_SWAP_INPUT_ID = "modules_swap_input_id"
TAG_SWAP_INPUT_NOTES = "modules_swap_input_notes"
TAG_SWAP_CONFIRM = "modules_swap_confirm"
TAG_SWAP_CANCEL = "modules_swap_cancel"

TAG_EXPORT_MODAL = "modules_export_modal"
TAG_EXPORT_INCLUDE_ARCHIVED = "modules_export_include_archived"
TAG_EXPORT_HEALTH_LIMIT = "modules_export_health_limit"
TAG_EXPORT_WEAR_WINDOW = "modules_export_wear_window"
TAG_EXPORT_GENERATE_MD = "modules_export_generate_md"
TAG_EXPORT_GENERATE_ZIP = "modules_export_generate_zip"
TAG_EXPORT_CANCEL = "modules_export_cancel"


_HEALTH_REPORT_LIMIT_CHOICES: tuple[str, ...] = ("1", "3", "5", "10")
_WEAR_WINDOW_CHOICES: tuple[str, ...] = ("30", "90", "180", "365")

TAG_WIZARD_PROGRESS_BAR = "modules_wizard_progress_bar"
TAG_WIZARD_REMAINING = "modules_wizard_remaining"
TAG_WIZARD_SAMPLES = "modules_wizard_samples"
TAG_WIZARD_PHASE = "modules_wizard_phase"


VIEW_LIST = "list"
VIEW_DETAIL = "detail"
VIEW_WIZARD = "wizard"
VIEW_ARCHIVED_LIST = "archived_list"
VIEW_ARCHIVED_DETAIL = "archived_detail"
VIEW_COMPARE = "compare"
VIEW_MODULE_TRENDS = "module_trends"


# Delta thresholds for the cross-side compare view. Heuristic only — these
# numbers don't represent wear; they're "this side reads notably different
# from the other side, take a look" cues. Tuned to flag clearly visible
# divergence without firing on routine noise. Operator judgment supersedes
# any flag; see modules.compare.delta_caveat in the locale files.
DELTA_THRESHOLD_NOISE_FLOOR_PERCENT = 0.5     # tentative — operator judgment
DELTA_THRESHOLD_CENTERING_EUCLIDEAN = 1.0     # tentative — operator judgment
DELTA_THRESHOLD_CIRCULARITY_PERCENT = 5.0     # tentative — operator judgment
DELTA_THRESHOLD_OUTER_DEADZONE_SPAN = 3.0     # tentative — operator judgment
DELTA_THRESHOLD_ASYMMETRY_SCORE = 0.05        # tentative — operator judgment
DELTA_THRESHOLD_BITNESS = 32                  # tentative — operator judgment
DELTA_THRESHOLD_TREMOR = 0.3                  # tentative — operator judgment
DELTA_THRESHOLD_LINEARITY = 0.05              # tentative — operator judgment


SPARKLINE_MAX_POINTS = 20
_REFRESH_FRAME_INTERVAL = 2


_STATUS_COLOR_KEY: dict[str, str] = {
    STATUS_GOOD: "good",
    STATUS_WATCH: "warn",
    STATUS_WEAR_OBSERVED: "bad",
}


_STATUS_I18N_KEY: dict[str, str] = {
    STATUS_GOOD: "modules.status.good",
    STATUS_WATCH: "modules.status.watch",
    STATUS_WEAR_OBSERVED: "modules.status.wear_observed",
}


_TREND_STATUS_COLOR_KEY: dict[str, str] = {
    TREND_STATUS_STABLE: "good",
    TREND_STATUS_DRIFTING: "warn",
    TREND_STATUS_INVESTIGATE: "bad",
    TREND_STATUS_INSUFFICIENT: "muted",
}


_TREND_STATUS_I18N_KEY: dict[str, str] = {
    TREND_STATUS_STABLE: "modules.trends.status.stable",
    TREND_STATUS_DRIFTING: "modules.trends.status.drifting",
    TREND_STATUS_INVESTIGATE: "modules.trends.status.investigate",
    TREND_STATUS_INSUFFICIENT: "modules.trends.status.insufficient_data",
}


_TREND_CONFIDENCE_I18N_KEY: dict[str, str] = {
    "high": "modules.trends.confidence.high",
    "moderate": "modules.trends.confidence.moderate",
    "low": "modules.trends.confidence.low",
    "insufficient_data": "modules.trends.confidence.insufficient_data",
}


# Metric-name i18n keys mirror the modules.compare.metric_name.* family used
# by the cross-side compare view — keep them in lock-step so the operator's
# eye learns the term once. Centering uses Euclidean magnitude here, same as
# in the verdict pipeline.
_TREND_METRIC_I18N_KEY: dict[str, str] = {
    "noise_floor": "modules.compare.metric_name.noise_floor",
    "centering_offset": "modules.compare.metric_name.centering",
    "circularity_coverage": "modules.compare.metric_name.circularity",
    "outer_deadzone_span": "modules.compare.metric_name.outer_deadzone",
    "asymmetry_score": "modules.compare.metric_name.asymmetry",
    "bitness": "modules.compare.metric_name.bitness",
    "tremor": "modules.compare.metric_name.tremor",
    "linearity": "modules.compare.metric_name.linearity",
}


_PHASE_I18N_KEY: dict[CharacterizationPhase, str] = {
    CharacterizationPhase.REST: "modules.wizard.phase.rest",
    CharacterizationPhase.ROTATION: "modules.wizard.phase.rotation",
    CharacterizationPhase.SETTLE: "modules.wizard.phase.settle",
}


_PHASE_NUMBER: dict[CharacterizationPhase, int] = {
    CharacterizationPhase.REST: 1,
    CharacterizationPhase.ROTATION: 2,
    CharacterizationPhase.SETTLE: 3,
}


_READY_TITLE_KEYS: dict[CharacterizationState, str] = {
    CharacterizationState.READY_REST: "modules.wizard.ready_rest.title",
    CharacterizationState.READY_ROTATION: "modules.wizard.ready_rotation.title",
    CharacterizationState.READY_SETTLE: "modules.wizard.ready_settle.title",
}


_READY_BODY_KEYS: dict[CharacterizationState, str] = {
    CharacterizationState.READY_REST: "modules.wizard.ready_rest.body",
    CharacterizationState.READY_ROTATION: "modules.wizard.ready_rotation.body",
    CharacterizationState.READY_SETTLE: "modules.wizard.ready_settle.body",
}


# ---------------------------------------------------------------------------
# View state
# ---------------------------------------------------------------------------


@dataclass
class ModulesScreenState:
    view: str = VIEW_LIST
    detail_side: Optional[str] = None
    expanded_fingerprint_idx: Optional[int] = None
    orchestrator: Optional[CharacterizationOrchestrator] = None
    wizard_side: Optional[str] = None
    status_text: str = ""
    status_kind: str = "info"
    pending_module_id: str = ""
    pending_notes: str = ""
    # Archived browser: which side's archives we're viewing, and (for the
    # archived-detail view) which archived entry — addressed by its index
    # within the archived list, plus the filename so we can re-find it
    # after a navigation round-trip even if the archive list mutates.
    archived_side: Optional[str] = None
    archived_detail_index: Optional[int] = None
    archived_expanded_fingerprint_idx: Optional[int] = None
    export_include_archived: bool = True
    export_health_limit: int = 5
    export_wear_window_days: int = 90
    sample_provider_factory: Callable[[], object] = field(
        default_factory=lambda: make_xinput_sample_provider
    )


def _ensure_state(shell) -> ModulesScreenState:
    state = getattr(shell, "modules_screen_state", None)
    if state is None or not isinstance(state, ModulesScreenState):
        state = ModulesScreenState()
        shell.modules_screen_state = state
    return state


def _service(shell) -> Optional[ModulePassportService]:
    service = getattr(shell, "module_passport_service", None)
    if isinstance(service, ModulePassportService):
        return service
    return None


def _bundle_service(shell) -> Optional[DiagnosticBundleService]:
    service = getattr(shell, "diagnostic_bundle_service", None)
    if isinstance(service, DiagnosticBundleService):
        return service
    return None


def _device_identity(shell) -> dict:
    """Best-effort device identity for the bundle's Hardware section.

    Reads ``product_name``/``firmware_version``/``connection_state`` off
    the shell's device service when present. The bundle service treats
    every missing field as "Unknown", so partial data is fine.
    """

    device_service = getattr(shell, "device_service", None)
    if device_service is None:
        return {}
    try:
        state = device_service.state
    except Exception:  # noqa: BLE001 — best-effort
        logger.exception("modules.export: device_service.state lookup raised")
        return {}
    return {
        "product_string": getattr(state, "product_name", None) or None,
        "firmware_version": getattr(state, "firmware_version", None) or None,
        "connection": getattr(state, "connection_state", None) or None,
        "active_slot": None,
    }


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


def build(shell, parent: str) -> None:
    """Render the modules screen into ``parent``."""

    state = _ensure_state(shell)
    service = _service(shell)

    with dpg.child_window(
        parent=parent,
        tag=TAG_ROOT_CONTAINER,
        autosize_x=True,
        autosize_y=True,
        border=False,
    ):
        screen_title(t("modules.title"))
        helper_text(t("modules.subtitle"), wrap=900)
        dpg.add_spacer(height=8)

        if service is None:
            dpg.add_text(t("modules.unavailable"), wrap=900)
            return

        if state.status_text:
            color_key = (
                shell.COLORS["muted"]
                if state.status_kind == "info"
                else shell.COLORS["warn"]
            )
            dpg.add_text(state.status_text, tag=TAG_STATUS_TEXT, color=color_key, wrap=900)
            dpg.add_spacer(height=8)

        if state.view == VIEW_DETAIL and state.detail_side in SIDES:
            _build_detail(shell, service, state)
        elif state.view == VIEW_WIZARD and state.orchestrator is not None:
            _build_wizard(shell, service, state)
        elif state.view == VIEW_ARCHIVED_LIST and state.archived_side in SIDES:
            _build_archived_list(shell, service, state)
        elif (
            state.view == VIEW_ARCHIVED_DETAIL
            and state.archived_side in SIDES
            and state.archived_detail_index is not None
        ):
            _build_archived_detail(shell, service, state)
        elif state.view == VIEW_COMPARE:
            _build_compare(shell, service, state)
        elif state.view == VIEW_MODULE_TRENDS and state.detail_side in SIDES:
            _build_module_trends(shell, service, state)
        else:
            # Default + fallback path: the list.
            state.view = VIEW_LIST
            _build_list(shell, service, state)

    if state.view == VIEW_WIZARD:
        _schedule_wizard_refresh(shell)


# ---------------------------------------------------------------------------
# LIST view — two cards (left + right)
# ---------------------------------------------------------------------------


def _build_list(shell, service: ModulePassportService, state: ModulesScreenState) -> None:
    section_title(t("modules.list.heading"))
    dpg.add_spacer(height=6)
    # Top-level cross-side action. Always visible — its behaviour adapts
    # to the per-side state inside _build_compare.
    dpg.add_button(
        label=t("modules.controls.compare_sides"),
        width=240,
        callback=lambda *_a: _on_open_compare(shell),
    )
    dpg.add_spacer(height=8)
    with dpg.group(horizontal=True):
        _build_side_card(shell, service, state, SIDE_LEFT)
        dpg.add_spacer(width=12)
        _build_side_card(shell, service, state, SIDE_RIGHT)
    # 8 (was 14): shaves >=2px off the list view's trailing block so the FILL
    # root never overshoots the exact-fit frame by the 1px int()-truncation nub
    # the overflow probe reported at near-fit window heights (content-side fix —
    # no frame reserve). Purely cosmetic gap; the export row still reads clearly.
    dpg.add_spacer(height=8)
    _build_export_row(shell)


def _build_export_row(shell) -> None:
    """Render the Diagnostic Bundle export entry-point below the side cards."""

    bundle = _bundle_service(shell)
    section_title(t("modules.export.row_heading"))
    dpg.add_text(
        t("modules.export.row_body"),
        color=shell.COLORS["muted"],
        wrap=900,
    )
    dpg.add_spacer(height=6)
    if bundle is None:
        dpg.add_text(t("modules.export.unavailable"), color=shell.COLORS["muted"])
        return
    dpg.add_button(
        label=t("modules.export.open_button"),
        width=280,
        callback=lambda *_a: _open_export_modal(shell),
    )


# Shared height for the left/right passport cards so the horizontal pair stays
# even regardless of which side has more content (one assigned, one empty). The
# taller (assigned) state — side label + 5 info rows + trend badge + 3 rows of
# two buttons — measured 396px of content on a real viewport at the shipped
# fonts (Inter 14 / SemiBold 18) in BOTH en and zh-CN
# (tools/diag_dpg_card_clip.py); the prior 260 clipped the bottom button row by
# 136px. 412 fits the assigned content with a small margin and is locale-stable
# (the height is button/row-dominated, not wrapped-text-dominated).
_SIDE_CARD_HEIGHT = 412

# Width of the left/right passport cards. Sized to the WIDEST row — the
# View-Trends (200) + View-Archived (240) button pair, ~484px of content at the
# shipped fonts; the prior 440 clipped that row ~44px (a horizontal scrollbar on
# the assigned-with-archive side, present at both 14 and 15px body — a fixed-
# width-button overflow, not font-driven). Locale-stable for the same reason (the
# overflow is fixed-width buttons, not wrapped text). Both sides use this width so
# the horizontal pair stays even, and 2x500 still fits the 1180 min-window.
_SIDE_CARD_WIDTH = 500


def _build_side_card(
    shell,
    service: ModulePassportService,
    state: ModulesScreenState,
    side: str,
) -> None:
    passport = service.get(side)
    side_label = t(f"modules.side.{side}")
    with dpg.child_window(width=_SIDE_CARD_WIDTH, height=_SIDE_CARD_HEIGHT, border=True):
        if passport is None:
            empty_title_key = (
                "modules.list.empty_card_title_left"
                if side == SIDE_LEFT
                else "modules.list.empty_card_title_right"
            )
            dpg.add_text(t(empty_title_key), color=shell.COLORS["text"])
            dpg.add_text(
                t("modules.list.empty_card_body"),
                wrap=400,
                color=shell.COLORS["muted"],
            )
            dpg.add_spacer(height=14)
            dpg.add_button(
                label=t("modules.list.button.assign"),
                width=200,
                callback=lambda *_a, s=side: _open_assign_modal(shell, s, is_swap=False),
            )
            return

        dpg.add_text(side_label, color=shell.COLORS["text"])
        dpg.add_spacer(height=2)
        dpg.add_text(
            f"{t('modules.list.assigned_label')}: {passport.module_id}",
            wrap=400,
        )
        dpg.add_text(
            f"{t('modules.list.assigned_at_label')}: {passport.assigned_at_utc}",
            color=shell.COLORS["muted"],
        )
        latest = passport.latest_fingerprint()
        if latest is None:
            dpg.add_text(
                t("modules.list.last_run_none"),
                color=shell.COLORS["muted"],
                wrap=400,
            )
        else:
            color = shell.COLORS.get(_STATUS_COLOR_KEY.get(latest.overall_status, "muted"), shell.COLORS["muted"])
            status_label = t(_STATUS_I18N_KEY.get(latest.overall_status, "modules.status.unknown"))
            dpg.add_text(
                f"{t('modules.list.last_run_label')}: {latest.timestamp_utc}",
                color=shell.COLORS["muted"],
            )
            with dpg.group(horizontal=True):
                dpg.add_text("■", color=color)
                dpg.add_text(status_label)
            dpg.add_text(
                t("modules.list.runs_count", n=len(passport.fingerprints)),
                color=shell.COLORS["muted"],
            )

        _build_trend_badge(shell, service, side)

        dpg.add_spacer(height=10)
        with dpg.group(horizontal=True):
            dpg.add_button(
                label=t("modules.list.button.view_detail"),
                width=120,
                callback=lambda *_a, s=side: _on_view_detail(shell, s),
            )
            dpg.add_button(
                label=t("modules.list.button.run_characterization"),
                width=220,
                callback=lambda *_a, s=side: _on_start_wizard(shell, s),
            )
        with dpg.group(horizontal=True):
            dpg.add_button(
                label=t("modules.list.button.edit_notes"),
                width=120,
                callback=lambda *_a, s=side: _open_notes_modal(shell, s),
            )
            dpg.add_button(
                label=t("modules.list.button.swap"),
                width=160,
                callback=lambda *_a, s=side: _open_assign_modal(shell, s, is_swap=True),
            )
        with dpg.group(horizontal=True):
            dpg.add_button(
                label=t("modules.list.button.view_trends"),
                width=200,
                callback=lambda *_a, s=side: _on_open_module_trends(shell, s),
            )
            if _has_archive_for_side(service, side):
                dpg.add_button(
                    label=t("modules.controls.view_archived"),
                    width=240,
                    callback=lambda *_a, s=side: _on_open_archived_list(shell, s),
                )


def _build_trend_badge(
    shell, service: ModulePassportService, side: str
) -> None:
    """Render the small trend-status row on a side card.

    Reads through :meth:`ModulePassportService.compute_passport_trend` so
    the badge is driven by the live passport — no caching at this layer.
    Returns silently when the trend can't be computed (e.g. no passport
    yet, which the card already handles with the empty state).
    """

    try:
        summary = service.compute_passport_trend(side)
    except Exception:  # noqa: BLE001 — never crash the card on trend math
        logger.exception("modules.trends: compute_passport_trend raised for %s", side)
        return
    if summary is None:
        return
    status_label = t(
        _TREND_STATUS_I18N_KEY.get(summary.status, "modules.trends.status.insufficient_data")
    )
    color_key = _TREND_STATUS_COLOR_KEY.get(summary.status, "muted")
    color = shell.COLORS.get(color_key, shell.COLORS["muted"])
    if summary.status in (TREND_STATUS_DRIFTING, TREND_STATUS_INVESTIGATE):
        badge_text = t(
            "modules.list.trend_badge_with_count",
            status=status_label,
            n=len(summary.attention_metrics),
        )
    else:
        badge_text = t("modules.list.trend_badge", status=status_label)
    with dpg.group(horizontal=True):
        dpg.add_text("◆", color=color)
        dpg.add_text(badge_text, color=shell.COLORS["muted"])


# ---------------------------------------------------------------------------
# DETAIL view
# ---------------------------------------------------------------------------


def _build_detail(shell, service: ModulePassportService, state: ModulesScreenState) -> None:
    side = state.detail_side or SIDE_LEFT
    passport = service.get(side)
    dpg.add_button(
        label=t("modules.detail.back_button"),
        width=200,
        callback=lambda *_a: _on_back_to_list(shell),
    )
    dpg.add_spacer(height=10)
    if passport is None:
        dpg.add_text(t("modules.list.empty_card_body"), wrap=900)
        return

    side_label = t(f"modules.side.{side}")
    section_title(f"{side_label} — {passport.module_id}")
    dpg.add_text(
        f"{t('modules.list.assigned_at_label')}: {passport.assigned_at_utc}",
        color=shell.COLORS["muted"],
    )
    dpg.add_spacer(height=8)

    # Trend banners belong above the notes / history so the operator sees
    # any standing "look here" cue before scrolling. They no-op silently
    # when the trend math is unavailable or doesn't flag anything.
    trend_summary = _safe_compute_trend(service, side)
    _build_trend_attention_banner(shell, trend_summary)
    _build_recharacterize_hint(shell, trend_summary)

    section_title(t("modules.detail.notes_heading"))
    if passport.notes:
        dpg.add_text(passport.notes, wrap=900)
    else:
        dpg.add_text(t("modules.detail.notes_empty"), color=shell.COLORS["muted"])
    dpg.add_spacer(height=10)

    _build_detail_sparkline(shell, passport)
    dpg.add_spacer(height=10)

    dpg.add_button(
        label=t("modules.detail.view_trends_button"),
        width=240,
        callback=lambda *_a, s=side: _on_open_module_trends(shell, s),
    )
    dpg.add_spacer(height=10)

    section_title(t("modules.detail.history_heading"))
    if not passport.fingerprints:
        dpg.add_text(t("modules.detail.history_empty"), color=shell.COLORS["muted"], wrap=900)
        return
    # Render newest first.
    for idx in range(len(passport.fingerprints) - 1, -1, -1):
        _build_fingerprint_row(shell, passport.fingerprints[idx], idx, state)


def _safe_compute_trend(
    service: ModulePassportService, side: str
) -> Optional[PassportTrendSummary]:
    """Best-effort trend lookup. Returns ``None`` on any failure path."""

    try:
        return service.compute_passport_trend(side)
    except Exception:  # noqa: BLE001
        logger.exception("modules.trends: compute_passport_trend raised for %s", side)
        return None


def _build_trend_attention_banner(
    shell, trend_summary: Optional[PassportTrendSummary]
) -> None:
    """Inline banner when the roll-up status is ``investigate``.

    Bordered, accent-coloured, but NOT modal — the operator can still scroll
    past. Names the driving metrics so the next click goes somewhere useful.
    """

    if trend_summary is None:
        return
    if trend_summary.status != TREND_STATUS_INVESTIGATE:
        return
    attention_names = [
        t(_TREND_METRIC_I18N_KEY.get(m, "modules.trends.metric_unknown"))
        for m in trend_summary.attention_metrics
    ]
    metric_list = ", ".join(attention_names) if attention_names else ""
    # Fit to content: the body names a variable number of metrics, so a fixed
    # 80px clipped once two or more were listed. DPG-2.x content-fit.
    with dpg.child_window(
        border=True, auto_resize_y=True, autosize_y=False, autosize_x=True
    ):
        dpg.add_text(
            t("modules.trends.attention_banner.title"),
            color=shell.COLORS.get("bad", shell.COLORS["warn"]),
        )
        dpg.add_text(
            t(
                "modules.trends.attention_banner.body",
                n=len(trend_summary.attention_metrics),
                metrics=metric_list,
            ),
            wrap=900,
        )
    dpg.add_spacer(height=8)


def _build_recharacterize_hint(
    shell, trend_summary: Optional[PassportTrendSummary]
) -> None:
    """Soft "your last run was a while ago" nudge.

    Renders only when the most-recent usable fingerprint is older than
    :data:`ATTENTION_AGE_DAYS`. Non-blocking — never a banner, just a
    muted line so it doesn't compete with the attention banner above.
    """

    if trend_summary is None:
        return
    age = trend_summary.last_fingerprint_age_days
    if age is None or age <= ATTENTION_AGE_DAYS:
        return
    dpg.add_text(
        t("modules.trends.recharacterize_hint", days=int(round(age))),
        color=shell.COLORS["muted"],
        wrap=900,
    )
    dpg.add_spacer(height=6)


def _build_detail_sparkline(shell, passport: ModulePassport) -> None:
    statuses: list[str] = [fp.overall_status for fp in passport.fingerprints]
    statuses = statuses[-SPARKLINE_MAX_POINTS:]
    section_title(t("modules.detail.sparkline.heading"))
    if not statuses:
        dpg.add_text(t("modules.detail.sparkline.empty"), color=shell.COLORS["muted"], wrap=900)
        return
    with dpg.group(horizontal=True):
        for status in statuses:
            bucket = _STATUS_COLOR_KEY.get(status)
            color = shell.COLORS[bucket] if bucket else shell.COLORS["muted"]
            dpg.add_text("■", color=color)
    dpg.add_text(
        t(
            "modules.detail.sparkline.legend",
            n=len(statuses),
            max=SPARKLINE_MAX_POINTS,
        ),
        color=shell.COLORS["muted"],
    )


def _build_fingerprint_row(
    shell,
    fingerprint: ModuleFingerprint,
    idx: int,
    state: ModulesScreenState,
) -> None:
    expanded = state.expanded_fingerprint_idx == idx
    status_label = t(_STATUS_I18N_KEY.get(fingerprint.overall_status, "modules.status.unknown"))
    bucket = _STATUS_COLOR_KEY.get(fingerprint.overall_status)
    color = shell.COLORS[bucket] if bucket else shell.COLORS["muted"]
    duration_s = max(0, int(round(fingerprint.duration_ms / 1000)))
    summary = t(
        "modules.detail.run_summary",
        ts=fingerprint.timestamp_utc,
        status=status_label,
        samples=fingerprint.samples_count,
        duration_s=duration_s,
    )
    # Fit to content height (collapsed AND expanded): DPG-2.x content-fit. A
    # fixed height (was ``60 + 24*8``) clips the 8-metric detail when expanded —
    # the rows are a stacked list, not an equal-height pair, so fitting is safe.
    with dpg.child_window(
        border=True, auto_resize_y=True, autosize_y=False, autosize_x=True
    ):
        with dpg.group(horizontal=True):
            dpg.add_text("■", color=color)
            dpg.add_text(summary, wrap=900)
        toggle_label = (
            t("modules.detail.collapse_row")
            if expanded
            else t("modules.detail.expand_row")
        )
        dpg.add_button(
            label=toggle_label,
            width=180,
            callback=lambda *_a, i=idx: _on_toggle_fingerprint(shell, i),
        )
        if expanded:
            _build_fingerprint_detail(shell, fingerprint)


def _build_fingerprint_detail(shell, fingerprint: ModuleFingerprint) -> None:
    rows: list[str] = [
        t(
            "modules.detail.metric.noise_floor",
            value=f"{fingerprint.noise_floor_percent:.2f}",
        ),
        t(
            "modules.detail.metric.centering",
            x=f"{fingerprint.centering_offset_x:.2f}",
            y=f"{fingerprint.centering_offset_y:.2f}",
        ),
        t(
            "modules.detail.metric.circularity",
            pct=f"{fingerprint.circularity_coverage_percent * 100:.1f}",
        ),
        t(
            "modules.detail.metric.outer_deadzone",
            min=f"{fingerprint.outer_deadzone_min_axis:.2f}",
            max=f"{fingerprint.outer_deadzone_max_axis:.2f}",
            span=(
                f"{(fingerprint.outer_deadzone_max_axis - fingerprint.outer_deadzone_min_axis):.2f}"
            ),
        ),
        t(
            "modules.detail.metric.asymmetry",
            value=f"{fingerprint.asymmetry_score:.3f}",
        ),
        t(
            "modules.detail.metric.bitness",
            value=fingerprint.bitness_observed,
        ),
        t(
            "modules.detail.metric.tremor",
            value=f"{fingerprint.tremor_metric:.2f}",
        ),
        t(
            "modules.detail.metric.linearity",
            value=f"{fingerprint.linearity_score:.3f}",
        ),
    ]
    for row in rows:
        dpg.add_text(f"  {row}", color=shell.COLORS["muted"], wrap=900)


# ---------------------------------------------------------------------------
# ARCHIVED LIST view — chronological browse of prior modules for one side
# ---------------------------------------------------------------------------


def _has_archive_for_side(service: ModulePassportService, side: str) -> bool:
    """Cheap, no-parse test: are there any archive files for this side?

    Used to gate the "View archived passports" button so a side with an
    empty archive doesn't show a dead-end action.
    """

    try:
        return any(service.archive_dir.glob(f"{side}_*.json"))
    except OSError:
        return False


def _notes_snippet(notes: str, *, length: int = 80) -> str:
    """Single-line snippet of operator notes for compact archive rows."""

    if not notes:
        return ""
    one_line = " ".join(notes.split())
    if len(one_line) <= length:
        return one_line
    return one_line[: length - 1] + "…"


def _build_archived_list(
    shell, service: ModulePassportService, state: ModulesScreenState
) -> None:
    side = state.archived_side or SIDE_LEFT
    side_label = t(f"modules.side.{side}")
    dpg.add_button(
        label=t("modules.archived.back"),
        width=200,
        callback=lambda *_a: _on_back_to_list(shell),
    )
    dpg.add_spacer(height=10)
    section_title(
        t("modules.archived.title", side=side_label),
    )
    dpg.add_spacer(height=6)
    entries = service.list_archive_entries(side)
    if not entries:
        dpg.add_text(t("modules.archived.empty"), color=shell.COLORS["muted"], wrap=900)
        return
    for idx, (passport, archived_at) in enumerate(entries):
        _build_archived_row(shell, passport, archived_at, idx)


def _build_archived_row(
    shell,
    passport: ModulePassport,
    archived_at: str,
    idx: int,
) -> None:
    """One row per archived passport in the list view."""

    latest = passport.latest_fingerprint()
    fingerprint_count = len(passport.fingerprints)
    # Fit to content: summary + status + optional notes snippet + button vary in
    # height, so a fixed 130px clipped. DPG-2.x content-fit.
    with dpg.child_window(
        border=True, auto_resize_y=True, autosize_y=False, autosize_x=True
    ):
        dpg.add_text(
            t(
                "modules.archived.entry_summary",
                module_id=passport.module_id,
                assigned_at=passport.assigned_at_utc,
                archived_at=archived_at or "?",
                fingerprint_count=fingerprint_count,
            ),
            wrap=900,
        )
        if latest is not None:
            status_label = t(
                _STATUS_I18N_KEY.get(latest.overall_status, "modules.status.unknown")
            )
            bucket = _STATUS_COLOR_KEY.get(latest.overall_status)
            color = shell.COLORS[bucket] if bucket else shell.COLORS["muted"]
            with dpg.group(horizontal=True):
                dpg.add_text("■", color=color)
                dpg.add_text(
                    t("modules.archived.entry_status", status=status_label),
                    color=shell.COLORS["muted"],
                )
        snippet = _notes_snippet(passport.notes)
        if snippet:
            dpg.add_text(
                t("modules.archived.entry_notes_snippet", snippet=snippet),
                color=shell.COLORS["muted"],
                wrap=900,
            )
        else:
            dpg.add_text(
                t("modules.archived.entry_notes_none"),
                color=shell.COLORS["muted"],
            )
        dpg.add_button(
            label=t("modules.archived.view_detail"),
            width=160,
            callback=lambda *_a, i=idx: _on_open_archived_detail(shell, i),
        )


# ---------------------------------------------------------------------------
# ARCHIVED DETAIL view — read-only inspection of one archived passport
# ---------------------------------------------------------------------------


def _build_archived_detail(
    shell, service: ModulePassportService, state: ModulesScreenState
) -> None:
    side = state.archived_side or SIDE_LEFT
    entries = service.list_archive_entries(side)
    detail_idx = state.archived_detail_index
    if detail_idx is None or detail_idx < 0 or detail_idx >= len(entries):
        # The entry vanished between navigations — go back to the list rather
        # than render an empty detail. The list_archive_entries call above is
        # cheap enough that this corner case is essentially free.
        state.view = VIEW_ARCHIVED_LIST
        state.archived_detail_index = None
        _build_archived_list(shell, service, state)
        return
    passport, archived_at = entries[detail_idx]
    dpg.add_button(
        label=t("modules.archived.detail_back"),
        width=200,
        callback=lambda *_a: _on_back_to_archived_list(shell),
    )
    dpg.add_spacer(height=10)
    section_title(
        t("modules.archived.detail_heading", module_id=passport.module_id),
    )
    dpg.add_text(
        t(
            "modules.archived.detail_dates",
            assigned_at=passport.assigned_at_utc,
            archived_at=archived_at or "?",
        ),
        color=shell.COLORS["muted"],
    )
    dpg.add_spacer(height=8)
    section_title(t("modules.detail.notes_heading"))
    if passport.notes:
        dpg.add_text(passport.notes, wrap=900)
    else:
        dpg.add_text(t("modules.detail.notes_empty"), color=shell.COLORS["muted"])
    dpg.add_spacer(height=10)
    _build_detail_sparkline(shell, passport)
    dpg.add_spacer(height=10)
    section_title(
        t("modules.archived.detail_history_heading"),
    )
    if not passport.fingerprints:
        dpg.add_text(
            t("modules.archived.detail_history_empty"),
            color=shell.COLORS["muted"],
            wrap=900,
        )
        return
    for idx in range(len(passport.fingerprints) - 1, -1, -1):
        _build_archived_fingerprint_row(
            shell, passport.fingerprints[idx], idx, state
        )


def _build_archived_fingerprint_row(
    shell,
    fingerprint: ModuleFingerprint,
    idx: int,
    state: ModulesScreenState,
) -> None:
    """Read-only fingerprint row in the archived-detail view.

    Mirrors :func:`_build_fingerprint_row` but expansion state lives on
    ``state.archived_expanded_fingerprint_idx`` so it doesn't collide with
    the active-detail view's expansion state.
    """

    expanded = state.archived_expanded_fingerprint_idx == idx
    status_label = t(
        _STATUS_I18N_KEY.get(fingerprint.overall_status, "modules.status.unknown")
    )
    bucket = _STATUS_COLOR_KEY.get(fingerprint.overall_status)
    color = shell.COLORS[bucket] if bucket else shell.COLORS["muted"]
    duration_s = max(0, int(round(fingerprint.duration_ms / 1000)))
    summary = t(
        "modules.detail.run_summary",
        ts=fingerprint.timestamp_utc,
        status=status_label,
        samples=fingerprint.samples_count,
        duration_s=duration_s,
    )
    # Fit to content height (collapsed AND expanded): DPG-2.x content-fit. A
    # fixed height (was ``60 + 24*8``) clips the 8-metric detail when expanded —
    # the rows are a stacked list, not an equal-height pair, so fitting is safe.
    with dpg.child_window(
        border=True, auto_resize_y=True, autosize_y=False, autosize_x=True
    ):
        with dpg.group(horizontal=True):
            dpg.add_text("■", color=color)
            dpg.add_text(summary, wrap=900)
        toggle_label = (
            t("modules.detail.collapse_row")
            if expanded
            else t("modules.detail.expand_row")
        )
        dpg.add_button(
            label=toggle_label,
            width=180,
            callback=lambda *_a, i=idx: _on_toggle_archived_fingerprint(shell, i),
        )
        if expanded:
            _build_fingerprint_detail(shell, fingerprint)


# ---------------------------------------------------------------------------
# COMPARE view — side-by-side LEFT vs RIGHT
# ---------------------------------------------------------------------------


def _build_compare(
    shell, service: ModulePassportService, state: ModulesScreenState
) -> None:
    left_passport = service.get(SIDE_LEFT)
    right_passport = service.get(SIDE_RIGHT)
    dpg.add_button(
        label=t("modules.compare.back"),
        width=200,
        callback=lambda *_a: _on_back_to_list(shell),
    )
    dpg.add_spacer(height=10)
    section_title(t("modules.compare.title"))
    dpg.add_spacer(height=8)

    with dpg.group(horizontal=True):
        _build_compare_column(shell, left_passport, SIDE_LEFT)
        dpg.add_spacer(width=12)
        _build_compare_column(shell, right_passport, SIDE_RIGHT)

    dpg.add_spacer(height=12)
    section_title(t("modules.compare.delta_highlights"))
    _build_compare_deltas(shell, left_passport, right_passport)


# Shared height for the two compare columns so the horizontal pair stays even
# regardless of which side has more fingerprint data. The populated column —
# heading + module/assigned rows + latest-status + full fingerprint detail +
# sparkline — measured 578px of content on a real viewport at the shipped fonts
# (tools/diag_dpg_card_clip.py); the prior 360 clipped the sparkline + lower
# detail by 218px. The 2026-06-22 body 14->15 / helper 13->14 readability bump
# grew that content to 593px (both locales), so the prior 592 clipped by 1px.
# 597 = the re-measured 593px floor + a 4px safety margin.
_COMPARE_CARD_HEIGHT = 597


def _build_compare_column(
    shell, passport: Optional[ModulePassport], side: str
) -> None:
    heading_key = (
        "modules.compare.column_left_heading"
        if side == SIDE_LEFT
        else "modules.compare.column_right_heading"
    )
    empty_key = (
        "modules.compare.no_left" if side == SIDE_LEFT else "modules.compare.no_right"
    )
    with dpg.child_window(width=_SIDE_CARD_WIDTH, height=_COMPARE_CARD_HEIGHT, border=True):
        dpg.add_text(t(heading_key), color=shell.COLORS["text"])
        dpg.add_spacer(height=4)
        if passport is None:
            dpg.add_text(t(empty_key), color=shell.COLORS["muted"], wrap=400)
            return
        dpg.add_text(
            f"{t('modules.compare.module_label')}: {passport.module_id}",
            wrap=400,
        )
        dpg.add_text(
            f"{t('modules.compare.assigned_label')}: {passport.assigned_at_utc}",
            color=shell.COLORS["muted"],
        )
        dpg.add_spacer(height=6)
        dpg.add_text(t("modules.compare.latest_heading"), color=shell.COLORS["text"])
        latest = passport.latest_fingerprint()
        if latest is None:
            dpg.add_text(
                t("modules.compare.no_fingerprints"),
                color=shell.COLORS["muted"],
                wrap=400,
            )
        else:
            status_label = t(
                _STATUS_I18N_KEY.get(latest.overall_status, "modules.status.unknown")
            )
            bucket = _STATUS_COLOR_KEY.get(latest.overall_status)
            color = shell.COLORS[bucket] if bucket else shell.COLORS["muted"]
            with dpg.group(horizontal=True):
                dpg.add_text("■", color=color)
                dpg.add_text(status_label)
            _build_fingerprint_detail(shell, latest)
        dpg.add_spacer(height=6)
        dpg.add_text(
            t("modules.compare.sparkline_heading"), color=shell.COLORS["text"]
        )
        statuses = [fp.overall_status for fp in passport.fingerprints][
            -SPARKLINE_MAX_POINTS:
        ]
        if not statuses:
            dpg.add_text(
                t("modules.compare.no_fingerprints"),
                color=shell.COLORS["muted"],
                wrap=400,
            )
        else:
            with dpg.group(horizontal=True):
                for status in statuses:
                    bucket = _STATUS_COLOR_KEY.get(status)
                    color = shell.COLORS[bucket] if bucket else shell.COLORS["muted"]
                    dpg.add_text("■", color=color)


def _build_compare_deltas(
    shell,
    left_passport: Optional[ModulePassport],
    right_passport: Optional[ModulePassport],
) -> None:
    left_latest = left_passport.latest_fingerprint() if left_passport else None
    right_latest = right_passport.latest_fingerprint() if right_passport else None
    if left_latest is None or right_latest is None:
        dpg.add_text(
            t("modules.compare.delta_unavailable"),
            color=shell.COLORS["muted"],
            wrap=900,
        )
        return
    flagged = _compute_delta_flags(left_latest, right_latest)
    if not flagged:
        dpg.add_text(
            t("modules.compare.delta_none"), color=shell.COLORS["muted"], wrap=900
        )
        return
    for flag in flagged:
        dpg.add_text(
            t(
                "modules.compare.delta_row",
                metric=t(f"modules.compare.metric_name.{flag.metric_key}"),
                left_value=flag.left_display,
                right_value=flag.right_display,
                delta=flag.delta_display,
            ),
            color=shell.COLORS.get("warn", shell.COLORS["muted"]),
            wrap=900,
        )
    dpg.add_spacer(height=4)
    dpg.add_text(
        t("modules.compare.delta_caveat"), color=shell.COLORS["muted"], wrap=900
    )


@dataclass(frozen=True)
class _DeltaFlag:
    """One flagged metric-divergence row.

    ``metric_key`` matches the suffix of ``modules.compare.metric_name.*``
    so the rendering loop can look up the localised metric name. Display
    strings are pre-formatted so the renderer doesn't need per-metric
    rounding rules.
    """

    metric_key: str
    left_display: str
    right_display: str
    delta_display: str


def _compute_delta_flags(
    left: ModuleFingerprint, right: ModuleFingerprint
) -> list[_DeltaFlag]:
    """Return one :class:`_DeltaFlag` per metric whose L-R gap exceeds its
    threshold. The order is fixed so the rendered list is stable across
    rebuilds (matches the order in the fingerprint detail block).

    Thresholds are intentionally generous: this surface is meant to nudge
    the operator to look closer, not to diagnose. See ``DELTA_THRESHOLD_*``
    constants for the per-metric numbers.
    """

    flags: list[_DeltaFlag] = []

    # 1. Noise floor — straight percentage delta.
    nf_delta = left.noise_floor_percent - right.noise_floor_percent
    if abs(nf_delta) >= DELTA_THRESHOLD_NOISE_FLOOR_PERCENT:
        flags.append(
            _DeltaFlag(
                metric_key="noise_floor",
                left_display=f"{left.noise_floor_percent:.2f}",
                right_display=f"{right.noise_floor_percent:.2f}",
                delta_display=f"{nf_delta:+.2f}",
            )
        )

    # 2. Centering — Euclidean distance from each side's center offset to
    # origin, then the L-R difference of those distances.
    left_center = (left.centering_offset_x ** 2 + left.centering_offset_y ** 2) ** 0.5
    right_center = (
        right.centering_offset_x ** 2 + right.centering_offset_y ** 2
    ) ** 0.5
    center_delta = left_center - right_center
    if abs(center_delta) >= DELTA_THRESHOLD_CENTERING_EUCLIDEAN:
        flags.append(
            _DeltaFlag(
                metric_key="centering",
                left_display=f"{left_center:.2f}",
                right_display=f"{right_center:.2f}",
                delta_display=f"{center_delta:+.2f}",
            )
        )

    # 3. Circularity — convert fraction → percent for human-friendly delta.
    circ_left_pct = left.circularity_coverage_percent * 100.0
    circ_right_pct = right.circularity_coverage_percent * 100.0
    circ_delta = circ_left_pct - circ_right_pct
    if abs(circ_delta) >= DELTA_THRESHOLD_CIRCULARITY_PERCENT:
        flags.append(
            _DeltaFlag(
                metric_key="circularity",
                left_display=f"{circ_left_pct:.1f}",
                right_display=f"{circ_right_pct:.1f}",
                delta_display=f"{circ_delta:+.1f}",
            )
        )

    # 4. Outer reach span — max minus min, then L-R.
    left_span = left.outer_deadzone_max_axis - left.outer_deadzone_min_axis
    right_span = right.outer_deadzone_max_axis - right.outer_deadzone_min_axis
    span_delta = left_span - right_span
    if abs(span_delta) >= DELTA_THRESHOLD_OUTER_DEADZONE_SPAN:
        flags.append(
            _DeltaFlag(
                metric_key="outer_deadzone",
                left_display=f"{left_span:.2f}",
                right_display=f"{right_span:.2f}",
                delta_display=f"{span_delta:+.2f}",
            )
        )

    # 5. Asymmetry score — scalar.
    asym_delta = left.asymmetry_score - right.asymmetry_score
    if abs(asym_delta) >= DELTA_THRESHOLD_ASYMMETRY_SCORE:
        flags.append(
            _DeltaFlag(
                metric_key="asymmetry",
                left_display=f"{left.asymmetry_score:.3f}",
                right_display=f"{right.asymmetry_score:.3f}",
                delta_display=f"{asym_delta:+.3f}",
            )
        )

    # 6. Bitness — integer step count.
    bit_delta = left.bitness_observed - right.bitness_observed
    if abs(bit_delta) >= DELTA_THRESHOLD_BITNESS:
        flags.append(
            _DeltaFlag(
                metric_key="bitness",
                left_display=str(left.bitness_observed),
                right_display=str(right.bitness_observed),
                delta_display=f"{bit_delta:+d}",
            )
        )

    # 7. Tremor metric — scalar.
    tremor_delta = left.tremor_metric - right.tremor_metric
    if abs(tremor_delta) >= DELTA_THRESHOLD_TREMOR:
        flags.append(
            _DeltaFlag(
                metric_key="tremor",
                left_display=f"{left.tremor_metric:.2f}",
                right_display=f"{right.tremor_metric:.2f}",
                delta_display=f"{tremor_delta:+.2f}",
            )
        )

    # 8. Linearity score — scalar.
    lin_delta = left.linearity_score - right.linearity_score
    if abs(lin_delta) >= DELTA_THRESHOLD_LINEARITY:
        flags.append(
            _DeltaFlag(
                metric_key="linearity",
                left_display=f"{left.linearity_score:.3f}",
                right_display=f"{right.linearity_score:.3f}",
                delta_display=f"{lin_delta:+.3f}",
            )
        )

    return flags


# ---------------------------------------------------------------------------
# MODULE_TRENDS view — per-metric breakdown for one side
# ---------------------------------------------------------------------------


def _build_module_trends(
    shell, service: ModulePassportService, state: ModulesScreenState
) -> None:
    side = state.detail_side or SIDE_LEFT
    side_label = t(f"modules.side.{side}")
    passport = service.get(side)
    dpg.add_button(
        label=t("modules.trends.back_button"),
        width=200,
        callback=lambda *_a, s=side: _on_view_detail(shell, s),
    )
    dpg.add_spacer(height=10)

    if passport is None:
        dpg.add_text(t("modules.list.empty_card_body"), wrap=900)
        return

    section_title(
        t("modules.trends.title", side=side_label, module_id=passport.module_id),
    )
    dpg.add_spacer(height=4)
    dpg.add_text(t("modules.trends.subtitle"), color=shell.COLORS["muted"], wrap=900)
    dpg.add_spacer(height=8)

    trend_summary = _safe_compute_trend(service, side)

    _build_trend_overall_row(shell, trend_summary)
    _build_trend_attention_banner(shell, trend_summary)
    _build_recharacterize_hint(shell, trend_summary)

    dpg.add_spacer(height=4)
    section_title(
        t("modules.trends.per_metric_heading")
    )
    if trend_summary is None:
        dpg.add_text(
            t("modules.trends.unavailable"),
            color=shell.COLORS["muted"],
            wrap=900,
        )
        return
    for trend in trend_summary.metric_trends:
        _build_trend_metric_row(shell, trend)


def _build_trend_overall_row(
    shell, trend_summary: Optional[PassportTrendSummary]
) -> None:
    if trend_summary is None:
        return
    status_label = t(
        _TREND_STATUS_I18N_KEY.get(
            trend_summary.status, "modules.trends.status.insufficient_data"
        )
    )
    color = shell.COLORS.get(
        _TREND_STATUS_COLOR_KEY.get(trend_summary.status, "muted"),
        shell.COLORS["muted"],
    )
    with dpg.group(horizontal=True):
        dpg.add_text("◆", color=color)
        dpg.add_text(
            t("modules.trends.overall_label", status=status_label),
            # accent: intentional (overall trend status roll-up — verdict signal,
            # paired with the status-coloured ◆ glyph; not a static heading)
            color=shell.COLORS["accent"],
        )
    dpg.add_text(
        t(
            "modules.trends.sample_summary",
            n=trend_summary.usable_fingerprint_count,
        ),
        color=shell.COLORS["muted"],
    )
    if trend_summary.last_fingerprint_age_days is not None:
        dpg.add_text(
            t(
                "modules.trends.last_run_age",
                days=int(round(trend_summary.last_fingerprint_age_days)),
            ),
            color=shell.COLORS["muted"],
        )
    dpg.add_spacer(height=8)


def _build_trend_metric_row(shell, trend: MetricTrend) -> None:
    """One bordered row per metric trend in the per-metric breakdown.

    Each row carries:

    - the metric name (i18n-driven so en/zh-CN line up)
    - latest value + signed slope-per-day
    - per-metric confidence label
    - per-metric status pill with colour
    - projected days-to-threshold ONLY when the slope is moderate-or-
      higher confidence AND degrading AND the metric isn't yet in the
      wear band (which the regression module enforces by returning None
      otherwise)
    """

    metric_label = t(
        _TREND_METRIC_I18N_KEY.get(trend.metric_name, "modules.trends.metric_unknown")
    )
    status_label = t(
        _TREND_STATUS_I18N_KEY.get(trend.status, "modules.trends.status.insufficient_data")
    )
    confidence_label = t(
        _TREND_CONFIDENCE_I18N_KEY.get(
            trend.confidence, "modules.trends.confidence.insufficient_data"
        )
    )
    status_color = shell.COLORS.get(
        _TREND_STATUS_COLOR_KEY.get(trend.status, "muted"),
        shell.COLORS["muted"],
    )
    # Fit to content: value/slope + confidence/status + projection lines wrap
    # at the card width, so a fixed 130px clipped the projection variant.
    with dpg.child_window(
        border=True, auto_resize_y=True, autosize_y=False, autosize_x=True
    ):
        with dpg.group(horizontal=True):
            dpg.add_text("◆", color=status_color)
            dpg.add_text(metric_label, color=shell.COLORS["text"])
        dpg.add_text(
            t(
                "modules.trends.metric_row.value_and_slope",
                value=f"{trend.latest_value:.3f}",
                slope=f"{trend.slope_per_day:+.4f}",
            ),
            color=shell.COLORS["muted"],
            wrap=900,
        )
        dpg.add_text(
            t(
                "modules.trends.metric_row.confidence_status",
                confidence=confidence_label,
                status=status_label,
            ),
            color=shell.COLORS["muted"],
        )
        if trend.projected_days_to_threshold is not None:
            dpg.add_text(
                t(
                    "modules.trends.metric_row.projection",
                    days=int(round(trend.projected_days_to_threshold)),
                ),
                color=shell.COLORS.get("warn", shell.COLORS["muted"]),
                wrap=900,
            )
        else:
            dpg.add_text(
                t("modules.trends.metric_row.no_projection"),
                color=shell.COLORS["muted"],
                wrap=900,
            )


# ---------------------------------------------------------------------------
# WIZARD view
# ---------------------------------------------------------------------------


def _build_wizard(
    shell,
    service: ModulePassportService,
    state: ModulesScreenState,
) -> None:
    orchestrator = state.orchestrator
    if orchestrator is None:
        state.view = VIEW_LIST
        _build_list(shell, service, state)
        return
    side_label = t(f"modules.side.{orchestrator.side}")
    s = orchestrator.state
    if s == CharacterizationState.IDLE:
        # Should not happen — start() must precede entering the wizard view.
        _build_wizard_preface(shell, orchestrator, side_label)
    elif s in _READY_TITLE_KEYS:
        _build_wizard_ready(shell, orchestrator, side_label)
    elif s in (
        CharacterizationState.REST,
        CharacterizationState.ROTATION,
        CharacterizationState.SETTLE,
    ):
        _build_wizard_running(shell, orchestrator, side_label)
    elif s == CharacterizationState.COMPLETE:
        _build_wizard_complete(shell, orchestrator, state, side_label)
    elif s == CharacterizationState.CANCELLED:
        _build_wizard_cancelled(shell, orchestrator)


def _build_wizard_preface(
    shell,
    orchestrator: CharacterizationOrchestrator,
    side_label: str,
) -> None:
    section_title(t("modules.wizard.preface.title", side=side_label))
    dpg.add_spacer(height=8)
    dpg.add_text(t("modules.wizard.preface.body"), wrap=900)
    dpg.add_spacer(height=14)
    with dpg.group(horizontal=True):
        dpg.add_button(
            label=t("modules.wizard.preface.start_button"),
            width=200,
            callback=lambda *_a: _on_wizard_proceed(shell),
        )
        dpg.add_button(
            label=t("modules.wizard.preface.cancel_button"),
            width=120,
            callback=lambda *_a: _on_wizard_cancel(shell),
        )


def _build_wizard_ready(
    shell,
    orchestrator: CharacterizationOrchestrator,
    side_label: str,
) -> None:
    title_key = _READY_TITLE_KEYS[orchestrator.state]
    body_key = _READY_BODY_KEYS[orchestrator.state]
    section_title(t(title_key))
    dpg.add_spacer(height=8)
    dpg.add_text(t(body_key), wrap=900)
    dpg.add_spacer(height=14)
    with dpg.group(horizontal=True):
        dpg.add_button(
            label=t("modules.wizard.ready.begin_button"),
            width=200,
            callback=lambda *_a: _on_wizard_proceed(shell),
        )
        dpg.add_button(
            label=t("modules.wizard.ready.cancel_button"),
            width=120,
            callback=lambda *_a: _on_wizard_cancel(shell),
        )


def _build_wizard_running(
    shell,
    orchestrator: CharacterizationOrchestrator,
    side_label: str,
) -> None:
    progress = orchestrator.progress()
    phase = progress.phase
    section_title(
        t("modules.wizard.running.title", side=side_label),
    )
    dpg.add_spacer(height=8)
    phase_label = ""
    if phase is not None:
        phase_label = t(
            "modules.wizard.running.phase_label",
            n=_PHASE_NUMBER[phase],
            phase=t(_PHASE_I18N_KEY[phase]),
        )
    dpg.add_text(phase_label, tag=TAG_WIZARD_PHASE)
    dpg.add_spacer(height=6)
    dpg.add_progress_bar(
        default_value=progress.phase_fraction,
        width=520,
        tag=TAG_WIZARD_PROGRESS_BAR,
    )
    dpg.add_spacer(height=4)
    dpg.add_text(
        t(
            "modules.wizard.running.remaining_label",
            seconds=f"{max(0.0, progress.phase_duration_s - progress.phase_elapsed_s):.1f}",
        ),
        color=shell.COLORS["muted"],
        tag=TAG_WIZARD_REMAINING,
    )
    dpg.add_text(
        t(
            "modules.wizard.running.samples_label",
            n=progress.samples_collected,
        ),
        color=shell.COLORS["muted"],
        tag=TAG_WIZARD_SAMPLES,
    )
    dpg.add_spacer(height=12)
    dpg.add_button(
        label=t("modules.wizard.running.cancel_button"),
        width=200,
        callback=lambda *_a: _on_wizard_cancel(shell),
    )


def _build_wizard_complete(
    shell,
    orchestrator: CharacterizationOrchestrator,
    state: ModulesScreenState,
    side_label: str,
) -> None:
    fingerprint = orchestrator.fingerprint
    section_title(t("modules.wizard.complete.title"))
    dpg.add_spacer(height=8)
    if fingerprint is None:
        dpg.add_text(t("modules.wizard.save_failed"), color=shell.COLORS["warn"])
        return
    status_label = t(_STATUS_I18N_KEY.get(fingerprint.overall_status, "modules.status.unknown"))
    bucket = _STATUS_COLOR_KEY.get(fingerprint.overall_status)
    color = shell.COLORS[bucket] if bucket else shell.COLORS["muted"]
    with dpg.group(horizontal=True):
        dpg.add_text("■", color=color)
        dpg.add_text(
            f"{t('modules.wizard.complete.verdict_label')}: {status_label}",
            # accent: intentional (characterization verdict — status signal,
            # paired with the status-coloured ■ glyph; not a static heading)
            color=shell.COLORS["accent"],
        )
    dpg.add_spacer(height=8)
    _build_fingerprint_detail(shell, fingerprint)
    dpg.add_spacer(height=12)
    with dpg.group(horizontal=True):
        dpg.add_button(
            label=t("modules.wizard.complete.save_button"),
            width=200,
            callback=lambda *_a: _on_wizard_save(shell),
        )
        dpg.add_button(
            label=t("modules.wizard.complete.discard_button"),
            width=160,
            callback=lambda *_a: _on_wizard_discard(shell),
        )


def _build_wizard_cancelled(
    shell,
    orchestrator: CharacterizationOrchestrator,
) -> None:
    section_title(t("modules.wizard.cancelled.title"))
    dpg.add_spacer(height=8)
    dpg.add_text(t("modules.wizard.cancelled.body"), wrap=900)
    dpg.add_spacer(height=12)
    dpg.add_button(
        label=t("modules.wizard.cancelled.reset_button"),
        width=200,
        callback=lambda *_a: _on_wizard_discard(shell),
    )


# ---------------------------------------------------------------------------
# Callbacks — navigation
# ---------------------------------------------------------------------------


def _on_view_detail(shell, side: str) -> None:
    state = _ensure_state(shell)
    state.view = VIEW_DETAIL
    state.detail_side = side
    state.expanded_fingerprint_idx = None
    state.status_text = ""
    state.status_kind = "info"
    shell.rebuild_current_screen()


def _on_back_to_list(shell) -> None:
    state = _ensure_state(shell)
    state.view = VIEW_LIST
    state.detail_side = None
    state.expanded_fingerprint_idx = None
    state.archived_side = None
    state.archived_detail_index = None
    state.archived_expanded_fingerprint_idx = None
    state.status_text = ""
    state.status_kind = "info"
    shell.rebuild_current_screen()


def _on_toggle_fingerprint(shell, idx: int) -> None:
    state = _ensure_state(shell)
    if state.expanded_fingerprint_idx == idx:
        state.expanded_fingerprint_idx = None
    else:
        state.expanded_fingerprint_idx = idx
    shell.rebuild_current_screen()


def _on_open_archived_list(shell, side: str) -> None:
    state = _ensure_state(shell)
    state.view = VIEW_ARCHIVED_LIST
    state.archived_side = side
    state.archived_detail_index = None
    state.archived_expanded_fingerprint_idx = None
    state.status_text = ""
    state.status_kind = "info"
    shell.rebuild_current_screen()


def _on_open_archived_detail(shell, idx: int) -> None:
    state = _ensure_state(shell)
    state.view = VIEW_ARCHIVED_DETAIL
    state.archived_detail_index = idx
    state.archived_expanded_fingerprint_idx = None
    shell.rebuild_current_screen()


def _on_back_to_archived_list(shell) -> None:
    state = _ensure_state(shell)
    state.view = VIEW_ARCHIVED_LIST
    state.archived_detail_index = None
    state.archived_expanded_fingerprint_idx = None
    shell.rebuild_current_screen()


def _on_toggle_archived_fingerprint(shell, idx: int) -> None:
    state = _ensure_state(shell)
    if state.archived_expanded_fingerprint_idx == idx:
        state.archived_expanded_fingerprint_idx = None
    else:
        state.archived_expanded_fingerprint_idx = idx
    shell.rebuild_current_screen()


def _on_open_compare(shell) -> None:
    state = _ensure_state(shell)
    state.view = VIEW_COMPARE
    state.detail_side = None
    state.expanded_fingerprint_idx = None
    state.archived_side = None
    state.archived_detail_index = None
    state.status_text = ""
    state.status_kind = "info"
    shell.rebuild_current_screen()


def _on_open_module_trends(shell, side: str) -> None:
    state = _ensure_state(shell)
    state.view = VIEW_MODULE_TRENDS
    state.detail_side = side
    state.expanded_fingerprint_idx = None
    state.status_text = ""
    state.status_kind = "info"
    shell.rebuild_current_screen()


# ---------------------------------------------------------------------------
# Callbacks — wizard lifecycle
# ---------------------------------------------------------------------------


def _on_start_wizard(shell, side: str) -> None:
    state = _ensure_state(shell)
    service = _service(shell)
    if service is None or service.get(side) is None:
        state.status_text = t("modules.list.empty_card_body")
        state.status_kind = "warn"
        shell.rebuild_current_screen()
        return
    try:
        provider = state.sample_provider_factory()
    except Exception:  # noqa: BLE001
        logger.exception("ModulePassport: sample provider factory raised")
        state.status_text = t("modules.wizard.save_failed")
        state.status_kind = "warn"
        shell.rebuild_current_screen()
        return
    orchestrator = CharacterizationOrchestrator(
        side=side,
        sample_provider=provider,
    )
    orchestrator.start()
    state.orchestrator = orchestrator
    state.wizard_side = side
    state.view = VIEW_WIZARD
    state.status_text = ""
    state.status_kind = "info"
    shell.rebuild_current_screen()


def _on_wizard_proceed(shell) -> None:
    state = _ensure_state(shell)
    if state.orchestrator is None:
        return
    try:
        state.orchestrator.proceed_to_step()
    except RuntimeError:
        logger.exception("ModulePassport: proceed_to_step from invalid state")
    shell.rebuild_current_screen()


def _on_wizard_cancel(shell) -> None:
    state = _ensure_state(shell)
    if state.orchestrator is not None:
        state.orchestrator.cancel()
    shell.rebuild_current_screen()


def _on_wizard_save(shell) -> None:
    state = _ensure_state(shell)
    service = _service(shell)
    orchestrator = state.orchestrator
    if service is None or orchestrator is None:
        state.status_text = t("modules.unavailable")
        state.status_kind = "warn"
        shell.rebuild_current_screen()
        return
    fingerprint = orchestrator.fingerprint
    if fingerprint is None:
        state.status_text = t("modules.wizard.save_failed")
        state.status_kind = "warn"
        shell.rebuild_current_screen()
        return
    updated = service.append_fingerprint(orchestrator.side, fingerprint)
    if updated is None:
        state.status_text = t("modules.wizard.save_failed")
        state.status_kind = "warn"
        shell.rebuild_current_screen()
        return
    state.orchestrator = None
    state.wizard_side = None
    state.view = VIEW_LIST
    state.status_text = ""
    state.status_kind = "info"
    shell.rebuild_current_screen()


def _on_wizard_discard(shell) -> None:
    state = _ensure_state(shell)
    if state.orchestrator is not None:
        state.orchestrator.reset()
    state.orchestrator = None
    state.wizard_side = None
    state.view = VIEW_LIST
    shell.rebuild_current_screen()


# ---------------------------------------------------------------------------
# Assign modal (first-time + swap)
# ---------------------------------------------------------------------------


def _open_assign_modal(shell, side: str, *, is_swap: bool) -> None:
    state = _ensure_state(shell)
    state.pending_module_id = ""
    state.pending_notes = ""
    try:
        if dpg.does_item_exist(TAG_ASSIGN_MODAL):
            dpg.delete_item(TAG_ASSIGN_MODAL)
    except SystemError:
        return
    side_label = t(f"modules.side.{side}")
    title_key = "modules.assign.title_swap" if is_swap else "modules.assign.title_assign"
    body_key = "modules.assign.body_swap" if is_swap else "modules.assign.body_assign"
    try:
        with dpg.window(
            tag=TAG_ASSIGN_MODAL,
            label=t(title_key, side=side_label),
            modal=True,
            no_close=False,
            no_resize=True,
            width=560,
            height=380,
        ):
            dpg.add_text(t(body_key), wrap=520)
            dpg.add_spacer(height=8)
            dpg.add_text(t("modules.assign.module_id_label"))
            dpg.add_input_text(
                tag=TAG_ASSIGN_INPUT_ID,
                width=520,
                hint=t("modules.assign.module_id_hint"),
            )
            dpg.add_spacer(height=8)
            dpg.add_text(t("modules.assign.notes_label"))
            dpg.add_input_text(
                tag=TAG_ASSIGN_INPUT_NOTES,
                multiline=True,
                width=520,
                height=120,
                hint=t("modules.assign.notes_hint"),
            )
            dpg.add_spacer(height=8)
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label=t("modules.assign.save"),
                    tag=TAG_ASSIGN_SAVE,
                    width=160,
                    callback=lambda *_a, s=side: _on_save_assign(shell, s),
                )
                dpg.add_button(
                    label=t("modules.assign.cancel"),
                    tag=TAG_ASSIGN_CANCEL,
                    width=120,
                    callback=lambda *_a: _close_assign_modal(),
                )
    except SystemError:
        return


def _close_assign_modal() -> None:
    try:
        if dpg.does_item_exist(TAG_ASSIGN_MODAL):
            dpg.delete_item(TAG_ASSIGN_MODAL)
    except SystemError:
        pass


def _on_save_assign(shell, side: str) -> None:
    state = _ensure_state(shell)
    service = _service(shell)
    if service is None:
        state.status_text = t("modules.unavailable")
        state.status_kind = "warn"
        _close_assign_modal()
        shell.rebuild_current_screen()
        return
    raw_id = ""
    raw_notes = ""
    try:
        raw_id = dpg.get_value(TAG_ASSIGN_INPUT_ID) or ""
        raw_notes = dpg.get_value(TAG_ASSIGN_INPUT_NOTES) or ""
    except SystemError:
        pass
    result = _save_assign(shell, side, raw_id, raw_notes)
    if result is None:
        # Keep the modal open so the user can adjust.
        shell.rebuild_current_screen()
        return
    _close_assign_modal()
    shell.rebuild_current_screen()


def _save_assign(
    shell, side: str, raw_id: str, raw_notes: str
) -> Optional[ModulePassport]:
    """Pure-ish helper exposed for unit tests of the assign flow."""

    state = _ensure_state(shell)
    service = _service(shell)
    if service is None:
        state.status_text = t("modules.unavailable")
        state.status_kind = "warn"
        return None
    passport = service.assign(side, raw_id, raw_notes)
    if passport is None:
        # Empty module_id -> show empty_warning; else generic save_failed.
        from zd_app.storage.module_passport_models import sanitize_module_id

        if not sanitize_module_id(raw_id):
            state.status_text = t("modules.assign.empty_warning")
        else:
            state.status_text = t("modules.assign.save_failed")
        state.status_kind = "warn"
        return None
    state.status_text = ""
    state.status_kind = "info"
    return passport


# ---------------------------------------------------------------------------
# Notes modal
# ---------------------------------------------------------------------------


def _open_notes_modal(shell, side: str) -> None:
    state = _ensure_state(shell)
    service = _service(shell)
    if service is None:
        state.status_text = t("modules.unavailable")
        state.status_kind = "warn"
        shell.rebuild_current_screen()
        return
    passport = service.get(side)
    if passport is None:
        state.status_text = t("modules.list.empty_card_body")
        state.status_kind = "warn"
        shell.rebuild_current_screen()
        return
    state.pending_notes = passport.notes
    try:
        if dpg.does_item_exist(TAG_NOTES_MODAL):
            dpg.delete_item(TAG_NOTES_MODAL)
    except SystemError:
        return
    try:
        with dpg.window(
            tag=TAG_NOTES_MODAL,
            label=t("modules.notes.title", module_id=passport.module_id),
            modal=True,
            no_close=False,
            no_resize=True,
            width=560,
            height=340,
        ):
            dpg.add_text(t("modules.notes.body"), wrap=520)
            dpg.add_spacer(height=8)
            dpg.add_input_text(
                tag=TAG_NOTES_INPUT,
                multiline=True,
                width=520,
                height=180,
                hint=t("modules.notes.hint"),
                default_value=passport.notes,
            )
            dpg.add_spacer(height=8)
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label=t("modules.notes.save"),
                    tag=TAG_NOTES_SAVE,
                    width=160,
                    callback=lambda *_a, s=side: _on_save_notes(shell, s),
                )
                dpg.add_button(
                    label=t("modules.notes.cancel"),
                    tag=TAG_NOTES_CANCEL,
                    width=120,
                    callback=lambda *_a: _close_notes_modal(),
                )
    except SystemError:
        return


def _close_notes_modal() -> None:
    try:
        if dpg.does_item_exist(TAG_NOTES_MODAL):
            dpg.delete_item(TAG_NOTES_MODAL)
    except SystemError:
        pass


def _on_save_notes(shell, side: str) -> None:
    state = _ensure_state(shell)
    service = _service(shell)
    if service is None:
        state.status_text = t("modules.unavailable")
        state.status_kind = "warn"
        _close_notes_modal()
        shell.rebuild_current_screen()
        return
    raw_notes = ""
    try:
        raw_notes = dpg.get_value(TAG_NOTES_INPUT) or ""
    except SystemError:
        pass
    updated = service.update_notes(side, raw_notes)
    if updated is None:
        state.status_text = t("modules.notes.save_failed")
        state.status_kind = "warn"
    else:
        state.status_text = ""
        state.status_kind = "info"
    _close_notes_modal()
    shell.rebuild_current_screen()


# ---------------------------------------------------------------------------
# Frame refresh (wizard only — list/detail rebuild on user action)
# ---------------------------------------------------------------------------


def _schedule_wizard_refresh(shell) -> None:
    try:
        next_frame = dpg.get_frame_count() + _REFRESH_FRAME_INTERVAL
    except SystemError:
        return

    def _tick(*_args) -> None:
        if not dpg.does_item_exist(TAG_ROOT_CONTAINER):
            return
        state = _ensure_state(shell)
        orchestrator = state.orchestrator
        if state.view != VIEW_WIZARD or orchestrator is None:
            return
        prior_state = orchestrator.state
        try:
            orchestrator.tick()
        except Exception:  # noqa: BLE001
            logger.exception("ModulePassport orchestrator.tick() raised")
        if orchestrator.state != prior_state:
            shell.rebuild_current_screen()
            return
        _refresh_wizard_widgets(orchestrator)
        _schedule_wizard_refresh(shell)

    try:
        dpg.set_frame_callback(next_frame, _tick)
    except SystemError:
        return


def _refresh_wizard_widgets(orchestrator: CharacterizationOrchestrator) -> None:
    if orchestrator.state not in (
        CharacterizationState.REST,
        CharacterizationState.ROTATION,
        CharacterizationState.SETTLE,
    ):
        return
    if not dpg.does_item_exist(TAG_WIZARD_PROGRESS_BAR):
        return
    progress = orchestrator.progress()
    dpg.set_value(TAG_WIZARD_PROGRESS_BAR, progress.phase_fraction)
    if dpg.does_item_exist(TAG_WIZARD_REMAINING):
        dpg.set_value(
            TAG_WIZARD_REMAINING,
            t(
                "modules.wizard.running.remaining_label",
                seconds=f"{max(0.0, progress.phase_duration_s - progress.phase_elapsed_s):.1f}",
            ),
        )
    if dpg.does_item_exist(TAG_WIZARD_SAMPLES):
        dpg.set_value(
            TAG_WIZARD_SAMPLES,
            t(
                "modules.wizard.running.samples_label",
                n=progress.samples_collected,
            ),
        )
    phase = progress.phase
    if phase is not None and dpg.does_item_exist(TAG_WIZARD_PHASE):
        dpg.set_value(
            TAG_WIZARD_PHASE,
            t(
                "modules.wizard.running.phase_label",
                n=_PHASE_NUMBER[phase],
                phase=t(_PHASE_I18N_KEY[phase]),
            ),
        )


# ---------------------------------------------------------------------------
# Export Diagnostic Bundle modal
# ---------------------------------------------------------------------------


def _open_export_modal(shell) -> None:
    state = _ensure_state(shell)
    bundle = _bundle_service(shell)
    if bundle is None:
        state.status_text = t("modules.export.unavailable")
        state.status_kind = "warn"
        shell.rebuild_current_screen()
        return
    try:
        if dpg.does_item_exist(TAG_EXPORT_MODAL):
            dpg.delete_item(TAG_EXPORT_MODAL)
    except SystemError:
        return
    try:
        with dpg.window(
            tag=TAG_EXPORT_MODAL,
            label=t("modules.export.modal_title"),
            modal=True,
            no_close=False,
            no_resize=True,
            width=600,
            height=420,
        ):
            dpg.add_text(t("modules.export.modal_body"), wrap=560)
            dpg.add_spacer(height=8)
            dpg.add_checkbox(
                tag=TAG_EXPORT_INCLUDE_ARCHIVED,
                label=t("modules.export.include_archived"),
                default_value=bool(state.export_include_archived),
            )
            dpg.add_spacer(height=6)
            dpg.add_text(t("modules.export.health_limit_label"))
            dpg.add_combo(
                tag=TAG_EXPORT_HEALTH_LIMIT,
                items=list(_HEALTH_REPORT_LIMIT_CHOICES),
                default_value=str(int(state.export_health_limit)),
                width=120,
            )
            dpg.add_spacer(height=6)
            dpg.add_text(t("modules.export.wear_window_label"))
            dpg.add_combo(
                tag=TAG_EXPORT_WEAR_WINDOW,
                items=list(_WEAR_WINDOW_CHOICES),
                default_value=str(int(state.export_wear_window_days)),
                width=120,
            )
            dpg.add_spacer(height=10)
            dpg.add_text(
                t("modules.export.claim_boundary_footer"),
                color=shell.COLORS["muted"],
                wrap=560,
            )
            dpg.add_spacer(height=10)
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label=t("modules.export.generate_md_button"),
                    tag=TAG_EXPORT_GENERATE_MD,
                    width=200,
                    callback=lambda *_a: _on_generate_markdown(shell),
                )
                dpg.add_button(
                    label=t("modules.export.generate_zip_button"),
                    tag=TAG_EXPORT_GENERATE_ZIP,
                    width=200,
                    callback=lambda *_a: _on_generate_zip(shell),
                )
                dpg.add_button(
                    label=t("modules.export.cancel_button"),
                    tag=TAG_EXPORT_CANCEL,
                    width=120,
                    callback=lambda *_a: _close_export_modal(),
                )
    except SystemError:
        return


def _close_export_modal() -> None:
    try:
        if dpg.does_item_exist(TAG_EXPORT_MODAL):
            dpg.delete_item(TAG_EXPORT_MODAL)
    except SystemError:
        pass


def _read_export_form(state: ModulesScreenState) -> None:
    """Pull modal-input values back into state. Tolerates missing widgets."""

    try:
        state.export_include_archived = bool(
            dpg.get_value(TAG_EXPORT_INCLUDE_ARCHIVED)
        )
    except SystemError:
        pass
    try:
        raw_limit = str(dpg.get_value(TAG_EXPORT_HEALTH_LIMIT) or "")
        if raw_limit:
            state.export_health_limit = int(raw_limit)
    except (SystemError, ValueError):
        pass
    try:
        raw_window = str(dpg.get_value(TAG_EXPORT_WEAR_WINDOW) or "")
        if raw_window:
            state.export_wear_window_days = int(raw_window)
    except (SystemError, ValueError):
        pass


def _on_generate_markdown(shell) -> None:
    state = _ensure_state(shell)
    bundle = _bundle_service(shell)
    if bundle is None:
        state.status_text = t("modules.export.unavailable")
        state.status_kind = "warn"
        _close_export_modal()
        shell.rebuild_current_screen()
        return
    _read_export_form(state)
    target = _generate_markdown(shell, bundle, state)
    if target is None:
        state.status_text = t("modules.export.generate_failed")
        state.status_kind = "warn"
    else:
        state.status_text = t(
            "modules.export.generate_success",
            filename=target.name,
        )
        state.status_kind = "info"
        _try_open_explorer(target.parent)
    _close_export_modal()
    shell.rebuild_current_screen()


def _on_generate_zip(shell) -> None:
    state = _ensure_state(shell)
    bundle = _bundle_service(shell)
    if bundle is None:
        state.status_text = t("modules.export.unavailable")
        state.status_kind = "warn"
        _close_export_modal()
        shell.rebuild_current_screen()
        return
    _read_export_form(state)
    _open_zip_preview(shell, bundle, state)


def _open_zip_preview(
    shell,
    bundle: DiagnosticBundleService,
    state: ModulesScreenState,
) -> None:
    try:
        manifest = bundle.preview_bundle_manifest(
            include_archived=bool(state.export_include_archived),
            health_report_limit=int(state.export_health_limit),
            wear_ledger_days=int(state.export_wear_window_days),
            device_identity=_device_identity(shell),
        )
    except Exception:  # noqa: BLE001 — surface as generate_failed
        logger.exception("modules.export: preview manifest raised")
        state.status_text = t("modules.export.generate_failed")
        state.status_kind = "warn"
        _close_export_modal()
        shell.rebuild_current_screen()
        return

    def _open() -> None:
        diagnostic_bundle_preview.open_preview_modal(
            shell,
            manifest,
            on_export=lambda: _finish_generate_zip(shell, bundle, state),
            on_cancel=lambda: shell.rebuild_current_screen(),
        )

    modal_swap = getattr(shell, "_defer_modal_swap", None)
    if callable(modal_swap):
        modal_swap(
            _open,
            delete_tags=(
                TAG_EXPORT_MODAL,
                diagnostic_bundle_preview.PREVIEW_MODAL_TAG,
            ),
            key="modules_export_zip_preview",
        )
    else:
        _close_export_modal()
        _open()


def _finish_generate_zip(
    shell,
    bundle: DiagnosticBundleService,
    state: ModulesScreenState,
) -> None:
    target = _generate_zip(shell, bundle, state)
    if target is None:
        state.status_text = t("modules.export.generate_failed")
        state.status_kind = "warn"
    else:
        state.status_text = t(
            "modules.export.generate_success",
            filename=target.name,
        )
        state.status_kind = "info"
        _try_open_explorer(target.parent)
    shell.rebuild_current_screen()


def _bundle_target_path(bundle: DiagnosticBundleService, suffix: str) -> Path:
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    return bundle.base_dir / f"diagnostic_bundle_{stamp}{suffix}"


def _generate_markdown(
    shell,
    bundle: DiagnosticBundleService,
    state: ModulesScreenState,
) -> Optional[Path]:
    """Test-friendly Markdown generation wrapper."""

    try:
        bundle.base_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("modules.export: failed to ensure base_dir")
        return None
    try:
        markdown = bundle.generate_markdown(
            include_archived=bool(state.export_include_archived),
            health_report_limit=int(state.export_health_limit),
            wear_ledger_days=int(state.export_wear_window_days),
            device_identity=_device_identity(shell),
        )
    except Exception:  # noqa: BLE001 — surface as generate_failed
        logger.exception("modules.export: generate_markdown raised")
        return None
    target = _bundle_target_path(bundle, ".md")
    try:
        target.write_text(markdown, encoding="utf-8")
    except OSError:
        logger.exception(
            "modules.export: failed to write %r", str(target)
        )
        return None
    bundle.emit_generated_event(
        output_filename=target.name,
        bundle_format="md",
        include_archived=bool(state.export_include_archived),
        health_report_limit=int(state.export_health_limit),
        wear_ledger_days=int(state.export_wear_window_days),
    )
    return target


def _generate_zip(
    shell,
    bundle: DiagnosticBundleService,
    state: ModulesScreenState,
) -> Optional[Path]:
    """Test-friendly ZIP generation wrapper."""

    target = _bundle_target_path(bundle, ".zip")
    try:
        result = bundle.generate_bundle_zip(
            target,
            include_archived=bool(state.export_include_archived),
            health_report_limit=int(state.export_health_limit),
            wear_ledger_days=int(state.export_wear_window_days),
            device_identity=_device_identity(shell),
        )
    except Exception:  # noqa: BLE001
        logger.exception("modules.export: generate_bundle_zip raised")
        return None
    if result is None:
        return None
    bundle.emit_generated_event(
        output_filename=result.name,
        bundle_format="zip",
        include_archived=bool(state.export_include_archived),
        health_report_limit=int(state.export_health_limit),
        wear_ledger_days=int(state.export_wear_window_days),
    )
    return result


def _try_open_explorer(path: Path) -> None:
    """Best-effort File Explorer open. Mirrors the crash-review pattern."""

    if not hasattr(os, "startfile"):
        return
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except OSError:
        logger.exception("modules.export: failed to open File Explorer at %r", str(path))


__all__ = [
    "DELTA_THRESHOLD_ASYMMETRY_SCORE",
    "DELTA_THRESHOLD_BITNESS",
    "DELTA_THRESHOLD_CENTERING_EUCLIDEAN",
    "DELTA_THRESHOLD_CIRCULARITY_PERCENT",
    "DELTA_THRESHOLD_LINEARITY",
    "DELTA_THRESHOLD_NOISE_FLOOR_PERCENT",
    "DELTA_THRESHOLD_OUTER_DEADZONE_SPAN",
    "DELTA_THRESHOLD_TREMOR",
    "ModulesScreenState",
    "SPARKLINE_MAX_POINTS",
    "VIEW_ARCHIVED_DETAIL",
    "VIEW_ARCHIVED_LIST",
    "VIEW_COMPARE",
    "VIEW_DETAIL",
    "VIEW_LIST",
    "VIEW_MODULE_TRENDS",
    "VIEW_WIZARD",
    "build",
]
