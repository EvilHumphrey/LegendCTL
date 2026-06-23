"""Device vs Profile screen — read-only live-device-vs-wrapper-profile diff.

Answers *"what is actually on my controller right now vs the profile I have
selected?"* — a field-by-field comparison of the **live device read** (Current)
against a **chosen wrapper profile** (Selected), in the honest-measurement
spirit of the Health Report / Restore Points. Purely informational: it never
writes to the device and never touches the apply / read / restore pipelines.

The comparison itself lives in the pure, fully-unit-tested
:func:`zd_app.services.snapshot_diff.compute_snapshot_diff`; this module is just
the layout dispatcher (mirrors the Wear Ledger / Restore Points pattern):
:func:`build` reads ``shell.device_vs_profile_screen_state`` and renders the
current view; callbacks mutate that state and request a rebuild. A full device
read is cached on the state so toggling a filter or changing the profile does
not re-issue the many sequential HID round-trips — only the explicit "Read from
controller" button (or first entry) does.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import dearpygui.dearpygui as dpg

from zd_app.i18n import t
from zd_app.services.settings_service import ControllerSnapshot
from zd_app.services.snapshot_diff import (
    DRIFT_DRIFTED,
    STATUS_CHANGED,
    STATUS_CURRENT_UNREADABLE,
    STATUS_ENCODING_DIFFERS,
    STATUS_ONLY_IN_PROFILE,
    STATUS_ONLY_ON_DEVICE,
    STATUS_READ_ONLY_UNSUPPORTED,
    STATUS_SAME,
    STATUS_WRITE_ONLY,
    SnapshotDiff,
    SnapshotFieldDiff,
    compute_snapshot_diff,
)
from zd_app.storage.last_applied_store import LastAppliedRecord
from zd_app.ui.components import FOOTER_RESERVE_PX, Column, table, table_empty_state
from zd_app.ui.typography import helper_text, screen_title


logger = logging.getLogger(__name__)


TAG_ROOT_CONTAINER = "device_vs_profile_root"
TAG_PROFILE_COMBO = "device_vs_profile_profile_combo"
TAG_READ_BUTTON = "device_vs_profile_read_button"
TAG_SHOW_ONLY_CHANGES = "device_vs_profile_show_only_changes"
TAG_STATUS_TEXT = "device_vs_profile_status_text"
TAG_DIFF_TABLE = "device_vs_profile_diff_table"
TAG_DIFF_SCROLL = "device_vs_profile_diff_scroll"


# Footer wrapper-profile selector tag — read (best-effort) to default the
# screen's profile picker to whatever the operator already has selected.
_FOOTER_PROFILE_COMBO = "wrapper_profile_combo"

# Category-section display order: matches snapshot_diff's grouping so the
# section headers appear in device → feel → layout → cosmetic → unsupported
# order. Device-global fields (polling_rate / step_size) live under "device".
_SECTION_ORDER: tuple[str, ...] = ("device", "feel", "layout", "cosmetic", "unsupported")

# Placeholder for a cell with no value (unreadable / write-only / absent).
_DASH = "—"

# Status → i18n suffix for the Status-column label (rendered next to the colour
# chip so the verdict is not conveyed by colour alone).
_STATUS_LABEL_KEY: dict[str, str] = {
    STATUS_CHANGED: "device_vs_profile.status.changed",
    STATUS_SAME: "device_vs_profile.status.same",
    STATUS_CURRENT_UNREADABLE: "device_vs_profile.status.unreadable",
    STATUS_WRITE_ONLY: "device_vs_profile.status.write_only",
    STATUS_READ_ONLY_UNSUPPORTED: "device_vs_profile.status.read_only",
    STATUS_ONLY_IN_PROFILE: "device_vs_profile.status.only_profile",
    STATUS_ONLY_ON_DEVICE: "device_vs_profile.status.only_device",
    STATUS_ENCODING_DIFFERS: "device_vs_profile.status.encoding_differs",
}


@dataclass
class DeviceVsProfileScreenState:
    selected_profile_id: Optional[str] = None  # wrapper-profile name (the store key)
    show_only_changes: bool = False
    status_text: str = ""
    status_kind: str = "info"  # info|warn|bad|good
    last_read_ts: Optional[float] = None  # time.time() of last live read (display only)
    # Cached live read so filter / profile toggles do not re-issue HID reads.
    read_attempted: bool = False
    read_failed: bool = False
    current_snapshot: Optional[ControllerSnapshot] = None
    current_read_success: Optional[dict] = None
    current_read_errors: Optional[dict] = None


def _ensure_state(shell) -> DeviceVsProfileScreenState:
    state = getattr(shell, "device_vs_profile_screen_state", None)
    if state is None:
        state = DeviceVsProfileScreenState()
        shell.device_vs_profile_screen_state = state
    return state


# ---------------------------------------------------------------------------
# Top-level build
# ---------------------------------------------------------------------------


def build(shell, parent: str) -> None:
    """Render the Device-vs-Profile screen into ``parent``."""

    state = _ensure_state(shell)
    store = getattr(shell, "wrapper_profile_store", None)

    with dpg.child_window(
        parent=parent,
        tag=TAG_ROOT_CONTAINER,
        autosize_x=True,
        autosize_y=True,
        border=False,
    ):
        screen_title(t("device_vs_profile.title"))
        helper_text(t("device_vs_profile.subtitle"), wrap=900)
        dpg.add_spacer(height=8)

        if store is None:
            dpg.add_text(t("device_vs_profile.unavailable"), wrap=900)
            return

        profile_names = _profile_names(store)
        if not profile_names:
            table_empty_state(t("device_vs_profile.no_profiles"))
            return

        # Resolve the selected profile (default to the footer selection, else
        # the first profile) and self-heal a stale selection.
        if state.selected_profile_id not in profile_names:
            state.selected_profile_id = _default_profile_id(profile_names)

        _build_controls(shell, state, profile_names)
        # Phase 2: the Last-Applied record (persisted by the profile-apply /
        # Safe-Import pipelines). One muted line states what is on record —
        # or that nothing is — before any comparison renders.
        record = _load_last_applied(shell)
        if record is not None:
            dpg.add_text(
                t(
                    "device_vs_profile.last_applied_header",
                    name=record.profile_name,
                    ts=record.applied_at,
                ),
                color=shell.COLORS["muted"],
                wrap=900,
            )
        else:
            dpg.add_text(
                t("device_vs_profile.no_apply_recorded"),
                color=shell.COLORS["muted"],
                wrap=900,
            )
        dpg.add_spacer(height=10)

        if state.selected_profile_id is None:
            dpg.add_text(t("device_vs_profile.pick_profile"), wrap=900)
            return

        # Load the selected profile snapshot.
        try:
            profile = store.load(state.selected_profile_id)
        except Exception as exc:  # noqa: BLE001 - corrupt / missing profile file
            logger.warning("device_vs_profile: failed to load profile: %s", exc)
            dpg.add_text(
                t("device_vs_profile.load_failed", reason=str(exc)),
                color=shell.COLORS["warn"],
                wrap=900,
            )
            return
        selected_snapshot = profile.snapshot

        connected = _is_connected(shell)

        # Read the live device (once per entry, or on the Read button). Gated on
        # connection so a disconnected device does not spam per-field "unreadable".
        if connected and not state.read_attempted:
            _perform_read(shell, state)

        last_applied = record.snapshot if record is not None else None
        last_applied_failed = (
            frozenset(record.failed_fields) if record is not None else frozenset()
        )

        if not connected:
            dpg.add_text(
                t("device_vs_profile.no_device"),
                color=shell.COLORS["warn"],
                wrap=900,
            )
            dpg.add_spacer(height=8)
            diff = compute_snapshot_diff(
                _empty_snapshot(),
                selected_snapshot,
                last_applied=last_applied,
                last_applied_failed=last_applied_failed,
            )
            _render_diff_table(
                shell, diff, state, current_override=_DASH, record=record
            )
            _caveat_footer(shell)
            return

        if state.read_failed or state.current_snapshot is None:
            # Connected but the read raised — show the failure banner and the
            # profile's values with an unavailable Current column.
            if state.status_text:
                dpg.add_text(state.status_text, color=shell.COLORS["bad"], wrap=900)
                dpg.add_spacer(height=8)
            diff = compute_snapshot_diff(
                _empty_snapshot(),
                selected_snapshot,
                last_applied=last_applied,
                last_applied_failed=last_applied_failed,
            )
            _render_diff_table(
                shell, diff, state, current_override=_DASH, record=record
            )
            _caveat_footer(shell)
            return

        diff = compute_snapshot_diff(
            state.current_snapshot,
            selected_snapshot,
            current_read_success=state.current_read_success,
            current_read_errors=state.current_read_errors,
            last_applied=last_applied,
            last_applied_failed=last_applied_failed,
        )
        if record is not None:
            summary = t(
                "device_vs_profile.summary_with_drift",
                changed=diff.n_changed,
                same=diff.n_same,
                unreadable=diff.n_unreadable,
                drifted=diff.n_drifted,
            )
        else:
            summary = t(
                "device_vs_profile.summary",
                changed=diff.n_changed,
                same=diff.n_same,
                unreadable=diff.n_unreadable,
            )
        dpg.add_text(summary, color=shell.COLORS["muted"], wrap=900)
        dpg.add_spacer(height=8)
        _render_diff_table(shell, diff, state, current_override=None, record=record)
        _caveat_footer(shell)


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------


def _build_controls(
    shell, state: DeviceVsProfileScreenState, profile_names: list[str]
) -> None:
    with dpg.group(horizontal=True):
        dpg.add_text(
            t("device_vs_profile.profile_picker_label"), color=shell.COLORS["muted"]
        )
        dpg.add_combo(
            items=profile_names,
            default_value=state.selected_profile_id or "",
            width=240,
            tag=TAG_PROFILE_COMBO,
            callback=lambda sender, app_data, *_a: _on_profile_changed(shell, app_data),
        )
        dpg.add_button(
            label=t("device_vs_profile.read_button"),
            tag=TAG_READ_BUTTON,
            width=180,
            callback=lambda *_a: _on_read_clicked(shell),
        )
        dpg.add_checkbox(
            label=t("device_vs_profile.show_only_changes"),
            default_value=state.show_only_changes,
            tag=TAG_SHOW_ONLY_CHANGES,
            callback=lambda sender, app_data, *_a: _on_show_only_changes(shell, app_data),
        )


# ---------------------------------------------------------------------------
# Diff table rendering
# ---------------------------------------------------------------------------


def _render_diff_table(
    shell,
    diff: SnapshotDiff,
    state: DeviceVsProfileScreenState,
    *,
    current_override: Optional[str],
    record: Optional[LastAppliedRecord] = None,
) -> None:
    """Render the grouped Field / Current / Selected [/ Last applied] / Status table.

    ``current_override`` (a placeholder string) forces every Current cell to
    that text with a muted status chip — used when the live device is
    unavailable (disconnected / read failed) so the screen still shows the
    selected profile's values without implying a comparison it cannot make.

    The Last-applied value column exists only when a ``record`` is present
    (Phase 2); without one the table is exactly the Phase-1 four-column layout.
    """

    rows = list(diff.fields)
    if state.show_only_changes and current_override is None:
        rows = [r for r in rows if r.status == STATUS_CHANGED]

    if not rows:
        table_empty_state(t("device_vs_profile.no_differences"))
        return

    by_category: dict[str, list[SnapshotFieldDiff]] = {}
    for row in rows:
        by_category.setdefault(row.category, []).append(row)

    with_last_applied = record is not None
    if with_last_applied:
        columns = [
            Column(t("device_vs_profile.col.field"), weight=0.28),
            Column(t("device_vs_profile.col.current"), weight=0.24),
            Column(t("device_vs_profile.col.selected"), weight=0.24),
            Column(t("device_vs_profile.col.last_applied"), weight=0.24),
            Column(t("device_vs_profile.col.status"), width=120, no_resize=True),
        ]
    else:
        columns = [
            Column(t("device_vs_profile.col.field"), weight=0.34),
            Column(t("device_vs_profile.col.current"), weight=0.27),
            Column(t("device_vs_profile.col.selected"), weight=0.27),
            Column(t("device_vs_profile.col.status"), width=120, no_resize=True),
        ]

    # Scroll discipline: the comparison table is the ONE scroll surface. Wrap it
    # in a child_window that fills the height remaining below the controls /
    # summary MINUS a reserve for the pinned caveat footer (height=
    # -FOOTER_RESERVE_PX, ImGui negative-size). The table grows to its row count
    # inside, so the child scrolls INTERNALLY on overflow while the page (the
    # autosize_y screen root) does NOT scroll and the caveat stays visible.
    # (Before this the table was bare in the root, so a long diff scrolled the
    # whole page and pushed the read-only caveat off the bottom.) border=False
    # keeps the screen's borderless look; the table draws its own row borders.
    with dpg.child_window(
        width=-1,
        height=-FOOTER_RESERVE_PX,
        border=False,
        tag=TAG_DIFF_SCROLL,
    ):
        with table(
            columns,
            tag=TAG_DIFF_TABLE,
            resizable=True,
        ):
            for category in _SECTION_ORDER:
                group_rows = by_category.get(category)
                if not group_rows:
                    continue
                _section_header_row(shell, category, with_last_applied=with_last_applied)
                for row in group_rows:
                    _field_row(
                        shell,
                        row,
                        current_override=current_override,
                        with_last_applied=with_last_applied,
                    )


def _section_header_row(shell, category: str, *, with_last_applied: bool = False) -> None:
    with dpg.table_row():
        dpg.add_text(
            t(f"device_vs_profile.section.{category}"),
            color=shell.COLORS["text"],
        )
        for _ in range(4 if with_last_applied else 3):
            dpg.add_text("")


def _field_row(
    shell,
    row: SnapshotFieldDiff,
    *,
    current_override: Optional[str],
    with_last_applied: bool = False,
) -> None:
    with dpg.table_row():
        dpg.add_text(row.field_name, color=shell.COLORS["text"])
        if current_override is not None:
            dpg.add_text(current_override, color=shell.COLORS["muted"])
        else:
            current_text = row.current_value if row.current_value is not None else _DASH
            note = f"  ({row.note})" if (row.current_value is None and row.note) else ""
            color = shell.COLORS["text"] if row.current_value is not None else shell.COLORS["muted"]
            dpg.add_text(f"{current_text}{note}", color=color, wrap=320)
        selected_text = row.selected_value if row.selected_value is not None else _DASH
        dpg.add_text(selected_text, color=shell.COLORS["text"], wrap=320)
        if with_last_applied:
            _last_applied_cell(shell, row)
        _status_chip(shell, row.status, muted=current_override is not None)


def _last_applied_cell(shell, row: SnapshotFieldDiff) -> None:
    """The Last-applied value cell: drift is the warn-coloured exception.

    * failed-at-apply → the recorded value with a "failed at apply" caveat,
      muted (it was sent, never ACKed — no honest comparison exists);
    * drifted → the value plus a "drifted" marker in the warn colour (the
      verdict is also in text, never colour alone);
    * everything else (matches / unknowable / no value) → muted value or dash.
    """

    value = row.last_applied_value if row.last_applied_value is not None else _DASH
    if row.last_applied_failed:
        dpg.add_text(
            f"{value}  ({t('device_vs_profile.status.apply_failed_field')})",
            color=shell.COLORS["muted"],
            wrap=320,
        )
    elif row.drift == DRIFT_DRIFTED:
        dpg.add_text(
            f"{value}  ({t('device_vs_profile.status.drifted')})",
            color=shell.COLORS["warn"],
            wrap=320,
        )
    else:
        dpg.add_text(value, color=shell.COLORS["muted"], wrap=320)


def _status_chip(shell, status: str, *, muted: bool) -> None:
    """A ``■`` colour chip + a translated label (verdict not by colour alone).

    bad = changed, good = same, muted = unreadable / informational. When the
    live device is unavailable the chip is forced muted (no real comparison was
    made).
    """

    if muted:
        bucket = "muted"
    elif status == STATUS_CHANGED:
        bucket = "bad"
    elif status == STATUS_SAME:
        bucket = "good"
    elif status == STATUS_ENCODING_DIFFERS:
        bucket = "muted"
    else:
        bucket = "muted"
    label = t(_STATUS_LABEL_KEY.get(status, "device_vs_profile.status.same"))
    dpg.add_text(f"■ {label}", color=shell.COLORS[bucket])


def _caveat_footer(shell) -> None:
    dpg.add_spacer(height=10)
    dpg.add_text(
        t("device_vs_profile.caveat"),
        color=shell.COLORS["muted"],
        wrap=900,
    )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def _perform_read(shell, state: DeviceVsProfileScreenState) -> None:
    """Issue one provenance-tracked live read, caching the result on the state.

    Defensive: a full read is many sequential HID round-trips and can raise
    (HID timeout / OSError). A failure sets ``read_failed`` + a status banner
    and never crashes the UI (mirrors ``AppShell.refresh_from_controller``).
    """

    if getattr(shell, "hid_busy", False):
        # A threaded HID flow (footer read / profile apply / RP restore) is
        # mid-job. Issuing this many-round-trip read now would interleave
        # with the worker's frames, so refuse it — render the existing
        # read-failed banner with the busy message instead. Refuse, don't
        # queue: the Read button re-fires the read once the job is done.
        state.read_attempted = True
        state.current_snapshot = None
        state.current_read_success = None
        state.current_read_errors = None
        state.read_failed = True
        state.status_text = t("apply.busy")
        state.status_kind = "bad"
        return

    state.read_attempted = True
    state.last_read_ts = time.time()
    service = getattr(shell, "restore_point_service", None)
    if service is None:
        # No fresh-read service — fall back to the cached shell snapshot with no
        # provenance (best-effort; the differ treats absences honestly).
        cached = getattr(shell, "last_controller_snapshot", None)
        state.current_snapshot = cached
        state.current_read_success = None
        state.current_read_errors = None
        state.read_failed = cached is None
        if cached is None:
            state.status_text = t("device_vs_profile.read_failed")
            state.status_kind = "bad"
        return
    try:
        snapshot, read_success, read_errors = service.read_current_state_with_provenance()
    except Exception as exc:  # noqa: BLE001 - HID timeout / OSError / anything
        logger.warning("device_vs_profile: live read failed: %s", exc)
        state.current_snapshot = None
        state.current_read_success = None
        state.current_read_errors = None
        state.read_failed = True
        state.status_text = t("device_vs_profile.read_failed_reason", reason=str(exc))
        state.status_kind = "bad"
        return
    state.current_snapshot = snapshot
    state.current_read_success = dict(read_success)
    state.current_read_errors = dict(read_errors)
    state.read_failed = False
    state.status_text = ""
    state.status_kind = "info"


def _empty_snapshot() -> ControllerSnapshot:
    """An all-absent snapshot — the Current side when the live device is unavailable."""

    return ControllerSnapshot(
        polling_rate=None,
        vibration=None,
        deadzones=None,
        axis_inversion_left=None,
        axis_inversion_right=None,
        sensitivity_left=None,
        sensitivity_right=None,
        trigger_left=None,
        trigger_right=None,
        button_bindings={},
        lighting_zones={},
    )


# ---------------------------------------------------------------------------
# Last-Applied record (Phase 2)
# ---------------------------------------------------------------------------


def _load_last_applied(shell) -> Optional[LastAppliedRecord]:
    """The persisted Last-Applied record, or ``None`` when unavailable.

    ``None`` covers every quiet case the same way: no store wired (tests /
    embedding callers), nothing recorded yet, or a corrupt file (the store
    already maps that to ``None`` + a log warning). The screen then simply
    renders its "no apply recorded" hint — never a crash, never a disclosure.
    """

    store = getattr(shell, "last_applied_store", None)
    if store is None:
        return None
    try:
        return store.load()
    except Exception as exc:  # noqa: BLE001 - never crash the screen on a bad store
        logger.warning("device_vs_profile: last-applied load failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------


def _profile_names(store) -> list[str]:
    try:
        profiles, _skipped = store.list_profiles()
    except Exception as exc:  # noqa: BLE001 - never crash the screen on a bad store
        logger.warning("device_vs_profile: list_profiles failed: %s", exc)
        return []
    return [p.name for p in profiles]


def _default_profile_id(profile_names: list[str]) -> Optional[str]:
    """Default to the footer's current wrapper-profile selection, else the first."""

    footer_choice = None
    try:
        if dpg.does_item_exist(_FOOTER_PROFILE_COMBO):
            footer_choice = dpg.get_value(_FOOTER_PROFILE_COMBO)
    except SystemError:
        # No DPG context (test path) — fall through to the first profile.
        footer_choice = None
    if footer_choice and footer_choice in profile_names:
        return footer_choice
    return profile_names[0] if profile_names else None


def _is_connected(shell) -> bool:
    device_service = getattr(shell, "device_service", None)
    if device_service is None:
        return False
    state = getattr(device_service, "state", None)
    return getattr(state, "connection_state", None) == "connected"


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


def _on_profile_changed(shell, app_data: str) -> None:
    state = _ensure_state(shell)
    if app_data and app_data != state.selected_profile_id:
        state.selected_profile_id = app_data
        shell.rebuild_current_screen()


def _on_read_clicked(shell) -> None:
    state = _ensure_state(shell)
    # Force a fresh read on the next build.
    state.read_attempted = False
    shell.rebuild_current_screen()


def _on_show_only_changes(shell, app_data: bool) -> None:
    state = _ensure_state(shell)
    state.show_only_changes = bool(app_data)
    shell.rebuild_current_screen()


__all__ = [
    "DeviceVsProfileScreenState",
    "TAG_DIFF_SCROLL",
    "TAG_DIFF_TABLE",
    "TAG_PROFILE_COMBO",
    "TAG_READ_BUTTON",
    "TAG_SHOW_ONLY_CHANGES",
    "build",
]
