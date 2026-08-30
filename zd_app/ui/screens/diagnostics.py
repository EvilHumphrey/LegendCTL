"""Diagnostics screen."""

from __future__ import annotations

import logging
import time

import dearpygui.dearpygui as dpg

from zd_app.i18n import t
from zd_app.services.compatibility_report import build_compatibility_report
from zd_app.services.diagnostic_bundle import DiagnosticBundleService
from zd_app.services.diagnostics_service import _redact_instance_id
from zd_app.services.share_card import build_share_card
from zd_app.services.model_fingerprint import fingerprint_display_rows
from zd_app.services.path_scrub import scrub_paths
from zd_app.services.trust_matrix import (
    TrustMatrixSignals,
    build_trust_matrix,
    row_label,
)
from zd_app.services.trust_self_check import build_trust_self_check
from zd_app.ui import right_rail, support_reference, trust_front_door, trust_labels
from zd_app.ui.screens import about, preferences
from zd_app.ui.typography import screen_title, section_title


logger = logging.getLogger(__name__)


STALE_WARNING_HEADLINE = "Controller state may have changed outside the app."
STALE_WARNING_HELPER = "Read the controller again before trusting summary fields or write context as current."
UNKNOWN_FRESHNESS_STATUS_TEXT = "Unknown"

# Actions-card link to the dedicated Live Verify nav screen (the live
# gamepad-tester surface that used to render as a Diagnostics section).
OPEN_LIVE_VERIFY_BUTTON_TAG = "diag_open_live_verify_button"

# "What To Trust" card. Originally the 3rd, width=-1 (flex) card in the trio
# row, where its real inner width collapsed to ~187px at the minimum window
# (Actions/Calibration take fixed 280/400; the trust card gets the leftover), so
# its fixed wrap=780 clipped the prose horizontally (269px overrun at 1480, 302px
# at 1180 — tools/diag_dpg_card_clip.py). It now renders on its OWN full-width
# row below the trio (see build()), where the card width is never squeezed and a
# wrap matching the codebase's full-width convention fits at every viewport.
TRUST_CARD_TAG = "diagnostics_trust_card"
TRUST_BODY_TAG = "diagnostics_trust_body"
# Full-width body wrap. At the minimum 1180 window the content region is ~875px
# wide, so 840 wraps inside it with margin; at wider windows it reads as a
# comfortable measure. Matches the codebase convention for full-width prose
# (restore_points reason wrap=860, modules subtitle wrap=900).
_TRUST_BODY_WRAP = 840

TRUST_SELF_CHECK_CARD_TAG = "diagnostics_trust_self_check_card"
TRUST_SELF_CHECK_INTRO_TAG = "diagnostics_trust_self_check_intro"
TRUST_SELF_CHECK_SCOPE_DETAILS_TAG = "diagnostics_trust_self_check_scope_details"
TRUST_SELF_CHECK_COPY_TAG = "diagnostics_trust_self_check_copy"
TRUST_SELF_CHECK_STATUS_TAG = "diagnostics_trust_self_check_status"
TRUST_SELF_CHECK_MODEL_FINGERPRINT_TAG = "diagnostics_trust_self_check_model_fingerprint"
_TRUST_SELF_CHECK_WRAP = 840

TRUST_MATRIX_CARD_TAG = "diagnostics_trust_matrix_card"
TRUST_MATRIX_INTRO_TAG = "diagnostics_trust_matrix_intro"
_TRUST_MATRIX_WRAP = 840


def _trust_matrix_label_tag(index: int) -> str:
    return f"diagnostics_trust_matrix_label_{index}"


def _trust_matrix_claim_tag(index: int) -> str:
    return f"diagnostics_trust_matrix_claim_{index}"


def _trust_matrix_why_tag(index: int) -> str:
    return f"diagnostics_trust_matrix_why_{index}"


def _trust_matrix_qualifier_tag(index: int) -> str:
    return f"diagnostics_trust_matrix_qualifier_{index}"


_CONNECTION_DETAILS_CARD_WIDTH = 500
_CONNECTION_DETAILS_WRAP = 470
_STATUS_TRUST_STRIP_HEIGHT = 66

# Rail-safe wrap for full-width prose OUTSIDE the Guidance cards (the stale
# banner, the event log, and the Developer tab's debug/evidence paragraphs).
# These blocks carried fixed wraps of 1040-1200, tuned for the WINDOWED
# content region (~1230px at the 1480 reference) — but the rail/wide layout
# fixes the work column at 1040px (~1000px inner), so the long paragraphs
# clipped at the right edge (2026-07-06 visual review, 57_diagnostics_
# developer_maximized.png). 840 is the codebase's established full-width
# measure (What-To-Trust / Self-Check / matrix / compat / share cards all
# wrap at 840 and read clean in BOTH layouts per the same review): it fits
# the rail work column with margin AND the ~875px content region at the 1180
# minimum window (see the _TRUST_BODY_WRAP rationale above).
_FULL_WIDTH_PROSE_WRAP = 840

COMPAT_REPORT_CARD_TAG = "diagnostics_compat_report_card"
COMPAT_REPORT_VARIANT_TAG = "diagnostics_compat_report_variant"
COMPAT_REPORT_FIRMWARE_TAG = "diagnostics_compat_report_firmware"
COMPAT_REPORT_REFRESH_TAG = "diagnostics_compat_report_refresh"
COMPAT_REPORT_COPY_TAG = "diagnostics_compat_report_copy"
COMPAT_REPORT_OPEN_TAG = "diagnostics_compat_report_open"
COMPAT_REPORT_STATUS_TAG = "diagnostics_compat_report_status"
COMPAT_REPORT_PREVIEW_TAG = "diagnostics_compat_report_preview"
_COMPAT_REPORT_WRAP = 840
_COMPAT_REPORT_PREVIEW_HEIGHT = 120

SHARE_CARD_TAG = "diagnostics_share_card"
SHARE_CARD_SAVE_TAG = "diagnostics_share_card_save"
SHARE_CARD_COPY_TAG = "diagnostics_share_card_copy"
SHARE_CARD_STATUS_TAG = "diagnostics_share_card_status"
_SHARE_CARD_WRAP = 840

_TRUST_FRONT_DOOR_FOCUS_TARGETS = {
    "self_check": TRUST_SELF_CHECK_CARD_TAG,
    "compat_report": COMPAT_REPORT_CARD_TAG,
    # The public target name intentionally points to the actionable evidence
    # card controls, not a same-named internal tag.
    "evidence_card": SHARE_CARD_COPY_TAG,
    "trust_matrix": TRUST_MATRIX_CARD_TAG,
}

