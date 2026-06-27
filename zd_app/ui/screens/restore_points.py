"""Restore Points screen — list / detail / confirm / in-progress / result.

State-pure layout dispatcher per the "Restore UX" design. The screen reads
``shell.restore_points_screen_state.view`` and renders only the layout for
that state; on each transition the shell calls
:func:`build` again to render the new layout (mirroring the health-report screen's pattern in
:mod:`zd_app.ui.screens.health_report`).

State machine (the "Restore UX" design):

    LIST -> DETAIL -> CONFIRM -> IN_PROGRESS -> RESULT
       ^_______________________________________|

Service surface:

- ``shell.restore_point_service`` — :class:`~zd_app.services.restore_point_service.RestorePointService`
  (or ``None`` if the wrapper is running without a settings service, in which
  case the screen renders an "unavailable" placeholder).
- ``shell.restore_points_screen_state`` — :class:`RestorePointsScreenState`,
  the small view-state container manipulated by callbacks here. Initialised
  lazily on first build so tests can construct an AppShell without
  pre-populating it.

The IN_PROGRESS state is visible because the click handler defers the actual
:meth:`RestorePointService.restore` call to a frame callback after the
IN_PROGRESS frame has rendered, then transitions to RESULT.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import dearpygui.dearpygui as dpg

from zd_app.i18n import t
from zd_app.services.restore_points import (
    CLAIM_BOUNDARY_PARAGRAPH,
    CLAIM_BOUNDARY_SHORT_UI,
)
from zd_app.storage.restore_point_models import (
    CaptureSource,
    CoverageCategory,
    CoverageState,
    RestorePoint,
    RestorePreview,
    RestoreResult,
    RestoreResultLabel,
    SkippedFile,
    restore_point_to_dict,
)
from zd_app.ui.components import (
    FOOTER_RESERVE_PX,
    Column,
    action_button,
    card,
    table,
    table_empty_state,
)
from zd_app.ui.themes import SPACE_LG, SPACE_MD
from zd_app.ui.typography import helper_text, screen_title, section_title


logger = logging.getLogger(__name__)

# Vertical-rhythm policy for this screen: route every add_spacer through the
# themes.py spacing scale instead of ad-hoc pixel numbers. Two tiers are in
# use here — SPACE_MD (8px) for gaps within a block (title→content, between
# adjacent rows) and SPACE_LG (16px) between sections or before an action
# button row. The block panels themselves use card() (autosize + the shared
# card-internal left-alignment origin) rather than hand-computed heights.


VIEW_LIST = "LIST"
VIEW_DETAIL = "DETAIL"
VIEW_CONFIRM = "CONFIRM"
VIEW_IN_PROGRESS = "IN_PROGRESS"
VIEW_RESULT = "RESULT"


TAG_ROOT_CONTAINER = "restore_points_root"
TAG_STATUS_TEXT = "restore_points_status_text"
TAG_LIST_REFRESH_BUTTON = "restore_points_list_refresh_button"
TAG_LIST_MANUAL_SAVE_BUTTON = "restore_points_list_manual_save_button"
TAG_LIST_TABLE = "restore_points_list_table"
TAG_LIST_FOOTER_CAVEAT = "restore_points_list_footer_caveat"
TAG_LIST_SKIPPED_FOOTER = "restore_points_list_skipped_footer"
TAG_LIST_SKIPPED_TOGGLE = "restore_points_list_skipped_toggle"
TAG_DETAIL_BACK_BUTTON = "restore_points_detail_back_button"
TAG_DETAIL_EXPORT_BUTTON = "restore_points_detail_export_button"
TAG_DETAIL_RESTORE_BUTTON = "restore_points_detail_restore_button"
TAG_CONFIRM_RESTORE_BUTTON = "restore_points_confirm_restore_button"
TAG_CONFIRM_VIEW_BUTTON = "restore_points_confirm_view_button"
TAG_CONFIRM_CANCEL_BUTTON = "restore_points_confirm_cancel_button"
TAG_RESULT_CLOSE_BUTTON = "restore_points_result_close_button"
TAG_RESULT_SAVE_DIAG_BUTTON = "restore_points_result_save_diag_button"
TAG_DELETE_CONFIRM_MODAL = "restore_points_delete_confirm_modal"
TAG_DELETE_CONFIRM_CONFIRM_BUTTON = "restore_points_delete_confirm_confirm"
TAG_DELETE_CONFIRM_CANCEL_BUTTON = "restore_points_delete_confirm_cancel"


_TRIGGER_LABEL_KEYS = {
    "first_readable_connect": "restore_points.trigger.first_readable_connect",
    "before_safe_import_apply": "restore_points.trigger.before_safe_import_apply",
    "before_profile_apply_with_device_settings": (
        "restore_points.trigger.before_profile_apply_with_device_settings"
    ),
    "before_manual_device_setting_write": (
        "restore_points.trigger.before_manual_device_setting_write"
    ),
    "before_restore": "restore_points.trigger.before_restore",
    "manual": "restore_points.trigger.manual",
}

_CAPTURE_SOURCE_LABEL_KEYS = {
    CaptureSource.FRESH_READ: "restore_points.detail.capture_source.fresh_read",
    CaptureSource.CACHED_SNAPSHOT: "restore_points.detail.capture_source.cached_snapshot",
    CaptureSource.PARTIAL_READ: "restore_points.detail.capture_source.partial_read",
    CaptureSource.FAILED_READ_NO_SNAPSHOT: (
        "restore_points.detail.capture_source.failed_read_no_snapshot"
    ),
}

_CATEGORY_LABEL_KEYS = {
    CoverageCategory.DEVICE: "restore_points.detail.field_category.device",
    CoverageCategory.FEEL: "restore_points.detail.field_category.feel",
    CoverageCategory.LAYOUT: "restore_points.detail.field_category.layout",
    CoverageCategory.COSMETIC: "restore_points.detail.field_category.cosmetic",
    CoverageCategory.METADATA: "restore_points.detail.field_category.metadata",
    CoverageCategory.UNSUPPORTED: "restore_points.detail.field_category.unsupported",
}

_RESULT_HEADER_KEYS = {
    RestoreResultLabel.VERIFIED: "restore_points.result.header.verified",
    RestoreResultLabel.RESTORED_WITH_WARNINGS: (
        "restore_points.result.header.restored_with_warnings"
    ),
    RestoreResultLabel.PARTIALLY_RESTORED: (
        "restore_points.result.header.partially_restored"
    ),
    RestoreResultLabel.MISMATCH_AFTER_RESTORE: (
        "restore_points.result.header.mismatch_after_restore"
    ),
    RestoreResultLabel.RESTORE_FAILED: "restore_points.result.header.restore_failed",
}


@dataclass
class RestorePointsScreenState:
    """Per-shell view state for the Restore Points screen.

    Held on the shell (not on the service) because the service is stateless
    w.r.t. UI navigation — capture/restore are pure RPC operations and the
    screen needs to remember which row is selected, which view is active,
    and whether the skipped-files disclosure is expanded.
    """

    view: str = VIEW_LIST
    selected_rp_id: Optional[str] = None
    result: Optional[RestoreResult] = None
    skipped_expanded: bool = False
    status_text: str = ""
    status_kind: str = "info"


def _ensure_state(shell) -> RestorePointsScreenState:
    state = getattr(shell, "restore_points_screen_state", None)
    if state is None:
        state = RestorePointsScreenState()
        shell.restore_points_screen_state = state
    return state


# ---------------------------------------------------------------------------
# Top-level build
# ---------------------------------------------------------------------------


def build(shell, parent: str) -> None:
    """Render the Restore Points screen for ``shell.restore_points_screen_state.view``.

    The shell is responsible for tearing down the previous render before
    calling :func:`build` again on a view change. The build function only
    handles the *current* view's layout.
    """

    state = _ensure_state(shell)
    service = getattr(shell, "restore_point_service", None)

    with dpg.child_window(
        parent=parent,
        tag=TAG_ROOT_CONTAINER,
        autosize_x=True,
        autosize_y=True,
        border=False,
    ):
        if service is None:
            screen_title(t("restore_points.title"))
            dpg.add_spacer(height=SPACE_MD)
            dpg.add_text(t("restore_points.list.unavailable"), wrap=900)
            return

        if state.view == VIEW_LIST:
            _build_list(shell, service, state)
        elif state.view == VIEW_DETAIL:
            _build_detail(shell, service, state)
        elif state.view == VIEW_CONFIRM:
            _build_confirm(shell, service, state)
        elif state.view == VIEW_IN_PROGRESS:
            _build_in_progress(shell, service, state)
        elif state.view == VIEW_RESULT:
            _build_result(shell, service, state)
        else:
            dpg.add_text(f"Unexpected restore-points view: {state.view}")


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------


def _build_list(shell, service, state: RestorePointsScreenState) -> None:
    screen_title(t("restore_points.title"))
    helper_text(t("restore_points.subtitle"), wrap=900)
    dpg.add_spacer(height=SPACE_MD)
    # Manual capture lives here (next to Refresh), not on Diagnostics: Restore
    # Points are a trust/recovery feature, and the empty-state copy already
    # tells users they "can save one manually after reading the controller."
    # The button delegates to AppShell.manual_save_restore_point, which refuses
    # with the footer busy banner while a threaded HID job is in flight.
    with dpg.group(horizontal=True):
        dpg.add_button(
            label=t("restore_points.list.refresh"),
            tag=TAG_LIST_REFRESH_BUTTON,
            width=120,
            callback=lambda: _on_refresh(shell),
        )
        dpg.add_button(
            label=t("restore_points.manual.button"),
            tag=TAG_LIST_MANUAL_SAVE_BUTTON,
            width=180,
            callback=lambda: _on_manual_save(shell),
        )
    dpg.add_text(
        t("restore_points.manual.helper"),
        color=shell.COLORS["muted"],
        wrap=900,
    )
    dpg.add_spacer(height=SPACE_MD)

    if state.status_text:
        dpg.add_text(
            state.status_text,
            tag=TAG_STATUS_TEXT,
            color=shell.COLORS["muted"]
            if state.status_kind == "info"
            else shell.COLORS["warn"],
            wrap=900,
        )
        dpg.add_spacer(height=SPACE_MD)

    try:
        valid, skipped = service.list_with_skipped()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Restore-points list failed")
        dpg.add_text(
            t("restore_points.manual.failure", reason=str(exc)),
            color=shell.COLORS["warn"],
            wrap=900,
        )
        return

    if not valid and not skipped:
        table_empty_state(t("restore_points.list.empty_state"))
        _build_footer_caveat(shell)
        return

    # Scroll discipline: the saved-points table is the ONE scroll surface. In the
    # common case (valid points, no skipped files) the table card fills the height
    # remaining BELOW the top controls MINUS a reserve for the pinned footer
    # caveat (card height=-FOOTER_RESERVE_PX), so it scrolls INTERNALLY on
    # overflow while the page (the autosize_y screen root) does NOT scroll and the
    # caveat stays visible — no second far-right scrollbar. (Before this, the
    # card used the legacy autosize_y fill and the caveat sat BELOW it, pushing
    # the page past the window: a page scrollbar PLUS the card's own.)
    #
    # When skipped files are present (a rare, degraded state: corrupt/unreadable
    # RP files) the bottom carries a variable-height disclosure card too, so a
    # fixed footer reserve can't keep everything on one page. There the table
    # card is content-fit instead and the whole page scrolls as a single bar —
    # still one scrollbar, just the page's, with the disclosure + caveat below.
    if valid and not skipped:
        _build_list_table(shell, valid, height=-FOOTER_RESERVE_PX)
        _build_footer_caveat(shell)
        return

    if valid:
        _build_list_table(shell, valid, fit=True)

    if skipped:
        dpg.add_spacer(height=SPACE_MD)
        _build_skipped_card(shell, state, skipped)

    _build_footer_caveat(shell)


def _build_footer_caveat(shell) -> None:
    """The muted claim-boundary caveat pinned at the bottom of the LIST view."""

    dpg.add_spacer(height=SPACE_LG)
    dpg.add_text(
        t("restore_points.list.footer_caveat"),
        tag=TAG_LIST_FOOTER_CAVEAT,
        color=shell.COLORS["muted"],
        wrap=900,
    )


def _build_list_table(
    shell, valid: list[RestorePoint], *, height: int = 0, fit: bool = False
) -> None:
    """Render the saved restore points as one native ``dpg.table``.

    Card-based layout: replaces the prior one-``child_window``-per-RP stack
    with a single table inside a :func:`~zd_app.ui.components.card`.
    The native table gives aligned columns, a header row, and alternating row
    backgrounds, and collapses the whole list to one widget — honoring the
    roadmap's "keep widget counts lean" guardrail so the overhaul doesn't undo
    the nav-responsiveness work. Per-row View / Restore / Export / Delete
    actions stay on each row in a fixed-width Actions column (see
    :func:`_build_list_row`).

    ``height`` / ``fit`` forward to :func:`card`: the LIST view passes
    ``height=-FOOTER_RESERVE_PX`` so the card fills the space below the top
    controls minus the pinned-footer reserve and scrolls internally (one
    scrollbar, page does not scroll), or ``fit=True`` in the skipped-files path
    so the card grows to content and the page scrolls as one bar instead.
    """

    with card(height=height, fit=fit):
        # Text columns stretch to fill the card width (kills the empty
        # horizontal space the screenshot review flagged); the Actions column
        # is fixed-width so the four action buttons can never be clipped — the
        # local testing showed Restore becoming unreachable when its
        # container under-sized, so button reachability is pinned, not
        # stretched. 370 fits the en labels (the wide locale: ~291px of
        # auto-width buttons + 3×8 ItemSpacing + 2×10 cell padding ≈ 335)
        # with headroom; zh-CN labels are narrower.
        with table(
            [
                Column(t("restore_points.list.col.name"), weight=0.34),
                Column(t("restore_points.list.col.created"), weight=0.22),
                Column(t("restore_points.list.col.coverage"), weight=0.22),
                Column(t("restore_points.list.col.last_restored"), weight=0.22),
                Column(
                    t("restore_points.list.col.actions"), width=370, no_resize=True
                ),
            ],
            tag=TAG_LIST_TABLE,
            borders_outerV=False,
        ):
            for rp in valid:
                _build_list_row(shell, rp)


def _build_list_row(shell, rp: RestorePoint) -> None:
    """One table row for a restore point. Stacks title + trigger/identity in
    the first cell, then created_at / coverage / last-restore-status cells, and
    the View / Restore / Export / Delete action group in the final cell.
    """

    with dpg.table_row():
        # Name cell: title in primary text + a muted trigger/identity sub-line.
        # Title is text.primary (not accent) — Phase 0/2 reserves accent for
        # actionable/active elements, so size + weight carry the hierarchy.
        with dpg.group():
            dpg.add_text(rp.title)
            identity = rp.device_identity.product_string or ""
            firmware = rp.device_identity.firmware_version or ""
            identity_text = (
                f"{identity}  fw {firmware}"
                if identity and firmware
                else identity or firmware
            )
            sub = f"[{_trigger_label(rp.trigger.type)}]"
            if identity_text:
                sub = f"{sub}  {identity_text}"
            dpg.add_text(sub, color=shell.COLORS["muted"])
        dpg.add_text(rp.created_at, color=shell.COLORS["muted"])
        dpg.add_text(
            t(
                "restore_points.list.row.coverage",
                captured=rp.coverage.captured_supported_count,
                total=rp.coverage.total_supported_count,
            ),
            color=shell.COLORS["muted"],
        )
        # Last-restore status, color-cued as a light status chip (verified =
        # good, mismatch/failed = warn, otherwise muted).
        dpg.add_text(_last_restore_label(rp), color=_last_restore_color(shell, rp))
        # Actions cell. The ``lambda *args, rp_id=rp_id`` idiom is load-bearing:
        # DPG passes one positional per POSITIONAL_OR_KEYWORD param, so *args
        # (VAR_POSITIONAL) makes it pass zero positionals and the closure-
        # captured rp_id survives. Do NOT switch to the ``user_data`` idiom here
        # (see ListRowButtonDpgSignatureTests). Buttons are auto-width so en +
        # zh-CN labels both fit inside the fixed Actions column.
        with dpg.group(horizontal=True):
            rp_id = rp.id
            rp_title = rp.title
            dpg.add_button(
                label=t("restore_points.list.row.view"),
                callback=lambda *args, rp_id=rp_id: _on_view(shell, rp_id),
            )
            dpg.add_button(
                label=t("restore_points.list.row.restore"),
                callback=lambda *args, rp_id=rp_id: _on_open_confirm(shell, rp_id),
            )
            dpg.add_button(
                label=t("restore_points.list.row.export"),
                callback=lambda *args, rp_id=rp_id: _on_export_rp(shell, rp_id),
            )
            action_button(
                t("restore_points.list.row.delete"),
                destructive=True,
                callback=lambda *args, rp_id=rp_id, rp_title=rp_title: (
                    _on_open_delete_confirm(shell, rp_id, rp_title)
                ),
            )


def _build_skipped_card(shell, state: RestorePointsScreenState, skipped) -> None:
    """Skipped-files disclosure as an auto-sizing :func:`card`.

    Replaces the prior hand-computed ``child_window`` height. Magic-number
    heights clipped rows twice (caught in local testing); a content-fit
    ``card(fit=True)`` measures rendered content each frame, so every skipped
    entry stays visible at any N / locale without an inner scrollbar.

    ``fit=True`` (DPG-2.x content-fit) rather than the legacy ``autosize_y``
    fill: this card stacks ABOVE the footer caveat in the skipped-files path, so
    it must shrink to its content (not balloon to fill the page and shove the
    caveat off-screen). It also coexists there with the content-fit list table,
    and two fill cards stacked would starve the second of height.
    """

    with card(fit=True):
        dpg.add_text(
            t("restore_points.list.skipped_footer", n=len(skipped)),
            tag=TAG_LIST_SKIPPED_FOOTER,
            color=shell.COLORS["muted"],
        )
        toggle_label = (
            t("restore_points.list.skipped_hide_details")
            if state.skipped_expanded
            else t("restore_points.list.skipped_show_details")
        )
        dpg.add_button(
            label=toggle_label,
            tag=TAG_LIST_SKIPPED_TOGGLE,
            width=160,
            callback=lambda: _on_toggle_skipped(shell),
        )
        if state.skipped_expanded:
            for skip in skipped:
                dpg.add_text(
                    f"- {Path(skip.path).name}: {skip.error}",
                    color=shell.COLORS["muted"],
                    wrap=860,
                )


# ---------------------------------------------------------------------------
# DETAIL
# ---------------------------------------------------------------------------


def _build_detail(shell, service, state: RestorePointsScreenState) -> None:
    rp = _load_selected(service, state)
    if rp is None:
        _back_to_list(shell, status=t("restore_points.list.unavailable"), status_kind="warn")
        return

    dpg.add_button(
        label=t("restore_points.detail.back"),
        tag=TAG_DETAIL_BACK_BUTTON,
        width=140,
        callback=lambda: _on_back_to_list(shell),
    )
    dpg.add_spacer(height=SPACE_MD)
    dpg.add_text(rp.title, color=shell.COLORS["text"])
    dpg.add_text(rp.created_at, color=shell.COLORS["muted"])
    dpg.add_spacer(height=SPACE_MD)

    # Claim-boundary paragraph at top — the "Detail view" design.
    dpg.add_text(CLAIM_BOUNDARY_PARAGRAPH, wrap=900)
    dpg.add_spacer(height=SPACE_LG)

    # Five text rows: identity, trigger, reason (wrap=860; reasons can be
    # multi-line for descriptive auto-RP triggers), capture_source, coverage.
    # Fit to content (DPG-2.x auto_resize_y, legacy fill flag suppressed) rather
    # than a fixed height: the prior 160 still clipped the lower rows by 38px at
    # the shipped fonts (tools/diag_dpg_card_clip.py) and a multi-line reason
    # makes the height variable. Fit can't clip and needs no magic number.
    with dpg.child_window(border=True, auto_resize_y=True, autosize_y=False):
        dpg.add_text(
            f"{t('restore_points.detail.identity_label')} "
            f"{_identity_summary(rp)}"
        )
        dpg.add_text(
            f"{t('restore_points.detail.trigger_label')} "
            f"{_trigger_label(rp.trigger.type)}"
        )
        dpg.add_text(
            f"{t('restore_points.detail.reason_label')} {rp.trigger.reason}",
            wrap=860,
        )
        dpg.add_text(
            f"{t('restore_points.detail.capture_source_label')} "
            f"{_capture_source_label(rp.coverage.capture_source)}"
        )
        dpg.add_text(
            f"{t('restore_points.detail.coverage_label')} "
            + t(
                "restore_points.list.row.coverage",
                captured=rp.coverage.captured_supported_count,
                total=rp.coverage.total_supported_count,
            )
        )

    dpg.add_spacer(height=SPACE_MD)
    _build_detail_field_groups(shell, rp)

    dpg.add_spacer(height=SPACE_MD)
    _build_will_restore_lists(shell, rp)

    dpg.add_spacer(height=SPACE_LG)
    with dpg.group(horizontal=True):
        dpg.add_button(
            label=t("restore_points.detail.export_button"),
            tag=TAG_DETAIL_EXPORT_BUTTON,
            width=140,
            callback=lambda: _on_export_rp(shell, rp.id),
        )
        dpg.add_button(
            label=t("restore_points.detail.restore_button"),
            tag=TAG_DETAIL_RESTORE_BUTTON,
            width=140,
            callback=lambda: _on_open_confirm(shell, rp.id),
        )

    if state.status_text:
        dpg.add_spacer(height=SPACE_MD)
        dpg.add_text(
            state.status_text,
            tag=TAG_STATUS_TEXT,
            color=shell.COLORS["muted"]
            if state.status_kind == "info"
            else shell.COLORS["warn"],
            wrap=900,
        )


def _build_detail_field_groups(shell, rp: RestorePoint) -> None:
    by_category: dict[CoverageCategory, list[tuple[str, str]]] = {}
    for name, coverage in rp.coverage.fields.items():
        by_category.setdefault(coverage.category, []).append((name, coverage.state.value))
    for category in (
        CoverageCategory.DEVICE,
        CoverageCategory.FEEL,
        CoverageCategory.LAYOUT,
        CoverageCategory.COSMETIC,
        CoverageCategory.METADATA,
        CoverageCategory.UNSUPPORTED,
    ):
        rows = by_category.get(category, [])
        if not rows:
            continue
        # Heading + N rows in an autosizing card(): the panel fits its content
        # at any N / locale instead of a guessed 45+21*N height (which clipped
        # the last row whenever the per-row math drifted from the real metrics).
        with card():
            dpg.add_text(
                t(_CATEGORY_LABEL_KEYS[category]),
                color=shell.COLORS["text"],
            )
            for name, state_value in rows:
                dpg.add_text(f"  {name}: {state_value}", color=shell.COLORS["muted"])


def _build_will_restore_lists(shell, rp: RestorePoint) -> None:
    will: list[str] = []
    wont: list[str] = []
    for name, coverage in rp.coverage.fields.items():
        if (
            coverage.writable
            and coverage.state in (CoverageState.CAPTURED, CoverageState.PARTIAL)
        ):
            will.append(name)
        else:
            wont.append(name)

    # Heading + N items (or "-" placeholder) per side, in autosizing card()
    # panels laid out as two columns. The fixed width=440 is real layout (two
    # equal side-by-side columns); only the guessed 45+21*N height goes away —
    # card() autosizes so neither list clips its trailing item at any count.
    with dpg.group(horizontal=True):
        with card(width=440):
            dpg.add_text(
                t("restore_points.detail.will_restore_heading"),
                color=shell.COLORS["text"],
            )
            for name in will:
                dpg.add_text(f"  {name}", color=shell.COLORS["muted"])
            if not will:
                dpg.add_text("  -", color=shell.COLORS["muted"])
        with card(width=440):
            dpg.add_text(
                t("restore_points.detail.will_not_restore_heading"),
                color=shell.COLORS["text"],
            )
            for name in wont:
                dpg.add_text(f"  {name}", color=shell.COLORS["muted"])
            if not wont:
                dpg.add_text("  -", color=shell.COLORS["muted"])


# ---------------------------------------------------------------------------
# CONFIRM
# ---------------------------------------------------------------------------


def _build_confirm(shell, service, state: RestorePointsScreenState) -> None:
    rp = _load_selected(service, state)
    if rp is None:
        _back_to_list(shell, status=t("restore_points.list.unavailable"), status_kind="warn")
        return

    screen_title(t("restore_points.confirm.title"))
    dpg.add_spacer(height=SPACE_MD)
    dpg.add_text(rp.title)
    dpg.add_text(rp.created_at, color=shell.COLORS["muted"])
    dpg.add_spacer(height=SPACE_MD)
    dpg.add_text(t("restore_points.confirm.body"), wrap=900)
    dpg.add_spacer(height=SPACE_MD)
    dpg.add_text(CLAIM_BOUNDARY_SHORT_UI, color=shell.COLORS["muted"], wrap=900)

    # Pre-restore preview — the pre-restore-preview work. Symmetric
    # counterpart to the restore-result-enrichment expected/observed render on
    # the result page: BEFORE the restore, show what's about to change so
    # the user isn't clicking blind on an auto-captured RP. The service
    # method is designed to swallow per-field read errors and reflect them
    # as field-level notes; the broad except is defensive belt-and-braces
    # so an unexpected store/HID failure mid-build still lets the modal
    # render (user can Cancel out).
    preview: Optional[RestorePreview] = None
    if not getattr(shell, "hid_busy", False):
        try:
            preview = service.compute_restore_preview(rp.id)
        except Exception:  # noqa: BLE001
            logger.exception("Pre-restore preview computation failed")
    # else: a threaded HID job is mid-flight (e.g. this rebuild came from
    # _on_confirm_restore's own busy refusal) — skip the preview's live read
    # and render CONFIRM without the section rather than interleave frames.

    if preview is not None:
        dpg.add_spacer(height=SPACE_MD)
        _build_confirm_preview(shell, preview)

    dpg.add_spacer(height=SPACE_LG)
    with dpg.group(horizontal=True):
        dpg.add_button(
            label=t("restore_points.confirm.restore_supported"),
            tag=TAG_CONFIRM_RESTORE_BUTTON,
            width=220,
            callback=lambda: _on_confirm_restore(shell),
        )
        dpg.add_button(
            label=t("restore_points.confirm.view_covered"),
            tag=TAG_CONFIRM_VIEW_BUTTON,
            width=180,
            callback=lambda: _on_back_to_detail(shell),
        )
        dpg.add_button(
            label=t("restore_points.confirm.cancel"),
            tag=TAG_CONFIRM_CANCEL_BUTTON,
            width=120,
            callback=lambda: _on_back_to_detail(shell),
        )


def _build_confirm_preview(shell, preview: RestorePreview) -> None:
    """Render the "What this restore will change" section under the
    claim-boundary text in CONFIRM. Shows only the changing-fields list
    (preview, not status report). Appends an unreadable-count note when
    applicable, or a no-op sentence when nothing differs and nothing is
    unreadable.
    """

    section_title(
        t("restore_points.confirm.preview_heading"),
    )
    if preview.fields_changing == 0 and preview.fields_unreadable == 0:
        dpg.add_text(
            t("restore_points.confirm.preview_no_op"),
            color=shell.COLORS["muted"],
            wrap=900,
        )
        return
    for delta in preview.fields:
        if not delta.will_change:
            continue
        dpg.add_text(
            f"  {delta.field_name}: {delta.current_value} → {delta.target_value}",
            color=shell.COLORS["muted"],
            wrap=900,
        )
    if preview.fields_unreadable > 0:
        dpg.add_text(
            t(
                "restore_points.confirm.preview_unreadable_n",
                n=preview.fields_unreadable,
            ),
            color=shell.COLORS["muted"],
            wrap=900,
        )


# ---------------------------------------------------------------------------
# IN_PROGRESS
# ---------------------------------------------------------------------------


def _build_in_progress(shell, service, state: RestorePointsScreenState) -> None:
    screen_title(t("restore_points.in_progress.title"))
    dpg.add_spacer(height=SPACE_MD)
    dpg.add_text(t("restore_points.in_progress.body"), wrap=900)
    dpg.add_spacer(height=SPACE_MD)
    # Indeterminate progress bar. DPG has no spinner widget; an animated
    # progress bar is the closest stock approximation.
    dpg.add_progress_bar(default_value=0.0, overlay="...", width=320)

    # Re-entry guard: a rebuild while the restore job is already in flight
    # (nav away + back, locale change, auto-read rebuild) must not register
    # a second _execute_restore — it would be refused by the busy guard and
    # snap the view back to CONFIRM while the real restore is still running.
    # The in-flight job's completion drives the IN_PROGRESS -> RESULT
    # transition. Stub shells without the accessor fall through.
    if getattr(shell, "hid_busy", False):
        return
    # Defer the actual restore call by one frame so the IN_PROGRESS layout
    # renders before we block. The frame callback fires on the DPG main
    # thread, so service.restore() runs synchronously there; once it
    # returns we transition to RESULT.
    try:
        next_frame = dpg.get_frame_count() + 1
        dpg.set_frame_callback(next_frame, lambda *_a: _execute_restore(shell))
    except SystemError:
        # No DPG context (test environment) — caller invokes _execute_restore
        # directly when driving the state machine.
        return


# ---------------------------------------------------------------------------
# RESULT
# ---------------------------------------------------------------------------


def _build_result(shell, service, state: RestorePointsScreenState) -> None:
    result = state.result
    if result is None:
        _back_to_list(shell, status="", status_kind="info")
        return

    header_key = _RESULT_HEADER_KEYS.get(result.label, "restore_points.result.header.restore_failed")
    screen_title(t(header_key))
    dpg.add_spacer(height=SPACE_MD)

    # Six count rows. Fit to content (DPG-2.x auto_resize_y, legacy fill flag
    # suppressed) rather than a fixed height: a real-viewport probe found the
    # prior 160 clipped the lower counts by 72px at the shipped fonts
    # (tools/diag_dpg_card_clip.py). Fit can't clip and needs no magic number.
    with dpg.child_window(border=True, auto_resize_y=True, autosize_y=False):
        dpg.add_text(t("restore_points.result.counts.attempted", n=result.attempted))
        dpg.add_text(t("restore_points.result.counts.wrote_succeeded", n=result.wrote_succeeded))
        dpg.add_text(t("restore_points.result.counts.write_failed", n=result.write_failed))
        dpg.add_text(t("restore_points.result.counts.verified_matched", n=result.verified_matched))
        dpg.add_text(t("restore_points.result.counts.could_not_verify", n=result.could_not_verify))
        dpg.add_text(t("restore_points.result.counts.mismatched", n=result.mismatched))

    dpg.add_spacer(height=SPACE_MD)
    if result.before_restore_point_id is not None:
        rp = _safe_load(service, result.before_restore_point_id)
        title = rp.title if rp is not None else result.before_restore_point_id
        dpg.add_text(
            t("restore_points.result.before_restore_note", title=title),
            color=shell.COLORS["muted"],
            wrap=900,
        )
    else:
        dpg.add_text(
            t("restore_points.result.before_restore_skipped"),
            color=shell.COLORS["warn"],
            wrap=900,
        )

    if result.fields:
        # Each field contributes one main row, plus an optional indented
        # "expected:/observed:" line on mismatch when both values are known.
        dpg.add_spacer(height=SPACE_MD)
        # Heading + N field rows (+ extra indented mismatch lines) in a
        # content-fit card(fit=True): it fits the full field list at any count
        # (worst case: a full 12-field RP restore) without a guessed 45+21*N
        # height. fit=True is required — the bare card() default uses the legacy
        # autosize_y, which FILLS its parent on DPG 2.x and scrolled the lower
        # fields out of view by 85px (tools/diag_dpg_card_clip.py).
        with card(fit=True):
            dpg.add_text(
                t("restore_points.result.field_details"),
                color=shell.COLORS["text"],
            )
            for outcome in result.fields:
                write_marker = "ok" if outcome.write_succeeded else "FAIL"
                if outcome.verify_matched is True:
                    verify_marker = "matched"
                elif outcome.verify_matched is False:
                    verify_marker = "mismatch"
                else:
                    verify_marker = "unverified"
                row = f"  {outcome.field_name}: write={write_marker} verify={verify_marker}"
                if outcome.write_error:
                    row += f"  ({outcome.write_error})"
                if outcome.verify_matched is None and outcome.verify_note:
                    row += f"  ({outcome.verify_note})"
                dpg.add_text(row, color=shell.COLORS["muted"])
                if (
                    outcome.verify_matched is False
                    and outcome.expected_value is not None
                    and outcome.observed_value is not None
                ):
                    dpg.add_text(
                        f"      expected: {outcome.expected_value}, "
                        f"observed: {outcome.observed_value}",
                        color=shell.COLORS["muted"],
                    )

    if state.status_text:
        dpg.add_spacer(height=SPACE_MD)
        dpg.add_text(
            state.status_text,
            tag=TAG_STATUS_TEXT,
            color=shell.COLORS["muted"]
            if state.status_kind == "info"
            else shell.COLORS["warn"],
            wrap=900,
        )

    dpg.add_spacer(height=SPACE_LG)
    with dpg.group(horizontal=True):
        dpg.add_button(
            label=t("restore_points.result.save_to_diagnostics"),
            tag=TAG_RESULT_SAVE_DIAG_BUTTON,
            width=240,
            callback=lambda: _on_save_result_to_diagnostics(shell),
        )
        dpg.add_button(
            label=t("restore_points.result.close"),
            tag=TAG_RESULT_CLOSE_BUTTON,
            width=120,
            callback=lambda: _on_back_to_list(shell),
        )


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


def _on_refresh(shell) -> None:
    state = _ensure_state(shell)
    state.status_text = ""
    state.status_kind = "info"
    shell.rebuild_current_screen()


def _on_manual_save(shell) -> None:
    """Capture a manual restore point from the list view.

    Delegates to :meth:`AppShell.manual_save_restore_point` (the app's only
    manual-capture entry point; relocated here from Diagnostics), then surfaces
    the outcome in the screen's status line and rebuilds so a fresh point shows
    in the list. ``None`` means the capture failed or was refused while a HID
    job was in flight (the footer busy banner carries the refusal reason);
    a non-None return is the newly captured RestorePoint.
    """
    state = _ensure_state(shell)
    rp = shell.manual_save_restore_point()
    if rp is None:
        state.status_text = t(
            "restore_points.manual.failure",
            reason=t("restore_points.manual.unavailable"),
        )
        state.status_kind = "warn"
    else:
        state.status_text = t("restore_points.manual.success")
        state.status_kind = "info"
    shell.rebuild_current_screen()


def _on_toggle_skipped(shell) -> None:
    state = _ensure_state(shell)
    state.skipped_expanded = not state.skipped_expanded
    shell.rebuild_current_screen()


def _on_view(shell, rp_id: str) -> None:
    state = _ensure_state(shell)
    state.view = VIEW_DETAIL
    state.selected_rp_id = rp_id
    state.status_text = ""
    shell.rebuild_current_screen()


def _on_open_confirm(shell, rp_id: str) -> None:
    state = _ensure_state(shell)
    if getattr(shell, "hid_busy", False):
        # CONFIRM's build computes a pre-restore preview — a live HID read
        # that would interleave with the in-flight threaded job's frames.
        # Refuse the navigation (stay on the current view) with the busy
        # status instead of queueing it; the user re-clicks Restore once
        # the job completes. Stub shells without the accessor fall through.
        state.status_text = t("apply.busy")
        state.status_kind = "warn"
        shell.rebuild_current_screen()
        return
    state.view = VIEW_CONFIRM
    state.selected_rp_id = rp_id
    state.status_text = ""
    shell.rebuild_current_screen()


def _on_back_to_detail(shell) -> None:
    state = _ensure_state(shell)
    state.view = VIEW_DETAIL
    state.status_text = ""
    shell.rebuild_current_screen()


def _on_back_to_list(shell) -> None:
    state = _ensure_state(shell)
    state.view = VIEW_LIST
    state.selected_rp_id = None
    state.result = None
    state.status_text = ""
    shell.rebuild_current_screen()


def _on_confirm_restore(shell) -> None:
    state = _ensure_state(shell)
    if getattr(shell, "_hid_job_in_flight", False):
        # Another HID flow is mid-job (threaded shells only; the confirm
        # button is also disabled for the duration). Entering IN_PROGRESS
        # would register a frame callback whose job gets refused — stay on
        # CONFIRM instead. Stub shells without the flag fall through.
        state.status_text = t("apply.busy")
        state.status_kind = "warn"
        shell.rebuild_current_screen()
        return
    state.view = VIEW_IN_PROGRESS
    state.result = None
    state.status_text = ""
    shell.rebuild_current_screen()


def _execute_restore(shell) -> None:
    """Run :meth:`RestorePointService.restore` and transition to RESULT.

    Invoked from the deferred frame callback registered by
    :func:`_build_in_progress`. Tests can call this directly to drive the
    state machine without DPG.

    The restore (capture + apply + read-back, ~4-9 s of HID round-trips) is
    the job half; the state-machine transition + rebuild is the on_done half.
    Routed through ``shell._run_hid_job`` so a threaded shell runs the job
    off the render thread — which is what lets the IN_PROGRESS progress bar
    actually animate. Sync shells (the test default) and stub shells without
    the runner execute inline, byte-for-byte the old flow.
    """

    state = _ensure_state(shell)
    service = getattr(shell, "restore_point_service", None)
    if service is None or state.selected_rp_id is None:
        state.view = VIEW_LIST
        state.status_text = t("restore_points.list.unavailable")
        state.status_kind = "warn"
        shell.rebuild_current_screen()
        return

    # Honesty gate: a restore re-applies a captured snapshot to the controller —
    # a HID write burst. Refuse on a non-allowlisted controller (drop back to
    # CONFIRM with the unverified-device status) so no write is attempted on a
    # non-ZD pad. Defaults to allowed for stub shells without a device state, so
    # the existing sync/stub restore tests are unchanged.
    device_state = getattr(getattr(shell, "device_service", None), "state", None)
    if device_state is not None and not getattr(device_state, "write_supported", True):
        state.view = VIEW_CONFIRM
        state.status_text = t("apply.device_unverified")
        state.status_kind = "warn"
        shell.rebuild_current_screen()
        return

    rp_id = state.selected_rp_id

    def job():
        return service.restore(rp_id)

    def on_done(outcome) -> None:
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):
                raise outcome  # keep today's `except Exception` scope
            logger.error("Restore failed", exc_info=outcome)
            state.view = VIEW_LIST
            state.status_text = t("restore_points.manual.failure", reason=str(outcome))
            state.status_kind = "warn"
            state.result = None
            shell.rebuild_current_screen()
            return
        state.view = VIEW_RESULT
        state.result = outcome
        state.status_text = ""
        shell.rebuild_current_screen()

    runner = getattr(shell, "_run_hid_job", None)
    if runner is None:
        # Stub shells (tests) drive the state machine synchronously.
        try:
            result = job()
        except BaseException as exc:  # noqa: BLE001
            result = exc
        on_done(result)
        return
    if not runner(job, on_done):
        # Refused — another flow is in flight. Leave IN_PROGRESS (it would
        # spin forever) and return to CONFIRM so the user can retry.
        state.view = VIEW_CONFIRM
        state.status_text = t("apply.busy")
        state.status_kind = "warn"
        shell.rebuild_current_screen()


def _on_export_rp(shell, rp_id: str) -> None:
    state = _ensure_state(shell)
    service = getattr(shell, "restore_point_service", None)
    if service is None:
        state.status_text = t("restore_points.list.unavailable")
        state.status_kind = "warn"
        shell.rebuild_current_screen()
        return
    rp = _safe_load(service, rp_id)
    if rp is None:
        state.status_text = t("restore_points.result.export_failure", reason="not found")
        state.status_kind = "warn"
        shell.rebuild_current_screen()
        return
    try:
        export_dir = _restore_points_export_dir(shell)
        target = export_dir / f"{rp_id}.json"
        # Defense-in-depth containment: the resolved target must stay directly
        # inside the export dir. ``rp_id`` is already constrained to a safe stem
        # by restore_point_from_dict's id validation, so this never fires for a
        # real restore point — but it turns any future gap (or an id sourced
        # from elsewhere) into a refusal instead of an out-of-tree write.
        if target.resolve().parent != export_dir.resolve():
            raise ValueError(f"export target escapes export dir: {rp_id!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(restore_point_to_dict(rp), indent=2),
            encoding="utf-8",
        )
        state.status_text = t("restore_points.result.export_success", path=str(target))
        state.status_kind = "info"
    except (OSError, ValueError) as exc:
        logger.exception("Restore-point export failed")
        state.status_text = t("restore_points.result.export_failure", reason=str(exc))
        state.status_kind = "warn"
    shell.rebuild_current_screen()


def _on_open_delete_confirm(shell, rp_id: str, title: str) -> None:
    """Open the localized delete-confirm modal for one list row.

    Mirrors the footer's ``AppShell.confirm_delete_named_wrapper_profile``
    modal pattern but lives here because this screen owns the action. The
    modal is a top-level window (not a child of the screen container), so
    both outcomes delete it explicitly before any rebuild.
    """

    if dpg.does_item_exist(TAG_DELETE_CONFIRM_MODAL):
        dpg.delete_item(TAG_DELETE_CONFIRM_MODAL)

    with dpg.window(
        label=t("restore_points.delete_confirm.title"),
        modal=True,
        no_close=False,
        no_resize=True,
        width=420,
        height=170,
        tag=TAG_DELETE_CONFIRM_MODAL,
    ):
        dpg.add_text(t("restore_points.delete_confirm.body", title=title), wrap=390)
        dpg.add_spacer(height=SPACE_MD)
        with dpg.group(horizontal=True):
            dpg.add_button(
                label=t("actions.cancel"),
                tag=TAG_DELETE_CONFIRM_CANCEL_BUTTON,
                width=100,
                callback=lambda *args: dpg.delete_item(TAG_DELETE_CONFIRM_MODAL),
            )
            # Same load-bearing ``lambda *args, ...=...`` idiom as the row
            # buttons (see _build_list_row / ListRowButtonDpgSignatureTests).
            action_button(
                t("restore_points.delete_confirm.confirm"),
                destructive=True,
                tag=TAG_DELETE_CONFIRM_CONFIRM_BUTTON,
                width=100,
                callback=lambda *args, rp_id=rp_id, title=title: (
                    _on_delete_confirmed(shell, rp_id, title)
                ),
            )


def _on_delete_confirmed(shell, rp_id: str, title: str) -> None:
    """Delete one restore point via the service and refresh the list.

    Both outcomes close the modal and rebuild so the row list reflects the
    vault: success shows an info status, a miss (already gone) or an
    unexpected store error shows a warn status instead of crashing the
    callback.
    """

    if dpg.does_item_exist(TAG_DELETE_CONFIRM_MODAL):
        dpg.delete_item(TAG_DELETE_CONFIRM_MODAL)

    state = _ensure_state(shell)
    service = getattr(shell, "restore_point_service", None)
    if service is None:
        state.status_text = t("restore_points.list.unavailable")
        state.status_kind = "warn"
        shell.rebuild_current_screen()
        return

    try:
        deleted = service.delete(rp_id)
    except Exception:  # noqa: BLE001
        logger.exception("Restore-point delete failed")
        deleted = False
    if deleted:
        state.status_text = t("restore_points.delete_success", title=title)
        state.status_kind = "info"
    else:
        state.status_text = t("restore_points.delete_failed", title=title)
        state.status_kind = "warn"
    shell.rebuild_current_screen()


def _on_save_result_to_diagnostics(shell) -> None:
    state = _ensure_state(shell)
    if state.result is None:
        return
    payload = {
        "kind": "restore_result",
        "label": state.result.label.value,
        "attempted": state.result.attempted,
        "wrote_succeeded": state.result.wrote_succeeded,
        "write_failed": state.result.write_failed,
        "verified_matched": state.result.verified_matched,
        "could_not_verify": state.result.could_not_verify,
        "mismatched": state.result.mismatched,
        "before_restore_point_id": state.result.before_restore_point_id,
        "completed_at": state.result.completed_at,
        "fields": [
            {
                "field_name": outcome.field_name,
                "write_succeeded": outcome.write_succeeded,
                "write_error": outcome.write_error,
                "verify_matched": outcome.verify_matched,
                "verify_note": outcome.verify_note,
                "expected_value": outcome.expected_value,
                "observed_value": outcome.observed_value,
            }
            for outcome in state.result.fields
        ],
    }
    try:
        target = _restore_points_export_dir(shell) / (
            f"restore_result_{time.strftime('%Y-%m-%d_%H%M%S')}.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        state.status_text = t(
            "restore_points.result.save_to_diagnostics_success",
            path=str(target),
        )
        state.status_kind = "info"
    except OSError as exc:
        logger.exception("Restore-result diagnostics save failed")
        state.status_text = t(
            "restore_points.result.save_to_diagnostics_failure",
            reason=str(exc),
        )
        state.status_kind = "warn"
    shell.rebuild_current_screen()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _back_to_list(shell, *, status: str, status_kind: str) -> None:
    state = _ensure_state(shell)
    state.view = VIEW_LIST
    state.selected_rp_id = None
    state.result = None
    state.status_text = status
    state.status_kind = status_kind
    shell.rebuild_current_screen()


def _load_selected(service, state: RestorePointsScreenState) -> Optional[RestorePoint]:
    if state.selected_rp_id is None:
        return None
    return _safe_load(service, state.selected_rp_id)


def _safe_load(service, rp_id: str) -> Optional[RestorePoint]:
    # Entry trace stays at DEBUG so successful navigation doesn't fill the
    # log on every click. The failure paths below stay at INFO so any future
    # regression of the load-by-id contract surfaces with a traceback in
    # zd_wrapper.log without needing to flip a debug flag first.
    logger.debug(
        "restore-points safe-load: rp_id=%r service_id=%s store_id=%s",
        rp_id, id(service), id(getattr(service, "_store", None)),
    )
    try:
        return service._store.load(rp_id)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "restore-points safe-load: primary store.load(%r) raised %s: %s",
            rp_id, type(exc).__name__, exc,
            exc_info=True,
        )
        # Fall back to scanning the list — slower but works without
        # depending on the private store attribute when a test swaps in a
        # fake service.
        try:
            valid, _ = service.list_with_skipped()
        except Exception as exc2:  # noqa: BLE001
            logger.info(
                "restore-points safe-load: fallback list_with_skipped() raised %s: %s",
                type(exc2).__name__, exc2,
                exc_info=True,
            )
            return None
        for rp in valid:
            if rp.id == rp_id:
                return rp
        logger.info(
            "restore-points safe-load: fallback list-scan found no match for rp_id=%r "
            "(scanned %d valid rps: %r)",
            rp_id, len(valid), [r.id for r in valid],
        )
        return None


def _trigger_label(trigger_type: str) -> str:
    key = _TRIGGER_LABEL_KEYS.get(trigger_type)
    if key is None:
        return trigger_type
    return t(key)


def _capture_source_label(source: CaptureSource) -> str:
    return t(_CAPTURE_SOURCE_LABEL_KEYS[source])


def _identity_summary(rp: RestorePoint) -> str:
    parts: list[str] = []
    if rp.device_identity.product_string:
        parts.append(rp.device_identity.product_string)
    if rp.device_identity.firmware_version:
        parts.append(f"fw {rp.device_identity.firmware_version}")
    if rp.device_identity.vid and rp.device_identity.pid:
        parts.append(f"{rp.device_identity.vid}:{rp.device_identity.pid}")
    return "  ".join(parts) or "-"


def _last_restore_label(rp: RestorePoint) -> str:
    record = rp.last_restore_attempt
    if record is None:
        return t("restore_points.list.row.never_restored")
    if record.label == RestoreResultLabel.VERIFIED:
        return t("restore_points.list.row.last_verified")
    if record.label == RestoreResultLabel.RESTORED_WITH_WARNINGS:
        return t("restore_points.list.row.last_warnings")
    if record.label == RestoreResultLabel.PARTIALLY_RESTORED:
        return t("restore_points.list.row.last_partial")
    if record.label == RestoreResultLabel.MISMATCH_AFTER_RESTORE:
        return t("restore_points.list.row.last_mismatch")
    return t("restore_points.list.row.last_failed")


def _last_restore_color(shell, rp: RestorePoint):
    """Color cue for the list row's last-restore status (a light status chip).

    Verified → good, mismatch / failed → warn, never-restored / warnings /
    partial → muted. Falls back to ``muted`` if the palette lacks a key so a
    minimal shell can never raise here.
    """

    record = rp.last_restore_attempt
    muted = shell.COLORS["muted"]
    if record is None:
        return muted
    if record.label == RestoreResultLabel.VERIFIED:
        return shell.COLORS.get("good", muted)
    if record.label in (
        RestoreResultLabel.MISMATCH_AFTER_RESTORE,
        RestoreResultLabel.RESTORE_FAILED,
    ):
        return shell.COLORS.get("warn", muted)
    return muted


def _restore_points_export_dir(shell) -> Path:
    override = getattr(shell, "restore_points_export_dir_override", None)
    if override:
        return Path(override)
    from zd_app.storage.settings_store import _default_user_data_dir
    return _default_user_data_dir() / "restore_points_exports"


__all__ = [
    "RestorePointsScreenState",
    "VIEW_LIST",
    "VIEW_DETAIL",
    "VIEW_CONFIRM",
    "VIEW_IN_PROGRESS",
    "VIEW_RESULT",
    "build",
]
