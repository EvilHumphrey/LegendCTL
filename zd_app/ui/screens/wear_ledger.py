"""Wear-ledger screen — chronological maintenance log.

Renders events emitted by :class:`zd_app.services.wear_ledger.WearLedgerService`
in reverse-chronological order with type + date-range filters and an
"add service note" modal. A small drift-trend sparkline at the top shows
the verdict colour of the last N health-report / readiness-check events
so the operator can spot drift trends without paging through the list.

State-pure layout dispatcher matching the Restore Points screen pattern:
:func:`build` reads ``shell.wear_ledger_screen_state`` and renders the
current view. The "add service note" modal is built lazily on the
``shell``'s DPG window registry; absence of a DPG context (test env)
short-circuits the modal-open callback without raising.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import dearpygui.dearpygui as dpg

from zd_app.i18n import t
from zd_app.services.wear_ledger import WearLedgerService, sanitize_service_note
from zd_app.services.wear_ledger.models import (
    EVENT_TYPES,
    FILTER_CATEGORY_HEALTH,
    FILTER_CATEGORY_MEMBERS,
    FILTER_CATEGORY_NOTES,
    FILTER_CATEGORY_PROFILES,
    FILTER_CATEGORY_RESTORES,
    FILTER_CATEGORY_SESSIONS,
    FILTER_CATEGORY_SLIDER,
    HEALTH_REPORT,
    READINESS_CHECK,
    SERVICE_NOTE,
    WearLedgerEvent,
    event_type_label_key,
)
from zd_app.ui.components import Column, section, table, table_empty_state
from zd_app.ui.themes import SPACE_LG, SPACE_MD
from zd_app.ui.typography import helper_text, screen_title


logger = logging.getLogger(__name__)


TAG_ROOT_CONTAINER = "wear_ledger_root"
TAG_TYPE_FILTER_COMBO = "wear_ledger_type_filter"
TAG_RANGE_FILTER_COMBO = "wear_ledger_range_filter"
TAG_MODULE_FILTER_COMBO = "wear_ledger_module_filter"
TAG_ADD_NOTE_BUTTON = "wear_ledger_add_note_button"
TAG_REFRESH_BUTTON = "wear_ledger_refresh_button"
TAG_NOTE_MODAL = "wear_ledger_note_modal"
TAG_NOTE_MODAL_INPUT = "wear_ledger_note_modal_input"
TAG_NOTE_MODAL_SAVE = "wear_ledger_note_modal_save"
TAG_NOTE_MODAL_CANCEL = "wear_ledger_note_modal_cancel"
TAG_STATUS_TEXT = "wear_ledger_status_text"
TAG_EVENT_TABLE = "wear_ledger_event_table"


FILTER_ALL = "all"
MODULE_FILTER_ALL = "all"

# Filter dropdown values: the all-events sentinel plus each category from the
# models module. Order here drives display order in the combo.
TYPE_FILTER_OPTIONS: tuple[str, ...] = (
    FILTER_ALL,
    FILTER_CATEGORY_SESSIONS,
    FILTER_CATEGORY_PROFILES,
    FILTER_CATEGORY_RESTORES,
    FILTER_CATEGORY_HEALTH,
    FILTER_CATEGORY_SLIDER,
    FILTER_CATEGORY_NOTES,
)


RANGE_FILTER_ALL = "all"
RANGE_FILTER_7 = "7"
RANGE_FILTER_30 = "30"
RANGE_FILTER_90 = "90"

RANGE_FILTER_OPTIONS: tuple[str, ...] = (
    RANGE_FILTER_ALL,
    RANGE_FILTER_7,
    RANGE_FILTER_30,
    RANGE_FILTER_90,
)


# Sparkline tuning. 20 verdicts gives the operator a meaningful trend without
# making the bar too narrow to read.
SPARKLINE_MAX_POINTS = 20

# The verdict-trend card holds a heading + one row of square glyphs + a
# one-line legend (or a one-line empty message). A real-viewport measurement at
# the shipped fonts (tools/diag_dpg_card_clip.py) put that content at ~146px
# under the card theme's WindowPadding (SPACE_LG) + ItemSpacing — the prior 140
# clipped it by 6px (a stray inner scrollbar). We keep a pinned compact height
# (rather than fit-to-content) deliberately: a legacy autosize_y child FILLS its
# parent on DPG 2.x and reserved a tall block that pushed the event-log table
# ~470px down, below the fold on load. 152 clears the measured content with
# slack for the taller CJK body font. Mirrors home.py's explicit card heights.
_SPARKLINE_CARD_HEIGHT = 152


@dataclass
class WearLedgerScreenState:
    type_filter: str = FILTER_ALL
    range_filter: str = RANGE_FILTER_ALL
    module_filter: str = MODULE_FILTER_ALL
    expanded_event_ts: Optional[str] = None  # ts of the currently-expanded row
    status_text: str = ""
    status_kind: str = "info"
    pending_note_text: str = ""


def _ensure_state(shell) -> WearLedgerScreenState:
    state = getattr(shell, "wear_ledger_screen_state", None)
    if state is None:
        state = WearLedgerScreenState()
        shell.wear_ledger_screen_state = state
    return state


# ---------------------------------------------------------------------------
# Top-level build
# ---------------------------------------------------------------------------


def build(shell, parent: str) -> None:
    """Render the wear-ledger screen into ``parent``."""

    state = _ensure_state(shell)
    service: Optional[WearLedgerService] = getattr(shell, "wear_ledger_service", None)

    with dpg.child_window(
        parent=parent,
        tag=TAG_ROOT_CONTAINER,
        autosize_x=True,
        autosize_y=True,
        border=False,
    ):
        screen_title(t("wear_ledger.title"))
        helper_text(t("wear_ledger.subtitle"), wrap=900)
        dpg.add_spacer(height=8)

        if service is None:
            dpg.add_text(t("wear_ledger.unavailable"), wrap=900)
            return

        try:
            events = service.read_events()
        except Exception as exc:  # noqa: BLE001
            logger.exception("wear_ledger read_events failed")
            # Still render controls even when read_events fails, so the
            # operator can change the filter and retry; but skip the
            # sparkline + list rendering.
            _build_controls(shell, service, state, module_options=(MODULE_FILTER_ALL,))
            dpg.add_spacer(height=10)
            dpg.add_text(
                t("wear_ledger.read_failed", reason=str(exc)),
                color=shell.COLORS["warn"],
                wrap=900,
            )
            return

        module_options = _module_filter_options(events)
        # Self-heal: if the currently-selected module id no longer appears
        # in the event log (e.g. after a clear-history rebuild), fall back
        # to "all" so the operator sees something instead of an empty list.
        if state.module_filter not in module_options:
            state.module_filter = MODULE_FILTER_ALL

        # Summary first (verdict-trend card), then the filterable event-log
        # card. Both blocks are component cards so the screen reads
        # as two panels instead of a flat stack of controls + rows.
        _build_sparkline(shell, events)
        dpg.add_spacer(height=SPACE_LG)

        with section(t("wear_ledger.log.heading"), card=True):
            _build_controls(shell, service, state, module_options=module_options)
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

            filtered = _apply_filters(events, state)
            if not filtered:
                table_empty_state(t("wear_ledger.empty_state"))
            else:
                _build_event_table(shell, filtered, state)


# ---------------------------------------------------------------------------
# Controls (type + range filter, add-note button, refresh)
# ---------------------------------------------------------------------------


def _build_controls(
    shell,
    service,
    state: WearLedgerScreenState,
    *,
    module_options: tuple[str, ...] = (MODULE_FILTER_ALL,),
) -> None:
    with dpg.group(horizontal=True):
        dpg.add_text(t("wear_ledger.filter.type_label"), color=shell.COLORS["muted"])
        dpg.add_combo(
            items=[t(f"wear_ledger.filter.type.{key}") for key in TYPE_FILTER_OPTIONS],
            default_value=t(f"wear_ledger.filter.type.{state.type_filter}"),
            width=220,
            tag=TAG_TYPE_FILTER_COMBO,
            callback=lambda sender, app_data, *_a: _on_type_filter_changed(shell, app_data),
        )
        dpg.add_text(t("wear_ledger.filter.range_label"), color=shell.COLORS["muted"])
        dpg.add_combo(
            items=[t(f"wear_ledger.filter.range.{key}") for key in RANGE_FILTER_OPTIONS],
            default_value=t(f"wear_ledger.filter.range.{state.range_filter}"),
            width=180,
            tag=TAG_RANGE_FILTER_COMBO,
            callback=lambda sender, app_data, *_a: _on_range_filter_changed(shell, app_data),
        )
        dpg.add_text(t("wear_ledger.filter.module_label"), color=shell.COLORS["muted"])
        dpg.add_combo(
            items=[_module_filter_label(key) for key in module_options],
            default_value=_module_filter_label(state.module_filter),
            width=200,
            tag=TAG_MODULE_FILTER_COMBO,
            callback=lambda sender, app_data, *_a: _on_module_filter_changed(
                shell, app_data, module_options
            ),
        )
        dpg.add_button(
            label=t("wear_ledger.controls.add_service_note"),
            tag=TAG_ADD_NOTE_BUTTON,
            width=180,
            callback=lambda *_a: _open_note_modal(shell),
        )
        dpg.add_button(
            label=t("wear_ledger.controls.refresh"),
            tag=TAG_REFRESH_BUTTON,
            width=120,
            callback=lambda *_a: _on_refresh(shell),
        )


def _module_filter_options(events: list[WearLedgerEvent]) -> tuple[str, ...]:
    """Collect the unique module_id values present in the event log.

    Returns ``("all", <module_id>, ...)`` in alphabetical order so the combo
    is stable across rebuilds. Events without a ``module_id`` in details
    don't contribute. The "all" sentinel is always first so it shows as
    the default option.
    """

    ids: set[str] = set()
    for event in events:
        mid = event.details.get("module_id")
        if isinstance(mid, str) and mid:
            ids.add(mid)
    return (MODULE_FILTER_ALL,) + tuple(sorted(ids))


def _module_filter_label(key: str) -> str:
    """Render combo label for a module filter key.

    The "all" sentinel maps to the translated "All modules" string; any
    other key is itself a module_id (operator-supplied freeform text) so
    it renders verbatim.
    """

    if key == MODULE_FILTER_ALL:
        return t("wear_ledger.filter.module.all")
    return key


# ---------------------------------------------------------------------------
# Drift-trend sparkline
# ---------------------------------------------------------------------------


# Mapping from health-report / readiness-check verdict strings to a color
# bucket. The compute layer (compute_overall_status) returns one of these
# string values via the OverallStatus enum; we map them to a coarse
# "green / yellow / red" so a non-stats-literate operator can read the
# sparkline at a glance.
_VERDICT_COLOR_BUCKET: dict[str, str] = {
    "normal": "good",
    "pass": "good",
    "tuning_suggested": "warn",
    "warning": "warn",
    "retest_recommended": "warn",
    "possible_issue": "bad",
    "fail": "bad",
}


def _build_sparkline(shell, events: list[WearLedgerEvent]) -> None:
    """Render a small per-verdict coloured-square sparkline inside a card.

    DPG has no first-class sparkline widget and adding a plot would inflate
    the screen footprint. Instead we render a row of small ``add_text``
    nodes coloured by verdict bucket — the visual is the same and the
    layout cost is one ``group(horizontal=True)``. The whole block lives in
    a titled :func:`section` card so it reads as a summary panel
    above the event-log table.
    """

    verdicts: list[tuple[str, str]] = []
    for event in events:
        if event.event_type not in (HEALTH_REPORT, READINESS_CHECK):
            continue
        status = str(event.details.get("overall_status", "")).lower()
        verdicts.append((event.ts, status))
    # Reverse-chrono list → trim then flip so the bar reads left-to-right
    # oldest-to-newest (which matches the user mental model "things got
    # worse over time" left-to-right).
    verdicts = verdicts[:SPARKLINE_MAX_POINTS]
    verdicts.reverse()

    with section(
        t("wear_ledger.sparkline.heading"),
        card=True,
        height=_SPARKLINE_CARD_HEIGHT,
    ):
        if not verdicts:
            dpg.add_text(
                t("wear_ledger.sparkline.empty"),
                color=shell.COLORS["muted"],
                wrap=900,
            )
            return
        with dpg.group(horizontal=True):
            for ts, verdict in verdicts:
                bucket = _VERDICT_COLOR_BUCKET.get(verdict)
                color = shell.COLORS[bucket] if bucket else shell.COLORS["muted"]
                dpg.add_text("■", color=color)  # filled black square glyph
        legend = t(
            "wear_ledger.sparkline.legend",
            n=len(verdicts),
            max=SPARKLINE_MAX_POINTS,
        )
        dpg.add_text(legend, color=shell.COLORS["muted"])


# ---------------------------------------------------------------------------
# Event-log table rendering
# ---------------------------------------------------------------------------


def _build_event_table(
    shell, events: list[WearLedgerEvent], state: WearLedgerScreenState
) -> None:
    """Render the filtered events as a single shared :func:`table`.

    One table with stretch-proportional columns (Time / Event / Status /
    Detail) replaces the prior stack of bordered child-window cards: far
    fewer widgets for a long ledger, alternating row backgrounds for
    scanability, and columns that shrink with the window so zh-CN labels are
    not truncated. The expand/collapse affordance and the raw-details dump
    live inside the Detail cell, so the table keeps a fixed column structure
    (no spanning sub-rows) — there is exactly one ``table_row`` per event.
    ``resizable=True`` keeps the operator's column-drag affordance from the
    original hand-rolled table.
    """

    with table(
        [
            Column(t("wear_ledger.table.col_time"), weight=0.18),
            Column(t("wear_ledger.table.col_type"), weight=0.22),
            Column(t("wear_ledger.table.col_status"), weight=0.10),
            Column(t("wear_ledger.table.col_detail"), weight=0.50),
        ],
        tag=TAG_EVENT_TABLE,
        resizable=True,
    ):
        for event in events:
            _build_event_table_row(shell, event, state)


def _build_event_table_row(
    shell, event: WearLedgerEvent, state: WearLedgerScreenState
) -> None:
    expanded = state.expanded_event_ts == event.ts
    detail_rows = _detail_rows(event)
    with dpg.table_row():
        # Time stays muted; the event-type label carries row identity in
        # primary text — accent-green is reserved for actionable controls
        # (the expand button + the filter/action row), never plain labels.
        dpg.add_text(_format_ts(event.ts), color=shell.COLORS["muted"])
        dpg.add_text(
            t(event_type_label_key(event.event_type)),
            color=shell.COLORS["text"],
        )
        _status_chip(shell, event)
        with dpg.group():
            dpg.add_text(event.summary or "", wrap=480)
            if detail_rows:
                toggle_label = (
                    t("wear_ledger.row.collapse_details")
                    if expanded
                    else t("wear_ledger.row.expand_details")
                )
                ts_capture = event.ts
                dpg.add_button(
                    label=toggle_label,
                    small=True,
                    callback=lambda *args, ts=ts_capture: _on_toggle_expand(shell, ts),
                )
                if expanded:
                    for label, value in detail_rows:
                        dpg.add_text(
                            f"  {label}: {value}",
                            color=shell.COLORS["muted"],
                            wrap=480,
                        )


def _status_chip(shell, event: WearLedgerEvent) -> None:
    """Render the Status-column cell: a verdict-coloured dot, else blank.

    Only health-report / readiness-check events carry an ``overall_status``
    verdict; every other row gets an empty cell so the column stays narrow
    and uncluttered. The dot reuses the same good/warn/bad buckets as the
    trend sparkline, but with a distinct glyph (``●`` vs the sparkline's
    ``■``) so the two read as related without being confused for each other.
    """

    if event.event_type in (HEALTH_REPORT, READINESS_CHECK):
        status = str(event.details.get("overall_status", "")).lower()
        bucket = _VERDICT_COLOR_BUCKET.get(status)
        if bucket:
            dpg.add_text("●", color=shell.COLORS[bucket])
            return
    # Keep the cell present but visually empty for non-verdict rows.
    dpg.add_text("")


def _format_ts(ts: str) -> str:
    """Trim an ISO-8601 ``YYYY-MM-DDThh:mm:ssZ`` stamp to ``YYYY-MM-DD hh:mm``.

    Presentation only — the stored event ``ts`` is untouched. A stamp that
    does not match the expected shape is returned verbatim so an unexpected
    format never blanks the Time column.
    """

    if "T" not in ts:
        return ts
    date_part, _, time_part = ts.partition("T")
    return f"{date_part} {time_part[:5]}"


def _detail_rows(event: WearLedgerEvent) -> list[tuple[str, str]]:
    """Return ``(label, value)`` pairs to render under an expanded event."""

    rows: list[tuple[str, str]] = []
    for key, value in event.details.items():
        if value is None or value == "":
            continue
        rows.append((str(key), _format_detail_value(value)))
    return rows


def _format_detail_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # Truncate long strings (e.g. service-note text in a detail field)
        # so the expand view stays one row per detail.
        if len(value) > 240:
            return value[:240] + "..."
        return value
    if isinstance(value, (list, tuple)):
        return ", ".join(_format_detail_value(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={_format_detail_value(v)}" for k, v in value.items())
    return str(value)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _apply_filters(
    events: list[WearLedgerEvent], state: WearLedgerScreenState
) -> list[WearLedgerEvent]:
    type_filter_set = _type_filter_to_event_types(state.type_filter)
    since = _range_filter_to_since(state.range_filter)
    module_filter = state.module_filter
    filtered: list[WearLedgerEvent] = []
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ") if since is not None else None
    for event in events:
        if type_filter_set is not None and event.event_type not in type_filter_set:
            continue
        if since_str is not None and event.ts < since_str:
            continue
        if module_filter != MODULE_FILTER_ALL:
            mid = event.details.get("module_id")
            if not isinstance(mid, str) or mid != module_filter:
                continue
        filtered.append(event)
    return filtered


def _type_filter_to_event_types(filter_key: str) -> Optional[frozenset[str]]:
    if filter_key == FILTER_ALL:
        return None
    members = FILTER_CATEGORY_MEMBERS.get(filter_key)
    if members is None:
        return None
    return members


def _range_filter_to_since(filter_key: str) -> Optional[datetime]:
    if filter_key == RANGE_FILTER_ALL:
        return None
    try:
        days = int(filter_key)
    except (ValueError, TypeError):
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


def _on_refresh(shell) -> None:
    state = _ensure_state(shell)
    state.status_text = ""
    state.status_kind = "info"
    shell.rebuild_current_screen()


def _on_type_filter_changed(shell, app_data: str) -> None:
    state = _ensure_state(shell)
    new_key = _label_to_type_filter_key(app_data)
    if new_key is not None and new_key != state.type_filter:
        state.type_filter = new_key
        shell.rebuild_current_screen()


def _on_range_filter_changed(shell, app_data: str) -> None:
    state = _ensure_state(shell)
    new_key = _label_to_range_filter_key(app_data)
    if new_key is not None and new_key != state.range_filter:
        state.range_filter = new_key
        shell.rebuild_current_screen()


def _on_module_filter_changed(
    shell, app_data: str, module_options: tuple[str, ...]
) -> None:
    state = _ensure_state(shell)
    new_key = _label_to_module_filter_key(app_data, module_options)
    if new_key is not None and new_key != state.module_filter:
        state.module_filter = new_key
        shell.rebuild_current_screen()


def _on_toggle_expand(shell, event_ts: str) -> None:
    state = _ensure_state(shell)
    if state.expanded_event_ts == event_ts:
        state.expanded_event_ts = None
    else:
        state.expanded_event_ts = event_ts
    shell.rebuild_current_screen()


def _label_to_type_filter_key(label: str) -> Optional[str]:
    for key in TYPE_FILTER_OPTIONS:
        if t(f"wear_ledger.filter.type.{key}") == label:
            return key
    return None


def _label_to_range_filter_key(label: str) -> Optional[str]:
    for key in RANGE_FILTER_OPTIONS:
        if t(f"wear_ledger.filter.range.{key}") == label:
            return key
    return None


def _label_to_module_filter_key(
    label: str, module_options: tuple[str, ...]
) -> Optional[str]:
    for key in module_options:
        if _module_filter_label(key) == label:
            return key
    return None


# ---------------------------------------------------------------------------
# Add-service-note modal
# ---------------------------------------------------------------------------


def _open_note_modal(shell) -> None:
    state = _ensure_state(shell)
    state.pending_note_text = ""
    try:
        if dpg.does_item_exist(TAG_NOTE_MODAL):
            dpg.delete_item(TAG_NOTE_MODAL)
    except SystemError:
        # No DPG context (test path). Nothing to clean up.
        return
    try:
        with dpg.window(
            tag=TAG_NOTE_MODAL,
            label=t("wear_ledger.modal.title"),
            modal=True,
            no_close=False,
            no_resize=True,
            width=520,
            height=320,
        ):
            dpg.add_text(t("wear_ledger.modal.body"), wrap=480)
            dpg.add_spacer(height=8)
            dpg.add_input_text(
                tag=TAG_NOTE_MODAL_INPUT,
                multiline=True,
                width=480,
                height=160,
                hint=t("wear_ledger.modal.hint"),
            )
            dpg.add_spacer(height=8)
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label=t("wear_ledger.modal.save"),
                    tag=TAG_NOTE_MODAL_SAVE,
                    width=160,
                    callback=lambda *_a: _on_save_note(shell),
                )
                dpg.add_button(
                    label=t("wear_ledger.modal.cancel"),
                    tag=TAG_NOTE_MODAL_CANCEL,
                    width=120,
                    callback=lambda *_a: _on_cancel_note(shell),
                )
    except SystemError:
        # No DPG context — silently no-op. Tests assert via the
        # _save_service_note pure helper.
        return


def _on_save_note(shell) -> None:
    raw_text = ""
    try:
        raw_text = dpg.get_value(TAG_NOTE_MODAL_INPUT) or ""
    except SystemError:
        raw_text = ""
    _save_service_note(shell, raw_text)
    try:
        if dpg.does_item_exist(TAG_NOTE_MODAL):
            dpg.delete_item(TAG_NOTE_MODAL)
    except SystemError:
        pass
    shell.rebuild_current_screen()


def _on_cancel_note(shell) -> None:
    try:
        if dpg.does_item_exist(TAG_NOTE_MODAL):
            dpg.delete_item(TAG_NOTE_MODAL)
    except SystemError:
        pass


def _save_service_note(shell, raw_text: str) -> Optional[WearLedgerEvent]:
    """Sanitise and persist a service note. Returns the persisted event."""

    state = _ensure_state(shell)
    service: Optional[WearLedgerService] = getattr(shell, "wear_ledger_service", None)
    if service is None:
        state.status_text = t("wear_ledger.unavailable")
        state.status_kind = "warn"
        return None
    cleaned = sanitize_service_note(raw_text)
    if not cleaned:
        state.status_text = t("wear_ledger.modal.empty_warning")
        state.status_kind = "warn"
        return None
    event = service.append(
        SERVICE_NOTE,
        summary=cleaned.split("\n", 1)[0][:120] or t("wear_ledger.event.service_note"),
        details={"note": cleaned},
    )
    if event is None:
        state.status_text = t("wear_ledger.modal.save_failed")
        state.status_kind = "warn"
        return None
    state.status_text = t("wear_ledger.modal.save_success")
    state.status_kind = "info"
    return event


__all__ = [
    "FILTER_ALL",
    "MODULE_FILTER_ALL",
    "RANGE_FILTER_ALL",
    "RANGE_FILTER_7",
    "RANGE_FILTER_30",
    "RANGE_FILTER_90",
    "SPARKLINE_MAX_POINTS",
    "TYPE_FILTER_OPTIONS",
    "WearLedgerScreenState",
    "build",
]