# Front-door anchoring does NOT hardcode a scroll container: the 2026-07-06
# hardware re-smoke falsified the "diagnostics_root is the scroll surface"
# assumption (its y_scroll_max reads ~0 while an INNER container really
# scrolls — the work column in the rail layout). The anchor instead walks the
# target's ancestor chain at anchor time and scrolls the NEAREST child_window
# whose y_scroll_max > 0, which self-corrects across the windowed
# (card -> tab -> tab_bar -> diagnostics_root -> content_region) and rail
# (card -> tab -> tab_bar -> diagnostics_work_column -> group ->
# diagnostics_root -> content_region) chains and any future reshuffle.
_TRUST_FRONT_DOOR_CHILD_WINDOW_TYPE = "mvAppItemType::mvChildWindow"
# Hard cap on the ancestor walk so a cyclic/mocked parent chain can't spin.
_TRUST_FRONT_DOOR_ANCESTOR_WALK_LIMIT = 50
# Layout may need rendered frames before any ancestor reports a scroll range;
# the anchor re-queues itself through the shell's deferred-UI seam (one drain
# pass — one rendered frame — apart) up to this many attempts, then gives up
# silently after a last-resort proportional scroll. REVISION-6 sizing: the
# rail/wide autosize chain publishes its scroll range only on the 4th-5th
# rendered frame after a rebuild (probe-measured at 1920x1040 and 2576x1408),
# so the old cap of 5 had ZERO headroom — one extra frame of latency on real
# hardware exhausted the budget and the maximized front-door click landed at
# the top (2026-07-06 round-2 verify, P3). Probe: cap 4 -> final scroll 0.0
# (the exact failure), cap 5 -> lands with no margin, cap 30 -> lands. 30
# attempts is ~0.5s at 60fps — still bounded, still a silent give-up, and
# every attempt stays existence-guarded so navigating away mid-wait is safe.
_TRUST_FRONT_DOOR_ANCHOR_MAX_ATTEMPTS = 30
# Small headroom so the anchored card's border isn't flush with the clip edge.
_TRUST_FRONT_DOOR_SCROLL_MARGIN = 8.0
# Rect-free LAST RESORT (final attempt only): approximate scroll position per
# target, as a fraction of the container's max y-scroll, from each card's
# position in the Guidance stack (What-To-Trust -> Self-Check -> matrix ->
# compat -> share -> event log). Scrolling near the card beats landing at the
# top of the tab (the hardware-smoke bug).
_TRUST_FRONT_DOOR_FALLBACK_SCROLL_FRACTIONS = {
    "self_check": 0.10,
    "trust_matrix": 0.35,
    "compat_report": 0.60,
    "evidence_card": 0.80,
}

# Tab identifiers for the Diagnostics screen (mirrors CONTROLLER_TAB_IDS in
# controller.py). The active tab is stashed on the shell as
# ``diagnostics_active_tab`` and re-selected after every rebuild — Diagnostics
# rebuilds on device-state ticks, so without persistence the tab would snap
# back to "status" each tick. "developer" only mounts when the Developer toggle
# is on; the does_item_exist guard on re-select handles its absence cleanly.
DIAGNOSTICS_TAB_IDS = ("status", "actions", "guidance", "developer")


def build(shell, parent: str) -> None:
    state = shell.device_service.state
    with right_rail.rail_screen(
        shell,
        parent,
        screen_id="diagnostics",
        root_tag="diagnostics_root",
        work_tag="diagnostics_work_column",
    ):
        screen_title(t("diagnostics.cards.diagnostics_title"))
        dpg.add_text(t("diagnostics.trust_anchor_intro"))
        dpg.add_spacer(height=10)

        # The stale-data warning is PINNED above the tab bar (never tucked
        # behind a tab) so "controller state may have changed outside the app"
        # stays visible whichever tab is active. Still conditional on freshness.
        if state.data_freshness == "stale":
            with dpg.child_window(height=92, border=True):
                dpg.add_text(t("diagnostics.stale_warning.headline"), color=shell.COLORS["warn"])
                dpg.add_text(t("diagnostics.stale_warning.helper"), wrap=_FULL_WIDTH_PROSE_WRAP, color=shell.COLORS["muted"])
            dpg.add_spacer(height=10)

        # Tabs keep each view inside the default 1480x920 window instead of one
        # ~1700px page scroll, mirroring the Controller screen. The active tab
        # is persisted on the shell and re-selected after each rebuild (see
        # DIAGNOSTICS_TAB_IDS) — Diagnostics rebuilds on device-state ticks, so
        # without persistence it would snap back to "status" every tick. The
        # developer panels collapse into one Developer tab that only mounts
        # when the Developer toggle is on.
        with dpg.tab_bar(
            tag="diagnostics_tab_bar",
            callback=lambda _s, selected_tab, _u: _remember_active_tab(shell, selected_tab),
        ):
            with dpg.tab(label=t("diagnostics.tab.status"), tag="diag_tab_status"):
                _build_status_tab(shell, state)
            with dpg.tab(label=t("diagnostics.tab.actions"), tag="diag_tab_actions"):
                _build_actions_tab(shell)
            with dpg.tab(label=t("diagnostics.tab.guidance"), tag="diag_tab_guidance"):
                _build_guidance_tab(shell)
            if getattr(shell.settings, "developer_panels_visible", False):
                with dpg.tab(label=t("diagnostics.tab.developer"), tag="diag_tab_developer"):
                    _build_developer_tab(shell)

        active_tab_tag = _diag_tab_id_to_tag(getattr(shell, "diagnostics_active_tab", "status"))
        if dpg.does_item_exist(active_tab_tag):
            dpg.set_value("diagnostics_tab_bar", active_tab_tag)
        _consume_trust_front_door_focus(shell)


def _remember_active_tab(shell, selected_tab) -> None:
    shell.diagnostics_active_tab = _diag_tab_tag_to_id(selected_tab)


def _diag_tab_id_to_tag(tab_id: str) -> str:
    if tab_id in DIAGNOSTICS_TAB_IDS:
        return f"diag_tab_{tab_id}"
    return "diag_tab_status"


def _diag_tab_tag_text(tab_tag) -> str:
    """Normalize a tab_bar value to its ``diag_tab_*`` alias string.

    Dear PyGui hands the tab_bar callback the *integer item id* of the
    selected tab as its ``app_data`` — NOT the string alias. ``str(<int id>)``
    never starts with ``diag_tab_``, so without resolving the id back to its
    alias every REAL tab selection fell through to the ``status`` default in
    :func:`_diag_tab_tag_to_id`: ``diagnostics_active_tab`` was silently reset
    to ``status`` on the next selection, so the persisted tab did not survive
    a rebuild and the consent-gate "How to verify this" link landed on Status
    instead of Guidance (v2.6.1 CU smoke). The suite injected the alias STRING
    directly, which passed straight through and masked the integer-id path —
    hence the REAL-render regression gate. String tags pass through untouched.
    """

    if isinstance(tab_tag, str):
        return tab_tag
    try:
        alias = dpg.get_item_alias(tab_tag)
    except Exception:  # pragma: no cover - defensive; alias lookup is total.
        alias = None
    return alias if alias else str(tab_tag)


def _diag_tab_tag_to_id(tab_tag) -> str:
    value = _diag_tab_tag_text(tab_tag)
    if value.startswith("diag_tab_"):
        tab_id = value.removeprefix("diag_tab_")
        if tab_id in DIAGNOSTICS_TAB_IDS:
            return tab_id
    return "status"


