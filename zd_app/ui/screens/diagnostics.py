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
from zd_app.services.trust_self_check import build_trust_self_check
from zd_app.ui import support_reference, trust_labels
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
TRUST_SELF_CHECK_COPY_TAG = "diagnostics_trust_self_check_copy"
TRUST_SELF_CHECK_STATUS_TAG = "diagnostics_trust_self_check_status"
_TRUST_SELF_CHECK_WRAP = 840

COMPAT_REPORT_CARD_TAG = "diagnostics_compat_report_card"
COMPAT_REPORT_VARIANT_TAG = "diagnostics_compat_report_variant"
COMPAT_REPORT_FIRMWARE_TAG = "diagnostics_compat_report_firmware"
COMPAT_REPORT_REFRESH_TAG = "diagnostics_compat_report_refresh"
COMPAT_REPORT_COPY_TAG = "diagnostics_compat_report_copy"
COMPAT_REPORT_OPEN_TAG = "diagnostics_compat_report_open"
COMPAT_REPORT_STATUS_TAG = "diagnostics_compat_report_status"
COMPAT_REPORT_PREVIEW_TAG = "diagnostics_compat_report_preview"
_COMPAT_REPORT_WRAP = 840

SHARE_CARD_TAG = "diagnostics_share_card"
SHARE_CARD_SAVE_TAG = "diagnostics_share_card_save"
SHARE_CARD_COPY_TAG = "diagnostics_share_card_copy"
SHARE_CARD_STATUS_TAG = "diagnostics_share_card_status"
_SHARE_CARD_WRAP = 840

# Tab identifiers for the Diagnostics screen (mirrors CONTROLLER_TAB_IDS in
# controller.py). The active tab is stashed on the shell as
# ``diagnostics_active_tab`` and re-selected after every rebuild — Diagnostics
# rebuilds on device-state ticks, so without persistence the tab would snap
# back to "status" each tick. "developer" only mounts when the Developer toggle
# is on; the does_item_exist guard on re-select handles its absence cleanly.
DIAGNOSTICS_TAB_IDS = ("status", "actions", "guidance", "developer")


def build(shell, parent: str) -> None:
    state = shell.device_service.state
    with dpg.child_window(parent=parent, autosize_x=True, autosize_y=True, border=False):
        screen_title(t("diagnostics.cards.diagnostics_title"))
        dpg.add_text(t("diagnostics.trust_anchor_intro"))
        dpg.add_spacer(height=10)

        # The stale-data warning is PINNED above the tab bar (never tucked
        # behind a tab) so "controller state may have changed outside the app"
        # stays visible whichever tab is active. Still conditional on freshness.
        if state.data_freshness == "stale":
            with dpg.child_window(height=92, border=True):
                dpg.add_text(t("diagnostics.stale_warning.headline"), color=shell.COLORS["warn"])
                dpg.add_text(t("diagnostics.stale_warning.helper"), wrap=1040, color=shell.COLORS["muted"])
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


def _remember_active_tab(shell, selected_tab) -> None:
    shell.diagnostics_active_tab = _diag_tab_tag_to_id(selected_tab)


def _diag_tab_id_to_tag(tab_id: str) -> str:
    if tab_id in DIAGNOSTICS_TAB_IDS:
        return f"diag_tab_{tab_id}"
    return "diag_tab_status"


def _diag_tab_tag_to_id(tab_tag) -> str:
    value = str(tab_tag)
    if value.startswith("diag_tab_"):
        tab_id = value.removeprefix("diag_tab_")
        if tab_id in DIAGNOSTICS_TAB_IDS:
            return tab_id
    return "status"


def _build_status_tab(shell, state) -> None:
    # Health (220) + Connection (320) sit side by side and fit the default
    # window.
    with dpg.group(horizontal=True):
        with dpg.child_window(width=220, height=220, border=True):
            dpg.add_text(t("diagnostics.cards.health"), color=shell.COLORS["muted"])
            # wrap to the card's inner width (220 - padding) so a long status
            # line (e.g. "Disconnected — No supported controller…") wraps
            # instead of overrunning the right edge (measured 53px H-clip).
            dpg.add_text(
                tag="diag_health_summary",
                default_value=health_summary_text(state, None),
                wrap=190,
            )
        with dpg.child_window(width=320, height=220, border=True):
            dpg.add_text(t("diagnostics.cards.connection_details"), color=shell.COLORS["muted"])
            dpg.add_text(
                tag="diag_connection_details",
                default_value=t("diagnostics.connection.waiting"),
            )


def _build_actions_tab(shell) -> None:
    with dpg.group(horizontal=True):
        # Actions card holds 1 label + 8 buttons + 1 wrap=236 helper text.
        # Heights are unchanged from the pre-tab layout (audit floors pinned by
        # DiagnosticsCardHeightTests): 360 clears the 8 buttons + helper.
        with dpg.child_window(width=280, height=360, border=True):
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
    _build_compatibility_report_card(shell)
    dpg.add_spacer(height=10)
    _build_share_card_card(shell)
    dpg.add_spacer(height=10)
    with dpg.child_window(height=320, border=True):
        dpg.add_text(t("ui.event_log_878e531b"), color=shell.COLORS["muted"])
        dpg.add_button(
            label=t("diagnostics.event_log.copy"),
            width=160,
            callback=lambda: dpg.set_clipboard_text("\n".join(shell.device_service.recent_events(100))),
        )
        dpg.add_text(
            tag="diag_event_log",
            default_value=t("diagnostics.event_log.empty"),
            wrap=1200,
        )


def _trust_self_check_result(shell):
    result = getattr(shell, "_trust_self_check_result", None)
    if result is None or not hasattr(result, "to_markdown"):
        result = build_trust_self_check()
        shell._trust_self_check_result = result
    return result


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
            t("trust_self_check.intro"),
            tag=TRUST_SELF_CHECK_INTRO_TAG,
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
            dpg.add_text(
                row.boundary,
                tag=f"diagnostics_trust_self_check_boundary_{index}",
                wrap=_TRUST_SELF_CHECK_WRAP,
                color=shell.COLORS["muted"],
            )
            dpg.add_spacer(height=4)
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


def _copy_trust_self_check(shell) -> None:
    result = _trust_self_check_result(shell)
    dpg.set_clipboard_text(result.to_markdown())
    if dpg.does_item_exist(TRUST_SELF_CHECK_STATUS_TAG):
        dpg.set_value(TRUST_SELF_CHECK_STATUS_TAG, t("trust_self_check.copy_success"))


def _build_compatibility_report_card(shell) -> None:
    report = _compatibility_report_result(shell)
    with dpg.child_window(
        width=-1,
        border=True,
        tag=COMPAT_REPORT_CARD_TAG,
        auto_resize_y=True,
        autosize_y=False,
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
            height=220,
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
        dpg.add_text(tag="diag_raw_hid_log", default_value=text, wrap=1180)


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
