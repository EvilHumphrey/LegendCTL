"""Diagnostics screen."""

from __future__ import annotations

import logging

import dearpygui.dearpygui as dpg

from zd_app.i18n import t
from zd_app.ui import support_reference, trust_labels
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
            _connection_row("device_id", snapshot.device_id),
            _connection_row("firmware", _connection_value_text(snapshot.firmware_version)),
            _connection_row("sleep", _connection_value_text(state.sleep_setting)),
            _connection_row("active_config", active_config_status_text(state)),
            _connection_row("summary_source", _connection_value_text(summary_source_summary)),
            _connection_row(
                "last_packet",
                snapshot.last_packet_timestamp or t("transport.path.none"),
            ),
            _connection_row("last_read", f"{snapshot.last_read_duration_ms or 0:.2f}ms"),
            _connection_row("last_write", f"{snapshot.last_write_duration_ms or 0:.2f}ms"),
        )
    )


def _connection_row(field_key: str, value: str) -> str:
    return f"{t(f'diagnostics.connection.field.{field_key}')}: {value}"


def _connection_value_text(value: str) -> str:
    value_key = {
        "Unknown": "common.unknown",
        "Not verified": "profile.config_state.not_verified",
        "XInput (Battery)": "diagnostics.connection.value.xinput_battery",
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