def _build_status_tab(shell, state) -> None:
    if right_rail.screen_wide_state(shell, "diagnostics"):
        with dpg.table(
            header_row=False,
            policy=dpg.mvTable_SizingStretchSame,
            tag="diagnostics_status_grid",
        ):
            dpg.add_table_column()
            dpg.add_table_column()
            with dpg.table_row():
                _build_health_card(shell, state, width=-1, wrap=360)
                _build_connection_card(shell, width=-1, wrap=360)
    else:
        _build_health_card(shell, state, width=220, wrap=190)
        dpg.add_spacer(height=8)
        _build_connection_card(
            shell,
            width=_CONNECTION_DETAILS_CARD_WIDTH,
            wrap=_CONNECTION_DETAILS_WRAP,
        )
    dpg.add_spacer(height=8)
    with dpg.child_window(
        width=-1,
        height=_STATUS_TRUST_STRIP_HEIGHT,
        border=True,
        tag="diagnostics_status_trust_front_door",
    ):
        with dpg.group(horizontal=True):
            dpg.add_text(
                t("diagnostics.trust_front_door.status_intro"),
                color=shell.COLORS["muted"],
            )
            trust_front_door.add_trust_link_buttons(
                shell,
                tag_prefix="diagnostics_status_trust_front_door",
                button_width=150,
            )


def _build_health_card(shell, state, *, width: int, wrap: int) -> None:
    with dpg.child_window(width=width, height=220, border=True):
        dpg.add_text(t("diagnostics.cards.health"), color=shell.COLORS["muted"])
        dpg.add_text(
            tag="diag_health_summary",
            default_value=health_summary_text(state, None),
            wrap=wrap,
        )


def _build_connection_card(shell, *, width: int, wrap: int) -> None:
    with dpg.child_window(width=width, height=220, border=True):
        dpg.add_text(t("diagnostics.cards.connection_details"), color=shell.COLORS["muted"])
        dpg.add_text(
            tag="diag_connection_details",
            default_value=t("diagnostics.connection.waiting"),
            wrap=wrap,
        )


def _build_actions_tab(shell) -> None:
    with dpg.group(horizontal=True):
        # Actions card holds routine actions plus a separated Maintenance group.
        # The extra maintenance text keeps Clear Logs visually away from safe
        # navigation/export actions.
        with dpg.child_window(width=280, height=440, border=True):
            dpg.add_text(t("ui.actions_c3cd636a"), color=shell.COLORS["muted"])
            dpg.add_button(label=t("ui.read_now_1d9d0f1e"), width=160, callback=lambda: shell.read_controller())
            # Live Verify (live button chips, per-stick circularity, inline
            # firmware-deadzone tuning) is now its own dedicated nav screen.
            # Link to it instead of driving an in-Diagnostics panel.
            dpg.add_button(
                label=t("diagnostics.live_verify.open_button"),
                tag=OPEN_LIVE_VERIFY_BUTTON_TAG,
                width=200,
                callback=lambda: shell.switch_screen("live_verify"),
            )
            # F3: the richer DiagnosticBundleService ZIP (report + module
            # passports + recent health reports + wear-ledger summary).
            dpg.add_button(label=t("ui.export_diagnostic_bundle_aa774599"), width=200, callback=lambda: shell.export_rich_diagnostic_bundle())
            dpg.add_text(
                t("diagnostics.actions.bundle_note"),
                color=shell.COLORS["muted"],
                # 280-wide card: inner content width is ~248 after padding,
                # so wrap=260 overran by ~9px. 236 wraps inside the card.
                wrap=236,
            )
            dpg.add_button(label=t("ui.open_calibration_guide_b87103fd"), width=180, callback=lambda: shell.open_support_guide("calibration"))
            dpg.add_button(label=t("ui.open_firmware_guide_620b9625"), width=180, callback=lambda: shell.open_support_guide("firmware"))
            dpg.add_button(label=t("ui.open_stack_guide_b240e63a"), width=180, callback=lambda: shell.open_support_guide("windows_component_model"))
            dpg.add_separator()
            dpg.add_text(t("diagnostics.actions.maintenance.title"), color=shell.COLORS["muted"])
            dpg.add_text(
                t("diagnostics.actions.maintenance.helper"),
                color=shell.COLORS["muted"],
                wrap=236,
            )
            dpg.add_button(label=t("ui.clear_logs_7c3089dc"), width=160, callback=lambda: shell.clear_diagnostic_logs())
        # Calibration card holds 1 label + summary + 5 bullets + 2 paragraphs.
        # 400 covers the worst-case English locale (DiagnosticsCardHeightTests
        # pins the floor).
        with dpg.child_window(width=400, height=400, border=True):
            dpg.add_text(t("ui.calibration_and_recovery_c8023571"), color=shell.COLORS["muted"])
            dpg.add_text(
                support_reference.localized_summary(support_reference.CALIBRATION_GUIDE),
                wrap=360,
            )
            dpg.add_spacer(height=4)
            for index, bullet in enumerate(
                support_reference.localized_bullets(support_reference.CALIBRATION_GUIDE),
                start=1,
            ):
                dpg.add_text(f"- {bullet}", tag=f"diagnostics_calibration_bullet_{index}", wrap=360)
            dpg.add_spacer(height=6)
            dpg.add_text(
                t("ui.diagnostics.firmware_target_split"),
                color=shell.COLORS["text"],
                wrap=360,
            )
            dpg.add_spacer(height=6)
            dpg.add_text(
                t("ui.windows_support_on_this_controller_is_a_stack_not_one_4b1748f1"),
                wrap=360,
            )


def _build_guidance_tab(shell) -> None:
    # "What To Trust" (full-width, content-fit so it never clips) + the Event
    # Log. The Event Log keeps its intentional bounded 320px scroll region — a
    # long log is meant to scroll within the card; that is not a content clip.
    with dpg.child_window(
        width=-1,
        border=True,
        tag=TRUST_CARD_TAG,
        auto_resize_y=True,
        autosize_y=False,
    ):
        dpg.add_text(t("ui.what_to_trust_fdc199d5"), color=shell.COLORS["muted"])
        dpg.add_text(
            t("diagnostics.trust.body"),
            wrap=_TRUST_BODY_WRAP,
            tag=TRUST_BODY_TAG,
        )
    dpg.add_spacer(height=10)
    _build_trust_self_check_card(shell)
    dpg.add_spacer(height=10)
    _build_trust_matrix_card(shell)
    dpg.add_spacer(height=10)
    _build_compatibility_report_card(shell)
    dpg.add_spacer(height=10)
    _build_share_card_card(shell)
    dpg.add_spacer(height=10)
    with dpg.child_window(
        border=True, auto_resize_y=True, autosize_y=False,
        always_auto_resize=True, tag="diagnostics_event_log_card",
    ):
        dpg.add_text(t("ui.event_log_878e531b"), color=shell.COLORS["muted"])
        dpg.add_button(
            label=t("diagnostics.event_log.copy"),
            tag="diagnostics_event_log_copy",
            width=0,
            callback=lambda: dpg.set_clipboard_text("\n".join(shell.device_service.recent_events(100))),
        )
        dpg.add_text(
            tag="diag_event_log",
            default_value=t("diagnostics.event_log.empty"),
            wrap=_FULL_WIDTH_PROSE_WRAP,
        )


def _trust_self_check_result(shell):
    result = getattr(shell, "_trust_self_check_result", None)
    fingerprint = _current_model_fingerprint(shell)
    fingerprint_digest = getattr(fingerprint, "digest", None)
    cached_digest = getattr(shell, "_trust_self_check_fingerprint_digest", None)
    if (
        result is None
        or not hasattr(result, "to_markdown")
        or cached_digest != fingerprint_digest
    ):
        result = build_trust_self_check(model_fingerprint=fingerprint)
        shell._trust_self_check_result = result
        shell._trust_self_check_fingerprint_digest = fingerprint_digest
    return result


def _current_model_fingerprint(shell):
    device_service = getattr(shell, "device_service", None)
    state = getattr(device_service, "state", None)
    return getattr(state, "model_fingerprint", None)


def _build_trust_self_check_card(shell) -> None:
    result = _trust_self_check_result(shell)
    with dpg.child_window(
        width=-1,
        border=True,
        tag=TRUST_SELF_CHECK_CARD_TAG,
        auto_resize_y=True,
        autosize_y=False,
    ):
        dpg.add_text(t("trust_self_check.title"), color=shell.COLORS["muted"])
        dpg.add_text(
            t("trust_self_check.display_caveat"),
            tag=TRUST_SELF_CHECK_INTRO_TAG,
            wrap=_TRUST_SELF_CHECK_WRAP,
            color=shell.COLORS["muted"],
        )
        shared_boundary = t("trust_self_check.boundary.session")
        with dpg.tree_node(
            label=t("trust_self_check.scope_details.title"),
            default_open=False,
            tag=TRUST_SELF_CHECK_SCOPE_DETAILS_TAG,
        ):
            dpg.add_text(
                t("trust_self_check.boundary.session"),
                wrap=_TRUST_SELF_CHECK_WRAP,
                color=shell.COLORS["muted"],
            )
            dpg.add_text(
                t("trust_self_check.drivers.boundary"),
                wrap=_TRUST_SELF_CHECK_WRAP,
                color=shell.COLORS["muted"],
            )
        dpg.add_spacer(height=6)
        for index, row in enumerate(result.rows):
            dpg.add_text(
                row.claim,
                tag=f"diagnostics_trust_self_check_claim_{index}",
                wrap=_TRUST_SELF_CHECK_WRAP,
            )
            dpg.add_text(
                row.evidence,
                tag=f"diagnostics_trust_self_check_evidence_{index}",
                wrap=_TRUST_SELF_CHECK_WRAP,
                color=shell.COLORS["muted"],
            )
            extra_boundary = _extra_boundary_text(row.boundary, shared_boundary)
            if extra_boundary:
                dpg.add_text(
                    extra_boundary,
                    tag=f"diagnostics_trust_self_check_boundary_extra_{index}",
                    wrap=_TRUST_SELF_CHECK_WRAP,
                    color=shell.COLORS["muted"],
                )
            dpg.add_spacer(height=4)
        if result.model_fingerprint is not None:
            _build_model_fingerprint_block(shell, result.model_fingerprint)
        dpg.add_button(
            label=t("trust_self_check.copy_button"),
            tag=TRUST_SELF_CHECK_COPY_TAG,
            width=160,
            callback=lambda: _copy_trust_self_check(shell),
        )
        dpg.add_text(
            "",
            tag=TRUST_SELF_CHECK_STATUS_TAG,
            wrap=_TRUST_SELF_CHECK_WRAP,
            color=shell.COLORS["muted"],
        )


def _extra_boundary_text(boundary: str, shared_boundary: str) -> str:
    """Return the row-specific boundary text beyond the shared sentence."""

    boundary = (boundary or "").strip()
    shared_boundary = (shared_boundary or "").strip()
    if not boundary or boundary == shared_boundary:
        return ""
    if shared_boundary and shared_boundary in boundary:
        return boundary.replace(shared_boundary, "", 1).strip()
    return boundary


def _build_model_fingerprint_block(shell, fingerprint) -> None:
    dpg.add_spacer(height=4)
    with dpg.group(tag=TRUST_SELF_CHECK_MODEL_FINGERPRINT_TAG):
        dpg.add_text(
            t("model_fingerprint.title"),
            wrap=_TRUST_SELF_CHECK_WRAP,
        )
        for index, (label_key, value) in enumerate(fingerprint_display_rows(fingerprint)):
            safe_value = scrub_paths(value)
            dpg.add_text(
                f"{t(label_key)}: {safe_value}",
                tag=f"diagnostics_model_fingerprint_row_{index}",
                wrap=_TRUST_SELF_CHECK_WRAP,
                color=shell.COLORS["muted"],
            )
        dpg.add_text(
            (
                f"{t('model_fingerprint.write_validation_basis_label')}: "
                f"{t('model_fingerprint.write_validation_basis_value')}"
            ),
            tag="diagnostics_model_fingerprint_basis",
            wrap=_TRUST_SELF_CHECK_WRAP,
            color=shell.COLORS["muted"],
        )
    dpg.add_spacer(height=4)


def _consume_trust_front_door_focus(shell) -> None:
    target = getattr(shell, trust_front_door.TRUST_FRONT_DOOR_FOCUS_ATTR, None)
    if not target:
        return
    setattr(shell, trust_front_door.TRUST_FRONT_DOOR_FOCUS_ATTR, None)

    tag = _TRUST_FRONT_DOOR_FOCUS_TARGETS.get(str(target))
    if not tag or not dpg.does_item_exist(tag):
        return

    _queue_trust_front_door_anchor(shell, str(target), tag, attempt=1)


def _queue_trust_front_door_anchor(shell, target: str, tag: str, *, attempt: int) -> None:
    """Queue one anchor attempt through the shell's deferred-UI seam.

    The consume runs in the same pass build() just populated, before a frame
    has rendered — no ancestor reports a scroll range yet. Routed through
    ``shell._defer_ui_call`` (the building block of the modal-swap idiom):
    armed, the attempt runs a drain pass later, after a rendered frame laid
    the items out; unarmed (tests, headless), it runs inline, preserving the
    synchronous contract those paths rely on. Failed attempts re-queue
    themselves (another frame apart when armed) up to
    ``_TRUST_FRONT_DOOR_ANCHOR_MAX_ATTEMPTS``.
    """

    def anchor() -> None:
        _run_trust_front_door_anchor(shell, target, tag, attempt=attempt)

    defer = getattr(shell, "_defer_ui_call", None)
    if callable(defer):
        defer(anchor)
    else:
        anchor()


def _run_trust_front_door_anchor(shell, target: str, tag: str, *, attempt: int) -> None:
    """One scroll-into-view attempt; re-queues itself until the layout is real.

    The explicit y-scroll is the WHOLE anchoring mechanism for card targets:
    ``dpg.focus_item`` only scrolls keyboard-focusable CONTROLS (the original
    landed-at-top bug), and on a child_window it issues its OWN ensure-visible
    scroll AFTER ours — the REVISION-5 probe measured focus driving the
    windowed root to 1155 where the correct anchor is 987, the ~130-170px
    overscroll the hardware verify caught. Focus therefore fires once, only
    for non-child_window targets (the evidence_card button), on the attempt
    that scrolled or on the final attempt regardless. Every step is
    existence-guarded and exception-contained — anchoring must never break
    navigation, and on give-up it fails SILENTLY (debug log only).
    """

    if not dpg.does_item_exist(tag):
        return
    final = attempt >= _TRUST_FRONT_DOOR_ANCHOR_MAX_ATTEMPTS
    scrolled = False
    try:
        scrolled = _scroll_trust_front_door_target_to_top(target, tag, final_attempt=final)
    except Exception:  # pragma: no cover - scroll is best-effort UI anchoring.
        logger.debug("Diagnostics trust-front-door scroll skipped", exc_info=True)
    if not scrolled and not final:
        _queue_trust_front_door_anchor(shell, target, tag, attempt=attempt + 1)
        return
    _focus_trust_front_door_target(tag)


def _focus_trust_front_door_target(tag: str) -> None:
    """Best-effort keyboard focus — actionable CONTROLS only, never cards."""

    if not dpg.does_item_exist(tag) or not hasattr(dpg, "focus_item"):
        return
    try:
        if dpg.get_item_type(tag) == _TRUST_FRONT_DOOR_CHILD_WINDOW_TYPE:
            return
        dpg.focus_item(tag)
    except Exception:  # pragma: no cover - focus is best-effort UI anchoring.
        logger.debug("Diagnostics trust-front-door focus skipped", exc_info=True)


def _scroll_trust_front_door_target_to_top(
    target: str, tag: str, *, final_attempt: bool
) -> bool:
    """Scroll the nearest scrollable ancestor so ``tag`` sits at the view top.

    Returns True when a scroll was issued. The container is DISCOVERED, not
    hardcoded: the 2026-07-06 hardware re-smoke falsified the fixed
    ``diagnostics_root`` assumption (its y_scroll_max reads ~0 while an inner
    child_window — the rail layout's work column — really scrolls). On the
    final attempt only, degraded inputs fall back to a proportional position
    on the best ancestor found rather than doing nothing.
    """

    container, nearest_child_window = _find_scrollable_ancestor(tag)
    if container is None:
        if not final_attempt or nearest_child_window is None:
            return False
        # Last resort: no ancestor advertises a scroll range even on the
        # final attempt — proportionally position the nearest child_window
        # (a 0 range makes this a no-op, which is then a genuine "fits").
        return _proportional_scroll(target, nearest_child_window)
    max_scroll = float(dpg.get_y_scroll_max(container))
    offset = _pos_derived_scroll_offset(tag, container)
    if offset is None:
        if not final_attempt:
            return False  # layout not real yet; caller re-queues for a later frame
        return _proportional_scroll(target, container)
    dpg.set_y_scroll(
        container,
        min(max(0.0, offset - _TRUST_FRONT_DOOR_SCROLL_MARGIN), max_scroll),
    )
    return True


def _proportional_scroll(target: str, container) -> bool:
    """Final-attempt fallback: scroll ``container`` to the target's stack fraction."""

    try:
        max_scroll = float(dpg.get_y_scroll_max(container))
    except Exception:
        return False
    if max_scroll <= 0.0:
        return False
    fraction = _TRUST_FRONT_DOOR_FALLBACK_SCROLL_FRACTIONS.get(target, 0.0)
    if fraction <= 0.0:
        return False
    dpg.set_y_scroll(container, min(max_scroll * fraction, max_scroll))
    return True


def _find_scrollable_ancestor(tag: str):
    """Walk up from ``tag``: (nearest scrollable child_window, nearest child_window).

    The first element is the anchor's scroll surface — the nearest ancestor
    child_window whose ``get_y_scroll_max() > 0`` — or None when nothing in
    the chain scrolls yet. The second is the nearest child_window ancestor
    regardless of scroll range, kept as the final attempt's last-resort
    container. Bounded, and every DPG query is exception-contained, so a
    mocked/cyclic parent chain degrades to (None, ...) instead of raising.
    """

    item = tag
    nearest_child_window = None
    for _ in range(_TRUST_FRONT_DOOR_ANCESTOR_WALK_LIMIT):
        try:
            parent = dpg.get_item_parent(item)
        except Exception:
            return None, nearest_child_window
        if not parent or parent == item:
            return None, nearest_child_window
        try:
            is_child_window = (
                dpg.get_item_type(parent) == _TRUST_FRONT_DOOR_CHILD_WINDOW_TYPE
            )
        except Exception:
            is_child_window = False
        if is_child_window:
            if nearest_child_window is None:
                nearest_child_window = parent
            try:
                if float(dpg.get_y_scroll_max(parent)) > 0.0:
                    return parent, nearest_child_window
            except Exception:
                pass
        item = parent
    return None, nearest_child_window


def _pos_derived_scroll_offset(tag: str, container) -> float | None:
    """Content-space y of ``tag`` inside ``container``, or None if unmeasured.

    Built on ``get_item_pos``, NOT ``get_item_rect_min``: the REVISION-5
    real-DPG probe showed child_window items expose NO rect_min in their item
    state (keys: pos / rect_size / scroll_* ...), so any rect-based math
    silently raised for card targets and never ran. ``get_item_pos`` is the
    reliable primitive — an item's layout position relative to its CONTAINING
    WINDOW (child windows count as windows), scroll-independent. The target's
    content-space offset inside the scrolled surface is therefore its own pos
    plus the pos of every child_window ancestor strictly between it and
    ``container`` (each hop re-bases into the next window out). A
    non-positive total means layout hasn't produced real positions yet (every
    Guidance card sits well below its window top) — return None so the
    caller re-queues for a later frame.
    """

    try:
        offset = float(dpg.get_item_pos(tag)[1])
    except Exception:
        return None
    item = tag
    for _ in range(_TRUST_FRONT_DOOR_ANCESTOR_WALK_LIMIT):
        try:
            parent = dpg.get_item_parent(item)
        except Exception:
            return None
        if not parent or parent == item:
            return None  # container never reached; treat as unmeasured
        if parent == container:
            return offset if offset > 0.0 else None
        try:
            if dpg.get_item_type(parent) == _TRUST_FRONT_DOOR_CHILD_WINDOW_TYPE:
                offset += float(dpg.get_item_pos(parent)[1])
        except Exception:
            return None
        item = parent
    return None


def _copy_trust_self_check(shell) -> None:
    result = _trust_self_check_result(shell)
    dpg.set_clipboard_text(result.to_markdown())
    if dpg.does_item_exist(TRUST_SELF_CHECK_STATUS_TAG):
        dpg.set_value(TRUST_SELF_CHECK_STATUS_TAG, t("trust_self_check.copy_success"))


def _provenance_color(shell, provenance: str):
    # "policy" is NOT an evidence class and must never take the "good" (green)
    # color — it marks a row that states a rule rather than reporting a device
    # reading (see services.trust_matrix.POLICY). Muted here is deliberate, not
    # a fallthrough: provenance drives the chip color, so a policy row that
    # leaked back to an evidence class would render green again even with
    # perfectly honest wording.
    return {
        "verified": shell.COLORS["good"],
        "inferred": shell.COLORS["warn"],
        "unknown": shell.COLORS["muted"],
        "policy": shell.COLORS["muted"],
    }.get(provenance, shell.COLORS["muted"])


def _trust_matrix_signals(shell) -> TrustMatrixSignals:
    """Gather the existing state signals the provenance rows derive from.

    Kept in the UI layer (not the pure service) because it reaches into
    shell/device-state; the derivation itself stays DPG-free in
    ``services.trust_matrix``. Every read is getattr-guarded so a partially
    built or mocked shell degrades to the honest ``unknown`` end.
    """

    device_service = getattr(shell, "device_service", None)
    state = getattr(device_service, "state", None)
    connection_state = getattr(state, "connection_state", "no_device")
    data_freshness = getattr(state, "data_freshness", "never_read")

    firmware = str(getattr(state, "firmware_version", "") or "").strip()
    firmware_known = bool(firmware and firmware != "Unknown")

    sources = getattr(state, "summary_sources", None) or {}
    active_profile_known = sources.get("active_profile", "unknown") != "unknown"
    # Source-aware chips: the firmware/profile rows earn "Verified from device"
    # only for a genuine device read (source "protocol"). The official ZD app UI
    # scrape ("official_app_ui") is not a device read, so it gets a distinct
    # chip. Flow the raw source values through; the derivation decides the chip.
    firmware_source = sources.get("firmware", "unknown")
    profile_source = sources.get("active_profile", "unknown")
    # A "protocol" profile source is sticky across a disconnect, so the matrix
    # earns "Verified from device" only when the protocol readback is still
    # valid for THIS connection (getattr-guarded to the honest False end).
    active_profile_protocol_verified_this_connection = bool(
        getattr(state, "active_profile_protocol_verified_this_connection", False)
    )

    snapshot = getattr(shell, "last_controller_snapshot", None)
    snapshot_ts = getattr(shell, "last_snapshot_ts", None)
    has_retained_settings = snapshot is not None and snapshot_ts is not None
    settings_read_current = has_retained_settings and _settings_read_matches_current(shell, state)

    settings_service = getattr(shell, "settings_service", None)
    raw_skipped = getattr(settings_service, "last_read_skipped_fields", 0)
    settings_skipped_fields = (
        int(raw_skipped)
        if isinstance(raw_skipped, (int, float)) and raw_skipped > 0
        else 0
    )

    fingerprint = getattr(state, "model_fingerprint", None)
    fingerprint_present = fingerprint is not None
    fingerprint_complete = fingerprint_present and getattr(fingerprint, "vid", None) is not None
    fingerprint_short_digest = (
        getattr(fingerprint, "short_digest", None) if fingerprint_present else None
    )

    return TrustMatrixSignals(
        connection_state=connection_state,
        data_freshness=data_freshness,
        firmware_known=firmware_known,
        active_profile_known=active_profile_known,
        firmware_source=firmware_source,
        profile_source=profile_source,
        active_profile_protocol_verified_this_connection=active_profile_protocol_verified_this_connection,
        settings_read_current=settings_read_current,
        has_retained_settings=has_retained_settings,
        settings_skipped_fields=settings_skipped_fields,
        fingerprint_present=fingerprint_present,
        fingerprint_complete=fingerprint_complete,
        fingerprint_short_digest=fingerprint_short_digest,
    )


def _settings_read_matches_current(shell, state) -> bool:
    """True when a retained settings read belongs to the unit connected now.

    Parallels ``home._settings_read_this_session``: a read only counts as
    "verified from device" while the SAME physical controller is still
    connected (identity match) — a disconnected unit or a mid-session swap
    demotes the retained snapshot to ``inferred`` rather than verified.
    """

    if getattr(state, "connection_state", None) != "connected":
        return False
    identity = getattr(state, "stable_identifier", None)
    if not identity or identity == "unknown":
        return False
    return getattr(shell, "last_snapshot_identity", None) == identity


def _build_trust_matrix_card(shell) -> None:
    rows = build_trust_matrix(_trust_matrix_signals(shell))
    with dpg.child_window(
        width=-1,
        border=True,
        tag=TRUST_MATRIX_CARD_TAG,
        auto_resize_y=True,
        autosize_y=False,
    ):
        dpg.add_text(t("trust_matrix.title"), color=shell.COLORS["muted"])
        dpg.add_text(
            t("trust_matrix.intro"),
            tag=TRUST_MATRIX_INTRO_TAG,
            wrap=_TRUST_MATRIX_WRAP,
            color=shell.COLORS["muted"],
        )
        dpg.add_spacer(height=6)
        for index, row in enumerate(rows):
            dpg.add_text(
                row.claim,
                tag=_trust_matrix_claim_tag(index),
                wrap=_TRUST_MATRIX_WRAP,
            )
            dpg.add_text(
                row_label(row),
                tag=_trust_matrix_label_tag(index),
                wrap=_TRUST_MATRIX_WRAP,
                color=_provenance_color(shell, row.provenance),
            )
            dpg.add_text(
                row.why,
                tag=_trust_matrix_why_tag(index),
                wrap=_TRUST_MATRIX_WRAP,
                color=shell.COLORS["muted"],
            )
            # Pre-built for every row (shown only when a qualifier exists) so the
            # in-place refresh is a pure value+show/hide update — never an
            # add_text into a live tree (the modal-teardown hazard).
            dpg.add_text(
                row.qualifier or "",
                tag=_trust_matrix_qualifier_tag(index),
                wrap=_TRUST_MATRIX_WRAP,
                color=shell.COLORS["warn"],
                show=row.qualifier is not None,
            )
            dpg.add_spacer(height=4)


def refresh_trust_matrix(shell) -> None:
    """Re-derive the provenance rows and update them in place.

    The matrix renders each row's label ONCE at Diagnostics build time, but the
    signals move afterward: the startup auto-read completes ~3s after the screen
    builds (settings/firmware/profile go verified), the fingerprint arrives
    async, and a disconnect must demote verified rows back to inferred/unknown.
    ``AppShell.refresh_shell`` calls this every tick so labels track live —
    exactly like ``home.refresh_setup_drawer``, but bidirectional (labels move
    BOTH ways here, unlike the drawer's one-directional flags).

    In-place tag updates only — never a ``rebuild_current_screen``. A full
    rebuild from a refresh path could collide with the DearPyGui modal-teardown
    law (an apply-confirm modal may be up when a read completes); reconfiguring
    existing tags is immune. No-op when the card tag is absent (a different
    screen is showing, or Diagnostics has not built this card).
    """

    if not dpg.does_item_exist(TRUST_MATRIX_CARD_TAG):
        return
    rows = build_trust_matrix(_trust_matrix_signals(shell))
    for index, row in enumerate(rows):
        label_tag = _trust_matrix_label_tag(index)
        if dpg.does_item_exist(label_tag):
            dpg.set_value(label_tag, row_label(row))
            dpg.configure_item(label_tag, color=_provenance_color(shell, row.provenance))
        why_tag = _trust_matrix_why_tag(index)
        if dpg.does_item_exist(why_tag):
            dpg.set_value(why_tag, row.why)
        qualifier_tag = _trust_matrix_qualifier_tag(index)
        if dpg.does_item_exist(qualifier_tag):
            dpg.set_value(qualifier_tag, row.qualifier or "")
            if row.qualifier is not None:
                dpg.show_item(qualifier_tag)
            else:
                dpg.hide_item(qualifier_tag)


def _build_compatibility_report_card(shell) -> None:
    report = _compatibility_report_result(shell)
    with dpg.child_window(
        width=-1,
        border=True,
        tag=COMPAT_REPORT_CARD_TAG,
        auto_resize_y=True,
        autosize_y=False,
        always_auto_resize=True,
    ):
        dpg.add_text(t("compat_report.title"), color=shell.COLORS["muted"])
        dpg.add_text(
            t("compat_report.intro"),
            wrap=_COMPAT_REPORT_WRAP,
            color=shell.COLORS["muted"],
        )
        dpg.add_spacer(height=6)
        with dpg.group(horizontal=True):
            with dpg.group():
                dpg.add_text(t("compat_report.variant_label"), color=shell.COLORS["muted"])
                dpg.add_input_text(
                    tag=COMPAT_REPORT_VARIANT_TAG,
                    default_value=getattr(shell, "_compat_report_variant", ""),
                    width=300,
                    hint=t("compat_report.variant_hint"),
                    callback=lambda _s, value, _u: _set_compatibility_report_field(shell, "variant", value),
                )
            with dpg.group():
                dpg.add_text(t("compat_report.firmware_label"), color=shell.COLORS["muted"])
                dpg.add_input_text(
                    tag=COMPAT_REPORT_FIRMWARE_TAG,
                    default_value=getattr(shell, "_compat_report_firmware", ""),
                    width=220,
                    hint=t("compat_report.firmware_hint"),
                    callback=lambda _s, value, _u: _set_compatibility_report_field(shell, "firmware", value),
                )
        dpg.add_spacer(height=8)
        with dpg.group(horizontal=True):
            dpg.add_button(
                label=t("compat_report.refresh_button"),
                tag=COMPAT_REPORT_REFRESH_TAG,
                width=140,
                callback=lambda: _refresh_compatibility_report(shell),
            )
            dpg.add_button(
                label=t("compat_report.copy_button"),
                tag=COMPAT_REPORT_COPY_TAG,
                width=160,
                callback=lambda: _copy_compatibility_report(shell),
            )
            dpg.add_button(
                label=t("compat_report.open_issue_button"),
                tag=COMPAT_REPORT_OPEN_TAG,
                width=190,
                enabled=bool(about.ISSUE_URL),
                callback=lambda: _open_compatibility_issue(shell),
            )
        dpg.add_text(
            "",
            tag=COMPAT_REPORT_STATUS_TAG,
            wrap=_COMPAT_REPORT_WRAP,
            color=shell.COLORS["muted"],
        )
        dpg.add_spacer(height=6)
        dpg.add_text(t("compat_report.preview_label"), color=shell.COLORS["muted"])
        dpg.add_input_text(
            tag=COMPAT_REPORT_PREVIEW_TAG,
            default_value=report.to_issue_body(),
            multiline=True,
            readonly=True,
            width=-1,
            height=_COMPAT_REPORT_PREVIEW_HEIGHT,
        )


def _set_compatibility_report_field(shell, field: str, value: str) -> None:
    if field == "variant":
        shell._compat_report_variant = value
    elif field == "firmware":
        shell._compat_report_firmware = value
    _refresh_compatibility_report(shell, status_key=None)


def _compatibility_report_result(shell):
    device_service = shell.device_service
    state = device_service.state
    return build_compatibility_report(
        device_state=state,
        variant=_compatibility_input_value(
            shell,
            COMPAT_REPORT_VARIANT_TAG,
            "_compat_report_variant",
        ),
        firmware=_compatibility_input_value(
            shell,
            COMPAT_REPORT_FIRMWARE_TAG,
            "_compat_report_firmware",
        ),
        last_read_duration_ms=getattr(device_service, "last_read_duration_ms", None),
        last_write_duration_ms=getattr(device_service, "last_write_duration_ms", None),
        last_apply_result=getattr(device_service, "last_apply_result", None),
        recent_events=device_service.recent_events(8),
        diagnostic_bundle_path=getattr(shell, "_last_diagnostic_bundle_path", None),
    )


def _compatibility_input_value(shell, tag: str, attr: str) -> str:
    if dpg.does_item_exist(tag):
        value = dpg.get_value(tag)
        if isinstance(value, str):
            setattr(shell, attr, value)
            return value
    return getattr(shell, attr, "")


def _refresh_compatibility_report(shell, *, status_key: str | None = "compat_report.refresh_success") -> None:
    report = _compatibility_report_result(shell)
    if dpg.does_item_exist(COMPAT_REPORT_PREVIEW_TAG):
        dpg.set_value(COMPAT_REPORT_PREVIEW_TAG, report.to_issue_body())
    if status_key and dpg.does_item_exist(COMPAT_REPORT_STATUS_TAG):
        dpg.set_value(COMPAT_REPORT_STATUS_TAG, t(status_key))


def _copy_compatibility_report(shell) -> None:
    report = _compatibility_report_result(shell)
    dpg.set_clipboard_text(report.to_issue_body())
    if dpg.does_item_exist(COMPAT_REPORT_PREVIEW_TAG):
        dpg.set_value(COMPAT_REPORT_PREVIEW_TAG, report.to_issue_body())
    if dpg.does_item_exist(COMPAT_REPORT_STATUS_TAG):
        dpg.set_value(COMPAT_REPORT_STATUS_TAG, t("compat_report.copy_success"))


def _open_compatibility_issue(shell) -> None:
    if not about.ISSUE_URL:
        if dpg.does_item_exist(COMPAT_REPORT_STATUS_TAG):
            dpg.set_value(COMPAT_REPORT_STATUS_TAG, t("compat_report.open_issue_unavailable"))
        return
    about._open_issue_url()
    if dpg.does_item_exist(COMPAT_REPORT_STATUS_TAG):
        dpg.set_value(COMPAT_REPORT_STATUS_TAG, t("compat_report.open_issue_status"))


def _build_share_card_card(shell) -> None:
    with dpg.child_window(
        width=-1,
        border=True,
        tag=SHARE_CARD_TAG,
        auto_resize_y=True,
        autosize_y=False,
    ):
        dpg.add_text(t("share_card.title"), color=shell.COLORS["muted"])
        dpg.add_text(
            t("share_card.intro"),
            wrap=_SHARE_CARD_WRAP,
            color=shell.COLORS["muted"],
        )
        dpg.add_spacer(height=8)
        with dpg.group(horizontal=True):
            dpg.add_button(
                label=t("share_card.save_button"),
                tag=SHARE_CARD_SAVE_TAG,
                width=180,
                callback=lambda: _save_share_card(shell),
            )
            dpg.add_button(
                label=t("share_card.copy_markdown_button"),
                tag=SHARE_CARD_COPY_TAG,
                width=250,
                callback=lambda: _copy_share_card_markdown(shell),
            )
        dpg.add_text(
            "",
            tag=SHARE_CARD_STATUS_TAG,
            wrap=_SHARE_CARD_WRAP,
            color=shell.COLORS["muted"],
        )


def _share_card_result(shell):
    device_service = shell.device_service
    bundle = getattr(shell, "diagnostic_bundle_service", None)
    if not isinstance(bundle, DiagnosticBundleService):
        bundle = None
    return build_share_card(
        device_state=device_service.state,
        variant=_compatibility_input_value(
            shell,
            COMPAT_REPORT_VARIANT_TAG,
            "_compat_report_variant",
        ),
        firmware=_compatibility_input_value(
            shell,
            COMPAT_REPORT_FIRMWARE_TAG,
            "_compat_report_firmware",
        ),
        last_read_duration_ms=getattr(device_service, "last_read_duration_ms", None),
        last_write_duration_ms=getattr(device_service, "last_write_duration_ms", None),
        last_apply_result=getattr(device_service, "last_apply_result", None),
        recent_events=device_service.recent_events(8),
        diagnostic_bundle_path=getattr(shell, "_last_diagnostic_bundle_path", None),
        diagnostic_bundle_service=bundle,
        trust_self_check=_trust_self_check_result(shell),
    )


def _copy_share_card_markdown(shell) -> None:
    dpg.set_clipboard_text(_share_card_result(shell).to_markdown())
    if dpg.does_item_exist(SHARE_CARD_STATUS_TAG):
        dpg.set_value(SHARE_CARD_STATUS_TAG, t("share_card.copy_success"))


def _save_share_card(shell) -> None:
    try:
        requested = preferences.diagnostics_bundle_dir_open_target(
            shell.settings.diagnostics_bundle_dir
        )
        output_dir = shell.diagnostics_service._safe_output_dir(str(requested))
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H%M%S")
        html_path = output_dir / f"legendctl_evidence_card_{stamp}.html"
        markdown_path = output_dir / f"legendctl_evidence_card_{stamp}.md"
        card = _share_card_result(shell)
        html_path.write_text(card.to_html(), encoding="utf-8")
        markdown_path.write_text(card.to_markdown(), encoding="utf-8")
    except Exception:  # noqa: BLE001 - save action should never crash the UI
        logger.exception("Diagnostics share card export failed")
        shell.device_service.log_i18n_event("log.diagnostics.share_card_failed")
        if dpg.does_item_exist(SHARE_CARD_STATUS_TAG):
            dpg.set_value(SHARE_CARD_STATUS_TAG, t("share_card.save_failed"))
        return

    shell._last_share_card_path = html_path
    shell.device_service.log_i18n_event(
        "log.diagnostics.share_card_saved",
        filename=html_path.name,
    )
    if dpg.does_item_exist(SHARE_CARD_STATUS_TAG):
        dpg.set_value(
            SHARE_CARD_STATUS_TAG,
            t("share_card.save_success", filename=html_path.name),
        )


def _build_developer_tab(shell) -> None:
    # Home for the developer panels (gated behind the Developer toggle). The
    # card fits its content (auto_resize_y) so it never grows an inner scrollbar;
    # the screen-root page bar is the only fallback scroll if it exceeds the
    # window.
    _build_raw_hid_section(shell)


def _build_raw_hid_section(shell) -> None:
    with dpg.child_window(border=True, auto_resize_y=True, autosize_y=False):
        section_title(t("diagnostics.raw_hid.title"))
        dpg.add_checkbox(
            label=t("diagnostics.raw_hid.enable"),
            tag="diag_raw_hid_enabled",
            default_value=False,
        )
        frames = getattr(shell.device_service, "raw_hid_frames", None)
        if not isinstance(frames, (list, tuple)):
            frames = []
        text = "\n".join(frames[-100:]) if frames else t("diagnostics.raw_hid.empty")
        dpg.add_text(tag="diag_raw_hid_log", default_value=text, wrap=_FULL_WIDTH_PROSE_WRAP)


def health_summary_text(state, last_packet_timestamp: str | None) -> str:
    if state.connection_state != "connected":
        return _health_summary("disconnected")
    if state.data_freshness == "stale":
        return _health_summary("stale")
    if last_packet_timestamp:
        return _health_summary("healthy")
    if state.last_read_time:
        return _health_summary("waiting")
    return _health_summary("never_read")


def _health_summary(state_key: str) -> str:
    return "\n".join(
        (
            t(f"diagnostics.health.state.{state_key}"),
            t(f"diagnostics.health.body.{state_key}"),
        )
    )


def connection_details_text(state, snapshot, summary_source_summary: str) -> str:
    return "\n".join(
        (
            _connection_row("transport", _connection_value_text(snapshot.connection_mode)),
            _connection_row("device_id", _redact_instance_id(snapshot.device_id)),
            _connection_row("firmware", _connection_value_text(snapshot.firmware_version)),
            _connection_row("sleep", _connection_value_text(state.sleep_setting)),
            _connection_row("active_config", active_config_status_text(state)),
            _connection_row("summary_source", _connection_value_text(summary_source_summary)),
            _connection_row(
                "last_packet",
                snapshot.last_packet_timestamp or t("transport.path.none"),
            ),
            _connection_row(
                "last_read",
                _duration_or_none(snapshot.last_read_duration_ms, "no_read_recorded"),
            ),
            _connection_row(
                "last_write",
                _duration_or_none(snapshot.last_write_duration_ms, "no_write_recorded"),
            ),
        )
    )


def _connection_row(field_key: str, value: str) -> str:
    return f"{t(f'diagnostics.connection.field.{field_key}')}: {value}"


def _duration_or_none(value: float | None, missing_key: str) -> str:
    if value is None:
        return t(f"diagnostics.connection.value.{missing_key}")
    return f"{value:.2f}ms"


def _connection_value_text(value: str) -> str:
    value_key = {
        "Unknown": "common.unknown",
        "Not verified": "profile.config_state.not_verified",
        "XInput (Battery)": "diagnostics.connection.value.xinput_battery",
        "XInput (source for: Battery)": "diagnostics.connection.value.xinput_battery",
    }.get(value)
    if value_key is not None:
        return t(value_key)
    return value


def active_config_status_text(state) -> str:
    label = trust_labels.active_config_label(state)
    if label == "Not verified":
        return t("profile.config_state.not_verified")
    if label.startswith("Config "):
        return t("profile.config_state.config", n=label.removeprefix("Config "))
    return label


def freshness_status_text(state) -> str:
    label = {
        "fresh": "Current",
        "stale": "Stale",
        "never_read": "Never Read",
        "reading": "Reading",
        "write_pending": "Write Pending",
        "write_success": "Write Succeeded",
        "write_failed": "Write Failed",
    }.get(state.data_freshness)
    if label is not None:
        return label
    logger.warning("Unmapped freshness status label requested: %s", state.data_freshness)
    return UNKNOWN_FRESHNESS_STATUS_TEXT
