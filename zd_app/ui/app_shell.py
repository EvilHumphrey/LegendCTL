"""App shell for the ZD Ultimate Legend wrapper."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import datetime
import logging
import os
import queue
import threading
import time
import traceback
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

import dearpygui.dearpygui as dpg

from zd_app.i18n import set_locale, t
from zd_app.models import (
    AppSettings,
    BUTTON_ACTIONS,
    StickSettings,
    TriggerSettings,
    WrapperProfile,
    utc_now_iso,
)
from zd_app.services.device_service import DeviceService, LogEntry, render_log_message
from zd_app.services.diagnostics_service import DiagnosticsService
from zd_app.services.health_report import (
    DeviceContext,
    HealthReportService,
    QuickCheckService,
    make_xinput_sample_provider,
)
from zd_app.services.preflight_service import PreflightService
from zd_app.services.locale_router import LocaleRouter
from zd_app.services.profile_service import ProfileService
from zd_app.services.diagnostic_bundle import DiagnosticBundleService
from zd_app.services.module_passport import ModulePassportService
from zd_app.services.restore_point_service import (
    RestorePointService,
    verify_applied_snapshot,
)
from zd_app.services.title_manager import TitleManager
from zd_app.services.wear_ledger import WearLedgerService
from zd_app.services.wear_ledger.models import (
    PROFILE_APPLY,
    SESSION_END,
    SESSION_START,
    SLIDER_WRITE,
)
from zd_app.services.settings_apply_coordinator import (
    ApplyFailure,
    ApplyResult,
    SettingsApplyCoordinator,
    outcome_is_success as _settings_outcome_is_success,
    outcome_label as _settings_outcome_label,
    outcome_used_retry as _settings_outcome_used_retry,
    result_error_text as _settings_result_error_text,
    result_is_transient as _settings_result_is_transient,
)
from zd_app.services.settings_service import (
    AxisInversion,
    BackPaddleBinding,
    ButtonMapping,
    ButtonSlot,
    ControllerButtonTarget,
    ControllerSnapshot,
    LightingMode,
    LightingSettings,
    LightingZone,
    MacroSlot,
    POLLING_RATE_HZ,
    PollingRate,
    RgbColor,
    SensitivityAnchor,
    SensitivityAnchorTuple8,
    SettingsService,
    StickDeadzones,
    TriggerMode,
    TriggerSettings as ServiceTriggerSettings,
    TriggerVibrationMode,
    VibrationSettings,
)
from zd_app.services.xinput_poll_service import XInputPollService
from zd_app.storage.last_applied_store import (
    LastAppliedRecord,
    LastAppliedStore,
    utc_now_iso_z,
)
from zd_app.storage.restore_point_models import (
    DeviceIdentity,
    IdentityConfidence,
    RestorePointTrigger,
)
from zd_app.storage.restore_point_store import RestorePointStore
from zd_app.storage.settings_store import SettingsStore
from zd_app.storage.snapshot_codec import snapshot_to_dict
from zd_app.storage.wrapper_profile_store import WrapperProfileError, WrapperProfileStore
from zd_app.ui import (
    diagnostic_bundle_preview,
    safe_import_model,
    support_reference,
)
from zd_app.ui.fonts import bind_default_font, register_fonts
from zd_app.ui.typography import screen_title
from zd_app.ui.localized_dpg import install_dearpygui_i18n
from zd_app.ui.widgets import trust_ritual as trust_ritual_widget
from zd_app.ui.safe_import_model import (
    DEFAULT_CHECKED_CATEGORIES,
    RiskCategory,
    SELECTABLE_CATEGORIES,
    filtered_snapshot,
    has_device_settings,
    without_device_settings,
)
from zd_app.ui.components import (
    register_card_theme,
    register_destructive_theme,
    register_table_theme,
)
from zd_app.ui.screens import about, controller, device_vs_profile, diagnostics, health_report, home, live_verify, modules as modules_screen, preferences, readiness_check, restore_points, safe_import, wear_ledger
from zd_app.ui.screens.legacy import buttons, sticks, triggers
from zd_app.ui.themes import (
    COLORS,
    LEGACY_COLOR_ALIASES,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    register_global_theme,
)
from zd_app.version import __app_name__, __build_commit__, __version__


# Device-settings widget/modal tags.
SAVE_AS_INCLUDE_DEVICE_CHECKBOX = "wrapper_profile_include_device_checkbox"
APPLY_DEVICE_CONFIRM_MODAL = "apply_device_confirm_modal"

# Fix B (2026-06-24): the dismissible "save changed step_size into the active
# profile" nudge rendered under the step-size slider row.
STEP_SIZE_SAVE_NUDGE_GROUP = "step_size_save_nudge_group"
STEP_SIZE_SAVE_NUDGE_BUTTON = "step_size_save_nudge_button"
STEP_SIZE_SAVE_NUDGE_DISMISS = "step_size_save_nudge_dismiss"


# Restore-Points debounce window for the
# ``before_manual_device_setting_write`` trigger. A drag-storm on the step
# slider or polling combo would otherwise create one restore point per tick;
# this window collapses any burst within ~7 s into a single pre-write snapshot.
# The "Trigger model" design recommends 5-10 s; 7 s is the middle of that range.
MANUAL_DEVICE_WRITE_RP_WINDOW_S = 7.0

# Inner drag-storm throttle for slider live-writes (step_size, polling_rate).
# Distinct from MANUAL_DEVICE_WRITE_RP_WINDOW_S: that one collapses the RP
# capture (outer, 7 s); this one collapses the HID write itself to ~6-7 writes
# per second of continuous drag, instead of 30-50.
SLIDER_LIVE_WRITE_THROTTLE_S = 0.15

# Transient first-read-after-burst HID timeout — mitigation constants.
# Observed twice on real hardware 2026-06-10: (1) the first snapshot refresh
# of a fresh app launch, right after the controller enumerated; (2) the
# automatic post-apply re-read one second after "OK: Applied profile 'aa'
# (29 writes)". Both raised "HID read timed out after 1000ms" and both
# succeeded immediately on a manual retry (footer Read). This matches the
# firmware's want-quiet-time-after-bursts behavior family — see the per-field
# trailer rationale in settings_apply_coordinator. These constants are a
# settle + single-retry MITIGATION, not a characterization; if first-read
# timeouts persist, the follow-up is a hardware characterization pass, not
# more retries.
#
# Settle slept by refresh_from_controller before its single retry after a
# first-read TimeoutError.
READ_TIMEOUT_RETRY_SETTLE_S = 0.3

# Settle slept between an apply write-burst and the automatic post-apply
# refresh_from_controller, so the firmware gets a quiet interval before the
# next feature-report read batch (same rationale as the apply coordinator's
# per-field trailers).
POST_APPLY_READ_SETTLE_S = 0.25

# A threaded HID job that outlives every per-read timeout has effectively
# wedged: the busy flag stays True and all HID flows keep refusing, with only
# a transient banner per click. Past this threshold _tick logs ONE warning
# per job and pins the footer status to apply.busy_long with a counting
# duration, so a stuck busy state is visible instead of silent.
HID_JOB_STALL_WARN_S = 30.0

# Buttons that start the long HID flows (footer Read / Apply / Delete plus the
# Restore-Points confirm-restore button). While a threaded HID job is in
# flight they are disabled as a set, then re-enabled when the job's completion
# drains — so the busy refusal is visible, not just a swallowed click.
_HID_FLOW_BUTTON_TAGS = (
    "footer_read_button",
    "footer_apply_button",
    "footer_delete_button",
    restore_points.TAG_CONFIRM_RESTORE_BUTTON,
)


def threaded_hid_executor(
    job: Callable[[], Any], deliver: Callable[[Any], None]
) -> None:
    """Production HID-job executor: one daemon worker thread per job.

    ``main_zd.py`` passes this as :class:`AppShell`'s ``hid_executor`` so the
    long HID flows run off the render thread. ``job`` must be DPG-free;
    ``deliver`` receives the job's return value — or the BaseException it
    raised — and is thread-safe (it enqueues onto the shell's completion
    queue, drained by :meth:`AppShell._tick` on the render thread). Daemon so
    a wedged HID read can never block process exit.
    """

    def _worker() -> None:
        try:
            result: Any = job()
        except BaseException as exc:  # noqa: BLE001 — routed to on_done, mirroring sync mode
            deliver(exc)
        else:
            deliver(result)

    threading.Thread(target=_worker, name="zd-hid-job", daemon=True).start()


class _SliderWriteThrottle:
    """Throttle slider live-writes with leading-edge fire + trailing-edge flush.

    A drag's first tick after the quiet window writes immediately so the
    controller responds instantly when the user starts dragging. Subsequent
    ticks within ``window_s`` store the latest value but don't fire. When
    :meth:`flush_pending` is called by the render loop and the window has
    elapsed with a pending value present, the trailing write fires so the
    controller ends up at whatever value the slider visually displays.

    Per-field state via the ``field_key`` parameter so step_size + polling_rate
    (and any future live-write fields) don't share a single throttle.
    """

    def __init__(self, *, window_s: float = SLIDER_LIVE_WRITE_THROTTLE_S):
        self._window_s = window_s
        self._last_write_ts: dict[str, float] = {}
        self._pending: dict[str, Any] = {}

    def should_write_now(self, field_key: str, value: Any, *, now: float) -> bool:
        """Returns True if ``value`` should be written immediately.

        If False, the value is stored as pending and will fire on the next
        :meth:`flush_pending` call once ``window_s`` has elapsed.
        """

        last = self._last_write_ts.get(field_key)
        if last is None or now - last >= self._window_s:
            self._last_write_ts[field_key] = now
            self._pending.pop(field_key, None)
            return True
        self._pending[field_key] = value
        return False

    def flush_pending(self, *, now: float) -> list[tuple[str, Any]]:
        """Return ``(field_key, value)`` pairs whose throttle window has elapsed.

        Updates ``_last_write_ts`` for each flushed field so a subsequent
        write within the next window is throttled normally.
        """

        flushed: list[tuple[str, Any]] = []
        for field_key, value in list(self._pending.items()):
            last = self._last_write_ts.get(field_key, 0.0)
            if now - last >= self._window_s:
                self._last_write_ts[field_key] = now
                del self._pending[field_key]
                flushed.append((field_key, value))
        return flushed

    def peek_pending(self) -> list[str]:
        """Field keys with a stored pending value, WITHOUT consuming them.

        Lets the render loop see which trailing writes are queued (e.g. to log
        that they're deferred behind an in-flight HID job) without discarding
        them — they stay queued and fire on the next :meth:`flush_pending` once
        the gate clears.
        """

        return list(self._pending)

    def quiet_window_elapsed(self, field_key: str, *, now: float) -> bool:
        """True once ``window_s`` has elapsed since the last write of ``field_key``.

        Read-only (touches neither pending nor timestamps): lets the render loop
        tell when a leading-edge value has SETTLED — i.e. no further write
        superseded it within the throttle window — so a deferred read-back verify
        can fire for a single-callback change that never created a trailing
        pending. ``False`` if the field has never been written.
        """

        last = self._last_write_ts.get(field_key)
        return last is not None and now - last >= self._window_s

POLLING_RATE_BY_LABEL = {
    "250Hz": PollingRate.HZ_250,
    "500Hz": PollingRate.HZ_500,
    "1000Hz": PollingRate.HZ_1000,
    "2000Hz": PollingRate.HZ_2000,
    "4000Hz": PollingRate.HZ_4000,
    "8000Hz": PollingRate.HZ_8000,
}

SETTING_LABEL_KEYS = {
    "language": "setting.label.language",
    "developer_panels_visible": "setting.label.developer_panels_visible",
    "auto_read_on_connect": "setting.label.auto_read_on_connect",
    "logging_verbosity": "setting.label.logging_verbosity",
    "diagnostics_bundle_dir": "setting.label.diagnostics_bundle_dir",
    "show_legacy_screens": "setting.label.show_legacy_screens",
}

APPLY_FAILURE_ROW_LABEL_KEYS = {
    "polling": "apply.failure.row_label.polling",
    "step_size": "apply.failure.row_label.step_size",
    "vibration": "apply.failure.row_label.vibration",
    "deadzones": "apply.failure.row_label.deadzones",
    "sens_left": "apply.failure.row_label.sens_left",
    "sens_right": "apply.failure.row_label.sens_right",
    "axis_inv_left": "apply.failure.row_label.axis_inv_left",
    "axis_inv_right": "apply.failure.row_label.axis_inv_right",
    "trigger_left": "apply.failure.row_label.trigger_left",
    "trigger_right": "apply.failure.row_label.trigger_right",
    "settings_service": "apply.failure.row_label.settings_service",
    **{
        f"binding_{slot.name}": f"apply.failure.row_label.binding_{slot.name}"
        for slot in ButtonSlot
    },
    **{
        f"back_paddle_{slot.name}": f"apply.failure.row_label.back_paddle_{slot.name}"
        for slot in MacroSlot
    },
    "lighting_HOME": "apply.failure.row_label.lighting_home",
    "lighting_LEFT_LIGHT": "apply.failure.row_label.lighting_left",
    "lighting_RIGHT_LIGHT": "apply.failure.row_label.lighting_right",
}

VIBRATION_MODE_BY_LABEL = {
    "Native Trigger Vibration": TriggerVibrationMode.NATIVE,
    "Stereo Resonance": TriggerVibrationMode.STEREO_RESONANCE,
    "Trigger Vibration": TriggerVibrationMode.TRIGGER_VIBRATION,
}

TRIGGER_MODE_BY_LABEL = {
    "Short": TriggerMode.SHORT,
    "Long": TriggerMode.LONG,
}

BUTTON_SLOT_BY_LABEL = {
    "Up": ButtonSlot.UP,
    "Right": ButtonSlot.RIGHT,
    "Down": ButtonSlot.DOWN,
    "Left": ButtonSlot.LEFT,
    "A": ButtonSlot.A,
    "B": ButtonSlot.B,
    "X": ButtonSlot.X,
    "Y": ButtonSlot.Y,
    "LB": ButtonSlot.LB,
    "RB": ButtonSlot.RB,
    "LT": ButtonSlot.LT,
    "RT": ButtonSlot.RT,
    "Back": ButtonSlot.BACK,
    "Start": ButtonSlot.START,
    "LS": ButtonSlot.LS,
    "RS": ButtonSlot.RS,
}

BUTTON_TARGET_BY_LABEL = {
    "LS": ControllerButtonTarget.LS,
    "RS": ControllerButtonTarget.RS,
    "Up": ControllerButtonTarget.UP,
    "Right": ControllerButtonTarget.RIGHT,
    "Down": ControllerButtonTarget.DOWN,
    "Left": ControllerButtonTarget.LEFT,
    "A": ControllerButtonTarget.A,
    "B": ControllerButtonTarget.B,
    "X": ControllerButtonTarget.X,
    "Y": ControllerButtonTarget.Y,
    "LB": ControllerButtonTarget.LB,
    "RB": ControllerButtonTarget.RB,
    "LT": ControllerButtonTarget.LT,
    "RT": ControllerButtonTarget.RT,
    "Back": ControllerButtonTarget.BACK,
    "Start": ControllerButtonTarget.START,
}


def _back_paddle_target_label(target: ControllerButtonTarget | None) -> str:
    if target is None:
        return t("controller.back_paddles.unbound")
    return t(f"controller.back_paddles.target.{target.name}")


def _back_paddle_target_by_label(label: str) -> ControllerButtonTarget | None:
    if label == t("controller.back_paddles.unbound"):
        return None
    for target in ControllerButtonTarget:
        if label == _back_paddle_target_label(target):
            return target
    return None


LIGHTING_ZONE_BY_LABEL = {
    "Home": LightingZone.HOME,
    "Left": LightingZone.LEFT_LIGHT,
    "Right": LightingZone.RIGHT_LIGHT,
}

LIGHTING_MODE_BY_LABEL = {
    "Off": LightingMode.OFF,
    "Always On": LightingMode.ALWAYS_ON,
    "Breath": LightingMode.BREATH,
    "Fade": LightingMode.FADE,
    "Flow": LightingMode.FLOW,
}

SENSITIVITY_PRESETS = {
    "Default": (
        SensitivityAnchor(0, 0),
        SensitivityAnchor(50, 50),
        SensitivityAnchor(100, 100),
    ),
    "Instant": (
        SensitivityAnchor(25, 25),
        SensitivityAnchor(26, 42),
        SensitivityAnchor(100, 100),
    ),
    "Balanced": (
        SensitivityAnchor(54, 54),
        SensitivityAnchor(86, 61),
        SensitivityAnchor(100, 100),
    ),
    "Delayed": (
        SensitivityAnchor(24, 24),
        SensitivityAnchor(86, 58),
        SensitivityAnchor(100, 100),
    ),
    "High Performance": (
        SensitivityAnchor(24, 24),
        SensitivityAnchor(54, 80),
        SensitivityAnchor(100, 100),
    ),
}

# 8-point (cat 0x86) curve presets. X stays at the standard sample positions
# (0,14,28,42,57,71,85,100); only Y varies. Every curve is monotonic
# non-decreasing in both axes — the constraint enforced by
# _validate_sensitivity_anchors_8point — so all four apply cleanly. These are
# structurally-correct starter shapes (NOT captured from the vendor app); the Y
# values can be swapped for vendor-matched curves later without touching the
# apply path. Keyed by the same names the controller screen's preset row uses.
_SENSITIVITY_8POINT_PRESET_X = (0, 14, 28, 42, 57, 71, 85, 100)
SENSITIVITY_PRESETS_8POINT = {
    # Linear (default): output tracks input 1:1.
    "Linear": tuple(
        SensitivityAnchor(x, y)
        for x, y in zip(_SENSITIVITY_8POINT_PRESET_X, (0, 14, 28, 42, 57, 71, 85, 100))
    ),
    # Aggressive: convex / fast initial response (sits above the diagonal).
    "Aggressive": tuple(
        SensitivityAnchor(x, y)
        for x, y in zip(_SENSITIVITY_8POINT_PRESET_X, (0, 26, 46, 62, 75, 85, 93, 100))
    ),
    # Smooth: concave / gentle initial response (sits below the diagonal).
    "Smooth": tuple(
        SensitivityAnchor(x, y)
        for x, y in zip(_SENSITIVITY_8POINT_PRESET_X, (0, 7, 15, 25, 38, 54, 74, 100))
    ),
    # Balanced: mild S-curve (gentle start, stronger finish).
    "Balanced": tuple(
        SensitivityAnchor(x, y)
        for x, y in zip(_SENSITIVITY_8POINT_PRESET_X, (0, 10, 22, 38, 58, 74, 88, 100))
    ),
}

POLLING_RATE_LABEL_BY_ENUM = {v: k for k, v in POLLING_RATE_BY_LABEL.items()}
VIBRATION_MODE_LABEL_BY_ENUM = {v: k for k, v in VIBRATION_MODE_BY_LABEL.items()}
TRIGGER_MODE_LABEL_BY_ENUM = {v: k for k, v in TRIGGER_MODE_BY_LABEL.items()}
BUTTON_SLOT_LABEL_BY_ENUM = {v: k for k, v in BUTTON_SLOT_BY_LABEL.items()}
BUTTON_TARGET_LABEL_BY_ENUM = {v: k for k, v in BUTTON_TARGET_BY_LABEL.items()}
LIGHTING_ZONE_LABEL_BY_ENUM = {v: k for k, v in LIGHTING_ZONE_BY_LABEL.items()}
LIGHTING_MODE_LABEL_BY_ENUM = {v: k for k, v in LIGHTING_MODE_BY_LABEL.items()}

logger = logging.getLogger(__name__)


def _is_unc_path_value(path_value: str) -> bool:
    expanded = os.path.expanduser(os.path.expandvars(str(path_value))).strip()
    if not expanded:
        return False
    drive = PureWindowsPath(expanded).drive.replace("/", "\\").lower()
    return drive.startswith("\\\\") and (
        not drive.startswith("\\\\?\\") or drive.startswith("\\\\?\\unc\\")
    )


def _resolve_non_strict(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()

_DPG_SAFE_WINDOW_TITLE = "LegendCTL - ZD Ultimate Legend"
_TOP_STATUS_BAR_HEIGHT = 40
# Footer bar height bumped 48→56 (UI refresh) so the
# Profile combo + Save As / Apply / Delete / Read buttons + status text get
# vertical breathing room instead of feeling cramped against the borders.
_FOOTER_PROFILE_BAR_HEIGHT = 56
_DEFAULT_VIEWPORT_HEIGHT = 920
_MIN_MIDDLE_BAND_HEIGHT = 240
# Vertical space the primary window's own chrome consumes BELOW the three stacked
# rows (top bar + middle band + footer) — i.e. the ImGui inter-item spacing
# between them plus the window's top+bottom padding. _middle_band_height()
# subtracts this so top+middle+footer+chrome fits the viewport WITHOUT the
# primary "main_window" itself scrolling. Measured at 52px on the shipped DPG 2.3
# theme (tools/diag_dpg_main_window_scroll.py: with the prior 28, main_window
# reported y_scroll_max=24 at every window size — a persistent ~24px full-height
# far-right bar drawn by the PRIMARY WINDOW on every screen even though every
# inner container fit, which 8 rounds of per-screen content trims never touched
# because they measured the screen root, not the outer window). 56 = the 52px
# measured chrome + a 4px safety margin so the primary window content fits the
# viewport with no bar at every window size (content = client - 4). A
# no_scrollbar flag on main_window does NOT help — set_primary_window() resets
# the window's flags — so the reservation here IS the fix.
_MAIN_WINDOW_VERTICAL_GUTTER = 56
# Frames to wait after a geometry-log request before reading + emitting the
# snapshot (_log_geometry): rect sizes / y_scroll_max are only meaningful once a
# frame has rendered the new layout. Doubles as a resize-drag debounce — each
# resize event resets the countdown, so only the settled size is logged.
_GEOMETRY_LOG_SETTLE_FRAMES = 3

# Sidebar nav layout. SIDEBAR_WIDTH 200→208 gives EN "Readiness Check" + the
# zh-CN labels comfortable room; NAV_ACCENT_STRIP_WIDTH carves out the
# left-edge active indicator so "this is the active screen" reads from the
# strip + a subtle bg.raised lift, not from a heavy accent-green fill (UI
# refresh). Reserving accent-green for actionable signals is a
# guardrail from the roadmap.
SIDEBAR_WIDTH = 208
NAV_BUTTON_HEIGHT = 36
NAV_ACCENT_STRIP_WIDTH = 4
# Active-row background tint (wayfinding refinement). The active
# nav button gets a low-alpha accent wash instead of the neutral bg.raised lift
# the earlier design used: a hue shift (green) reads more clearly than a small lightness
# bump, especially on dim/low-contrast monitors where the strip + gray lift was
# flagged "too faint". Kept low-alpha so it stays a subtle wash over the
# sidebar's bg.surface — it reinforces the full-accent 4px strip, it is NOT a
# full accent fill (that heavy fill was retired in an earlier revision; accent-green stays
# reserved for active + actionable signals). One number to tune if later
# review wants it louder/quieter.
NAV_ACTIVE_ROW_TINT = (*COLORS["accent.primary"][:3], 56)

# Footer profile bar layout. Combo widened 200→220 to fit longer profile names
# (and CN "另存为..." profile names without truncation). Action-button widths
# stay differentiated by label length: Save As needs more room than Apply/Delete.
FOOTER_PROFILE_COMBO_WIDTH = 220
FOOTER_BUTTON_WIDTH_WIDE = 120
FOOTER_BUTTON_WIDTH = 100


def _format_result_failure(key: str, result, **kwargs) -> str:
    return t(key, reason=_settings_result_error_text(result), **kwargs)


def _make_result_log_entry(key: str, result, **kwargs) -> LogEntry:
    return _make_log_entry(key, reason=_settings_result_error_text(result), **kwargs)


def _side_label(side: str) -> str:
    return t(f"apply.side.{side}", fallback=side)


def _top_connection_mode_label(mode: str) -> str:
    normalized = (mode or "").strip().lower()
    key_by_mode = {
        "usb": "status.transport.usb",
        "bt": "status.transport.bt",
        "bluetooth": "status.transport.bt",
        "xinput": "status.transport.xinput",
        "unknown": "status.transport.unknown",
    }
    key = key_by_mode.get(normalized)
    return t(key) if key else mode


def _top_sync_status_label(status: str) -> str:
    key_by_status = {
        "Disconnected": "status.state.disconnected",
        "Connected": "status.state.connected",
        "Reading": "status.state.reading",
        "Ready": "status.state.ready",
        "Unsaved Changes": "status.state.unsaved_changes",
        "Applying": "status.state.applying",
        "Apply Failed": "status.state.apply_failed",
    }
    key = key_by_status.get(status)
    return t(key) if key else status


def _apply_banner_message(message: str, success: bool) -> str:
    known_prefixes = (
        "OK:",
        "OK：",
        "Partial:",
        "部分完成：",
        "Failed:",
        "失败：",
        "Apply failed:",
        "应用失败：",
        "Read failed:",
        "读取失败：",
        "Save failed:",
        "保存失败：",
        "Delete failed:",
        "删除失败：",
    )
    if message.startswith(known_prefixes):
        return message
    if success:
        return t("apply.status.success_prefix", message=message)
    return t("apply.status.failure_prefix", message=message)


def _make_log_entry(key: str, **fmt_args) -> LogEntry:
    return LogEntry(
        timestamp=time.strftime("%H:%M:%S"),
        key=key,
        fmt_args=dict(fmt_args),
    )


def _deadzone_verify_status_key(readback, written: "StickDeadzones") -> str:
    """Map a deadzone read-back result to a diagnostics status key (item J).

    ``readback`` is the value ``get_deadzones()`` returned — or, when that read
    was jobbed and raised, the ``BaseException`` itself (``_run_hid_job``
    delivers a raised job's exception as the result). A matching value is
    ``verified``; a differing value is ``mismatch``; anything else (a timeout
    or other read failure) is ``sent_unverified`` — the write already
    succeeded, so a failed confirm read is advisory, not a mismatch/failure.
    """

    if isinstance(readback, StickDeadzones):
        return "verified" if readback == written else "mismatch"
    return "sent_unverified"


def _translate_or_raw(key: str, raw: str) -> str:
    translated = t(key)
    return raw if translated == f"[{key}]" else translated


def _format_apply_failure_row(failure: "ApplyFailure") -> str:
    label_key = APPLY_FAILURE_ROW_LABEL_KEYS.get(failure.setting_label)
    label = (
        _translate_or_raw(label_key, failure.setting_label)
        if label_key is not None
        else failure.setting_label
    )
    scope_key = (
        "apply.failure.scope.transient"
        if failure.is_transient
        else "apply.failure.scope.device"
    )
    return t(
        "apply.failure.row_format",
        label=label,
        error=_translate_or_raw(failure.error, failure.error),
        scope=t(scope_key),
    )


def _build_default_health_report_service(
    *,
    settings_service: "SettingsService | None",
    device_service: DeviceService,
    wrapper_profile_store: WrapperProfileStore,
    wear_ledger: WearLedgerService | None = None,
    hid_busy: Callable[[], bool] | None = None,
) -> HealthReportService:
    """Create a HealthReportService wired against the live app services.

    The sample provider lazy-loads XInput via the shared loader (the same
    fallback chain XInputPollService uses); on systems without DirectX it
    returns a stub provider that yields no samples — the Health Report just
    can't run, but the wrapper doesn't crash.

    The device-context provider reads the controller name, configured
    polling rate, and active profile name at the moment the report begins,
    so a profile / polling-rate change during the run doesn't disturb the
    captured snapshot. The polling-rate lookup is one real HID read — when
    ``hid_busy`` reports a threaded HID job in flight it is skipped
    (``configured_polling_hz=None``, the same cosmetic degradation the
    late-connect path already accepts) rather than interleaved with the
    worker's frames.
    """

    sample_provider = make_xinput_sample_provider()

    def _device_context() -> DeviceContext:
        controller_name: str | None = None
        configured_hz: int | None = None
        profile_name: str | None = None
        try:
            state = device_service.state
            controller_name = state.product_name or None
        except Exception:  # noqa: BLE001
            logger.debug("Health Report device-context lookup failed", exc_info=True)
        if settings_service is not None and hid_busy is not None and hid_busy():
            logger.debug(
                "Health Report polling-rate lookup skipped: HID job in flight"
            )
        elif settings_service is not None:
            try:
                rate = settings_service.get_polling_rate()
                if rate is not None:
                    configured_hz = POLLING_RATE_HZ.get(rate)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Health Report polling-rate lookup failed", exc_info=True
                )
        try:
            for profile in wrapper_profile_store.list_profiles()[0]:
                # Best-effort: name the most-recently-applied profile if the
                # store tracks one; otherwise leave None.
                profile_name = getattr(profile, "name", None)
                if profile_name:
                    break
        except Exception:  # noqa: BLE001
            logger.debug(
                "Health Report wrapper-profile lookup failed", exc_info=True
            )
        return DeviceContext(
            controller_name=controller_name,
            configured_polling_hz=configured_hz,
            profile_name=profile_name,
        )

    return HealthReportService(
        sample_provider=sample_provider,
        device_context_provider=_device_context,
        app_version=__version__,
        app_build_commit=__build_commit__ or None,
        wear_ledger=wear_ledger,
    )


def _build_default_quick_check_service(
    *,
    settings_service: "SettingsService | None",
    device_service: DeviceService,
    wear_ledger: WearLedgerService | None = None,
    hid_busy: Callable[[], bool] | None = None,
) -> QuickCheckService:
    """Create a QuickCheckService wired against the live app services.

    Shares the same XInput sample provider as the full Health Report; only
    ``configured_polling_hz`` from the device context is consumed by the
    quick-mode cadence band, so this builder is lighter than the full-mode
    one (no profile-name lookup). The polling-rate lookup is skipped while
    ``hid_busy`` reports a threaded HID job in flight (see the Health
    Report builder).
    """

    sample_provider = make_xinput_sample_provider()

    def _device_context() -> DeviceContext:
        controller_name: str | None = None
        configured_hz: int | None = None
        try:
            state = device_service.state
            controller_name = state.product_name or None
        except Exception:  # noqa: BLE001
            logger.debug("Readiness Check device-context lookup failed", exc_info=True)
        if settings_service is not None and hid_busy is not None and hid_busy():
            logger.debug(
                "Readiness Check polling-rate lookup skipped: HID job in flight"
            )
        elif settings_service is not None:
            try:
                rate = settings_service.get_polling_rate()
                if rate is not None:
                    configured_hz = POLLING_RATE_HZ.get(rate)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Readiness Check polling-rate lookup failed", exc_info=True
                )
        return DeviceContext(
            controller_name=controller_name,
            configured_polling_hz=configured_hz,
            profile_name=None,
        )

    return QuickCheckService(
        sample_provider=sample_provider,
        device_context_provider=_device_context,
        wear_ledger=wear_ledger,
    )


def _build_default_module_passport_service(
    *,
    wear_ledger: WearLedgerService | None = None,
) -> ModulePassportService:
    """Wire a ModulePassportService against ``<user_data_dir>/module_passport/``.

    The service is UI-free; the Modules screen reads it back through
    :attr:`AppShell.module_passport_service`. Tests can inject a tempdir-
    backed instance by passing ``module_passport_service=...`` to the
    AppShell constructor.
    """

    from zd_app.storage.settings_store import initialize_user_data_dir

    return ModulePassportService(
        base_dir=initialize_user_data_dir() / "module_passport",
        wear_ledger=wear_ledger,
    )


def _build_default_diagnostic_bundle_service(
    *,
    module_passport_service: ModulePassportService | None,
    wear_ledger: WearLedgerService | None,
) -> DiagnosticBundleService:
    """Wire a DiagnosticBundleService against ``<user_data_dir>/diagnostic_bundles/``.

    The service is UI-free; the Modules screen invokes it from the
    Export modal. The module-passport service + wear ledger flow through
    the bundle so the rendered Markdown can summarise both. The
    Health-Report directory is the same one the Health Report screen
    writes its exports to.
    """

    from zd_app.storage.settings_store import initialize_user_data_dir

    user_data_dir = initialize_user_data_dir()
    return DiagnosticBundleService(
        base_dir=user_data_dir / "diagnostic_bundles",
        module_passport_service=module_passport_service,
        health_report_dir=user_data_dir / "health_reports",
        wear_ledger=wear_ledger,
        app_data_dir=user_data_dir,
    )


def _build_default_restore_point_service(
    *,
    settings_service: "SettingsService",
    settings_apply_coordinator: "SettingsApplyCoordinator",
    wear_ledger: WearLedgerService | None = None,
) -> RestorePointService:
    """Wire a RestorePointService against the live SettingsService + the
    shell-owned SettingsApplyCoordinator.

    The store defaults to ``<user_data_dir>/restore_points/`` (a sibling of
    ``wrapper_profiles/`` / ``diagnostics/``). Callers (the apply-pipeline
    hooks below + the manual button + the Restore Points screen) all use the
    same instance so retention pruning and id resolution stay consistent.
    """

    return RestorePointService(
        store=RestorePointStore(),
        settings_service=settings_service,
        apply_coordinator=settings_apply_coordinator,
        app_version=__version__,
        app_build_commit=__build_commit__ or None,
        wear_ledger=wear_ledger,
    )


class AppShell:
    NAV_ITEMS = (
        "home",
        "controller",
        "diagnostics",
        "live_verify",
        "readiness_check",
        "restore_points",
        "device_vs_profile",
        "health_report",
        "wear_ledger",
        "modules",
        "settings",
        "about",
    )
    LEGACY_NAV_ITEMS = ("legacy_buttons", "legacy_sticks", "legacy_triggers")
    SCREEN_ALIASES = {
        "Home": "home",
        "Controller": "controller",
        "Diagnostics": "diagnostics",
        "Live Verify": "live_verify",
        "Readiness Check": "readiness_check",
        "Health Check": "health_report",
        "Health Report": "health_report",
        "Restore Points": "restore_points",
        "Device vs Profile": "device_vs_profile",
        "Wear Ledger": "wear_ledger",
        "Modules": "modules",
        "Settings": "settings",
        "Preferences": "settings",
        "About": "about",
        "Buttons": "legacy_buttons",
        "Sticks": "legacy_sticks",
        "Triggers": "legacy_triggers",
    }

    COLORS = LEGACY_COLOR_ALIASES

    SCREEN_BUILDERS = {
        "home": home.build,
        "controller": controller.build,
        "diagnostics": diagnostics.build,
        "live_verify": live_verify.build,
        "readiness_check": readiness_check.build,
        "restore_points": restore_points.build,
        "device_vs_profile": device_vs_profile.build,
        "health_report": health_report.build,
        "wear_ledger": wear_ledger.build,
        "modules": modules_screen.build,
        "settings": preferences.build,
        "about": about.build,
        "legacy_buttons": buttons.build,
        "legacy_sticks": sticks.build,
        "legacy_triggers": triggers.build,
    }

    def __init__(
        self,
        device_service: DeviceService,
        profile_service: ProfileService,
        diagnostics_service: DiagnosticsService,
        settings_store: SettingsStore,
        preflight_service: PreflightService | None = None,
        settings_service: SettingsService | None = None,
        wrapper_profile_store: WrapperProfileStore | None = None,
        health_report_service: "HealthReportService | None" = None,
        quick_check_service: "QuickCheckService | None" = None,
        restore_point_service: "RestorePointService | None" = None,
        wear_ledger_service: WearLedgerService | None = None,
        module_passport_service: ModulePassportService | None = None,
        diagnostic_bundle_service: DiagnosticBundleService | None = None,
        hid_executor: Callable[..., None] | None = None,
        last_applied_store: LastAppliedStore | None = None,
        xinput_poll_service: "XInputPollService | None" = None,
    ):
        self.device_service = device_service
        self.profile_service = profile_service
        self.diagnostics_service = diagnostics_service
        self.settings_store = settings_store
        self.preflight_service = preflight_service or PreflightService()
        self.settings_service = settings_service
        self._apply_coordinator = SettingsApplyCoordinator(settings_service)
        # Wear ledger — optional; when None the per-event ledger.append calls
        # below become no-ops. main_zd.py wires a real one; tests can pass an
        # in-memory or temp-dir-backed service to assert event recording.
        self.wear_ledger_service = wear_ledger_service
        self._session_start_emitted = False
        self._session_end_emitted = False
        self.wrapper_profile_store = wrapper_profile_store or WrapperProfileStore()
        self._wrapper_profile_skipped_paths: list[object] = []
        # Last-Applied record store (Device-vs-Profile Phase 2). Deliberately
        # NOT default-constructed: the apply pipeline best-effort WRITES through
        # it, so a default here would make every test apply (and any embedding
        # caller) write into the real user-data dir. main_zd.py wires the real
        # store; None keeps the recording hooks no-ops.
        self.last_applied_store = last_applied_store
        self.settings = settings_store.load()
        set_locale(self.settings.language)
        self._locale_router = LocaleRouter()
        self._locale_router.subscribe(self._on_locale_changed)
        self.current_screen = "home"
        self.selected_button_input: str | None = None
        self.selected_local_profile_id: str | None = None
        self.selected_stick_side = "left"
        self.link_triggers = False
        self.last_controller_snapshot: ControllerSnapshot | None = None
        self._last_back_paddle_bindings: dict[MacroSlot, BackPaddleBinding] = {}
        # ``_last_back_paddle_bindings`` is touched from BOTH threads. Readers:
        # the HID worker-thread read job and the render thread, both via
        # _with_last_back_paddle_bindings. Writers (``_remember_back_paddle_binding``'s
        # in-place ``[slot] =`` write) fire from BOTH threads too: on the render
        # thread for a direct Save-As / combo back-paddle set, AND on the HID
        # worker thread during a profile apply, where the apply-coordinator's
        # ``on_back_paddle_apply`` callback is wired to _remember_back_paddle_binding
        # (see _apply_snapshot_to_controller). The Save-As button isn't HID-flow-
        # gated, so a back-paddle write can race a background read; this lock
        # serializes the cache reads/writes so a concurrent mutation can't tear an
        # iteration.
        self._last_back_paddle_bindings_lock = threading.Lock()
        self.last_snapshot_ts: float | None = None
        self.last_snapshot_status = ""
        self._apply_status_text: str | None = None
        self._apply_status_clear_after: float | None = None
        self._last_apply_result: ApplyResult | None = None
        self._needs_hydration = False
        # Reconnect-while-busy deferral: _tick's reconnect branch must not
        # stop() the settings service while a worker job holds the cached
        # HID handles — it flags this instead, and
        # _tick_settings_service_tasks consumes it once the job drains
        # (mirrors the _needs_hydration deferral).
        self._needs_service_restart = False
        # Live-write widgets (step-size slider, polling-rate combo) must not
        # present a writable value until a real controller read hydrates them.
        # These flags gate the live-write callbacks against a read-miss clobber.
        self._step_size_hydrated = False
        self._polling_rate_hydrated = False
        # Fix B (2026-06-24): one-click persist of a manually-changed step_size
        # into the active wrapper profile. ``_active_wrapper_profile_name`` is the
        # last named wrapper profile APPLIED (the "loaded" profile, the one whose
        # stored step_size an apply would overwrite); ``_pending_step_size_save``
        # is the (profile_name, value) the save-to-profile nudge currently offers,
        # or None when nothing is offered. The nudge appears only after a live
        # step_size write whose committed value DIFFERS from that profile's stored
        # step_size — never on every change, never when no profile is loaded.
        self._active_wrapper_profile_name: str | None = None
        self._pending_step_size_save: tuple[str, int] | None = None
        # App-scoped XInput poll worker for the live Diagnostics panel.
        # Lazy: constructed on first access (the live-verify panel's build)
        # so headless / non-panel sessions never load the XInput DLL or spawn
        # a worker. Stopped in the render-loop teardown below.
        self._xinput_poll_service = xinput_poll_service
        # Diagnostics live-verify inline deadzone control. Like the step-size
        # slider, its callback must not write the firmware StickDeadzones
        # until a real get_deadzones() read hydrates the panel sliders, or it
        # would clobber the controller with a default. The status key drives
        # the panel's "sending / verified" line — the panel reads it each
        # frame so the shell never reaches into a screen widget directly.
        self._diag_deadzone_hydrated = False
        self._diag_deadzone_status_key = "idle"
        # The last leading-edge (verify=False) deadzone value that still needs a
        # read-back verify. A single-callback change does the leading
        # write but creates no trailing pending, so the trailing flush never
        # verifies it; _flush_slider_throttle arms this value's verify once the
        # throttle quiet window elapses with no superseding write, so the inline
        # status always resolves instead of stranding at "sending". Cleared when
        # a multi-callback trailing write (verify=True) supersedes it.
        self._deadzone_pending_verify: StickDeadzones | None = None
        self._dpg_context_ready = False
        self._last_tick = 0.0
        self._last_presence_poll = 0.0
        self._last_connection_state: str | None = None
        self._stick_preview_deadline = 0.0
        self._stick_preview_backup: tuple[StickSettings, StickSettings] | None = None
        self._active_nav_theme: int | None = None
        self._inactive_nav_theme: int | None = None
        # Strip themes for the left-edge accent indicator that replaces the
        # heavy accent-green fill on the active nav item. Built alongside the
        # button themes in :meth:`_setup_theme`; render paths that skip theme
        # setup leave these None and the strip just renders in DPG's default
        # ChildBg color (still visible — strip width/position is unaffected).
        self._active_nav_strip_theme: int | None = None
        self._inactive_nav_strip_theme: int | None = None
        # Series themes for the 8-point sensitivity curve plot (built once in
        # _setup_theme, bound per series by the controller screen). None until
        # theme setup runs — render paths that skip it just draw default colors.
        self._sensitivity_plot_themes: dict[str, int] | None = None
        self._save_as_modal_open = False
        # Safe Import preview state. _safe_import_result holds the classified
        # ImportResult across the multi-step preview; _safe_import_selected is
        # the set of RiskCategory the user has checked (Device unchecked by
        # default by design). Both reset on each new scan.
        self._safe_import_result = None
        self._safe_import_selected: set[RiskCategory] = set()
        self._dpg_viewport_title_seeded = False
        self._footer_profile_combo_ready = False
        self._title_manager = TitleManager(
            apply_fn=lambda title: set_window_title_unicode(title),
        )
        self.controller_active_tab = "vibration"
        # Health Report orchestrator. main_zd.py wires a real one using
        # XInput + the live SettingsService; tests can pass an explicit
        # ``health_report_service`` (with a scripted sample provider) or
        # leave it None to let _build_default_health_report_service create
        # an inert one. The screen reads ``shell.health_report_service``.
        self.health_report_service = (
            health_report_service
            if health_report_service is not None
            else _build_default_health_report_service(
                settings_service=self.settings_service,
                device_service=self.device_service,
                wrapper_profile_store=self.wrapper_profile_store,
                wear_ledger=self.wear_ledger_service,
                # getattr: the seam state is initialized later in __init__;
                # the closure only fires at report-run time.
                hid_busy=lambda: getattr(self, "_hid_job_in_flight", False),
            )
        )
        # Quick-mode Readiness Check service. Independent of HealthReportService
        # so the two screens can be open in parallel and operate on their own
        # SampleCollector instances. The screen reads ``shell.quick_check_service``.
        self.quick_check_service = (
            quick_check_service
            if quick_check_service is not None
            else _build_default_quick_check_service(
                settings_service=self.settings_service,
                device_service=self.device_service,
                wear_ledger=self.wear_ledger_service,
                hid_busy=lambda: getattr(self, "_hid_job_in_flight", False),
            )
        )
        # Restore Points service. Requires a settings_service for fresh-read
        # capture + apply_coordinator for restore-pipeline writes; tests
        # that construct an AppShell without a SettingsService get a None
        # service and the screen + hooks become no-ops.
        if restore_point_service is not None:
            self.restore_point_service = restore_point_service
        elif self.settings_service is not None:
            self.restore_point_service = _build_default_restore_point_service(
                settings_service=self.settings_service,
                settings_apply_coordinator=self._apply_coordinator,
                wear_ledger=self.wear_ledger_service,
            )
        else:
            self.restore_point_service = None
        # Restore-Points screen view state. Lazily replaced by the screen
        # module's RestorePointsScreenState on first build.
        self.restore_points_screen_state = None
        # Module Passport service. Per-side passports + characterization
        # history live under <user_data_dir>/module_passport/. Tests can
        # inject a tempdir-backed instance via ``module_passport_service``.
        self.module_passport_service = (
            module_passport_service
            if module_passport_service is not None
            else _build_default_module_passport_service(
                wear_ledger=self.wear_ledger_service,
            )
        )
        # Diagnostic Bundle service. Aggregates module-passport + health-
        # report + wear-ledger records into a shareable Markdown / ZIP. The
        # Modules screen invokes it from the Export modal; main_zd.py wires
        # a real one with the live wear ledger so events flow through to
        # the chronological log.
        self.diagnostic_bundle_service = (
            diagnostic_bundle_service
            if diagnostic_bundle_service is not None
            else _build_default_diagnostic_bundle_service(
                module_passport_service=self.module_passport_service,
                wear_ledger=self.wear_ledger_service,
            )
        )
        # Modules screen view state. Lazily replaced by the screen module's
        # ModulesScreenState on first build.
        self.modules_screen_state = None
        # Debounce state for the before_manual_device_setting_write trigger.
        # Map: setting-field-key -> monotonic timestamp of the last RP we
        # captured for that field. A new RP only fires if the elapsed time
        # exceeds MANUAL_DEVICE_WRITE_RP_WINDOW_S — keeps slider drags from
        # creating one RP per tick (the "Trigger model" design + spec RPU4).
        self._last_manual_rp_for_field: dict[str, float] = {}
        # Inner drag-storm throttle for slider live-writes. The outer
        # debounce above governs RP capture (7 s); this inner throttle
        # governs the HID write itself (~150 ms). Drag-storm debounce
        # work, 2026-05-24.
        self._slider_throttle = _SliderWriteThrottle(
            window_s=SLIDER_LIVE_WRITE_THROTTLE_S
        )
        # Per-session set of device product_strings we've already captured a
        # first_readable_connect RP for. The "Trigger model" design simplifies
        # v1 to "capture once per identity per app session" — reconnects of
        # the same controller within the session do NOT re-fire.
        self._first_connect_captured: set[str] = set()
        # HID-job executor seam (worker-thread phase 1). None (default) runs
        # jobs synchronously inline — byte-for-byte the pre-seam behavior the
        # existing suite exercises. main_zd.py passes threaded_hid_executor so
        # production moves the three long HID flows (footer Read, profile
        # apply, RP restore) off the render thread.
        self._hid_executor = hid_executor
        # True from threaded-job start until its completion drains in _tick.
        # Set and cleared on the render thread only; sync mode never sets it.
        self._hid_job_in_flight = False
        # (on_done, result_or_exception, completes_job) triples pushed by
        # worker threads, drained by _tick on the render thread.
        # completes_job=False marks completions of nested jobs (the post-apply
        # read inside a profile apply) whose enclosing job is still running —
        # only the outermost completion clears the busy flag.
        self._hid_job_completions: queue.Queue = queue.Queue()
        # Thread-local marker letting a jobbed flow invoked from inside a
        # running job (apply's post-apply read) execute inline on the worker
        # while its DPG on_done still drains on the render thread.
        self._hid_job_context = threading.local()
        # Busy-staleness surfacing: monotonic start of the
        # in-flight threaded job, plus once-per-job warn / once-per-second
        # status-refresh latches for _tick_hid_job_staleness.
        self._hid_job_started_monotonic: float | None = None
        self._hid_job_stall_warned = False
        self._hid_job_stall_last_seconds = -1
        # Deferred-UI / modal-swap seam.
        # DPG's empirically-benched modal law (thread-INDEPENDENT; matrix
        # in tools/diag_dpg_modal_thread_visibility.py): a modal window
        # created in the same pass that another modal was showing —
        # deleted, hidden, or simply left up — exists (children and all)
        # but never becomes visible; no exception, no DPG error anywhere.
        # One rendered frame between the teardown and the create makes it
        # render, and two modals can never show stacked. Modal-opening
        # callbacks queue through _defer_ui_call / _defer_modal_swap and
        # _tick drains ONE pass per frame, giving swap halves their frame
        # gap. Armed only while run()'s render loop is live; unarmed
        # (tests, sync/headless paths) calls execute inline on the
        # caller's thread.
        self._deferred_ui_calls: queue.Queue = queue.Queue()
        self._defer_ui_armed = False
        # Deferred geometry logging (permanent diagnostic — _log_geometry). A
        # one-slot request that _tick drains a few frames later, once layout has
        # settled: rect sizes / y_scroll_max are only meaningful AFTER a frame
        # renders. Repeated requests reset the countdown, so a continuous resize
        # drag debounces to a single log emitted when the drag stops.
        self._geometry_log_pending = None
        self._geometry_log_countdown = 0
        # In-flight _defer_modal_swap coalescing keys: a re-fired callback
        # whose swap is still pending is dropped instead of double-queued
        # (a second create of the same modal in one pass is the poison).
        self._pending_modal_swap_keys: set[str] = set()

    @property
    def button_actions(self) -> list[str]:
        return BUTTON_ACTIONS

    def _record_wear_event(
        self,
        event_type: str,
        *,
        summary: str,
        details: dict | None = None,
    ) -> None:
        """Append a wear-ledger event if a service is wired; otherwise no-op.

        Wraps the underlying service's ``append`` call so callers don't have
        to repeat the ``None`` check at every hook. The wear ledger service
        itself never raises from append, so this helper doesn't need a
        try/except — failures already log inside the service.
        """

        ledger = self.wear_ledger_service
        if ledger is None:
            return
        ledger.append(event_type, summary=summary, details=details)

    def run(self) -> None:
        self.device_service.refresh_state(background=False)
        self._last_connection_state = self.device_service.state.connection_state
        dpg.create_context()
        self._dpg_context_ready = True
        trust_ritual_widget.drain_pending_trust_ritual(self)
        install_dearpygui_i18n(dpg)
        register_fonts()
        self._setup_theme()
        bind_default_font(self.settings.language)
        self._build_ui()
        if self.settings_service is not None:
            self.refresh_from_controller()
        title = self.window_title()
        dpg.create_viewport(title=_DPG_SAFE_WINDOW_TITLE, width=1480, height=1040, min_width=1180, min_height=760)
        self._dpg_viewport_title_seeded = True
        self._bind_viewport_resize_callback()
        dpg.setup_dearpygui()
        dpg.show_viewport()
        # Open maximized so the app uses the full screen by default: most panels
        # then fit with no far-right page scrollbar, and the scroll appears only
        # when genuinely needed (a small/un-maximized window, or a truly tall
        # screen like Home on a small monitor). The 1480x1040 create_viewport size
        # is kept as the restore size: un-maximizing yields a window whose
        # un-maximized content clip (~876px) clears the trimmed Home dashboard
        # (~816px content at the shipped fonts, DPG 2.3), so dragging off
        # maximized does NOT bring back the far-right page scrollbar (the prior
        # 920 restore clipped Home at ~756px). 1040 <= 1080, so the restore size
        # still fits common smaller monitors. maximize_viewport is
        # screen-adaptive — it never exceeds the display — so it is safe on small
        # monitors too. The screen roots are autosize_x, so cards/tables reflow to
        # the wider width without breaking.
        dpg.maximize_viewport()
        # Record the real maximized geometry once the first frames settle
        # (permanent diagnostic — lets a post-smoke log read recover the
        # operator's true maximized client height + Home's real y_scroll_max).
        self._request_geometry_log("startup")
        self._set_window_title(title)
        # First-run acknowledgment gate (clickwrap): the one-time legal accept
        # the user must give before using the app. On a fresh install it is the
        # only modal that can show — prior-run crash reports cannot exist yet —
        # so when the gate shows we skip crash review this launch: two modal=True
        # windows never render stacked (DPG modal law, docs/ARCHITECTURE.md). The
        # trust card is modal=False and does not occupy the modal slot, so it is
        # unaffected. Crash review, if any, surfaces on the next launch (by then
        # the flag is set and the gate is a no-op).
        if not self._show_first_run_acknowledgment_modal_if_needed():
            self._show_crash_review_modal_if_any()

        self._emit_session_start_event()

        # Keep the (pnputil) presence cache warm on a background thread so the
        # per-frame tick's refresh_state(allow_probe=False) never blocks the
        # render loop on device enumeration.
        self.device_service.start_presence_primer()

        # Arm the deferred-UI seam only for the lifetime of the render
        # loop: once frames are rendering, modal swaps must be paced one
        # pass per frame through _tick's drain. Before the loop (and in
        # headless paths) there is no showing modal to teardown-race, so
        # _defer_ui_call's unarmed inline path is already correct.
        self._defer_ui_armed = True
        while dpg.is_dearpygui_running():
            self._tick()
            dpg.render_dearpygui_frame()
        self._defer_ui_armed = False

        self.device_service.stop_presence_primer()
        # Stop the live-diagnostics XInput poll worker if it was ever started
        # (lazy — only the live-verify panel constructs it). Idempotent.
        if self._xinput_poll_service is not None:
            self._xinput_poll_service.stop()
        self._emit_session_end_event()
        dpg.destroy_context()
        self._dpg_context_ready = False

    def _emit_session_start_event(self) -> None:
        if self._session_start_emitted:
            return
        self._session_start_emitted = True
        self._record_wear_event(
            SESSION_START,
            summary=f"Wrapper session started (v{__version__})",
            details={
                "app_version": __version__,
                "app_build_commit": __build_commit__ or None,
                "language": self.settings.language,
            },
        )

    def _emit_session_end_event(self) -> None:
        if self._session_end_emitted:
            return
        self._session_end_emitted = True
        self._record_wear_event(
            SESSION_END,
            summary="Wrapper session ended",
            details={
                "app_version": __version__,
                "app_build_commit": __build_commit__ or None,
            },
        )

    def _setup_theme(self) -> None:
        register_global_theme()
        # Build the reusable card-surface theme once so components.card() can
        # bind it (it no-ops the bind when this never ran — e.g. headless tests).
        register_card_theme()
        # Same register-once contract for the shared data-table chrome and the
        # destructive-action (red Delete) button theme — components.table() /
        # components.action_button() bind them, no-op when unregistered.
        register_table_theme()
        register_destructive_theme()
        (
            self._active_nav_theme,
            self._inactive_nav_theme,
            self._active_nav_strip_theme,
            self._inactive_nav_strip_theme,
        ) = _build_nav_themes()
        try:
            self._sensitivity_plot_themes = _build_sensitivity_plot_themes()
        except Exception:
            # Plot-series theming is cosmetic; never let a theme-constant
            # surprise block startup — the plot still renders in default colors.
            logger.debug("Sensitivity plot themes unavailable", exc_info=True)
            self._sensitivity_plot_themes = None

    def _build_ui(self) -> None:
        self._dpg_context_ready = True
        self._build_main_window()
        self.rebuild_current_screen()
        self.refresh_shell()

    def _build_main_window(self) -> None:
        # The primary window is a fixed top-bar / middle-band / footer scaffold;
        # all real scrolling happens INSIDE the screen root. It must NOT scroll
        # itself — otherwise its own chrome (inter-row spacing + window padding,
        # ~52px) overflows the viewport and draws a persistent ~24px full-height
        # far-right scrollbar on every screen even when content fits (the
        # multi-round phantom bar; measured main_window y_scroll_max=24 with the
        # old 28px gutter on DPG 2.3). A no_scrollbar flag here does NOT stick —
        # set_primary_window() resets the window's flags — so the fix lives in
        # _MAIN_WINDOW_VERTICAL_GUTTER, which reserves the full chrome in
        # _middle_band_height() so the three rows fit with margin and the primary
        # window has nothing to scroll. Benched in tools/diag_dpg_main_window_scroll.py.
        with dpg.window(
            tag="main_window",
            label=_DPG_SAFE_WINDOW_TITLE,
            no_resize=False,
            no_close=True,
        ):
            dpg.set_primary_window("main_window", True)
            self._build_top_bar()
            with dpg.child_window(
                tag="middle_band",
                height=self._middle_band_height(),
                border=False,
                no_scrollbar=True,
            ):
                with dpg.group(horizontal=True, tag="below_status_bar"):
                    self._build_sidebar()
                    # ONE content surface. Each screen's root (mounted into
                    # content_region by rebuild_current_screen) is itself an
                    # autosize_y child_window: it grows to its content and, when
                    # that content overflows the viewport, clamps to this frame's
                    # height and scrolls INTERNALLY — so the screen root is the
                    # real scroll surface (one bar, on the far-right border, only
                    # on genuine overflow).
                    #
                    # This frame is therefore a fixed-height (height=-1) clip
                    # region with no_scrollbar: it never shows a bar of its own.
                    # The previous design nested a SECOND autosize_y layer
                    # (content_region) inside a scrollable outer (content_area);
                    # that redundant autosize layer measured a hair past the
                    # outer's inner height (accumulated theme padding), so the
                    # outer surfaced a permanent ~1-2px scrollbar on every screen
                    # even when content fit. Collapsing to this single
                    # non-scrolling frame removes that redundant outer bar without
                    # touching the screen root's real scroll. Verified on short
                    # (About) and tall (Diagnostics) screens via
                    # tools/diag_dpg_content_scrollbar.py.
                    with dpg.child_window(
                        tag="content_region",
                        height=-1,
                        border=False,
                        no_scrollbar=True,
                    ):
                        pass
            self._build_footer()

    def _build_top_bar(self) -> None:
        with dpg.child_window(tag="top_status_bar", height=_TOP_STATUS_BAR_HEIGHT, border=False, no_scrollbar=True):
            with dpg.group(horizontal=True):
                profile_not_verified = _top_profile_not_verified(self.device_service.state)
                dpg.add_text("●", tag="top_connection_dot", color=COLORS["text.muted"])
                dpg.add_text(tag="top_product_name", default_value=t("status.app_name"))
                dpg.add_text("|", color=self.COLORS["muted"])
                dpg.add_text(
                    tag="top_connection_mode",
                    default_value=_top_connection_mode_label(self.device_service.state.connection_mode),
                )
                dpg.add_text("|", color=self.COLORS["muted"])
                dpg.add_text(tag="top_polling_rate", default_value=t("shell.polling_rate.unknown"))
                dpg.add_text("|", color=self.COLORS["muted"])
                dpg.add_text(tag="top_active_profile", default_value=_top_profile_label(self.device_service.state))
                if dpg.does_item_exist("top_active_profile"):
                    with dpg.tooltip("top_active_profile", tag="top_active_profile_tooltip", show=profile_not_verified):
                        dpg.add_text(
                            t("status.config.not_verified_tooltip"),
                            wrap=360,
                            tag="top_active_profile_tooltip_text",
                        )
                dpg.add_text(tag="top_profile", default_value=_top_profile_label(self.device_service.state), show=False)
                dpg.add_text("|", color=self.COLORS["muted"])
                dpg.add_text(tag="top_sync_status", default_value=t("status.disconnected"), color=self.COLORS["warn"])
                dpg.add_spacer(width=20)
                dpg.add_text(__version__, tag="top_version", color=self.COLORS["muted"])

    def _build_sidebar(self) -> None:
        with dpg.child_window(tag="sidebar", width=SIDEBAR_WIDTH, border=False, no_scrollbar=True):
            dpg.add_text(t("nav.title"), color=self.COLORS["muted"])
            dpg.add_spacer(height=SPACE_SM)
            for screen_id in self._visible_nav_items():
                self._add_nav_button(screen_id)

    def _add_nav_button(self, screen_id: str) -> None:
        label = t(f"nav.{screen_id}")
        is_active = self._canonical_screen_id(self.current_screen) == screen_id
        btn_tag = f"nav_{screen_id}"
        strip_tag = f"nav_strip_{screen_id}"
        # Horizontal row: a 4px child_window acts as the left-edge accent
        # strip (colored via theme when active, invisible-matched-to-sidebar
        # when inactive); the button fills the remaining width. The default
        # ~8px item spacing between strip and button reads as deliberate
        # indentation, mirroring the previous "  label" prefix.
        with dpg.group(horizontal=True, tag=f"nav_row_{screen_id}"):
            dpg.add_child_window(
                tag=strip_tag,
                width=NAV_ACCENT_STRIP_WIDTH,
                height=NAV_BUTTON_HEIGHT,
                border=False,
                no_scrollbar=True,
            )
            dpg.add_button(
                label=label,
                tag=btn_tag,
                width=-1,
                height=NAV_BUTTON_HEIGHT,
                user_data=screen_id,
                callback=self._switch_screen_callback,
            )
        strip_theme = (
            self._active_nav_strip_theme if is_active else self._inactive_nav_strip_theme
        )
        if strip_theme is not None:
            dpg.bind_item_theme(strip_tag, strip_theme)
        btn_theme = self._active_nav_theme if is_active else self._inactive_nav_theme
        if btn_theme is not None:
            dpg.bind_item_theme(btn_tag, btn_theme)

    def _refresh_nav_selection(self) -> None:
        if not self._dpg_context_ready:
            return
        active = self._canonical_screen_id(self.current_screen)
        try:
            for screen_id in self._visible_nav_items():
                btn_tag = f"nav_{screen_id}"
                strip_tag = f"nav_strip_{screen_id}"
                is_active = screen_id == active
                btn_theme = (
                    self._active_nav_theme if is_active else self._inactive_nav_theme
                )
                strip_theme = (
                    self._active_nav_strip_theme
                    if is_active
                    else self._inactive_nav_strip_theme
                )
                if dpg.does_item_exist(btn_tag) and btn_theme is not None:
                    dpg.bind_item_theme(btn_tag, btn_theme)
                if dpg.does_item_exist(strip_tag) and strip_theme is not None:
                    dpg.bind_item_theme(strip_tag, strip_theme)
        except Exception:
            logger.debug("Nav selection refresh skipped", exc_info=True)

    def _build_footer(self) -> None:
        with dpg.child_window(tag="footer_profile_bar", height=_FOOTER_PROFILE_BAR_HEIGHT, border=True, no_scrollbar=True):
            with dpg.group(horizontal=True):
                dpg.add_text(t("footer.profile_label"), color=self.COLORS["muted"])
                profiles = [profile.name for profile in self.list_wrapper_profiles()]
                dpg.add_combo(
                    items=profiles,
                    default_value=profiles[0] if profiles else "",
                    width=FOOTER_PROFILE_COMBO_WIDTH,
                    tag="wrapper_profile_combo",
                )
                self._footer_profile_combo_ready = True
                # Visually separates the Profile selector from the action group
                # that follows; keeps Save As / Apply / Delete tight together.
                dpg.add_spacer(width=SPACE_MD)
                dpg.add_button(tag="footer_save_as_button", label=t("footer.save_as"), width=FOOTER_BUTTON_WIDTH_WIDE, callback=lambda: self._open_save_as_modal())
                dpg.add_button(tag="footer_apply_button", label=t("footer.apply"), width=FOOTER_BUTTON_WIDTH, callback=lambda: self.apply_selected_wrapper_profile())
                with dpg.tooltip("footer_apply_button", tag="footer_apply_tooltip"):
                    dpg.add_text(
                        t("footer.apply.tooltip"),
                        wrap=320,
                        tag="footer_apply_tooltip_text",
                    )
                dpg.add_button(tag="footer_delete_button", label=t("footer.delete"), width=FOOTER_BUTTON_WIDTH, callback=lambda: self.confirm_delete_wrapper_profile())
                with dpg.tooltip("footer_delete_button", tag="footer_delete_tooltip"):
                    dpg.add_text(
                        t("footer.delete.tooltip"),
                        wrap=320,
                        tag="footer_delete_tooltip_text",
                    )
                dpg.add_spacer(width=SPACE_LG)
                dpg.add_button(tag="footer_read_button", label=t("footer.read"), width=FOOTER_BUTTON_WIDTH, callback=lambda: self.refresh_from_controller())
                dpg.add_spacer(width=SPACE_LG)
                # The live status readout ("Ready.", apply/read results) is the
                # footer's primary dynamic signal — render it at text.primary so
                # it stays legible against the bar (wayfinding refinement;
                # the panel read the prior secondary-tier tint as
                # low-contrast). Body size is already inherited from the global
                # default font (fonts.bind_default_font binds the body face), so
                # color is the lever here. The static "Profile" label above stays
                # quieter (secondary) so the hierarchy label < live-status holds.
                dpg.add_text(tag="footer_status_text", default_value="", color=COLORS["text.primary"])
                dpg.add_text(tag="bottom_last_event", default_value="", show=False)
                dpg.add_text(tag="bottom_apply_result", default_value="", show=False)
                dpg.add_text(tag="bottom_pending_changes", default_value="", show=False)

    def rebuild_current_screen(self) -> None:
        if dpg.does_item_exist("content_region"):
            children = dpg.get_item_children("content_region", 1)
            for child in children or []:
                dpg.delete_item(child)
        try:
            self.current_screen = self._canonical_screen_id(self.current_screen)
            builder = self.SCREEN_BUILDERS.get(self.current_screen)
            if builder is None:
                invalid_screen = repr(self.current_screen)
                self.device_service.log_event(f"Unknown screen requested: {invalid_screen}. Falling back to Home.")
                self.current_screen = "home"
                builder = self.SCREEN_BUILDERS["home"]
            builder(self, "content_region")
            self.refresh_current_screen()
            if self.current_screen == "controller":
                self._hydrate_current_settings_screen()
        except Exception:
            error_text = traceback.format_exc()
            self.device_service.log_event(f"Failed to render {self.current_screen}: {error_text.splitlines()[-1]}")
            self._build_screen_error(error_text)
        # Builders create HID-flow buttons with their default enabled=True,
        # so a rebuild mid-job (locale change, busy-refusal rebuild, RP
        # delete-confirm rebuild) re-presents clickable buttons whose clicks
        # only get refused. Re-apply the disable here — one call site
        # instead of teaching every builder about the busy flag.
        if self._hid_job_in_flight:
            self._set_hid_flow_buttons_enabled(False)
        # Log the new screen's geometry once it has rendered (permanent
        # diagnostic). Deferred so the read sees the laid-out root, not the
        # just-built (un-sized) one.
        self._request_geometry_log(f"rebuild:{self.current_screen}")

    def rebuild_full_ui(self) -> None:
        """Rebuild top bar, sidebar, content, and footer after global changes."""

        if dpg.does_item_exist("main_window"):
            self._footer_profile_combo_ready = False
            dpg.delete_item("main_window")
        self._build_main_window()
        self.rebuild_current_screen()
        self.refresh_shell()
        self._set_window_title(self.window_title())

    def window_title(self) -> str:
        return t("app.window_title")

    def _set_window_title(self, title: str) -> None:
        self._ensure_dpg_safe_window_title()
        self._title_manager.set_title(title)

    def _ensure_dpg_safe_window_title(self) -> None:
        if self._dpg_viewport_title_seeded:
            return
        try:
            dpg.set_viewport_title(_DPG_SAFE_WINDOW_TITLE)
            self._dpg_viewport_title_seeded = True
        except Exception:
            logger.debug("Dear PyGui viewport title update skipped", exc_info=True)

    def _bind_viewport_resize_callback(self) -> None:
        try:
            dpg.set_viewport_resize_callback(lambda *_args: self._resize_shell_layout())
        except Exception:
            logger.debug("Dear PyGui viewport resize callback skipped", exc_info=True)

    def _resize_shell_layout(self) -> None:
        if not dpg.does_item_exist("middle_band"):
            return
        try:
            dpg.configure_item("middle_band", height=self._middle_band_height())
        except Exception:
            logger.debug("Shell layout resize skipped", exc_info=True)
        # Capture the new (e.g. un-maximized) geometry. The settle countdown
        # resets on every resize event, so a continuous drag debounces to one
        # log emitted once the window stops resizing (permanent diagnostic).
        self._request_geometry_log("resize")

    def _middle_band_height(self) -> int:
        viewport_h = self._viewport_client_height()
        reserved = _TOP_STATUS_BAR_HEIGHT + _FOOTER_PROFILE_BAR_HEIGHT + _MAIN_WINDOW_VERTICAL_GUTTER
        return max(_MIN_MIDDLE_BAND_HEIGHT, int(viewport_h - reserved))

    def _viewport_client_height(self) -> int:
        try:
            height = dpg.get_viewport_client_height()
        except Exception:
            height = 0
        if not height:
            try:
                height = dpg.get_viewport_height()
            except Exception:
                height = 0
        return int(height or _DEFAULT_VIEWPORT_HEIGHT)

    def _viewport_client_width(self) -> int:
        """Best-effort viewport client width (mirrors _viewport_client_height)."""
        for getter in ("get_viewport_client_width", "get_viewport_width"):
            try:
                width = getattr(dpg, getter)()
            except Exception:
                width = 0
            if width:
                return int(width)
        return -1

    def _request_geometry_log(self, reason: str) -> None:
        """Queue a geometry snapshot to be logged once layout settles.

        Stores only the trigger label + a frame countdown; the DPG reads happen
        later on the render thread in :meth:`_drain_geometry_log`. Repeated calls
        reset the countdown so a continuous resize drag debounces to one log.
        Safe to call before the render loop (e.g. during build): the countdown
        simply elapses on the first frames of the loop.
        """
        self._geometry_log_pending = reason
        self._geometry_log_countdown = _GEOMETRY_LOG_SETTLE_FRAMES

    def _drain_geometry_log(self) -> None:
        """Emit a pending geometry snapshot once its settle countdown elapses.

        Called once per frame from :meth:`_tick` (render thread). No-op until a
        request is pending and the countdown reaches zero.
        """
        if self._geometry_log_pending is None:
            return
        if self._geometry_log_countdown > 0:
            self._geometry_log_countdown -= 1
            return
        reason = self._geometry_log_pending
        self._geometry_log_pending = None
        self._log_geometry(reason)

    def _log_geometry(self, reason: str) -> None:
        """Log current viewport / content-region geometry at INFO.

        Permanent harmless diagnostic. A post-smoke read of ``zd_wrapper.log``
        recovers the operator's REAL maximized client height and each screen's
        real root ``y_scroll_max`` — values the headless overflow probe cannot
        see (it has no knowledge of the operator's window chrome / display
        scale). Reuses the existing ``content_region`` tag + its first child (the
        screen root, the real scroll surface). All DPG reads are
        ``does_item_exist``-guarded and run on the render thread (via
        :meth:`_tick`); a cosmetic failure never disturbs the loop.
        """
        try:
            if not self._dpg_context_ready or not dpg.does_item_exist("content_region"):
                return
            vp_w = self._viewport_client_width()
            vp_h = self._viewport_client_height()
            try:
                clip_h = int(dpg.get_item_rect_size("content_region")[1])
            except Exception:
                clip_h = -1
            root_rect_h = -1
            root_ysm = 0.0
            kids = dpg.get_item_children("content_region", 1) or []
            if kids and dpg.does_item_exist(kids[0]):
                root = kids[0]
                try:
                    root_rect_h = int(dpg.get_item_rect_size(root)[1])
                except Exception:
                    root_rect_h = -1
                try:
                    root_ysm = float(dpg.get_y_scroll_max(root))
                except Exception:
                    root_ysm = 0.0
            # The PRIMARY window's own scroll — the layer that drew the multi-round
            # phantom far-right bar (its chrome overflowed the viewport while every
            # inner container fit). Logged so a future smoke catches a primary-
            # window regression directly; must be 0.0 in a healthy layout.
            main_window_ysm = 0.0
            if dpg.does_item_exist("main_window"):
                try:
                    main_window_ysm = float(dpg.get_y_scroll_max("main_window"))
                except Exception:
                    main_window_ysm = 0.0
            logger.info(
                "geometry: screen=%s viewport_client=(%d,%d) "
                "content_region_clip_h=%d root_rect_h=%d root_y_scroll_max=%.1f "
                "main_window_y_scroll_max=%.1f trigger=%s",
                self.current_screen,
                vp_w,
                vp_h,
                clip_h,
                root_rect_h,
                root_ysm,
                main_window_ysm,
                reason,
            )
        except Exception:  # pragma: no cover - diagnostic must never block render
            logger.debug("geometry log skipped", exc_info=True)

    def _canonical_screen_id(self, screen: str) -> str:
        return self.SCREEN_ALIASES.get(screen, screen)

    def _visible_nav_items(self) -> tuple[str, ...]:
        if self.settings.show_legacy_screens:
            return self.NAV_ITEMS + self.LEGACY_NAV_ITEMS
        return self.NAV_ITEMS

    def _build_screen_error(self, error_text: str) -> None:
        with dpg.child_window(parent="content_region", autosize_x=True, autosize_y=True, border=False):
            dpg.add_text("Screen Error", color=self.COLORS["bad"])
            dpg.add_text(
                f"The {self.current_screen} screen failed to render. "
                "Please report the details below so we can fix it quickly.",
                wrap=980,
            )
            dpg.add_spacer(height=8)
            with dpg.child_window(height=520, border=True):
                dpg.add_text(error_text, wrap=960)

    def refresh_shell(self) -> None:
        state = self.device_service.state
        pending = self.profile_service.pending_changes_count()
        display_sync_status = state.sync_status
        if pending > 0 and state.connection_state == "connected" and state.sync_status not in {"Reading", "Applying", "Apply Failed"}:
            display_sync_status = "Unsaved Changes"
        elif pending == 0 and state.connection_state == "connected" and state.sync_status == "Unsaved Changes":
            display_sync_status = "Ready" if state.last_read_time else "Connected"
        elif state.connection_state != "connected":
            display_sync_status = "Disconnected"

        state.sync_status = display_sync_status
        sync_color = self.COLORS["good"] if display_sync_status in {"Ready", "Connected"} else self.COLORS["warn"]
        if display_sync_status == "Apply Failed":
            sync_color = self.COLORS["bad"]

        polling_rate = _polling_rate_label(self.last_controller_snapshot)
        profile_label = _top_profile_label(state)
        self._set_if_exists("top_device_name", state.product_name)
        self._set_if_exists("top_product_name", t("status.app_name"))
        self._set_if_exists("top_connection_mode", _top_connection_mode_label(state.connection_mode))
        self._set_if_exists(
            "top_firmware",
            f"{t('shell.firmware')} {self.device_service.format_firmware_version()}",
        )
        self._set_if_exists(
            "top_battery",
            f"{t('shell.battery')} {self.device_service.format_battery_level()}",
        )
        self._set_if_exists("top_profile", profile_label)
        self._set_if_exists("top_active_profile", profile_label)
        self._set_if_exists("top_active_profile_tooltip_text", t("status.config.not_verified_tooltip"))
        if dpg.does_item_exist("top_active_profile_tooltip"):
            dpg.configure_item(
                "top_active_profile_tooltip",
                show=_top_profile_not_verified(state),
            )
        self._set_if_exists("top_polling_rate", polling_rate)
        self._set_if_exists("top_sync_status", _top_sync_status_label(display_sync_status))
        if dpg.does_item_exist("top_sync_status"):
            dpg.configure_item("top_sync_status", color=sync_color)
        if dpg.does_item_exist("top_connection_dot"):
            dot_color = (
                COLORS["accent.primary"]
                if state.connection_state == "connected" and display_sync_status in {"Connected", "Ready"}
                else COLORS["text.muted"]
            )
            dpg.configure_item("top_connection_dot", color=dot_color)

        recent_events = self.device_service.recent_events(1)
        last_apply_result = _last_apply_result_text(self.device_service)
        self._set_if_exists("bottom_last_event", recent_events[0] if recent_events else t("shell.no_events"))
        self._set_if_exists("bottom_apply_result", last_apply_result)
        self._set_if_exists("bottom_pending_changes", str(pending))
        self._set_if_exists("footer_apply_tooltip_text", t("footer.apply.tooltip"))
        self._set_if_exists("footer_delete_tooltip_text", t("footer.delete.tooltip"))
        if not _apply_status_active(self):
            self._set_if_exists("footer_status_text", last_apply_result)

    def _set_if_exists(self, tag: str, value) -> None:
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, value)

    def attach_settings_service(
        self, settings_service: SettingsService | None
    ) -> None:
        """Late-bind a SettingsService once the watchdog sees the controller.

        The shell is constructed with ``settings_service=None`` when the
        wrapper boots with no controller, and the apply coordinator +
        RestorePointService are wired at construction — so flipping
        ``shell.settings_service`` alone left footer Apply-profile /
        Safe-Import apply and Restore Points dead until restart. Idempotent: a
        repeat call with the same service
        re-binds harmlessly and never rebuilds an existing
        ``restore_point_service``.
        """

        # Accepted residual: the health-report / quick-check device-context
        # closures keep the construction-time settings_service (None after a
        # late connect) — cosmetic only (configured_polling_hz=None in reports).
        self.settings_service = settings_service
        self._apply_coordinator.set_settings_service(settings_service)
        if self.restore_point_service is None and settings_service is not None:
            self.restore_point_service = _build_default_restore_point_service(
                settings_service=settings_service,
                settings_apply_coordinator=self._apply_coordinator,
                wear_ledger=self.wear_ledger_service,
            )
        self.request_settings_service_hydration()

    def request_settings_service_hydration(self) -> None:
        self._needs_hydration = True

    # -- HID-job executor seam (worker-thread phase 1) -----------------------

    def _run_hid_job(
        self,
        job: Callable[[], Any],
        on_done: Callable[[Any], None],
    ) -> bool:
        """Run a long HID flow as a DPG-free ``job`` plus a DPG ``on_done``.

        ``job`` must be strictly DPG-free (service calls, sleeps, logging,
        shell-state bookkeeping); ``on_done`` receives the job's return value
        — or the BaseException it raised — and does all DearPyGui work.

        Sync mode (``hid_executor=None``, the default): job and on_done run
        inline on the caller's thread in today's exact statement order.

        Threaded mode: the executor runs the job off the render thread and
        the completion is queued for :meth:`_tick` to drain on the render
        thread. While a job is in flight, further flows are refused (returns
        False) with a status-bar message and the HID-flow buttons stay
        disabled. A jobbed flow started from INSIDE a running job (the
        profile apply's post-apply read) runs its job inline on the worker
        and queues its on_done — composition without re-entrancy.
        """

        if getattr(self._hid_job_context, "in_job", False):
            # Nested call from a worker-side job: stay on the worker for the
            # job, defer the DPG work to the render thread.
            self._hid_job_completions.put((on_done, self._call_hid_job(job), False))
            return True
        if self._hid_executor is None:
            on_done(self._call_hid_job(job))
            return True
        if self._hid_job_in_flight:
            self._refuse_hid_job()
            return False
        self._hid_job_in_flight = True
        self._hid_job_started_monotonic = time.monotonic()
        self._hid_job_stall_warned = False
        self._hid_job_stall_last_seconds = -1
        self._set_hid_flow_buttons_enabled(False)

        def wrapped_job() -> Any:
            self._hid_job_context.in_job = True
            try:
                return job()
            finally:
                self._hid_job_context.in_job = False

        def deliver(result: Any) -> None:
            self._hid_job_completions.put((on_done, result, True))

        try:
            self._hid_executor(wrapped_job, deliver)
        except BaseException:
            # Executor failed to start the job (e.g. thread-spawn failure):
            # roll back the busy state so the UI isn't wedged, then surface.
            self._hid_job_in_flight = False
            self._hid_job_started_monotonic = None
            self._set_hid_flow_buttons_enabled(True)
            raise
        return True

    @staticmethod
    def _call_hid_job(job: Callable[[], Any]) -> Any:
        try:
            return job()
        except BaseException as exc:  # noqa: BLE001 — on_done re-raises whatever today's code didn't catch
            return exc

    def _refuse_hid_job(self) -> None:
        logger.info("HID flow refused: a controller operation is already in flight")
        self._update_apply_status(t("apply.busy"), False)

    def _device_write_supported(self) -> bool:
        """True when the connected controller is an allowlisted ZD Ultimate Legend."""

        state = getattr(self.device_service, "state", None)
        return bool(getattr(state, "write_supported", True))

    def _refuse_hid_write_unverified_device(self) -> None:
        logger.info(
            "HID write refused: connected controller is not an allowlisted "
            "ZD Ultimate Legend"
        )
        self._update_apply_status(t("apply.device_unverified"), False)

    def _zd_write_allowed_or_refuse(self) -> bool:
        """Gate HID write bursts that do not enter through _hid_available_or_refuse."""

        if self._device_write_supported():
            return True
        self._refuse_hid_write_unverified_device()
        return False

    @property
    def hid_busy(self) -> bool:
        """True while a threaded HID job is in flight.

        Sync mode (``hid_executor=None``) never sets the underlying flag, so
        this is always False there. Screens read this instead of reaching
        into ``_hid_job_in_flight`` (Device-vs-Profile gates its live read on
        it; Restore Points gates the pre-restore preview).
        """

        return self._hid_job_in_flight

    @property
    def xinput_poll_service(self) -> "XInputPollService":
        """App-scoped XInput poll worker for the live Diagnostics panel.

        Constructed lazily ONCE per run on first access and memoized (item K):
        the DLL loads a single time and a single worker thread is reused across
        navigations and across build -> teardown -> rebuild of the live-verify
        panel (the panel calls the idempotent ``start()`` on build and ``stop()``
        on teardown, but never reconstructs the service). Headless / test shells
        that never open the panel never touch this, so they load no DLL and
        spawn no worker. This memoized property is the SOLE construction site;
        the debug line lets a smoke confirm a single "Loaded XInput DLL" per run
        originates here.
        """

        service = self._xinput_poll_service
        if service is None:
            logger.debug("Constructing XInputPollService (single instance per app run).")
            service = XInputPollService()
            self._xinput_poll_service = service
        return service

    def _hid_available_or_refuse(self) -> bool:
        """Busy gate for the synchronous device-touching UI entry points.

        The worker-thread seam keeps the UI live during a 2-9 s threaded
        apply/restore/read, so a render-thread callback that talks to the
        controller directly (slider live-writes, per-tab Apply buttons,
        manual RP capture, Safe-Import apply) could otherwise interleave with
        the in-flight job's burst: SettingsService has no internal lock,
        interleaved writes can trip the firmware's in-burst rejection quirk,
        and concurrent ``_read_response`` loops consume each other's frames.

        Returns True when the connected controller is write-capable and no job
        is in flight (always, in sync mode). Otherwise surfaces a localized
        refusal and returns False — callers early-return and must NOT queue the
        refused action.
        """

        if not self._device_write_supported():
            self._refuse_hid_write_unverified_device()
            return False
        if not self._hid_job_in_flight:
            return True
        self._refuse_hid_job()
        return False

    def _set_hid_flow_buttons_enabled(self, enabled: bool) -> None:
        if not self._dpg_context_ready:
            return
        for tag in _HID_FLOW_BUTTON_TAGS:
            try:
                if dpg.does_item_exist(tag):
                    dpg.configure_item(tag, enabled=enabled)
            except Exception:
                logger.debug("HID-flow button toggle failed for %r", tag, exc_info=True)

    def _drain_hid_job_completions(self) -> None:
        while True:
            try:
                on_done, result, completes_job = self._hid_job_completions.get_nowait()
            except queue.Empty:
                return
            if completes_job:
                # Clear before invoking so an on_done that chains a new flow
                # isn't refused by its own predecessor.
                self._hid_job_in_flight = False
                self._hid_job_started_monotonic = None
                self._set_hid_flow_buttons_enabled(True)
            try:
                on_done(result)
            except Exception:
                # Worker-side failures arrive as values in ``result``; an
                # exception HERE is a bug in the render-side handler. Contain
                # it (parity with DPG's own callback-exception handling)
                # rather than killing the render loop.
                logger.exception("HID-job completion handler failed")

    def _defer_ui_call(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` in a later ``_tick`` drain pass on the render thread.

        Building block of the modal-swap seam: DPG eats a modal create
        that shares a pass with
        another modal's teardown — on ANY thread (matrix in
        tools/diag_dpg_modal_thread_visibility.py) — so modal-opening
        flows queue here and :meth:`_drain_deferred_ui_calls` paces the
        queued calls one pass per rendered frame. Most call sites want
        :meth:`_defer_modal_swap`, which splits teardown and create
        across two passes; use this directly only for work that doesn't
        pair a modal create with a same-pass teardown.

        Unarmed — no live render loop (tests, headless/sync paths,
        pre-loop startup) — the call executes inline on the caller's
        thread, preserving the synchronous contract those paths rely on.
        Inline execution is transparent: exceptions propagate to the
        caller exactly as a direct call would. Deferred execution is
        contained by the drain instead.
        """

        if not self._defer_ui_armed:
            fn()
            return
        self._deferred_ui_calls.put(fn)

    def _drain_deferred_ui_calls(self) -> None:
        """Execute this pass's deferred-UI calls in FIFO order (render thread).

        Bounded to the entries present when the pass starts: a call
        enqueued DURING the drain (e.g. the create half of a modal swap)
        runs on the NEXT pass, after a rendered frame. That frame gap is
        load-bearing — DPG never shows a modal created in the same pass
        another modal was torn down (see :meth:`_defer_modal_swap`).

        Exception parity with :meth:`_drain_hid_job_completions`: a
        raising deferred call is a render-side bug — contain and log it
        rather than killing the render loop, and keep draining so one bad
        call can't starve the calls queued behind it.
        """

        for _ in range(self._deferred_ui_calls.qsize()):
            try:
                fn = self._deferred_ui_calls.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                logger.exception("Deferred UI call failed")

    def _defer_modal_swap(
        self,
        open_fn: Callable[[], None],
        *,
        delete_tags: tuple[str, ...] = (),
        hide_tags: tuple[str, ...] = (),
        key: str | None = None,
    ) -> None:
        """Tear down modals, let a frame render, then run modal-opening ``open_fn``.

        Encodes DPG's benched modal law (2026-06-11, thread-independent;
        tools/diag_dpg_modal_thread_visibility.py): a modal created in the
        same pass that another modal was showing — deleted, hidden, or
        left up — never becomes visible, and one rendered frame between
        teardown and create cures it. Armed, the teardown runs in the next
        drain pass and ``open_fn`` the pass after (the bounded drain
        guarantees the rendered frame between). Unarmed, teardown and
        ``open_fn`` run inline immediately — the synchronous contract the
        headless suite relies on.

        ``delete_tags`` are deleted; ``hide_tags`` are hidden instead so
        their widget state survives (the preview keeps its typed profile
        name while the apply-confirm modal is up). ``open_fn``'s own
        defensive ``_delete_if_exists`` calls become no-ops by create
        time, making it a pure create — the only shape DPG renders.

        ``key`` coalesces re-entrant requests: while a swap with the same
        key is pending, further requests are dropped. A double-fired
        callback would otherwise delete and recreate the target modal in
        one pass — exactly the poison this seam exists to avoid.
        """

        if key is not None:
            if key in self._pending_modal_swap_keys:
                return
            self._pending_modal_swap_keys.add(key)

        def run_open() -> None:
            try:
                open_fn()
            finally:
                self._pending_modal_swap_keys.discard(key)

        def teardown_then_defer_open() -> None:
            try:
                for tag in delete_tags:
                    if dpg.does_item_exist(tag):
                        dpg.delete_item(tag)
                for tag in hide_tags:
                    if dpg.does_item_exist(tag):
                        dpg.configure_item(tag, show=False)
            except BaseException:
                # Don't let a teardown failure wedge the coalescing key:
                # the hop must stay reachable after the contained error.
                self._pending_modal_swap_keys.discard(key)
                raise
            self._defer_ui_call(run_open)

        self._defer_ui_call(teardown_then_defer_open)

    def _tick_hid_job_staleness(self) -> None:
        """Surface a threaded job stuck past HID_JOB_STALL_WARN_S.

        Runs every _tick after the completion drain. One log warning per
        job; the footer status is re-pinned each elapsed second (it would
        otherwise fade after the 5 s status hold) with the counting
        duration. A completing job clears the start marker in the drain, so
        a job that finishes normally never reaches this.
        """

        if not self._hid_job_in_flight or self._hid_job_started_monotonic is None:
            return
        elapsed = time.monotonic() - self._hid_job_started_monotonic
        if elapsed < HID_JOB_STALL_WARN_S:
            return
        seconds = int(elapsed)
        if not self._hid_job_stall_warned:
            self._hid_job_stall_warned = True
            logger.warning(
                "HID job still in flight after %d s; controller flows stay "
                "refused until it completes",
                seconds,
            )
        if seconds != self._hid_job_stall_last_seconds:
            self._hid_job_stall_last_seconds = seconds
            self._update_apply_status(
                t("apply.busy_long", seconds=seconds), False
            )

    def refresh_from_controller(self, *, include_device: bool = True) -> None:
        """Read current controller settings and hydrate Wrapper Settings widgets.

        ``include_device`` defaults to True (normal read / Read button / startup
        / reconnect). The apply path passes False after an "Apply profile only"
        choice so the device-field widgets (step-size slider, polling-rate
        combo) keep whatever value the user just set via live-write, instead of
        being snapped to a stale read or to the profile's value (the
        step-size apply-snapback work, 2026-05-24).

        The read itself (a ~27-round-trip HID batch) runs as a HID job — see
        :meth:`_run_hid_job`. With the default sync executor this is inline
        and identical to the pre-seam flow.
        """

        if self.settings_service is None:
            self.last_snapshot_status = t("apply.read.waiting")
            self.last_snapshot_ts = None
            self._render_settings_snapshot_status()
            self.device_service.log_i18n_event("log.read.waiting")
            self.refresh_shell()
            return

        self._run_hid_job(
            self._refresh_read_job,
            lambda outcome: self._refresh_read_on_done(
                outcome, include_device=include_device
            ),
        )

    def _refresh_read_job(self) -> tuple[ControllerSnapshot, DeviceIdentity | None]:
        """DPG-free job half of :meth:`refresh_from_controller`.

        Full settings read plus the first-readable-connect RP capture (the
        capture is its own HID read, so it must stay job-side; the
        trust-ritual card it may earn renders in on_done).
        """

        snapshot = self._read_all_settings_with_timeout_retry()
        # Trigger model #1 — first readable connect. Fire at most
        # once per (product_string, app session) so reconnecting the same
        # controller within one session doesn't spam the vault. The capture
        # itself is best-effort and never blocks the read.
        first_connect_identity = self._capture_first_readable_connect()
        return snapshot, first_connect_identity

    def _refresh_read_on_done(self, outcome, *, include_device: bool) -> None:
        """DPG on_done half of :meth:`refresh_from_controller`."""

        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):
                raise outcome  # keep today's `except Exception` scope
            exc = outcome
            # Broad catch is intentional. Includes TimeoutError raised by
            # _default_read_file's HID-read timeout (default
            # 1500ms) — without it, a wedged controller would block startup
            # before dpg.create_viewport runs and the wrapper would never
            # render its UI. Surfacing the failure here lets the wrapper
            # show a "read failed" status string and proceed to viewport
            # creation. SettingsServiceError, OSError, and any other
            # downstream failure flow through the same path.
            self.last_snapshot_status = t("apply.read.failed", reason=exc)
            self.last_snapshot_ts = None
            self._render_settings_snapshot_status()
            self.device_service.log_i18n_event("log.snapshot.refresh_failed", reason=exc)
            self.refresh_shell()
            return

        snapshot, first_connect_identity = outcome
        # Hydrate from what the store actually folded in (under the lock), NOT the
        # pre-fold local: a worker-thread back-paddle landing in the merge->store
        # gap is folded into last_controller_snapshot but is absent from this
        # local, so hydrating the local would omit that paddle from the UI until
        # the next refresh. Symmetric to the Save-As store site, which persists
        # `stored` for the same reason. `missing` is unaffected — the fold only
        # adds back-paddle bindings, and _hydrate_back_paddle_bindings consumes
        # them without contributing to the readable-field set.
        stored = self._set_last_controller_snapshot(snapshot)
        if stored is None:  # snapshot is non-None here; keep the type checker happy
            stored = snapshot
        self.last_snapshot_ts = time.time()

        self._show_first_readable_connect_card(first_connect_identity)

        missing: list[str] = []
        self._hydrate_controller_snapshot(stored, missing, include_device=include_device)
        if missing:
            self.last_snapshot_status = t("apply.read.missing_fields", fields=", ".join(missing))
        else:
            self.last_snapshot_status = t("apply.read.success")

        if missing:
            self.device_service.log_i18n_event(
                "log.snapshot.refreshed_missing",
                fields=", ".join(missing),
            )
        else:
            self.device_service.log_i18n_event("log.snapshot.refreshed_ok")
        self._render_settings_snapshot_status()
        self.refresh_shell()

    def _read_all_settings_with_timeout_retry(self) -> ControllerSnapshot:
        """One full settings read, with a single settle+retry on TimeoutError.

        Mitigation for the transient first-read-after-burst timeout observed
        twice on hardware 2026-06-10 (see READ_TIMEOUT_RETRY_SETTLE_S). Only
        a TimeoutError on the first attempt triggers the retry; any other
        exception — and anything the retry itself raises — propagates to the
        caller's existing failure handling unchanged.
        """

        try:
            return self._with_last_back_paddle_bindings(
                self.settings_service.get_all_settings()
            )
        except TimeoutError:
            logger.info(
                "first read timed out; settling %.1fs and retrying once",
                READ_TIMEOUT_RETRY_SETTLE_S,
            )
            time.sleep(READ_TIMEOUT_RETRY_SETTLE_S)
            return self._with_last_back_paddle_bindings(
                self.settings_service.get_all_settings()
            )

    def _hydrate_current_settings_screen(self) -> None:
        if self.last_controller_snapshot is not None:
            self._hydrate_controller_snapshot(self.last_controller_snapshot, [])
        self._render_settings_snapshot_status()

    def _hydrate_controller_snapshot(
        self,
        snapshot: ControllerSnapshot,
        missing: list[str],
        *,
        include_device: bool = True,
    ) -> None:
        # ``include_device=False`` is set by the apply path after "Apply profile
        # only" — the device-class widgets (polling rate, step size) stay at
        # whatever the user just set via live-write rather than snapping to the
        # post-write read. See refresh_from_controller for full context.
        if include_device:
            self._hydrate_polling_rate(snapshot.polling_rate, missing)
        self._hydrate_vibration(snapshot.vibration, missing)
        self._hydrate_triggers(snapshot.trigger_left, snapshot.trigger_right, missing)
        self._hydrate_deadzones(snapshot.deadzones, missing)
        self._hydrate_sensitivity(snapshot.sensitivity_left, snapshot.sensitivity_right, missing)
        self._hydrate_sensitivity_8point(
            snapshot.sensitivity_left_8point,
            snapshot.sensitivity_right_8point,
        )
        self._hydrate_axis_inversion(
            snapshot.axis_inversion_left,
            snapshot.axis_inversion_right,
            missing,
        )
        self._hydrate_button_bindings(snapshot.button_bindings, missing)
        # Refresh the read-only Current Bindings list so a snapshot refresh (the
        # async read on_done / footer Read updates last_controller_snapshot
        # without rebuilding the screen body) reflects the fresh bindings in
        # place — no tab re-entry needed. Value-only update (no widget creation),
        # so it is safe in the hydrate path; per-slot no-op when not mounted.
        controller.refresh_current_bindings(self)
        self._hydrate_back_paddle_bindings(snapshot.back_paddle_bindings)
        live_verify.refresh_inspector_binding(self)
        self._hydrate_lighting(snapshot.lighting_zones, missing)
        if include_device:
            self._hydrate_step_size(snapshot.step_size, missing)

    def _hydrate_step_size(self, value: int | None, missing: list[str]) -> None:
        # Diagnostic for the step-size apply-snapback work (2026-05-24): every
        # hydrate that writes the slider is logged to help map a
        # repro to the snap-back hypothesis bullets (snapshot vs ordering vs
        # callback re-fire vs HID read race).
        logger.debug("_hydrate_step_size: value=%s", value)
        if value is None:
            self._step_size_hydrated = False
            self._set_widget_enabled("step_size_slider", False)
            self._set_widget_shown("step_size_unread_hint", True)
            missing.append("step_size")
            return
        # Set the value while the write-guard still blocks, so a DPG set_value
        # that fires the callback can't clobber the controller's real value.
        self._step_size_hydrated = False
        self._set_widget("step_size_slider", value)
        self._set_widget_enabled("step_size_slider", True)
        self._set_widget_shown("step_size_unread_hint", False)
        self._step_size_hydrated = True

    def _hydrate_polling_rate(
        self,
        rate: PollingRate | None,
        missing: list[str],
    ) -> None:
        # Diagnostic parallel to _hydrate_step_size — same work-item (2026-05-24).
        logger.debug("_hydrate_polling_rate: rate=%s", rate)
        label = POLLING_RATE_LABEL_BY_ENUM.get(rate) if rate is not None else None
        if label is None:
            self._polling_rate_hydrated = False
            self._set_widget_enabled("usb_polling_rate_combo", False)
            self._set_widget_shown("usb_polling_rate_unread_hint", True)
            missing.append("polling_rate")
            return
        self._polling_rate_hydrated = False
        self._set_widget("usb_polling_rate_combo", label)
        self._set_widget_enabled("usb_polling_rate_combo", True)
        self._set_widget_shown("usb_polling_rate_unread_hint", False)
        self._polling_rate_hydrated = True

    def _hydrate_vibration(
        self,
        vib: VibrationSettings | None,
        missing: list[str],
    ) -> None:
        if vib is None:
            missing.append("vibration")
            return
        self._set_widget("vibration_lg_slider", vib.left_grip_strength)
        self._set_widget("vibration_rg_slider", vib.right_grip_strength)
        self._set_widget("vibration_lm_slider", vib.left_trigger_motor_strength)
        self._set_widget("vibration_rm_slider", vib.right_trigger_motor_strength)
        label = VIBRATION_MODE_LABEL_BY_ENUM.get(vib.mode)
        if label is None:
            missing.append("vibration")
            return
        self._set_widget("vibration_mode_combo", label)

    def _hydrate_triggers(
        self,
        left: ServiceTriggerSettings | None,
        right: ServiceTriggerSettings | None,
        missing: list[str],
    ) -> None:
        if left is None:
            missing.append("trigger_left")
        else:
            self._set_widget("trigger_left_min_slider", left.range_min)
            self._set_widget("trigger_left_max_slider", left.range_max)
            label = TRIGGER_MODE_LABEL_BY_ENUM.get(left.mode)
            if label is None:
                missing.append("trigger_left")
            else:
                self._set_widget("trigger_left_mode_combo", label)
        if right is None:
            missing.append("trigger_right")
        else:
            self._set_widget("trigger_right_min_slider", right.range_min)
            self._set_widget("trigger_right_max_slider", right.range_max)
            label = TRIGGER_MODE_LABEL_BY_ENUM.get(right.mode)
            if label is None:
                missing.append("trigger_right")
            else:
                self._set_widget("trigger_right_mode_combo", label)

    def _hydrate_deadzones(
        self,
        dz: StickDeadzones | None,
        missing: list[str],
    ) -> None:
        if dz is None:
            missing.append("deadzones")
            return
        self._set_widget("deadzone_left_center_slider", dz.left_center)
        self._set_widget("deadzone_right_center_slider", dz.right_center)
        self._set_widget("deadzone_left_outer_slider", dz.left_outer)
        self._set_widget("deadzone_right_outer_slider", dz.right_outer)

    def _hydrate_sensitivity(
        self,
        left: tuple[SensitivityAnchor, SensitivityAnchor, SensitivityAnchor] | None,
        right: tuple[SensitivityAnchor, SensitivityAnchor, SensitivityAnchor] | None,
        missing: list[str],
    ) -> None:
        for side, anchors in (("left", left), ("right", right)):
            if anchors is None:
                missing.append(f"sensitivity_{side}")
                continue
            for index, anchor in enumerate(anchors, start=1):
                self._set_widget(f"sensitivity_{side}_a{index}x_slider", anchor.x)
                self._set_widget(f"sensitivity_{side}_a{index}y_slider", anchor.y)

    def _hydrate_sensitivity_8point(
        self,
        left: SensitivityAnchorTuple8 | None,
        right: SensitivityAnchorTuple8 | None,
    ) -> None:
        # 1.2.9 / fw-1.24 8-point curves (cat 0x86). Populated only on capable
        # devices; None elsewhere. A None side is NOT a missing read — the device
        # genuinely has no 8-point curve — so we don't append to ``missing`` (the
        # 3-point hydrate already tracks the always-present cat-0x06 fields). The
        # _8point-suffixed sliders only exist when the screen rendered the 8-point
        # editor; _set_widget no-ops via does_item_exist when they don't.
        for side, anchors in (("left", left), ("right", right)):
            if anchors is None:
                continue
            # The just-hydrated anchors ARE the plot's data — write both widget
            # twins (slider + exact-entry input) and paint the curve straight in
            # (write-only; no slider read-back). No-ops when the 8-point editor /
            # plot isn't on screen.
            self._set_sensitivity_8point_anchor_widgets(side, anchors)

    def _refresh_sensitivity_8point_plot(self, side: str) -> None:
        """Repaint a side's 8-point curve plot from its LIVE slider values.

        Reads the 16 ``_8point`` sliders into 8 ``(x, y)`` points and pushes them
        into the line + scatter series. This is the live-drag path — the slider
        callbacks wire here. No-ops when the plot series doesn't exist (the
        3-point editor rendered instead, or there is no DPG context). The hydrate
        and preset paths instead feed values straight to
        :meth:`_set_sensitivity_8point_plot_points` (they already hold the
        anchors), which keeps those write-only paths from issuing a ``get_value``
        the widget-capture test harness intentionally leaves unstubbed.
        """

        series_tag = f"sensitivity_{side}_plot_series_8point"
        if not dpg.does_item_exist(series_tag):
            return
        tag_prefix = f"sensitivity_{side}_"
        xs = [dpg.get_value(f"{tag_prefix}a{index}x_slider_8point") for index in range(1, 9)]
        ys = [dpg.get_value(f"{tag_prefix}a{index}y_slider_8point") for index in range(1, 9)]
        self._set_sensitivity_8point_plot_points(side, xs, ys)

    def _set_sensitivity_8point_plot_points(self, side: str, xs, ys) -> None:
        """Push explicit 8-point ``(x, y)`` arrays into a side's curve + scatter
        series, and reposition the 8 draggable anchor handles to match.

        This is the single sink every plot-update path funnels through (hydrate,
        preset, and the live slider/input/drag edit via
        :meth:`_refresh_sensitivity_8point_plot`), so updating the drag points
        here is what keeps the graph-editing handles in two-way sync with the
        slider/input values no matter which editor the user touched. Write-only
        (every ``set_value`` sits behind a ``does_item_exist`` guard, so legacy
        3-point renders and headless paths that never built the drag points
        simply no-op); the static y=x diagonal is never touched."""

        series_tag = f"sensitivity_{side}_plot_series_8point"
        if not dpg.does_item_exist(series_tag):
            return
        xs = list(xs)
        ys = list(ys)
        dpg.set_value(series_tag, [xs, ys])
        scatter_tag = f"sensitivity_{side}_plot_scatter_8point"
        if dpg.does_item_exist(scatter_tag):
            dpg.set_value(scatter_tag, [xs, ys])
        # Keep the draggable anchor handles on the curve. The drag point value is
        # a 2-list ``[x, y]``; setting it to its own (unchanged) position during a
        # drag is a no-op, while neighbours pushed by the monotonic auto-assist
        # follow here.
        for index in range(min(len(xs), len(ys), 8)):
            drag_tag = f"sensitivity_{side}_drag_a{index + 1}_8point"
            if dpg.does_item_exist(drag_tag):
                dpg.set_value(drag_tag, (xs[index], ys[index]))

    def _set_sensitivity_8point_anchor_widgets(self, side: str, anchors) -> None:
        """Write a side's 8 anchors into BOTH the slider and the exact-entry
        input widgets (kept in lockstep) and repaint the curve plot.

        Used by the hydrate and preset paths, which already hold validated,
        monotonic anchors. ``_set_widget`` no-ops via ``does_item_exist`` when a
        widget isn't on screen (3-point editor rendered instead, or no DPG
        context), so this is safe to call unconditionally on a capable side.
        """

        tag_prefix = f"sensitivity_{side}_"
        xs: list[int] = []
        ys: list[int] = []
        for index, anchor in enumerate(anchors, start=1):
            self._set_widget(f"{tag_prefix}a{index}x_slider_8point", anchor.x)
            self._set_widget(f"{tag_prefix}a{index}y_slider_8point", anchor.y)
            self._set_widget(f"{tag_prefix}a{index}x_input_8point", anchor.x)
            self._set_widget(f"{tag_prefix}a{index}y_input_8point", anchor.y)
            xs.append(anchor.x)
            ys.append(anchor.y)
        self._set_sensitivity_8point_plot_points(side, xs, ys)

    def _on_sensitivity_8point_edit(
        self, side: str, anchor_index: int, axis: str, raw_value
    ) -> None:
        """Single funnel for a live 8-point anchor edit from either the coarse
        drag slider or the exact-entry input (commit on Enter).

        Clamps the new value to 0-100, then applies the monotonic auto-assist on
        the edited axis: later anchors are pushed *up* and earlier anchors pushed
        *down* so the whole axis stays non-decreasing with the user's edit
        honoured (the vendor app's "dragging a point pushes the neighbours",
        generalised to both directions so a decrease can't strand a higher
        earlier anchor). The assisted axis is mirrored into BOTH widget twins for
        every anchor, then the plot is repainted from the now-synced sliders. The
        service-layer ``_validate_sensitivity_anchors_8point`` stays the backstop
        at Apply — this only stops the user from *building* an invalid curve.

        No-ops when the edited slider isn't on screen (3-point editor / no DPG
        context). Only the edited axis is touched; the other axis keeps whatever
        the prior hydrate / edits left it (already monotonic), so the combined
        curve stays valid.
        """

        if axis not in ("x", "y"):
            return
        try:
            anchor_index = int(anchor_index)
        except (TypeError, ValueError):
            return
        if not 1 <= anchor_index <= 8:
            return
        tag_prefix = f"sensitivity_{side}_"
        if not dpg.does_item_exist(f"{tag_prefix}a{anchor_index}{axis}_slider_8point"):
            return

        # Current axis sequence from the canonical sliders, with the edit applied
        # at its slot. ``raw_value`` is the freshly-edited widget's value (the
        # input's, when the input fired — the slider hasn't been written yet).
        values = [
            dpg.get_value(f"{tag_prefix}a{index}{axis}_slider_8point")
            for index in range(1, 9)
        ]
        try:
            new_value = int(raw_value)
        except (TypeError, ValueError):
            new_value = values[anchor_index - 1]
        new_value = max(0, min(100, new_value))
        values[anchor_index - 1] = new_value

        # Forward-push: each later anchor rises to at least its predecessor.
        for index in range(anchor_index, 8):
            if values[index] < values[index - 1]:
                values[index] = values[index - 1]
        # Backward-push: each earlier anchor drops to at most its successor.
        for index in range(anchor_index - 2, -1, -1):
            if values[index] > values[index + 1]:
                values[index] = values[index + 1]

        # Mirror the assisted axis into both twins (slider stays canonical for the
        # apply-time read; the input is the exact-entry mirror).
        for index in range(1, 9):
            self._set_widget(f"{tag_prefix}a{index}{axis}_slider_8point", values[index - 1])
            self._set_widget(f"{tag_prefix}a{index}{axis}_input_8point", values[index - 1])

        # Repaint from the synced sliders (reads both axes).
        self._refresh_sensitivity_8point_plot(side)

    def _hydrate_axis_inversion(
        self,
        left: AxisInversion | None,
        right: AxisInversion | None,
        missing: list[str],
    ) -> None:
        if left is None:
            missing.append("axis_inversion_left")
        else:
            self._set_widget("axis_inv_left_x_checkbox", left.x_inverted)
            self._set_widget("axis_inv_left_y_checkbox", left.y_inverted)
        if right is None:
            missing.append("axis_inversion_right")
        else:
            self._set_widget("axis_inv_right_x_checkbox", right.x_inverted)
            self._set_widget("axis_inv_right_y_checkbox", right.y_inverted)

    def _hydrate_button_bindings(
        self,
        bindings: dict[ButtonSlot, ButtonMapping],
        missing: list[str],
    ) -> None:
        if not bindings:
            missing.append("button_bindings")
            return
        slot = ButtonSlot.A if ButtonSlot.A in bindings else next(iter(bindings))
        self._set_widget("binding_source_combo", BUTTON_SLOT_LABEL_BY_ENUM[slot])
        self._update_target_combo_for_slot(slot)

    def _hydrate_back_paddle_bindings(
        self,
        bindings: dict[MacroSlot, BackPaddleBinding],
    ) -> None:
        for slot, binding in (bindings or {}).items():
            self._set_widget(
                f"back_paddle_combo_{slot.name}",
                _back_paddle_target_label(binding.target),
            )

    def _hydrate_lighting(
        self,
        zones: dict[LightingZone, LightingSettings],
        missing: list[str],
    ) -> None:
        if not zones:
            missing.append("lighting_zones")
            return
        zone = LightingZone.HOME if LightingZone.HOME in zones else next(iter(zones))
        self._set_widget("lighting_zone_combo", LIGHTING_ZONE_LABEL_BY_ENUM[zone])
        self._update_lighting_widgets_for_zone(zone)

    def _set_widget(self, tag: str, value) -> None:
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, value)

    def _set_widget_enabled(self, tag: str, enabled: bool) -> None:
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, enabled=enabled)

    def _set_widget_shown(self, tag: str, show: bool) -> None:
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, show=show)

    def _render_settings_snapshot_status(self) -> None:
        is_apply_active = (
            self._apply_status_clear_after is not None
            and time.time() < self._apply_status_clear_after
        )
        logger.info(
            "_render_settings_snapshot_status called: apply_active=%s "
            "apply_text=%r snapshot=%r",
            is_apply_active,
            self._apply_status_text,
            self.last_snapshot_status,
        )
        if is_apply_active:
            if self._apply_status_text is not None:
                self._set_if_exists("settings_v2_status_text", self._apply_status_text)
                self._set_if_exists("footer_status_text", self._apply_status_text)
            return
        if self.last_snapshot_ts is not None:
            ts_str = time.strftime("%H:%M:%S", time.localtime(self.last_snapshot_ts))
            text = t("apply.read.status_with_time", status=self.last_snapshot_status, time=ts_str)
        else:
            text = self.last_snapshot_status
        self._set_if_exists("settings_v2_status_text", text)
        self._set_if_exists("footer_status_text", text)

    def _record_settings_apply_result(self, success: bool, message: str | LogEntry) -> None:
        rendered = render_log_message(message)
        record_message = message if isinstance(self.device_service, DeviceService) else rendered
        self.device_service.record_apply_result(success, record_message)
        self._update_apply_status(rendered, success)

    def list_wrapper_profiles(self) -> list[WrapperProfile]:
        try:
            result = self.wrapper_profile_store.list_profiles()
            if isinstance(result, tuple) and len(result) == 2:
                profiles, skipped = result
            else:
                profiles, skipped = result, []
            self._wrapper_profile_skipped_paths = list(skipped)
            return list(profiles)
        except Exception as exc:
            logger.warning("Failed to list wrapper profiles: %s", exc)
            self._wrapper_profile_skipped_paths = []
            return []

    def wrapper_profiles_skipped_count(self) -> int:
        return len(self._wrapper_profile_skipped_paths)

    def save_current_as_wrapper_profile(self) -> None:
        if self.last_controller_snapshot is None:
            self.save_current_as_named_wrapper_profile("")
            return
        name = (dpg.get_value("wrapper_profile_name_input") or "").strip()
        self.save_current_as_named_wrapper_profile(
            name, include_device=self._read_save_as_include_device()
        )

    def _read_save_as_include_device(self) -> bool:
        if not self._dpg_context_ready or not dpg.does_item_exist(
            SAVE_AS_INCLUDE_DEVICE_CHECKBOX
        ):
            return False
        return bool(dpg.get_value(SAVE_AS_INCLUDE_DEVICE_CHECKBOX))

    def save_current_as_named_wrapper_profile(
        self, name: str, *, include_device: bool = False
    ) -> None:
        if self.last_controller_snapshot is None:
            message = t("apply.profile.save.no_snapshot")
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return

        if not name:
            message = t("apply.profile.save.no_name")
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return

        snapshot = self._with_last_back_paddle_bindings(self.last_controller_snapshot)
        # Persist what the store actually folded in (under the lock), NOT the
        # pre-store local: a worker-thread back-paddle landing in the merge->store
        # gap is folded into the stored snapshot but not into this local, so
        # building the profile from the local would drop it from the saved file
        # even though it survives in memory.
        stored_snapshot = self._set_last_controller_snapshot(snapshot)
        if stored_snapshot is None:  # snapshot is non-None above; keep the type checker happy
            stored_snapshot = snapshot
        # Device settings (polling rate, step-size) are global, not per-profile;
        # exclude them unless the user opted in via the Save As checkbox (Device opt-in).
        saved_snapshot = (
            stored_snapshot if include_device else without_device_settings(stored_snapshot)
        )
        profile = WrapperProfile(name=name, snapshot=saved_snapshot)
        try:
            self.wrapper_profile_store.save(profile)
        except WrapperProfileError as exc:
            message = t("apply.profile.save.failed", reason=exc)
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return

        message = t("apply.profile.saved", name=name)
        self._record_settings_apply_result(True, message)
        self._refresh_footer_profile_combo(selected_name=name)
        if self._dpg_context_ready and dpg.does_item_exist("wrapper_profile_name_input"):
            dpg.set_value("wrapper_profile_name_input", "")
        if (
            self._dpg_context_ready
            and self._save_as_modal_open
            and dpg.does_item_exist("wrapper_profile_save_as_modal")
        ):
            dpg.delete_item("wrapper_profile_save_as_modal")
            self._save_as_modal_open = False
        self.rebuild_current_screen()

    def apply_selected_wrapper_profile(self) -> None:
        if self.settings_service is None:
            self.apply_named_wrapper_profile("")
            return
        name = dpg.get_value("wrapper_profile_combo")
        self.apply_named_wrapper_profile(name)

    def apply_named_wrapper_profile(self, name: str) -> None:
        if not self._zd_write_allowed_or_refuse():
            self.refresh_shell()
            return

        if self.settings_service is None:
            message = t("apply.profile.unavailable")
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return

        if not name:
            message = t("apply.profile.no_selection")
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return

        try:
            profile = self.wrapper_profile_store.load(name)
        except WrapperProfileError as exc:
            message = t("apply.profile.load_failed", reason=exc)
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return

        # A profile carrying Device overrides (polling rate / step-size) affects
        # the controller globally — confirm before applying those (Device confirm). The
        # modal needs a UI context; headless callers apply the profile as-is.
        if self._dpg_context_ready and has_device_settings(profile.snapshot):
            self._open_apply_device_confirm(name, profile)
            return

        self._apply_wrapper_profile_snapshot(name, profile.snapshot)

    # -- Restore-Points apply-pipeline helpers ------------------------------

    def _capture_restore_point_safe(
        self,
        trigger: RestorePointTrigger,
        *,
        title: str | None = None,
    ):
        """Capture a restore point without ever crashing the apply path.

        Wraps :meth:`RestorePointService.capture` in a broad try/except —
        per the "Apply coordinator interaction" design, a failed capture must
        not block the actual write. Returns the
        :class:`~zd_app.storage.restore_point_models.RestorePoint` on
        success, ``None`` on no-service or any exception.

        The shell does not pass ``cached_snapshot`` because
        ``self.last_snapshot_ts`` is wall-clock and the service's staleness
        check uses :func:`time.monotonic`; the two clocks can't be mixed
        safely across an NTP sync. Fresh-read is what the service tries
        first and is sufficient in the apply-path contexts where these
        hooks fire (the user is actively interacting with the app, so the
        controller is in a normally-responding state).
        """

        service = getattr(self, "restore_point_service", None)
        if service is None:
            return None
        identity = self._current_device_identity()
        try:
            return service.capture(
                trigger,
                title=title,
                device_identity=identity,
            )
        except Exception:
            logger.exception(
                "Restore-point capture failed for trigger %r — apply path continues",
                trigger.type,
            )
            return None

    def _record_last_applied_safe(
        self,
        profile_name: str,
        snapshot: ControllerSnapshot,
        apply_result,
        *,
        include_device: bool,
    ) -> None:
        """Best-effort Last-Applied record write (Device-vs-Profile Phase 2).

        Mirrors :meth:`_capture_restore_point_safe`'s philosophy: a storage
        failure must NEVER affect the apply result or raise into the job.
        Called from the named-profile apply and Safe Import apply jobs only —
        Restore Points and per-field writes deliberately do not record (drift
        after the apply, including restore-caused drift, is exactly what the
        Last-Applied column exists to show). Runs on the worker thread in
        threaded mode (file IO only, no DearPyGui — same as RP capture).
        """

        store = getattr(self, "last_applied_store", None)
        if store is None:
            return
        try:
            if not getattr(apply_result, "total_attempted", 0) and apply_result.failed:
                # The coordinator's no-service sentinel: nothing was attempted,
                # so there is no apply to record — a record here would
                # overclaim. (An empty snapshot with zero attempts and zero
                # failures is a real, if vacuous, apply and still records.)
                logger.debug(
                    "Skipping last-applied record for %r — apply attempted nothing",
                    profile_name,
                )
                return
            store.save(
                LastAppliedRecord(
                    profile_name=profile_name,
                    applied_at=utc_now_iso_z(),
                    include_device=include_device,
                    failed_fields=tuple(
                        failure.setting_label for failure in apply_result.failed
                    ),
                    snapshot=snapshot,
                )
            )
        except Exception:
            logger.exception(
                "Last-applied record write failed for %r — apply result unaffected",
                profile_name,
            )

    def _update_last_applied_after_retry_safe(
        self, attempted_failures, retry_result
    ) -> None:
        """Best-effort: drop retry-recovered labels from the stored record.

        The retry flow always retries the LAST apply's failures (no profile
        name matching needed), so labels that succeeded on retry can be
        removed from the record's ``failed_fields`` — their recorded values
        are now ACKed on the device. Labels still failing stay. No record →
        nothing to update (e.g. the apply predates this feature or recording
        failed). Never raises into the retry job.
        """

        store = getattr(self, "last_applied_store", None)
        if store is None:
            return
        try:
            record = store.load()
            if record is None:
                return
            still_failed = {f.setting_label for f in retry_result.failed}
            recovered = {
                f.setting_label for f in attempted_failures
            } - still_failed
            if not recovered:
                return
            remaining = tuple(
                name for name in record.failed_fields if name not in recovered
            )
            if remaining == record.failed_fields:
                return
            store.save(replace(record, failed_fields=remaining))
        except Exception:
            logger.exception(
                "Last-applied retry update failed — retry result unaffected"
            )

    def _maybe_capture_first_readable_connect(self) -> None:
        """Capture a first_readable_connect RP at most once per identity per session.

        Trigger model #1 simplification for v1: identity key is
        the device's product_string (or a sentinel if missing). Reconnecting
        the same controller within the same app session does not re-fire.

        Composition of the job-side capture and the on_done-side card render;
        the refresh HID job calls the halves separately so the capture's HID
        read stays off the render thread while the card stays on it.
        """

        self._show_first_readable_connect_card(self._capture_first_readable_connect())

    def _capture_first_readable_connect(self) -> DeviceIdentity | None:
        """Job-side half: capture the RP; HID work only, no DearPyGui.

        Returns the device identity when a NEW restore point was captured —
        the caller renders the trust-ritual card for it — else ``None``.
        """

        if self.restore_point_service is None:
            return None
        identity = self._current_device_identity()
        key = identity.product_string or "__unknown__"
        if key in self._first_connect_captured:
            return None
        rp = self._capture_restore_point_safe(
            RestorePointTrigger(
                type="first_readable_connect",
                source_label="First readable connect",
                reason=(
                    f"Created automatically after the first successful read for "
                    f"{key} this session"
                ),
            ),
        )
        if rp is None:
            return None
        self._first_connect_captured.add(key)
        return identity

    def _show_first_readable_connect_card(
        self, identity: DeviceIdentity | None
    ) -> None:
        """on_done-side half: render the trust-ritual card for a new capture.

        Surface a brief trust ritual card to the user inline with the first
        successful read. Gated by the same per-session product_string set the
        widget keeps internally, so reconnecting the same device
        never re-renders. Failure-mode: if DPG context is not yet
        ready (early-startup race), the widget no-ops but still
        records the key — no retry storm.
        """

        if identity is None:
            return
        try:
            trust_ritual_widget.show_trust_ritual_card(self, identity)
        except Exception:
            logger.exception(
                "Trust ritual card render failed for %r — first_readable_connect "
                "RP already captured, ignoring",
                identity.product_string or "__unknown__",
            )

    def _maybe_capture_before_manual_device_write(self, *, field_key: str) -> None:
        """Debounced capture before a manual write to a global device-class field.

        ``field_key`` is the field being written (e.g. ``"polling_rate"`` or
        ``"step_size"``). If a restore point for the same field was already
        captured within :data:`MANUAL_DEVICE_WRITE_RP_WINDOW_S` seconds, skip
        this capture — collapses slider drag-storms / combo bounces into a
        single pre-change snapshot per interaction window.
        """

        if self.restore_point_service is None:
            return
        now = time.monotonic()
        last = self._last_manual_rp_for_field.get(field_key)
        if last is not None and (now - last) < MANUAL_DEVICE_WRITE_RP_WINDOW_S:
            return
        rp = self._capture_restore_point_safe(
            RestorePointTrigger(
                type="before_manual_device_setting_write",
                source_label="Manual Device write",
                reason=(
                    f"Created automatically before a manual write to {field_key!r}"
                ),
            ),
        )
        if rp is not None:
            self._last_manual_rp_for_field[field_key] = now

    def _current_device_identity(self) -> DeviceIdentity:
        """Build a :class:`DeviceIdentity` from the current device-service state.

        VID/PID are hard-coded for the ZD Ultimate Legend wrapper — this is
        a single-controller-family app and the existing device_service
        already filters by ``VID_413D&PID_2104``. Confidence is "readable"
        when both product_string and firmware are known, "partial" when one
        is missing, "unknown" when both are.
        """

        state = self.device_service.state
        product = state.product_name if state.product_name else None
        firmware = state.firmware_version if state.firmware_version not in ("Unknown", "") else None
        if product and firmware:
            confidence = IdentityConfidence.READABLE
        elif product or firmware:
            confidence = IdentityConfidence.PARTIAL
        else:
            confidence = IdentityConfidence.UNKNOWN
        return DeviceIdentity(
            vid="413D",
            pid="2104",
            product_string=product,
            firmware_version=firmware,
            identity_confidence=confidence,
        )

    def manual_save_restore_point(self, *, title: str | None = None):
        """Create a manual-trigger restore point. Wired to the Diagnostics
        "Save Restore Point" button (RPU5).

        Returns the captured :class:`RestorePoint` or ``None`` on failure.
        The caller is responsible for surfacing a toast / log line.
        """

        # The capture is a fresh HID read — refuse it while a threaded job
        # is mid-flight (footer status carries the busy reason; the caller's
        # generic failure toast is the per-screen echo).
        if not self._hid_available_or_refuse():
            return None
        if self.restore_point_service is None:
            return None
        return self._capture_restore_point_safe(
            RestorePointTrigger(
                type="manual",
                source_label="Manual",
                reason="Created from the Restore Points 'Save Restore Point' button",
            ),
            title=title,
        )

    def _apply_wrapper_profile_resolved(
        self, name: str, profile: WrapperProfile, *, include_device: bool
    ) -> None:
        """Apply a profile after the Device-override choice (Device confirm).

        ``include_device`` keeps the Device fields; otherwise they are filtered
        out so the apply touches Profile settings only.
        """

        if self._dpg_context_ready and dpg.does_item_exist(APPLY_DEVICE_CONFIRM_MODAL):
            dpg.delete_item(APPLY_DEVICE_CONFIRM_MODAL)
        snapshot = (
            profile.snapshot
            if include_device
            else without_device_settings(profile.snapshot)
        )

        def job() -> None:
            if include_device:
                # Trigger model #3 — capture before applying a
                # profile whose payload includes Device settings (polling
                # rate / step size). Skipped when include_device=False
                # because "Profile settings only" doesn't change the device
                # class. The capture is its own HID read, hence job-side.
                self._capture_restore_point_safe(
                    RestorePointTrigger(
                        type="before_profile_apply_with_device_settings",
                        source_label="Profile apply (with Device settings)",
                        reason=(
                            f"Created automatically before applying profile "
                            f"{name!r} with Device settings"
                        ),
                    ),
                    title=f"Before Device settings apply — {name}",
                )
            # Nested jobbed flow: under the sync default this runs inline; on
            # a worker it executes here and its DPG on_done drains in _tick.
            self._apply_wrapper_profile_snapshot(
                name, snapshot, include_device=include_device
            )

        def on_done(outcome) -> None:
            if isinstance(outcome, BaseException):
                raise outcome  # today's path has no catch at this level

        self._run_hid_job(job, on_done)

    def _apply_wrapper_profile_snapshot(
        self, name: str, snapshot: ControllerSnapshot, *, include_device: bool = True
    ) -> None:
        if not self._zd_write_allowed_or_refuse():
            self.refresh_shell()
            return

        # ``include_device=False`` flows through the post-write
        # refresh_from_controller so the step-size slider and polling-rate
        # combo do not snap to the post-write read; the user's live-write value
        # is what they expect to see after "Apply profile only".
        logger.debug(
            "_apply_wrapper_profile_snapshot: name=%r include_device=%s "
            "snapshot.step_size=%s snapshot.polling_rate=%s",
            name,
            include_device,
            snapshot.step_size,
            snapshot.polling_rate,
        )

        # This named profile is now the "loaded"/active one — the profile whose
        # stored step_size a later live-write would diverge from (Fix B nudge
        # target). Applying a different profile clears any stale save nudge.
        self._active_wrapper_profile_name = name
        self._clear_step_size_save_nudge()

        def job() -> ApplyResult:
            apply_result = self._apply_snapshot_to_controller(snapshot)
            # Phase 2: persist what was just sent (best-effort, never affects
            # the apply). ``snapshot`` is post any device-field filtering, so
            # the record holds exactly what the coordinator received.
            self._record_last_applied_safe(
                name, snapshot, apply_result, include_device=include_device
            )

            self._record_wear_event(
                PROFILE_APPLY,
                summary=f"Applied profile: {name}",
                details={
                    "profile_name": name,
                    "device_settings_included": include_device,
                    "succeeded": apply_result.succeeded,
                    "total_attempted": apply_result.total_attempted,
                    "failed_count": len(apply_result.failed),
                },
            )

            # Firmware wants a quiet interval after a write burst before the
            # next feature-report read batch (same rationale as the apply
            # coordinator's per-field trailers) — without it the first
            # post-apply read can hit a transient HID timeout (hardware,
            # 2026-06-10).
            time.sleep(POST_APPLY_READ_SETTLE_S)
            # The post-apply read is itself a jobbed flow: inline under the
            # sync default, inline-on-this-worker when threaded (its DPG
            # hydration on_done drains on the render thread either way).
            self.refresh_from_controller(include_device=include_device)
            return apply_result

        def on_done(outcome) -> None:
            if isinstance(outcome, BaseException):
                raise outcome  # today's path has no catch at this level
            apply_result: ApplyResult = outcome
            self._last_apply_result = apply_result
            if not apply_result.failed:
                if apply_result.retry_recoveries:
                    message = _make_log_entry(
                        "apply.profile.success_recovered",
                        name=name,
                        n=apply_result.total_attempted,
                        k=apply_result.retry_recoveries,
                    )
                else:
                    message = _make_log_entry(
                        "apply.profile.success",
                        name=name,
                        n=apply_result.total_attempted,
                    )
                self._record_settings_apply_result(True, message)
            else:
                message = _make_log_entry(
                    "apply.profile.partial",
                    name=name,
                    m=apply_result.succeeded,
                    n=apply_result.total_attempted,
                    k=len(apply_result.failed),
                )
                self._record_settings_apply_result(False, message)
                self._show_apply_failure_modal(apply_result)

        self._run_hid_job(job, on_done)

    def _apply_device_confirm_body(self, profile: WrapperProfile) -> str:
        """Build the Device-confirm modal body listing the ACTUAL device values
        the "profile + device settings" apply will WRITE.

        Lists step_size and polling_rate (whichever the profile carries) as
        ``current -> new`` when the live device value is known, otherwise
        ``-> new``, so the user can SEE which controller-global values change
        before consenting. The pre-2026-06-24 body was generic and hid the
        values, so applying a profile saved with step_size 1 silently clobbered
        a manually-set 73 with no indication — this surfaces exactly that.
        """

        snapshot = profile.snapshot
        current = self.last_controller_snapshot
        rows: list[str] = []

        new_step = getattr(snapshot, "step_size", None)
        if new_step is not None:
            cur_step = getattr(current, "step_size", None) if current is not None else None
            if cur_step is not None:
                rows.append(
                    t(
                        "apply.profile.device_confirm.row_step_size",
                        current=cur_step,
                        new=new_step,
                    )
                )
            else:
                rows.append(
                    t("apply.profile.device_confirm.row_step_size_new_only", new=new_step)
                )

        new_poll = getattr(snapshot, "polling_rate", None)
        if new_poll is not None:
            new_poll_label = POLLING_RATE_LABEL_BY_ENUM.get(
                new_poll, t("shell.polling_rate.unknown")
            )
            cur_poll = getattr(current, "polling_rate", None) if current is not None else None
            if cur_poll is not None:
                rows.append(
                    t(
                        "apply.profile.device_confirm.row_polling",
                        current=POLLING_RATE_LABEL_BY_ENUM.get(
                            cur_poll, t("shell.polling_rate.unknown")
                        ),
                        new=new_poll_label,
                    )
                )
            else:
                rows.append(
                    t(
                        "apply.profile.device_confirm.row_polling_new_only",
                        new=new_poll_label,
                    )
                )

        intro = t("apply.profile.device_confirm.body_intro")
        outro = t("apply.profile.device_confirm.body_outro")
        bullets = "\n".join(f"  - {row}" for row in rows)
        return f"{intro}\n\n{bullets}\n\n{outro}"

    def _open_apply_device_confirm(self, name: str, profile: WrapperProfile) -> None:
        if not self._dpg_context_ready:
            return
        if dpg.does_item_exist(APPLY_DEVICE_CONFIRM_MODAL):
            dpg.delete_item(APPLY_DEVICE_CONFIRM_MODAL)

        with dpg.window(
            tag=APPLY_DEVICE_CONFIRM_MODAL,
            label=t("apply.profile.device_confirm.title"),
            modal=True,
            no_close=False,
            no_resize=True,
            width=520,
            height=240,
        ):
            dpg.add_text(self._apply_device_confirm_body(profile), wrap=480)
            dpg.add_spacer(height=10)
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label=t("apply.profile.device_confirm.profile_only"),
                    width=180,
                    callback=lambda: self._apply_wrapper_profile_resolved(
                        name, profile, include_device=False
                    ),
                    tag="apply_device_confirm_profile_only_button",
                )
                dpg.add_button(
                    label=t("apply.profile.device_confirm.profile_and_device"),
                    width=230,
                    callback=lambda: self._apply_wrapper_profile_resolved(
                        name, profile, include_device=True
                    ),
                    tag="apply_device_confirm_profile_and_device_button",
                )
                dpg.add_button(
                    label=t("actions.cancel"),
                    width=100,
                    callback=lambda: dpg.delete_item(APPLY_DEVICE_CONFIRM_MODAL),
                    tag="apply_device_confirm_cancel_button",
                )

    def _apply_snapshot_to_controller(self, snapshot: ControllerSnapshot) -> ApplyResult:
        return self._apply_coordinator.apply_snapshot(
            snapshot,
            on_back_paddle_apply=self._remember_back_paddle_binding,
        )

    def _show_crash_review_modal_if_any(self) -> None:
        """Surface unread crash reports from prior runs in a modal dialog.

        Called once near the end of ``run()`` startup. If no unread reports
        exist (or crash_reporter wasn't installed in this process), the
        method is a no-op and the modal is never built.
        """

        if not self._dpg_context_ready:
            return
        try:
            from zd_app.services import crash_reporter
        except ImportError:
            return

        user_data_dir = self.settings_store.path.parent
        since: datetime.datetime | None = None
        if self.settings.last_reviewed_crash_timestamp:
            try:
                since = datetime.datetime.fromisoformat(
                    self.settings.last_reviewed_crash_timestamp.replace("Z", "+00:00")
                )
            except ValueError:
                since = None

        try:
            unread = crash_reporter.list_unread_crash_reports(user_data_dir, since=since)
        except Exception:  # noqa: BLE001 - never let the modal init fail the app launch
            logger.exception("crash review: list_unread_crash_reports failed")
            return

        if not unread:
            return

        most_recent = unread[-1]
        try:
            preview_body = most_recent.read_text(encoding="utf-8")
        except OSError:
            preview_body = ""

        try:
            mtime = datetime.datetime.fromtimestamp(
                most_recent.stat().st_mtime, tz=datetime.timezone.utc
            )
            time_label = mtime.astimezone().strftime("%Y-%m-%d %H:%M")
        except OSError:
            time_label = "?"

        crashes_dir = user_data_dir / "crashes"
        unread_paths = list(unread)

        def _mark_all_reviewed() -> None:
            try:
                crash_reporter.mark_crashes_reviewed(user_data_dir, unread_paths)
            except Exception:  # noqa: BLE001
                logger.exception("crash review: mark_crashes_reviewed failed")
            self.settings.last_reviewed_crash_timestamp = datetime.datetime.now(
                datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                self.settings_store.save(self.settings)
            except OSError:
                logger.exception("crash review: settings save failed")
            if dpg.does_item_exist("crash_review_modal"):
                dpg.delete_item("crash_review_modal")

        def _on_save() -> None:
            try:
                if hasattr(os, "startfile"):
                    os.startfile(str(crashes_dir))  # type: ignore[attr-defined]
            except OSError:
                logger.exception("crash review: could not open crashes dir")
            _mark_all_reviewed()

        if dpg.does_item_exist("crash_review_modal"):
            dpg.delete_item("crash_review_modal")

        with dpg.window(
            tag="crash_review_modal",
            label=t("crash.review.title"),
            modal=True,
            no_close=False,
            no_resize=True,
            width=720,
            height=520,
            on_close=lambda *_args: _mark_all_reviewed(),
        ):
            dpg.add_text(
                t("crash.review.body").format(time=time_label),
                wrap=680,
            )
            dpg.add_spacer(height=6)
            dpg.add_input_text(
                multiline=True,
                readonly=True,
                default_value=preview_body,
                width=680,
                height=320,
                tag="crash_review_preview",
            )
            if len(unread_paths) > 1:
                dpg.add_text(
                    t("crash.review.preview_more").format(n=len(unread_paths) - 1),
                    wrap=680,
                )
            dpg.add_spacer(height=8)
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label=t("crash.review.save_button"),
                    width=160,
                    callback=lambda *args: _on_save(),
                    tag="crash_review_save_button",
                )
                github_btn = dpg.add_button(
                    label=t("crash.review.github_button"),
                    width=160,
                    enabled=False,
                    tag="crash_review_github_button",
                )
                with dpg.tooltip(github_btn):
                    dpg.add_text(t("crash.review.github_disabled"), wrap=320)
                send_btn = dpg.add_button(
                    label=t("crash.review.send_button"),
                    width=180,
                    enabled=False,
                    tag="crash_review_send_button",
                )
                with dpg.tooltip(send_btn):
                    dpg.add_text(t("crash.review.send_disabled"), wrap=320)
                dpg.add_button(
                    label=t("crash.review.dismiss_button"),
                    width=110,
                    callback=lambda *args: _mark_all_reviewed(),
                    tag="crash_review_dismiss_button",
                )

    def _request_app_exit(self) -> None:
        """Stop the render loop so :meth:`run` falls through to clean shutdown.

        Used by the first-run acknowledgment gate's decline / title-bar-close
        paths: a user who does not affirmatively accept must not proceed into
        the app. While the render loop is live — the only time the gate is
        interactive — this ends it, and :meth:`run` then runs its normal
        teardown (session-end event, context destroy). Wrapped so a stop
        failure can never wedge the callback. Headless callers (tests) patch
        ``dpg.stop_dearpygui``; calling it without a live viewport is unsafe.
        """

        try:
            dpg.stop_dearpygui()
        except Exception:  # noqa: BLE001 - never let teardown failure wedge the gate
            logger.exception("first-run ack: stop_dearpygui failed")

    def _show_first_run_acknowledgment_modal_if_needed(self) -> bool:
        """Show the one-time first-run acknowledgment (clickwrap) gate.

        Returns True when the gate modal was built (acknowledgment still
        pending), False when it was skipped — the user already acknowledged
        on a prior run, or no DPG context is up (headless / sync paths). The
        caller in :meth:`run` uses the return value to suppress the
        crash-review modal for the one launch the gate shows on: two
        ``modal=True`` windows never render stacked (DPG modal law, benched
        in tools/diag_dpg_modal_thread_visibility.py + docs/ARCHITECTURE.md).

        Acceptance is the gate. The "I understand and accept" button sets
        ``settings.first_run_acknowledged`` and persists it through the
        settings store, so the gate shows exactly once per install and never
        nags again. The decline button and the title-bar close both treat the
        launch as not-accepted — they leave the flag unset (so the gate
        returns next launch) and exit via :meth:`_request_app_exit` rather
        than proceeding into the app. ``dpg.delete_item`` does not fire
        ``on_close``, so accept's teardown never trips the decline path.

        The body reuses ``about.zd_disclaimer`` — the single source of truth
        for the ZD-required disclaimer — and adds concise risk language. It
        is ``modal=True`` so it blocks the app behind it; the per-session
        trust card is ``modal=False`` and does not occupy DPG's single modal
        slot, so the two never fight over it and need no sequencing here.

        Testable without a render loop: the method only creates the window
        and returns (it never spins a loop), and the accept/decline callbacks
        are plain closures the suite invokes directly.
        """

        if self.settings.first_run_acknowledged:
            return False
        if not self._dpg_context_ready:
            return False

        def _accept() -> None:
            self.settings.first_run_acknowledged = True
            try:
                self.settings_store.save(self.settings)
            except OSError:
                logger.exception("first-run ack: settings save failed")
            if dpg.does_item_exist("first_run_ack_modal"):
                dpg.delete_item("first_run_ack_modal")

        def _decline() -> None:
            # Not accepted: leave first_run_acknowledged unset so the gate
            # shows again on a future launch, and do not proceed into the app.
            if dpg.does_item_exist("first_run_ack_modal"):
                dpg.delete_item("first_run_ack_modal")
            self._request_app_exit()

        if dpg.does_item_exist("first_run_ack_modal"):
            dpg.delete_item("first_run_ack_modal")

        with dpg.window(
            tag="first_run_ack_modal",
            label=t("first_run.title"),
            modal=True,
            no_close=False,
            no_resize=True,
            no_collapse=True,
            width=660,
            height=540,
            on_close=lambda *_args: _decline(),
        ):
            dpg.add_text(t("first_run.intro"), wrap=620, tag="first_run_ack_intro_text")
            dpg.add_spacer(height=8)
            dpg.add_text(
                t("about.zd_disclaimer"),
                wrap=620,
                tag="first_run_ack_disclaimer_text",
            )
            dpg.add_spacer(height=10)
            dpg.add_text(
                t("first_run.risk.writes"),
                wrap=600,
                bullet=True,
                tag="first_run_ack_risk_writes_text",
            )
            dpg.add_text(
                t("first_run.risk.hardware"),
                wrap=600,
                bullet=True,
                tag="first_run_ack_risk_hardware_text",
            )
            dpg.add_text(
                t("first_run.risk.reversible"),
                wrap=600,
                bullet=True,
                tag="first_run_ack_risk_reversible_text",
            )
            dpg.add_text(
                t("first_run.risk.as_is"),
                wrap=600,
                bullet=True,
                tag="first_run_ack_risk_as_is_text",
            )
            dpg.add_spacer(height=14)
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label=t("first_run.accept"),
                    width=260,
                    callback=lambda *_args: _accept(),
                    tag="first_run_ack_accept_button",
                )
                dpg.add_button(
                    label=t("first_run.decline"),
                    width=180,
                    callback=lambda *_args: _decline(),
                    tag="first_run_ack_decline_button",
                )
        return True

    def _show_apply_failure_modal(self, apply_result: ApplyResult) -> None:
        if not self._dpg_context_ready or not apply_result.failed:
            return
        if dpg.does_item_exist("apply_failure_modal"):
            dpg.delete_item("apply_failure_modal")

        with dpg.window(
            tag="apply_failure_modal",
            label=t("apply.partial_failure_title"),
            modal=True,
            no_close=False,
            no_resize=True,
            width=520,
            height=300,
        ):
            subtitle = t("apply.partial_failure_subtitle").format(
                count=len(apply_result.failed),
            )
            dpg.add_text(subtitle, wrap=480)
            dpg.add_spacer(height=8)
            with dpg.child_window(height=150, border=True):
                for failure in apply_result.failed:
                    dpg.add_text(
                        f"- {_format_apply_failure_row(failure)}",
                        wrap=460,
                    )
            dpg.add_spacer(height=8)
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label=t("apply.retry_failed_button"),
                    width=150,
                    callback=lambda *args: self._retry_failed_settings(
                        list(apply_result.failed),
                    ),
                )
                dpg.add_button(
                    label=t("apply.dismiss_button"),
                    width=110,
                    callback=lambda *args: dpg.delete_item(
                        "apply_failure_modal",
                    ),
                )

    def _retry_failed_settings(
        self,
        failures: list[ApplyFailure] | None = None,
    ) -> ApplyResult | None:
        """Retry the failed subset of the last apply as a HID job.

        Jobbed like the profile apply: the retry write burst
        + post-burst settle run as the DPG-free job; status recording, the
        failure-modal re-show, and the chained refresh are the on_done half.
        Sync mode returns the :class:`ApplyResult` inline (pre-seam
        contract); threaded mode returns ``None`` immediately and reports
        through on_done when the completion drains.
        """

        retry_failures = list(
            failures
            if failures is not None
            else (self._last_apply_result.failed if self._last_apply_result else [])
        )
        if not retry_failures:
            return ApplyResult(total_attempted=0)
        # Refuse BEFORE touching the modal: the failure modal stays open so
        # the user can re-click Retry once the in-flight job drains (the
        # busy banner is the refusal echo).
        if not self._hid_available_or_refuse():
            return None

        if self._dpg_context_ready and dpg.does_item_exist("apply_failure_modal"):
            dpg.delete_item("apply_failure_modal")

        def job() -> ApplyResult:
            retry_result = self._apply_coordinator.retry_failures(retry_failures)
            # Phase 2: labels that just recovered are no longer "failed at
            # apply" — amend the stored Last-Applied record (best-effort).
            self._update_last_applied_after_retry_safe(retry_failures, retry_result)
            # Same post-write-burst quiet interval as
            # _apply_wrapper_profile_snapshot (see POST_APPLY_READ_SETTLE_S);
            # the chained refresh starts only after this settle.
            time.sleep(POST_APPLY_READ_SETTLE_S)
            return retry_result

        completed: list[ApplyResult] = []

        def on_done(outcome) -> None:
            if isinstance(outcome, BaseException):
                raise outcome  # today's path has no catch at this level
            retry_result: ApplyResult = outcome
            completed.append(retry_result)
            self._last_apply_result = retry_result
            if retry_result.failed:
                message = t(
                    "apply.retry.partial",
                    m=retry_result.succeeded,
                    n=retry_result.total_attempted,
                    k=len(retry_result.failed),
                )
                self._record_settings_apply_result(False, message)
                self._show_apply_failure_modal(retry_result)
            else:
                message = t(
                    "apply.retry.success",
                    n=retry_result.total_attempted,
                )
                self._record_settings_apply_result(True, message)
            # Chained post-retry refresh, itself a jobbed read: the drain
            # clears the busy flag before invoking on_done, so this starts
            # its own job instead of being refused by its predecessor.
            self.refresh_from_controller()

        self._run_hid_job(job, on_done)
        return completed[0] if completed else None

    def confirm_delete_wrapper_profile(self) -> None:
        name = dpg.get_value("wrapper_profile_combo")
        self.confirm_delete_named_wrapper_profile(name)

    def confirm_delete_named_wrapper_profile(self, name: str) -> None:
        if not name:
            message = t("apply.profile.delete.no_selection")
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return

        def delete_callback(_sender, _app_data, user_data):
            self._delete_wrapper_profile_confirmed(user_data)

        # Modal-swap hop: the delete-confirm popup is frequently opened right
        # after another modal was up (e.g. a Save + Apply confirm/result), and a
        # modal created in the same render pass another modal was showing/torn
        # down exists but never becomes interactive (benched DPG modal law,
        # 2026-06-11 — tools/diag_dpg_modal_thread_visibility.py). Inline
        # creation here left the Confirm button dead and the delete callback
        # never fired. Route the create through _defer_modal_swap so any stale
        # popup drains in one pass and the fresh one is created a frame later,
        # guaranteeing the rendered frame between teardown and create. Modeled on
        # the Safe-Import confirm swap (safe_import_request_apply_to_controller).
        def open_fn() -> None:
            with dpg.window(
                label=t("footer.delete_confirm.title"),
                modal=True,
                no_close=False,
                no_resize=True,
                width=380,
                height=140,
                tag="wrapper_profile_delete_popup",
            ):
                dpg.add_text(t("footer.delete_confirm.body", name=name), wrap=350)
                dpg.add_spacer(height=8)
                with dpg.group(horizontal=True):
                    dpg.add_button(
                        label=t("actions.cancel"),
                        width=100,
                        callback=lambda: dpg.delete_item("wrapper_profile_delete_popup"),
                    )
                    dpg.add_button(
                        label=t("footer.delete_confirm.confirm"),
                        width=100,
                        user_data=name,
                        callback=delete_callback,
                        tag="wrapper_profile_delete_confirm_button",
                    )

        self._defer_modal_swap(
            open_fn,
            delete_tags=("wrapper_profile_delete_popup",),
            key="wrapper_profile_delete",
        )

    def _open_save_as_modal(self) -> None:
        if dpg.does_item_exist("wrapper_profile_save_as_modal"):
            dpg.delete_item("wrapper_profile_save_as_modal")
        self._save_as_modal_open = True
        with dpg.window(
            tag="wrapper_profile_save_as_modal",
            label=t("footer.save_as_modal.title"),
            modal=True,
            no_close=False,
            no_resize=True,
            width=460,
            height=210,
        ):
            dpg.add_input_text(
                tag="wrapper_profile_name_input",
                label=t("footer.save_as_modal.name_input"),
                width=260,
            )
            dpg.add_spacer(height=8)
            dpg.add_checkbox(
                tag=SAVE_AS_INCLUDE_DEVICE_CHECKBOX,
                label=t("footer.save_as_modal.include_device"),
                default_value=False,
            )
            dpg.add_text(
                t("footer.save_as_modal.include_device_hint"),
                color=self.COLORS["muted"],
                wrap=420,
            )
            dpg.add_spacer(height=8)
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label=t("footer.save_as_modal.save"),
                    width=100,
                    callback=lambda: self.save_current_as_wrapper_profile(),
                )
                dpg.add_button(
                    label=t("actions.cancel"),
                    width=100,
                    callback=lambda: dpg.delete_item("wrapper_profile_save_as_modal"),
                )

    def _delete_wrapper_profile_confirmed(self, name: str) -> None:
        if self._dpg_context_ready and dpg.does_item_exist("wrapper_profile_delete_popup"):
            dpg.delete_item("wrapper_profile_delete_popup")

        deleted = self.wrapper_profile_store.delete(name)
        if not deleted:
            message = t("apply.profile.delete.not_found", name=name)
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return

        message = t("apply.profile.deleted", name=name)
        self._record_settings_apply_result(True, message)
        self._refresh_footer_profile_combo()
        self.rebuild_current_screen()

    def _refresh_footer_profile_combo(self, selected_name: str | None = None) -> None:
        if (
            not self._dpg_context_ready
            or not self._footer_profile_combo_ready
            or not dpg.does_item_exist("wrapper_profile_combo")
        ):
            return

        profiles = self.list_wrapper_profiles()
        profile_names = [profile.name for profile in profiles]
        try:
            current_value = dpg.get_value("wrapper_profile_combo")
            dpg.configure_item("wrapper_profile_combo", items=profile_names)
            if selected_name and selected_name in profile_names:
                dpg.set_value("wrapper_profile_combo", selected_name)
            elif current_value in profile_names:
                dpg.set_value("wrapper_profile_combo", current_value)
            else:
                dpg.set_value("wrapper_profile_combo", profile_names[0] if profile_names else "")
        except Exception:
            logger.debug("Footer profile combo refresh skipped", exc_info=True)

    def _update_apply_status(self, message: str, success: bool) -> None:
        if not self._dpg_context_ready:
            logger.info("_update_apply_status skipped: context not ready")
            return
        try:
            has_settings_status = dpg.does_item_exist("settings_v2_status_text")
            has_footer_status = dpg.does_item_exist("footer_status_text")
            if not has_settings_status and not has_footer_status:
                logger.info("_update_apply_status skipped: status widgets do not exist")
                return
        except Exception:
            logger.info(
                "_update_apply_status skipped: widget lookup failed",
                exc_info=True,
            )
            return
        self._apply_status_text = _apply_banner_message(message, success)
        self._apply_status_clear_after = time.time() + 5.0
        logger.info("_update_apply_status setting widget to: %s", self._apply_status_text)
        self._set_if_exists("settings_v2_status_text", self._apply_status_text)
        self._set_if_exists("footer_status_text", self._apply_status_text)

    def _tick_settings_service_tasks(self, now: float) -> None:
        # A reconnect that landed mid-job deferred its service restart (see
        # _tick's reconnect branch). Consume it only when no job is in
        # flight, mirroring the hydration deferral below: stop() closes the
        # cached handles so the next read re-opens against the new
        # connection, and the hydration request rides the same tick.
        if (
            self._needs_service_restart
            and self.settings_service is not None
            and not self._hid_job_in_flight
        ):
            self._needs_service_restart = False
            self.settings_service.stop()
            self._needs_hydration = True

        # While a HID job is in flight, DEFER (don't consume) a pending
        # hydration request: consuming it would start a refresh job that gets
        # refused, silently dropping a late-connect/reconnect hydration. The
        # flag survives to a later tick. Sync mode never observes the busy
        # flag, so this guard is invisible to the pre-seam behavior.
        if (
            self._needs_hydration
            and self.settings_service is not None
            and not self._hid_job_in_flight
        ):
            self._needs_hydration = False
            if self._canonical_screen_id(self.current_screen) in {"controller", "settings"}:
                self.rebuild_current_screen()
            self.refresh_from_controller()

        if (
            self._apply_status_clear_after is not None
            and now >= self._apply_status_clear_after
        ):
            self._apply_status_text = None
            self._apply_status_clear_after = None
            self._render_settings_snapshot_status()

    def _update_target_combo_for_slot(self, slot: ButtonSlot) -> None:
        if self.last_controller_snapshot is None:
            return
        mapping = self.last_controller_snapshot.button_bindings.get(slot)
        if mapping is None or mapping.target_kind != 0x01:
            return
        try:
            target = ControllerButtonTarget(mapping.target_value)
        except ValueError:
            return
        label = BUTTON_TARGET_LABEL_BY_ENUM.get(target)
        if label:
            self._set_widget("binding_target_combo", label)

    def _with_last_back_paddle_bindings(
        self,
        snapshot: ControllerSnapshot,
    ) -> ControllerSnapshot:
        # Lock: this runs on the worker thread (read job) AND the render thread
        # (Save As); guard the shared cache against a concurrent in-place write
        # from _remember_back_paddle_binding.
        with self._last_back_paddle_bindings_lock:
            if snapshot.back_paddle_bindings:
                self._last_back_paddle_bindings = dict(snapshot.back_paddle_bindings)
                return snapshot
            if not self._last_back_paddle_bindings:
                return snapshot
            return replace(
                snapshot,
                back_paddle_bindings=dict(self._last_back_paddle_bindings),
            )

    def _remember_back_paddle_binding(
        self,
        slot: MacroSlot,
        binding: BackPaddleBinding,
    ) -> None:
        # Both the cache write AND the snapshot read-modify-write run under the
        # SAME lock so the whole update is atomic against a plain
        # ``last_controller_snapshot`` overwrite (refresh on_done / Save-As, both
        # via _set_last_controller_snapshot). This callback fires on the HID
        # worker thread (the apply-coordinator's on_back_paddle_apply); leaving
        # the snapshot RMW unlocked let a concurrent overwrite land between its
        # read and write — a bidirectional lost update (GIL-atomic, so no crash,
        # but the in-memory snapshot corrupts).
        with self._last_back_paddle_bindings_lock:
            self._last_back_paddle_bindings[slot] = binding
            snapshot = self.last_controller_snapshot
            if snapshot is not None:
                merged = dict(snapshot.back_paddle_bindings or {})
                merged[slot] = binding
                self.last_controller_snapshot = replace(
                    snapshot,
                    back_paddle_bindings=merged,
                )

    def _remember_button_binding(
        self, slot: ButtonSlot, mapping: ButtonMapping
    ) -> None:
        """Fold a just-applied button binding into the cached snapshot.

        ``apply_button_binding`` wrote ``mapping`` to the device, so it is the
        authoritative current value for ``slot`` — record it WITHOUT a device
        read so the read-only Current Bindings display can refresh in place.
        Mirrors :meth:`_remember_back_paddle_binding`'s locked
        read-modify-write so a concurrent snapshot overwrite (refresh on_done /
        Save-As) can't drop it.

        No-op when nothing has been read yet (``last_controller_snapshot is
        None``): we deliberately don't fabricate a whole snapshot from a single
        known slot — the Current Bindings list stays in its honest "not read
        yet" state.
        """

        with self._last_back_paddle_bindings_lock:
            snapshot = self.last_controller_snapshot
            if snapshot is None:
                return
            merged = dict(snapshot.button_bindings or {})
            merged[slot] = mapping
            self.last_controller_snapshot = replace(
                snapshot,
                button_bindings=merged,
            )

    def _set_last_controller_snapshot(
        self, snapshot: ControllerSnapshot | None
    ) -> ControllerSnapshot | None:
        """Overwrite the cached controller snapshot under the back-paddle lock.

        ``last_controller_snapshot`` is written from BOTH threads — the render
        thread (refresh on_done, Save-As) and the HID worker thread (the
        apply-coordinator's ``on_back_paddle_apply`` callback, via
        :meth:`_remember_back_paddle_binding`, which read-modify-writes the
        back-paddle bindings). Serializing every write on the same lock that
        guards that RMW makes the RMW atomic against a plain overwrite here, so
        neither update is lost. Does NOT change the locking of
        ``_last_back_paddle_bindings`` itself.

        Before storing, fold any cached back-paddle bindings the incoming
        snapshot is *missing* back in. Both store call sites first merge the
        cache into the snapshot in a SEPARATE locked step
        (:meth:`_with_last_back_paddle_bindings`), then call this store — two
        locked ops with a gap between them. A worker-thread
        :meth:`_remember_back_paddle_binding` landing in that gap adds its paddle
        to the cache (and to ``last_controller_snapshot``) under this lock, but
        the plain overwrite here would then drop it, because the snapshot was
        built before that paddle existed. Re-reading the cache here closes the
        window: the snapshot's own bindings win per slot and the cache only
        fills slots the snapshot lacks, so a concurrently-added paddle survives
        and nothing is resurrected (the preceding merge reconciles the cache to
        the snapshot, so a binding that was *removed* is already gone from the
        cache as well).

        Returns the snapshot that was actually stored (post-fold). A caller that
        must *persist* exactly what landed in ``last_controller_snapshot`` — i.e.
        Save-As building a profile — has to use this return value rather than its
        own pre-store local, since a paddle folded in here (one that landed in the
        merge->store gap) is absent from that local and would otherwise be dropped
        from the saved file even though it survives in memory.
        """

        with self._last_back_paddle_bindings_lock:
            if snapshot is not None and self._last_back_paddle_bindings:
                merged = dict(snapshot.back_paddle_bindings or {})
                for slot, binding in self._last_back_paddle_bindings.items():
                    merged.setdefault(slot, binding)
                if merged != (snapshot.back_paddle_bindings or {}):
                    snapshot = replace(snapshot, back_paddle_bindings=merged)
            self.last_controller_snapshot = snapshot
        return snapshot

    def _update_lighting_widgets_for_zone(self, zone: LightingZone) -> None:
        if self.last_controller_snapshot is None:
            return
        settings_obj = self.last_controller_snapshot.lighting_zones.get(zone)
        if settings_obj is None:
            return
        self._set_widget("lighting_on_checkbox", settings_obj.light_on)
        label = LIGHTING_MODE_LABEL_BY_ENUM.get(settings_obj.mode)
        if label:
            self._set_widget("lighting_mode_combo", label)
        self._set_widget("lighting_brightness_slider", settings_obj.brightness_byte)
        self._set_widget("lighting_r_slider", settings_obj.color.r)
        self._set_widget("lighting_g_slider", settings_obj.color.g)
        self._set_widget("lighting_b_slider", settings_obj.color.b)

    def on_binding_source_changed(self, label: str) -> None:
        slot = BUTTON_SLOT_BY_LABEL.get(label)
        if slot:
            self._update_target_combo_for_slot(slot)

    def on_lighting_zone_changed(self, label: str) -> None:
        zone = LIGHTING_ZONE_BY_LABEL.get(label)
        if zone:
            self._update_lighting_widgets_for_zone(zone)

    def refresh_current_screen(self) -> None:
        state = self.device_service.state
        screen = self._canonical_screen_id(self.current_screen)
        if screen == "home" and dpg.does_item_exist("home_recent_events"):
            dpg.set_value("home_recent_events", "\n".join(self.device_service.recent_events(10)) or t("home.recent.empty"))
        elif screen == "legacy_buttons":
            # Live button-press state came from the diagnostics input poller,
            # which has been removed (the wrapper no longer does continuous
            # real-time input monitoring). The legacy "Live" column now stays
            # Idle; use an external gamepad tester to verify presses.
            pressed: set[str] = set()
            for mapping in self.profile_service.current_draft.button_mappings:
                live_tag = f"buttons_live_{mapping.physical_input_id}"
                action_tag = f"buttons_action_{mapping.physical_input_id}"
                dirty_tag = f"buttons_dirty_{mapping.physical_input_id}"
                if dpg.does_item_exist(action_tag):
                    dpg.set_value(action_tag, mapping.action)
                if dpg.does_item_exist(dirty_tag):
                    is_dirty = self.profile_service.is_button_dirty(mapping.physical_input_id)
                    dpg.set_value(dirty_tag, "Dirty" if is_dirty else "Clean")
                    dpg.configure_item(dirty_tag, color=self.COLORS["warn"] if is_dirty else self.COLORS["muted"])
                if dpg.does_item_exist(live_tag):
                    is_pressed = mapping.physical_label in pressed
                    dpg.set_value(live_tag, "Pressed" if is_pressed else "Idle")
                    dpg.configure_item(live_tag, color=self.COLORS["good"] if is_pressed else self.COLORS["muted"])
            selected = self.selected_button_mapping()
            if selected is not None and dpg.does_item_exist("buttons_editor_current"):
                dpg.set_value("buttons_editor_current", selected.action)
        elif screen == "legacy_sticks" and dpg.does_item_exist("sticks_current_values"):
            settings_obj = self.profile_service.current_draft.left_stick if self.selected_stick_side == "left" else self.profile_service.current_draft.right_stick
            dpg.set_value(
                "sticks_current_values",
                f"Center: {settings_obj.center_deadzone}\n"
                f"Peripheral: {settings_obj.peripheral_deadzone}\n"
                f"Compensation: {settings_obj.deadzone_compensation}\n"
                f"Invert X: {settings_obj.invert_x}\n"
                f"Invert Y: {settings_obj.invert_y}",
            )
        elif screen == "diagnostics":
            snapshot = self.diagnostics_service.build_snapshot(state, self.device_service.last_read_duration_ms, self.device_service.last_write_duration_ms)
            if dpg.does_item_exist("diag_health_summary"):
                health_summary = diagnostics.health_summary_text(state, snapshot.last_packet_timestamp)
                dpg.set_value("diag_health_summary", health_summary)
            if dpg.does_item_exist("diag_write_summary"):
                last_apply_result = _last_apply_result_text(self.device_service)
                dpg.set_value(
                    "diag_write_summary",
                    f"Sync: {state.sync_status}\n"
                    f"Freshness: {diagnostics.freshness_status_text(state)}\n"
                    f"Last Apply: {state.last_apply_time or t('common.never')}\n"
                    f"Result: {last_apply_result}",
                )
            if dpg.does_item_exist("diag_connection_details"):
                dpg.set_value(
                    "diag_connection_details",
                    diagnostics.connection_details_text(
                        state,
                        snapshot,
                        self.device_service.summary_source_summary(),
                    ),
                )
            # diag_event_log lives in its own child_window and can be missing
            # during screen-build transitions, so guard it independently —
            # without this, dpg raises "Item not found: 0" mid-tick and
            # crashes the UI loop.
            if dpg.does_item_exist("diag_event_log"):
                combined_log = self.device_service.recent_events(12)
                if snapshot.event_log:
                    combined_log = combined_log + [t("log.diagnostics.divider")] + snapshot.event_log
                dpg.set_value("diag_event_log", "\n".join(combined_log) or t("diagnostics.event_log.empty"))
        elif screen == "legacy_triggers":
            # Live trigger values came from the diagnostics input poller,
            # which has been removed. The legacy trigger bars now read 0;
            # use an external gamepad tester to verify trigger travel.
            if dpg.does_item_exist("triggers_live_left"):
                left = 0
                right = 0
                dpg.set_value("triggers_live_left", left / 255.0)
                dpg.set_value("triggers_live_right", right / 255.0)
                dpg.set_value("triggers_live_left_label", f"Left Trigger Live: {left}")
                dpg.set_value("triggers_live_right_label", f"Right Trigger Live: {right}")

    def _tick(self) -> None:
        now = time.time()
        # Land worker-thread HID-job completions first: their on_done
        # callbacks (hydration, status text, restore state transitions) are
        # the render-thread half of the executor seam and should run before
        # this frame's other shell maintenance.
        self._drain_hid_job_completions()
        # Then land one pass of deferred-UI calls (the modal-swap seam):
        # DPG never shows a modal created in the same pass another modal
        # was torn down, so swaps run teardown here this frame and the
        # create here next frame.
        self._drain_deferred_ui_calls()
        # Emit a pending geometry snapshot once its settle countdown elapses
        # (permanent diagnostic — see _log_geometry). Render-thread only.
        self._drain_geometry_log()
        # A job still in flight after the drain may be wedged — surface it.
        self._tick_hid_job_staleness()
        # Fire any drag-storm-throttled trailing-edge slider writes whose
        # quiet window has elapsed. Done at the top of _tick so the final
        # value of a drag lands on the controller within one frame of the
        # user releasing the slider.
        self._flush_slider_throttle()
        self._tick_settings_service_tasks(now)
        # Trust ritual card auto-fade. Uses time.monotonic() inside the
        # widget rather than the wall-clock ``now`` above so the timer
        # is immune to NTP / DST jumps that could otherwise cancel the
        # card prematurely.
        trust_ritual_widget.tick_trust_ritual_card(self)

        if now - self._last_presence_poll > 2.5:
            self._last_presence_poll = now
            # allow_probe=False keeps the per-frame presence poll off the
            # pnputil subprocess: it reads the cache the background primer
            # keeps warm (started in run()) so the render thread never stalls
            # ~60ms on device enumeration. XInput still runs here, so
            # connect/disconnect is still detected within one poll interval.
            self.device_service.refresh_state(background=True, allow_probe=False)
            current_connection_state = self.device_service.state.connection_state

            was_disconnected = self._last_connection_state in (None, "no_device")
            is_connected = current_connection_state == "connected"
            if was_disconnected and is_connected:
                self.device_service.refresh_state(background=False, force_probe=True)
                current_connection_state = self.device_service.state.connection_state
                if (
                    current_connection_state == "connected"
                    and self.settings_service is not None
                ):
                    if self._hid_job_in_flight:
                        # A worker job is mid-batch on the cached HID
                        # handles — stop() here would CloseHandle them out
                        # from under its in-flight ReadFile/WriteFile.
                        # Defer exactly like the hydration request:
                        # _tick_settings_service_tasks consumes the flag
                        # (stop + hydration request) once the job drains.
                        self._needs_service_restart = True
                    else:
                        self.settings_service.stop()
                        self._needs_hydration = True
                    self.device_service.log_event(
                        "Controller reconnected; refreshing Wrapper Settings."
                    )
            self._last_connection_state = current_connection_state

            if self.settings.auto_read_on_connect and self.device_service.state.connection_state == "connected" and self.device_service.state.last_read_time is None:
                self.read_controller()

        if self._stick_preview_deadline and now >= self._stick_preview_deadline:
            self._stick_preview_deadline = 0.0
            if self._stick_preview_backup is not None:
                self.profile_service.current_draft.left_stick = deepcopy(self._stick_preview_backup[0])
                self.profile_service.current_draft.right_stick = deepcopy(self._stick_preview_backup[1])
                self.profile_service.current_draft.dirty = self.profile_service.pending_changes_count() > 0
                self.device_service.log_i18n_event("log.preview.expired_reverted")
                self._stick_preview_backup = None
                if self._canonical_screen_id(self.current_screen) == "legacy_sticks":
                    self.rebuild_current_screen()

        if now - self._last_tick > 0.15:
            self._last_tick = now
            self.refresh_shell()
            self.refresh_current_screen()

    def capability_summary(self) -> str:
        caps = self.device_service.state.supported_capabilities
        supported = [name for name, status in caps.items() if status == "supported"]
        return ", ".join(supported[:4]) + ("..." if len(supported) > 4 else "")

    def _switch_screen_callback(self, _sender, _app_data, user_data) -> None:
        # Re-clicking the nav button for the screen already on display would
        # tear down and rebuild an identical widget tree for no visible change
        # — pure work the operator feels as a hitch. Skip it; the per-frame
        # tick keeps live values current. Programmatic switch_screen() callers
        # (home quick-actions, readiness_check) are unaffected and still build.
        if self._is_active_screen_built(user_data):
            return
        self.switch_screen(user_data)

    def _is_active_screen_built(self, target: str) -> bool:
        """True when ``target`` is already the built, on-screen nav screen."""
        if self._canonical_screen_id(target) != self._canonical_screen_id(self.current_screen):
            return False
        if not self._dpg_context_ready or not dpg.does_item_exist("content_region"):
            return False
        return bool(dpg.get_item_children("content_region", 1))

    def switch_screen(self, screen: str) -> None:
        self.current_screen = self._canonical_screen_id(screen)
        self._refresh_nav_selection()
        self.rebuild_current_screen()

    def read_controller(self) -> None:
        state = self.device_service.read_device_state()
        if state.connection_state == "connected" or state.summary_sources.get("active_profile") != "unknown":
            self.profile_service.read_from_controller(
                state.active_onboard_profile,
                active_slot_source=self.device_service.summary_source_label_for("active_profile"),
            )
            if self.selected_button_input is None and self.profile_service.current_draft.button_mappings:
                self.selected_button_input = self.profile_service.current_draft.button_mappings[0].physical_input_id
        self.refresh_shell()
        self.rebuild_current_screen()

    def save_draft(self) -> None:
        profile = self.profile_service.save_draft_locally()
        self.device_service.log_i18n_event(
            "log.draft.saved_locally", name=profile.display_name
        )
        self.refresh_shell()
        if self.current_screen == "Profiles":
            self.rebuild_current_screen()

    def revert_unsaved_changes(self) -> None:
        self.profile_service.revert_unsaved_changes()
        self.device_service.log_i18n_event("log.draft.reverted")
        self.refresh_shell()
        self.rebuild_current_screen()

    def restore_safe_defaults(self) -> None:
        self.profile_service.restore_safe_defaults(self.device_service.state.active_onboard_profile)
        self.device_service.restore_safe_defaults()
        self.refresh_shell()
        self.rebuild_current_screen()

    def selected_button_mapping(self):
        if self.selected_button_input is None:
            return None
        for mapping in self.profile_service.current_draft.button_mappings:
            if mapping.physical_input_id == self.selected_button_input:
                return mapping
        return None

    def select_button_mapping(self, input_id: str) -> None:
        self.selected_button_input = input_id
        self.rebuild_current_screen()

    def update_button_mapping(self, input_id: str, action: str) -> None:
        self.profile_service.set_button_mapping(input_id, action)
        self.device_service.state.sync_status = "Unsaved Changes"
        self.refresh_shell()
        self.refresh_current_screen()

    def reset_button_mapping(self, input_id: str) -> None:
        self.profile_service.reset_button_mapping(input_id)
        self.refresh_shell()
        self.rebuild_current_screen()

    def apply_button_changes(self) -> None:
        success, message = self.profile_service.apply_button_changes()
        self.device_service.record_apply_result(success, message)
        self.refresh_shell()

    def select_stick_side(self, side: str) -> None:
        self.selected_stick_side = side
        self.rebuild_current_screen()

    def update_stick_settings(self, side: str, patch: dict) -> None:
        if patch.get("curve_preset") != "Custom" and any(key != "curve_preset" for key in patch):
            patch = dict(patch)
            patch["curve_preset"] = "Custom"
        self.profile_service.set_stick_settings(side, patch)
        self.device_service.state.sync_status = "Unsaved Changes"
        self.refresh_shell()
        self.refresh_current_screen()

    def start_stick_preview(self) -> None:
        baseline = self.profile_service.baseline_profile
        if baseline is None:
            self.device_service.log_i18n_event("log.preview.no_data_blocked")
            return
        self._stick_preview_backup = (
            deepcopy(baseline.left_stick),
            deepcopy(baseline.right_stick),
        )
        self._stick_preview_deadline = time.time() + 10.0
        self.device_service.log_i18n_event("log.preview.started")
        self.rebuild_current_screen()

    def confirm_stick_preview(self) -> None:
        if not self._stick_preview_deadline:
            return
        self._stick_preview_deadline = 0.0
        self._stick_preview_backup = None
        self.device_service.log_i18n_event("log.preview.kept_as_draft")
        self.refresh_shell()
        self.rebuild_current_screen()

    def preview_seconds_remaining(self) -> int:
        if self._stick_preview_deadline <= 0:
            return 0
        remaining = int(round(self._stick_preview_deadline - time.time()))
        return max(0, remaining)

    def apply_stick_changes(self) -> None:
        success, message = self.profile_service.apply_stick_changes()
        self.device_service.record_apply_result(success, message)
        self.refresh_shell()

    def set_link_triggers(self, value: bool) -> None:
        self.link_triggers = value

    def update_trigger_settings(self, side: str, patch: dict) -> None:
        self.profile_service.set_trigger_settings(side, patch)
        if self.link_triggers:
            opposite = "right" if side == "left" else "left"
            self.profile_service.set_trigger_settings(opposite, patch)
        self.device_service.state.sync_status = "Unsaved Changes"
        self.refresh_shell()

    def reset_trigger_settings(self, side: str) -> None:
        defaults = TriggerSettings()
        self.update_trigger_settings(side, defaults.to_dict())
        self.rebuild_current_screen()

    def apply_trigger_changes(self) -> None:
        success, message = self.profile_service.apply_trigger_changes()
        self.device_service.record_apply_result(success, message)
        self.refresh_shell()

    def select_onboard_slot(self, slot_id: int) -> None:
        self.profile_service.select_onboard_target(slot_id)
        self.rebuild_current_screen()

    def apply_polling_rate(self, label: str):
        try:
            rate = POLLING_RATE_BY_LABEL[label]
        except KeyError as exc:
            raise ValueError(f"Unsupported polling-rate label: {label!r}") from exc

        if not self._hid_available_or_refuse():
            return None

        if self.settings_service is None:
            message = t("apply.polling_rate.unavailable")
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        if not self._polling_rate_hydrated:
            # Combo is disabled/unhydrated (read-miss). Ignore stray callbacks
            # so we never write a stale/default value the user didn't choose.
            logger.debug("Ignoring polling-rate callback before hydration from a real read.")
            return None

        # Trigger model #4 — capture before a manual device-class
        # write. Debounced so drag-storms collapse into one RP (see
        # ``_maybe_capture_before_manual_device_write``). The RP hook fires
        # BEFORE the throttle check so user intent is captured even when the
        # inner slider write is suppressed.
        self._maybe_capture_before_manual_device_write(field_key="polling_rate")

        if not self._slider_throttle.should_write_now(
            "polling_rate", (rate, label), now=time.monotonic()
        ):
            # Stored as pending; trailing-edge fire happens via
            # _flush_slider_throttle() on the next render tick.
            return None
        return self._do_write_polling_rate(rate, label)

    def _do_write_polling_rate(self, rate: PollingRate, label: str):
        result = self.settings_service.set_polling_rate(rate)
        success = _settings_outcome_is_success(result.outcome)
        if success and rate is PollingRate.HZ_8000:
            # 8000 Hz is the one rate with a firmware-capability cliff: it needs
            # controller fw v1.18+. On a pre-1.18 device the WriteFile still ACKs,
            # but the firmware silently keeps its previous (lower) rate. Confirm
            # the commit with ONE read-back — the same get_polling_rate() the
            # hydrate path already uses — fired ONLY on an 8000 Hz selection, so
            # every other rate (and a successful 8K on capable hardware) pays
            # nothing extra. Mirrors the cat-0x86 8-point fail-safe philosophy:
            # when we cannot confirm the high-capability value, tell the truth
            # and reconcile the UI to the device's real rate rather than leaving
            # a false "8000Hz" displayed.
            committed = self._read_polling_rate_for_confirm()
            if committed is not None and committed is not PollingRate.HZ_8000:
                return self._record_polling_rate_non_commit(result, committed)
            # committed is HZ_8000 (capable device honoured it) or None
            # (unverifiable — fail safe by trusting the ACK rather than crying
            # non-commit on a transient read miss). Either way, fall through to
            # the normal success path below.
        if success:
            message = _make_log_entry("apply.polling_rate.success", label=label)
        else:
            message = _make_result_log_entry("apply.polling_rate.failed", result, label=label)

        self._record_settings_apply_result(success, message)
        self.refresh_shell()
        return result

    def _read_polling_rate_for_confirm(self) -> PollingRate | None:
        """Best-effort single read-back of the rate the device actually committed.

        Reuses the service's existing ``get_polling_rate`` (one single-category
        HID read — not the full ~27-round-trip settings batch) and never lets a
        read failure crash the apply: any error or unreadable response yields
        ``None``, which the caller treats as "could not confirm" and falls back
        to trusting the write ACK. An unverifiable 8K must never masquerade as a
        non-commit (fail-safe: unknown capability never blocks a capable device).
        """

        try:
            return self.settings_service.get_polling_rate()
        except Exception as exc:
            logger.info("polling-rate commit read-back failed: %s", exc)
            return None

    def _record_polling_rate_non_commit(self, result, committed: PollingRate):
        """Handle a confirmed 8000 Hz non-commit (firmware kept a lower rate).

        Surfaces the localized firmware-capability message and reconciles the
        combo to the device's real rate (so the UI never shows an 8000 Hz the
        device did not take), then returns the original write ``result`` so
        callers keep their contract. Recorded as a non-success because the user's
        chosen rate was not applied — the device-truthful outcome, not a
        transport failure.
        """

        committed_label = POLLING_RATE_LABEL_BY_ENUM.get(committed) or getattr(
            committed, "name", str(committed)
        )
        # Reconcile the combo to the device's actual rate (re-enables + hydrates).
        self._hydrate_polling_rate(committed, [])
        message = _make_log_entry(
            "apply.polling_rate.non_commit_8000", kept=committed_label
        )
        self._record_settings_apply_result(False, message)
        self.refresh_shell()
        return result

    def apply_step_size(self, value: int):
        if not self._hid_available_or_refuse():
            return None

        if self.settings_service is None:
            message = t("apply.step_size.unavailable")
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        if not self._step_size_hydrated:
            # Slider is disabled/unhydrated (read-miss). Ignore stray callbacks
            # so we never clobber the controller's real value with a stale 146.
            logger.debug("Ignoring step-size callback before hydration from a real read.")
            return None

        # Trigger model #4 — capture before a manual device-class
        # write. Debounced so slider drag-storms collapse into one RP. The
        # RP hook fires BEFORE the throttle check so user intent is captured
        # even when the inner slider write is suppressed.
        self._maybe_capture_before_manual_device_write(field_key="step_size")

        if not self._slider_throttle.should_write_now(
            "step_size", int(value), now=time.monotonic()
        ):
            # Stored as pending; trailing-edge fire happens via
            # _flush_slider_throttle() on the next render tick.
            return None
        return self._do_write_step_size(int(value))

    def _do_write_step_size(self, value: int):
        # Live slider path: a PLAIN write. This fires rapidly during a drag (the
        # slider callback plus the trailing-edge throttle flush), and a per-move
        # verified read-back is both slow and timeout-prone -- on real hardware
        # the read-back's HID timeout raised straight through the verified setter
        # and crashed the app mid-drag. The live path never needed verification:
        # the original revert-to-floor concern was a profile clobber, not a
        # failed live write. The deliberate Apply still confirms a single
        # committed step_size via the apply-coordinator's verified trailer (where
        # the in-burst-rejection quirk actually bites), so nothing is lost by
        # writing plainly here.
        result = self.settings_service.set_step_size(value)
        success = _settings_outcome_is_success(result.outcome)
        if success:
            message = _make_log_entry("apply.step_size.success", value=value)
        else:
            message = _make_result_log_entry("apply.step_size.failed", result, value=value)

        self._record_settings_apply_result(success, message)
        # Fix B: after a successful live change, offer to persist it into the
        # active profile when it diverges from that profile's stored step_size.
        # A failed write (device gone / write error) changed nothing on-device,
        # so there is nothing new to save — clear any stale nudge instead.
        if success:
            self._maybe_offer_save_step_size_to_profile(value)
        else:
            self._clear_step_size_save_nudge()
        self.refresh_shell()
        return result

    # -- Fix B: save a changed step_size into the active profile -------------

    def _step_size_save_nudge_target(self, value: int) -> str | None:
        """Return the active profile name to offer a step_size save for, or None.

        Offers only when ALL of the following hold:

        * a named wrapper profile is loaded/active (one was applied this
          session), AND
        * that profile carries a *stored* step_size — a device-settings profile.
          A profile-only profile has none, so applying it never touches
          step_size and there is nothing to keep in sync; nudging there would
          also silently convert it into a device-settings profile, AND
        * the just-committed ``value`` DIFFERS from that stored step_size.

        Returns ``None`` on any miss, on a profile-load failure, or when the
        value already matches — so the caller offers nothing.
        """

        name = self._active_wrapper_profile_name
        if not name:
            return None
        store = getattr(self, "wrapper_profile_store", None)
        if store is None:
            return None
        try:
            profile = store.load(name)
        except WrapperProfileError:
            return None
        except Exception:  # noqa: BLE001 - a nudge must never crash the write path
            logger.exception(
                "step_size save-nudge: could not load active profile %r", name
            )
            return None
        stored = getattr(getattr(profile, "snapshot", None), "step_size", None)
        if stored is None or stored == value:
            return None
        return name

    def step_size_save_nudge_offered(self) -> bool:
        """True when the save-to-profile affordance is currently offered."""

        return self._pending_step_size_save is not None

    def _maybe_offer_save_step_size_to_profile(self, value: int) -> None:
        """Offer (or withdraw) the save-to-profile nudge for a committed value."""

        target = self._step_size_save_nudge_target(value)
        if target is None:
            self._clear_step_size_save_nudge()
            return
        self._pending_step_size_save = (target, value)
        self._render_step_size_save_nudge()

    def _clear_step_size_save_nudge(self) -> None:
        self._pending_step_size_save = None
        self._render_step_size_save_nudge()

    def dismiss_step_size_save_nudge(self) -> None:
        """Dismiss the nudge without saving (the affordance's × / Dismiss)."""

        self._clear_step_size_save_nudge()

    def _render_step_size_save_nudge(self) -> None:
        """Sync the nudge widget to ``_pending_step_size_save``.

        Idempotent and safe to call on every screen build (the controller
        Sticks tab calls it after laying out the row) so the affordance
        survives a ``rebuild_current_screen`` without re-deciding. A no-op until
        a DPG context exists.
        """

        if not self._dpg_context_ready:
            return
        pending = self._pending_step_size_save
        if pending is None:
            self._set_widget_shown(STEP_SIZE_SAVE_NUDGE_GROUP, False)
            return
        name, value = pending
        if dpg.does_item_exist(STEP_SIZE_SAVE_NUDGE_BUTTON):
            dpg.configure_item(
                STEP_SIZE_SAVE_NUDGE_BUTTON,
                label=t(
                    "controller.sticks.step_size.save_to_profile",
                    value=value,
                    name=name,
                ),
            )
        self._set_widget_shown(STEP_SIZE_SAVE_NUDGE_GROUP, True)

    def save_step_size_to_active_profile(self) -> None:
        """Persist the pending step_size into the active profile (one click).

        Reuses the device-inclusive wrapper-profile save path: the changed
        step_size is folded into the active profile's stored snapshot (device
        fields included) and re-saved through ``wrapper_profile_store.save`` —
        the same store write ``save_current_as_named_wrapper_profile`` uses with
        the Save-As "include device" checkbox on. Writing into the loaded
        profile's own snapshot (rather than re-snapshotting the whole
        controller) keeps the edit surgical: only step_size changes, the
        profile's other settings are untouched, and it does not depend on the
        global ``last_controller_snapshot`` being refreshed after the live
        write. No-op (with a cleared nudge) if the offer went stale.
        """

        pending = self._pending_step_size_save
        if pending is None:
            return
        name, value = pending
        store = getattr(self, "wrapper_profile_store", None)
        if store is None:
            self._clear_step_size_save_nudge()
            return
        try:
            profile = store.load(name)
        except WrapperProfileError as exc:
            self._record_settings_apply_result(
                False,
                t("apply.step_size.save_to_profile_failed", name=name, reason=exc),
            )
            self._clear_step_size_save_nudge()
            self.refresh_shell()
            return

        updated = replace(
            profile,
            snapshot=replace(profile.snapshot, step_size=value),
            last_modified_at=utc_now_iso(),
        )
        try:
            store.save(updated)
        except WrapperProfileError as exc:
            self._record_settings_apply_result(
                False,
                t("apply.step_size.save_to_profile_failed", name=name, reason=exc),
            )
            self._clear_step_size_save_nudge()
            self.refresh_shell()
            return

        self._record_settings_apply_result(
            True,
            t("apply.step_size.saved_to_profile", value=value, name=name),
        )
        # Stored == committed now, so the nudge withdraws itself.
        self._clear_step_size_save_nudge()
        self.refresh_shell()

    def _flush_slider_throttle(self) -> None:
        """Fire any drag-storm-throttled writes whose quiet window has elapsed.

        Called per-frame from :meth:`_tick`. Safe to call when the throttle
        is empty (no-op) or before :meth:`__init__` has finished (defensive
        ``getattr`` for the early-construction window). While a threaded HID
        job holds the gate the elapsed trailing writes are LEFT queued (not
        dropped) so they fire the moment the gate clears (item L).
        """

        throttle = getattr(self, "_slider_throttle", None)
        if throttle is None:
            return
        if getattr(self, "_hid_job_in_flight", False):
            # A threaded HID job is mid-flight — e.g. a deadzone read-back
            # verify, which holds the gate for ~1s (POST_APPLY_READ_SETTLE_S +
            # the confirm read, both >> the throttle window). LEAVE any elapsed
            # trailing writes QUEUED (don't consume them): the final drag value
            # still lands, and — for deadzones — still read-back-verifies, the
            # moment the gate clears. _tick drains job completions (which clears
            # _hid_job_in_flight) immediately before this flush, so a re-queued
            # pending fires on the very frame the gate opens. Consuming +
            # dropping here instead stranded the inline deadzone status at
            # "sending" forever when the user released mid-verify (item L): the
            # pending was discarded, so no trailing flush ever ran the verify
            # that firms the status to verified / sent_unverified.
            pending = throttle.peek_pending()
            if pending:
                logger.debug(
                    "Slider trailing writes deferred behind in-flight HID job: %r",
                    pending,
                )
            return
        now = time.monotonic()
        for field_key, value in throttle.flush_pending(now=now):
            if field_key == "step_size":
                committed_value = int(value)
                self._do_write_step_size(committed_value)
                self._record_wear_event(
                    SLIDER_WRITE,
                    summary=f"Slider committed: step_size = {committed_value}",
                    details={
                        "field_name": "step_size",
                        "value": committed_value,
                    },
                )
            elif field_key == "polling_rate":
                rate, label = value
                self._do_write_polling_rate(rate, label)
                self._record_wear_event(
                    SLIDER_WRITE,
                    summary=f"Slider committed: polling_rate = {label}",
                    details={
                        "field_name": "polling_rate",
                        "value": label,
                        "rate": getattr(rate, "name", str(rate)),
                    },
                )
            elif field_key == "deadzones":
                # Trailing-edge (drag release): write the final value AND
                # read it back to confirm the firmware committed it.
                self._do_write_deadzones(value, verify=True)
                self._record_wear_event(
                    SLIDER_WRITE,
                    summary="Slider committed: stick deadzones",
                    details={"field_name": "deadzones"},
                )
        # Single-callback deadzone change: a quick flick / a drag that
        # ended on its leading edge wrote its value immediately (verify=False)
        # but stored no trailing pending, so the loop above never verified it and
        # the inline status would strand at "sending". Once the throttle quiet
        # window has elapsed with no superseding write, fire the deferred
        # read-back verify for that settled value so the status always resolves
        # to a final state. A multi-callback drag's trailing verify=True write
        # cleared this armed value in _do_write_deadzones (so it never
        # double-verifies), and the in-flight-gate early return above means this
        # can't start while another HID job holds the gate.
        pending_verify = self._deadzone_pending_verify
        if pending_verify is not None and throttle.quiet_window_elapsed(
            "deadzones", now=now
        ):
            self._deadzone_pending_verify = None
            self._diag_deadzone_status_key = "verifying"
            self._schedule_deadzone_readback_verify(pending_verify)

    def apply_back_paddle_binding_from_combo(self, slot: MacroSlot):
        if not self._hid_available_or_refuse():
            return None
        if self.settings_service is None:
            message = t("apply.back_paddle.unavailable")
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None
        if not isinstance(slot, MacroSlot):
            message = t("apply.back_paddle.unknown_slot", slot=slot)
            logger.warning(message)
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        combo_tag = f"back_paddle_combo_{slot.name}"
        target_label = dpg.get_value(combo_tag)
        if target_label == t("controller.back_paddles.not_set_here"):
            # The unread placeholder is still selected. There is nothing to write,
            # and we must NOT overwrite the device's (unreadable) paddle state with
            # a blank just because the user clicked Apply. Tell them how to act and
            # leave the on-device paddle untouched.
            message = t("apply.back_paddle.nothing_to_apply", slot=slot.name)
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None
        target = _back_paddle_target_by_label(target_label)
        if target_label != _back_paddle_target_label(None) and target is None:
            message = t("apply.back_paddle.unknown_target", target=target_label)
            logger.warning(message)
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        result = self.settings_service.set_back_paddle_binding(slot, target)
        success = _settings_outcome_is_success(result.outcome)
        binding = BackPaddleBinding(target=target)
        if success:
            self._remember_back_paddle_binding(slot, binding)
            message = _make_log_entry(
                "apply.back_paddle.success",
                slot=slot.name,
                target=_back_paddle_target_label(target),
            )
        else:
            message = _make_result_log_entry("apply.back_paddle.failed", result, slot=slot.name)

        self._record_settings_apply_result(success, message)
        self.refresh_shell()
        if success:
            live_verify.refresh_inspector_binding(self)
        return result

    def apply_vibration_settings(self):
        if not self._hid_available_or_refuse():
            return None
        if self.settings_service is None:
            message = t("apply.vibration.unavailable")
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        mode_label = dpg.get_value("vibration_mode_combo")
        mode = VIBRATION_MODE_BY_LABEL.get(mode_label)
        if mode is None:
            message = t("apply.vibration.unknown_mode", mode=mode_label)
            logger.warning(message)
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        vibration_settings = VibrationSettings(
            left_grip_strength=dpg.get_value("vibration_lg_slider"),
            right_grip_strength=dpg.get_value("vibration_rg_slider"),
            left_trigger_motor_strength=dpg.get_value("vibration_lm_slider"),
            right_trigger_motor_strength=dpg.get_value("vibration_rm_slider"),
            mode=mode,
        )
        result = self.settings_service.set_vibration(vibration_settings)
        success = _settings_outcome_is_success(result.outcome)
        if success:
            message = _make_log_entry("apply.vibration.success")
        else:
            message = _make_result_log_entry("apply.vibration.failed", result)

        self._record_settings_apply_result(success, message)
        self.refresh_shell()
        return result

    def apply_left_trigger_settings(self):
        return self._apply_trigger_settings(
            side="left",
            min_tag="trigger_left_min_slider",
            max_tag="trigger_left_max_slider",
            mode_tag="trigger_left_mode_combo",
        )

    def apply_right_trigger_settings(self):
        return self._apply_trigger_settings(
            side="right",
            min_tag="trigger_right_min_slider",
            max_tag="trigger_right_max_slider",
            mode_tag="trigger_right_mode_combo",
        )

    def _apply_trigger_settings(self, side: str, min_tag: str, max_tag: str, mode_tag: str):
        if not self._hid_available_or_refuse():
            return None
        if self.settings_service is None:
            message = t("apply.trigger.unavailable", side=_side_label(side))
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        mode_label = dpg.get_value(mode_tag)
        mode = TRIGGER_MODE_BY_LABEL.get(mode_label)
        if mode is None:
            message = t("apply.trigger.unknown_mode", mode=mode_label, side=_side_label(side))
            logger.warning(message)
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        range_min = dpg.get_value(min_tag)
        range_max = dpg.get_value(max_tag)
        if range_min > range_max:
            message = t(
                "apply.trigger.invalid_range",
                min=range_min,
                max=range_max,
                side=_side_label(side),
            )
            logger.warning(message)
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        settings_obj = ServiceTriggerSettings(
            range_min=range_min,
            range_max=range_max,
            mode=mode,
        )
        if side == "left":
            result = self.settings_service.set_left_trigger_settings(settings_obj)
        else:
            result = self.settings_service.set_right_trigger_settings(settings_obj)

        success = _settings_outcome_is_success(result.outcome)
        side_label = _side_label(side)
        if success:
            message = _make_log_entry("apply.trigger.success", side=side_label)
        else:
            message = _make_result_log_entry("apply.trigger.failed", result, side=side_label)

        self._record_settings_apply_result(success, message)
        self.refresh_shell()
        return result

    def apply_deadzone_settings(self):
        if not self._hid_available_or_refuse():
            return None
        if self.settings_service is None:
            message = t("apply.deadzone.unavailable")
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        deadzones = StickDeadzones(
            left_center=dpg.get_value("deadzone_left_center_slider"),
            right_center=dpg.get_value("deadzone_right_center_slider"),
            left_outer=dpg.get_value("deadzone_left_outer_slider"),
            right_outer=dpg.get_value("deadzone_right_outer_slider"),
        )
        result = self.settings_service.set_all_deadzones(deadzones)
        success = _settings_outcome_is_success(result.outcome)
        if success:
            message = _make_log_entry("apply.deadzone.success")
        else:
            message = _make_result_log_entry("apply.deadzone.failed", result)

        self._record_settings_apply_result(success, message)
        self.refresh_shell()
        return result

    def apply_diagnostics_deadzone(self, deadzones: StickDeadzones):
        """Live-write the firmware ``StickDeadzones`` from the Diagnostics panel.

        Mirrors :meth:`apply_step_size`'s live-slider contract: busy-gate
        (refuse, never queue) -> hydration guard (ignore stray callbacks
        before a real read) -> RP capture -> slider throttle. The leading-edge
        write fires immediately; in-window ticks store a pending value flushed
        by :meth:`_flush_slider_throttle` on the next render tick. Read-back
        verify is deferred to the trailing flush (the natural drag release) so
        we never issue a blocking HID read on every tick. Distinct from the
        Controller-screen one-shot :meth:`apply_deadzone_settings`: this drives
        the live panel and its circularity plot in real time.
        """

        if not self._hid_available_or_refuse():
            return None
        if self.settings_service is None:
            self._diag_deadzone_status_key = "unavailable"
            return None
        if not self._diag_deadzone_hydrated:
            # Sliders not yet hydrated from a real read; ignore stray
            # callbacks so we never clobber the controller with a default.
            logger.debug("Ignoring diagnostics deadzone callback before hydration.")
            return None
        # Trigger model #4 — capture a restore point before a manual
        # device-class write, debounced so a drag-storm collapses into one RP.
        self._maybe_capture_before_manual_device_write(field_key="deadzones")
        if not self._slider_throttle.should_write_now(
            "deadzones", deadzones, now=time.monotonic()
        ):
            # Stored as pending; the trailing-edge write + read-back verify
            # fires via _flush_slider_throttle() once the quiet window elapses.
            self._diag_deadzone_status_key = "sending"
            return None
        return self._do_write_deadzones(deadzones, verify=False)

    def _do_write_deadzones(self, deadzones: StickDeadzones, *, verify: bool):
        """Write a full ``StickDeadzones`` frame; optionally read-back-verify.

        One cat-0x09 frame carries all four deadzone fields, so every write
        sends the whole ``StickDeadzones`` the panel built from its four
        sliders. ``verify`` is True only on the trailing flush (drag release):
        the write lands synchronously here, then the confirmation read is run
        OFF the render thread (item J) so a slow / timed-out read can never
        freeze the UI, and its completion sets the final verified / mismatch /
        sent-unverified status.
        """

        result = self.settings_service.set_all_deadzones(deadzones)
        success = _settings_outcome_is_success(result.outcome)
        if success:
            message = _make_log_entry("apply.deadzone.success")
            if verify:
                # The write landed. Confirm it with a read-back, but jobbed off
                # the render thread: the firmware is often busy right after a
                # write burst, and a blocking read here froze the UI up to
                # 1000ms and mis-reported the timeout as a value "mismatch".
                # This trailing (multi-callback) verify supersedes any deferred
                # single-callback verify armed below — clear it so the
                # value is verified exactly once.
                self._deadzone_pending_verify = None
                self._diag_deadzone_status_key = "verifying"
                self._schedule_deadzone_readback_verify(deadzones)
            else:
                # Leading-edge write: arm a deferred read-back verify for this
                # value. If no further write supersedes it within the
                # throttle quiet window, _flush_slider_throttle fires the verify
                # so the status resolves instead of stranding at "sending"; a
                # multi-callback drag's trailing verify=True write clears it
                # above, so the settled value is still verified exactly once.
                self._deadzone_pending_verify = deadzones
                self._diag_deadzone_status_key = "sending"
        else:
            # The write failed — nothing committed to confirm; drop any armed
            # deferred verify so it can't fire against a value that never landed.
            self._deadzone_pending_verify = None
            message = _make_result_log_entry("apply.deadzone.failed", result)
            self._diag_deadzone_status_key = "failed"
        self._record_settings_apply_result(success, message)
        self.refresh_shell()
        return result

    def _schedule_deadzone_readback_verify(self, written: StickDeadzones) -> None:
        """Confirm a committed deadzone write with a read-back, off the render
        thread (item J).

        Routed through the HID-job seam (:meth:`_run_hid_job`) so the read runs
        on the worker thread in production and its completion drains on the
        render thread; in sync mode (tests) it runs inline. The seam serializes
        the read against other HID flows via the in-flight gate, so it can never
        interleave with a concurrent slider write or footer Read against the
        lock-free SettingsService. The completion sets:

        * ``verified`` - a successful read whose value matches the write;
        * ``mismatch`` - a successful read whose value differs;
        * ``sent_unverified`` - a timeout / read failure: the WRITE succeeded,
          the device just couldn't answer the confirm read in time, so this is
          an advisory ("sent, couldn't verify"), never a failure or mismatch.
        """

        settle = self._hid_executor is not None

        def job():
            # Give the firmware a quiet interval to settle after the write burst
            # before the confirm read (it often can't answer in the first
            # ~100ms, which is what produced the 1000ms timeouts) - same
            # rationale as POST_APPLY_READ_SETTLE_S. Off the render thread, so
            # the sleep costs nothing there; skipped in sync mode (no executor)
            # so tests stay instant.
            if settle:
                time.sleep(POST_APPLY_READ_SETTLE_S)
            return self.settings_service.get_deadzones()

        def on_done(result):
            self._diag_deadzone_status_key = _deadzone_verify_status_key(
                result, written
            )

        if not self._run_hid_job(job, on_done):
            # Defensive: a refused job (another HID flow already in flight)
            # would otherwise leave the status pinned at "verifying". The write
            # already landed, so degrade to the advisory, not a failure.
            self._diag_deadzone_status_key = "sent_unverified"

    def apply_left_sensitivity_curve(self):
        return self._apply_sensitivity_curve(
            side="left",
            tag_prefix="sensitivity_left_",
        )

    def apply_right_sensitivity_curve(self):
        return self._apply_sensitivity_curve(
            side="right",
            tag_prefix="sensitivity_right_",
        )

    def apply_left_sensitivity_preset(self, name: str):
        return self._apply_sensitivity_preset("left", name)

    def apply_right_sensitivity_preset(self, name: str):
        return self._apply_sensitivity_preset("right", name)

    def _apply_sensitivity_preset(self, side: str, name: str):
        # Guard before the widget writes below, not just before the delegated
        # apply — otherwise a busy refusal would leave the sliders showing a
        # preset the device never received.
        if not self._hid_available_or_refuse():
            return None
        anchors = SENSITIVITY_PRESETS.get(name)
        if anchors is None:
            message = t("apply.sensitivity.unknown_preset", preset=name)
            logger.warning(message)
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        tag_prefix = f"sensitivity_{side}_"
        for index, anchor in enumerate(anchors, start=1):
            self._set_widget(f"{tag_prefix}a{index}x_slider", anchor.x)
            self._set_widget(f"{tag_prefix}a{index}y_slider", anchor.y)

        if side == "left":
            return self.apply_left_sensitivity_curve()
        if side == "right":
            return self.apply_right_sensitivity_curve()

        message = t("apply.sensitivity.unknown_side", side=side)
        logger.warning(message)
        self._record_settings_apply_result(False, message)
        self.refresh_shell()
        return None

    def _apply_sensitivity_curve(self, side: str, tag_prefix: str):
        if not self._hid_available_or_refuse():
            return None
        if self.settings_service is None:
            message = t("apply.sensitivity.unavailable", side=_side_label(side))
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        anchors = (
            SensitivityAnchor(
                x=dpg.get_value(f"{tag_prefix}a1x_slider"),
                y=dpg.get_value(f"{tag_prefix}a1y_slider"),
            ),
            SensitivityAnchor(
                x=dpg.get_value(f"{tag_prefix}a2x_slider"),
                y=dpg.get_value(f"{tag_prefix}a2y_slider"),
            ),
            SensitivityAnchor(
                x=dpg.get_value(f"{tag_prefix}a3x_slider"),
                y=dpg.get_value(f"{tag_prefix}a3y_slider"),
            ),
        )

        if side == "left":
            result = self.settings_service.set_left_stick_sensitivity_curve(anchors)
        else:
            result = self.settings_service.set_right_stick_sensitivity_curve(anchors)

        success = _settings_outcome_is_success(result.outcome)
        side_label = _side_label(side)
        if success:
            message = _make_log_entry("apply.sensitivity.success", side=side_label)
        else:
            message = _make_result_log_entry("apply.sensitivity.failed", result, side=side_label)

        self._record_settings_apply_result(success, message)
        self.refresh_shell()
        return result

    def apply_left_sensitivity_curve_8point(self):
        return self._apply_sensitivity_curve_8point(
            side="left",
            tag_prefix="sensitivity_left_",
        )

    def apply_right_sensitivity_curve_8point(self):
        return self._apply_sensitivity_curve_8point(
            side="right",
            tag_prefix="sensitivity_right_",
        )

    def _apply_sensitivity_curve_8point(self, side: str, tag_prefix: str):
        # 8-point (cat 0x86) mirror of _apply_sensitivity_curve. Reads the 16
        # ``_8point``-suffixed sliders into 8 anchors and dispatches to the
        # cat-0x86 service writer. No client-side monotonic clamp: the service
        # layer's _validate_sensitivity_anchors_8point raises on a non-monotonic
        # curve, surfaced here as an apply.sensitivity_8point.failed log entry.
        if not self._hid_available_or_refuse():
            return None
        if self.settings_service is None:
            message = t("apply.sensitivity.unavailable", side=_side_label(side))
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        anchors = tuple(
            SensitivityAnchor(
                x=dpg.get_value(f"{tag_prefix}a{index}x_slider_8point"),
                y=dpg.get_value(f"{tag_prefix}a{index}y_slider_8point"),
            )
            for index in range(1, 9)
        )

        if side == "left":
            result = self.settings_service.set_left_stick_sensitivity_curve_8point(anchors)
        else:
            result = self.settings_service.set_right_stick_sensitivity_curve_8point(anchors)

        success = _settings_outcome_is_success(result.outcome)
        side_label = _side_label(side)
        if success:
            message = _make_log_entry("apply.sensitivity_8point.success", side=side_label)
        else:
            message = _make_result_log_entry("apply.sensitivity_8point.failed", result, side=side_label)

        self._record_settings_apply_result(success, message)
        self.refresh_shell()
        return result

    def apply_left_sensitivity_preset_8point(self, name: str):
        return self._apply_sensitivity_preset_8point("left", name)

    def apply_right_sensitivity_preset_8point(self, name: str):
        return self._apply_sensitivity_preset_8point("right", name)

    def _apply_sensitivity_preset_8point(self, side: str, name: str):
        # 8-point mirror of _apply_sensitivity_preset: set the 16 sliders from the
        # named curve, repaint the live plot, then dispatch the normal 8-point
        # apply (which re-reads the sliders → 8 anchors → cat-0x86 write). The
        # preset curves are pre-validated monotonic, so the apply won't trip the
        # service-layer non-decreasing check.
        # Guard before the widget writes (same rationale as the 3-point preset).
        if not self._hid_available_or_refuse():
            return None
        anchors = SENSITIVITY_PRESETS_8POINT.get(name)
        if anchors is None:
            message = t("apply.sensitivity.unknown_preset", preset=name)
            logger.warning(message)
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        # Set both widget twins (slider + exact-entry input) from the named curve
        # and repaint the plot; the apply below re-reads the sliders → 8 anchors.
        self._set_sensitivity_8point_anchor_widgets(side, anchors)

        if side == "left":
            return self.apply_left_sensitivity_curve_8point()
        if side == "right":
            return self.apply_right_sensitivity_curve_8point()

        message = t("apply.sensitivity.unknown_side", side=side)
        logger.warning(message)
        self._record_settings_apply_result(False, message)
        self.refresh_shell()
        return None

    def apply_left_axis_inversion(self):
        return self._apply_axis_inversion(
            side="left",
            x_tag="axis_inv_left_x_checkbox",
            y_tag="axis_inv_left_y_checkbox",
        )

    def apply_right_axis_inversion(self):
        return self._apply_axis_inversion(
            side="right",
            x_tag="axis_inv_right_x_checkbox",
            y_tag="axis_inv_right_y_checkbox",
        )

    def _apply_axis_inversion(self, side: str, x_tag: str, y_tag: str):
        if not self._hid_available_or_refuse():
            return None
        if self.settings_service is None:
            message = t("apply.axis_inversion.unavailable", side=_side_label(side))
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        inversion = AxisInversion(
            x_inverted=bool(dpg.get_value(x_tag)),
            y_inverted=bool(dpg.get_value(y_tag)),
        )

        if side == "left":
            result = self.settings_service.set_left_stick_inversion(inversion)
        else:
            result = self.settings_service.set_right_stick_inversion(inversion)

        success = _settings_outcome_is_success(result.outcome)
        side_label = _side_label(side)
        if success:
            message = _make_log_entry("apply.axis_inversion.success", side=side_label)
        else:
            message = _make_result_log_entry("apply.axis_inversion.failed", result, side=side_label)

        self._record_settings_apply_result(success, message)
        self.refresh_shell()
        return result

    def apply_button_binding(self):
        if not self._hid_available_or_refuse():
            return None
        if self.settings_service is None:
            message = t("apply.binding.unavailable")
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        source_label = dpg.get_value("binding_source_combo")
        target_label = dpg.get_value("binding_target_combo")

        slot = BUTTON_SLOT_BY_LABEL.get(source_label)
        if slot is None:
            message = t("apply.binding.unknown_source", source=source_label)
            logger.warning(message)
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        target = BUTTON_TARGET_BY_LABEL.get(target_label)
        if target is None:
            message = t("apply.binding.unknown_target", target=target_label)
            logger.warning(message)
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        mapping = ButtonMapping.controller_button(target)
        result = self.settings_service.set_button_binding(slot, mapping)
        success = _settings_outcome_is_success(result.outcome)
        if success:
            message = _make_log_entry(
                "apply.binding.success",
                source=source_label,
                target=target_label,
            )
        else:
            message = _make_result_log_entry(
                "apply.binding.failed",
                result,
                source=source_label,
                target=target_label,
            )

        self._record_settings_apply_result(success, message)
        self.refresh_shell()
        if success:
            # Authoritative: we just wrote this mapping, so fold it into the
            # cached snapshot (no device read) and refresh the read-only Current
            # Bindings list in place — it reflects the change without the user
            # leaving and re-entering the Buttons tab. The source/target picker
            # combos are left untouched (value-only update, no screen rebuild).
            self._remember_button_binding(slot, mapping)
            controller.refresh_current_bindings(self)
            live_verify.refresh_inspector_binding(self)
        return result

    def apply_lighting(self):
        if not self._hid_available_or_refuse():
            return None
        if self.settings_service is None:
            message = t("apply.lighting.unavailable")
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        zone_label = dpg.get_value("lighting_zone_combo")
        zone = LIGHTING_ZONE_BY_LABEL.get(zone_label)
        if zone is None:
            message = t("apply.lighting.unknown_zone", zone=zone_label)
            logger.warning(message)
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        mode_label = dpg.get_value("lighting_mode_combo")
        mode = LIGHTING_MODE_BY_LABEL.get(mode_label)
        if mode is None:
            message = t("apply.lighting.unknown_mode", mode=mode_label)
            logger.warning(message)
            self._record_settings_apply_result(False, message)
            self.refresh_shell()
            return None

        settings_obj = LightingSettings(
            light_on=bool(dpg.get_value("lighting_on_checkbox")),
            mode=mode,
            brightness_byte=int(dpg.get_value("lighting_brightness_slider")),
            color=RgbColor(
                r=int(dpg.get_value("lighting_r_slider")),
                g=int(dpg.get_value("lighting_g_slider")),
                b=int(dpg.get_value("lighting_b_slider")),
            ),
        )
        result = self.settings_service.set_zone_lighting(zone, settings_obj)
        success = _settings_outcome_is_success(result.outcome)
        if success:
            message = _make_log_entry("apply.lighting.success", zone=zone_label)
        else:
            message = _make_result_log_entry("apply.lighting.failed", result, zone=zone_label)

        self._record_settings_apply_result(success, message)
        self.refresh_shell()
        return result

    def run_session_preflight(self) -> None:
        snapshot = self.preflight_service.run_session_preflight()
        self.device_service.log_event(
            f"Session preflight: {snapshot.state_label}. {snapshot.action_hint}"
        )
        self.refresh_shell()
        self.rebuild_current_screen()

    def select_local_profile(self, profile_id: str) -> None:
        self.selected_local_profile_id = profile_id
        self.rebuild_current_screen()

    def load_selected_profile(self) -> None:
        if not self.selected_local_profile_id:
            self.device_service.log_i18n_event("log.config.no_selection_blocked")
            return
        self.profile_service.load_local_profile(self.selected_local_profile_id)
        self.device_service.log_i18n_event("log.config.loaded")
        self.rebuild_current_screen()

    def duplicate_selected_profile(self) -> None:
        if not self.selected_local_profile_id:
            self.device_service.log_i18n_event("log.config.no_selection_blocked")
            return
        duplicate = self.profile_service.duplicate_local_profile(self.selected_local_profile_id)
        self.selected_local_profile_id = duplicate.profile_id
        self.device_service.log_i18n_event("log.config.duplicated", name=duplicate.display_name)
        self.rebuild_current_screen()

    def open_rename_profile_modal(self) -> None:
        if not self.selected_local_profile_id:
            self.device_service.log_i18n_event("log.config.no_selection_blocked")
            return
        if dpg.does_item_exist("rename_profile_modal"):
            dpg.delete_item("rename_profile_modal")
        current_name = next((profile.display_name for profile in self.profile_service.list_local_profiles() if profile.profile_id == self.selected_local_profile_id), "Config")
        with dpg.window(tag="rename_profile_modal", label=t("config.rename_modal.title"), modal=True, width=360, height=150, no_resize=True):
            dpg.add_text(t("config.rename_modal.body"))
            dpg.add_input_text(tag="rename_profile_input", default_value=current_name, width=280)
            with dpg.group(horizontal=True):
                dpg.add_button(label=t("config.rename_modal.save"), width=100, callback=lambda: self.rename_selected_profile())
                dpg.add_button(label=t("actions.cancel"), width=100, callback=lambda: dpg.delete_item("rename_profile_modal"))

    def rename_selected_profile(self) -> None:
        if not self.selected_local_profile_id or not dpg.does_item_exist("rename_profile_input"):
            return
        new_name = dpg.get_value("rename_profile_input").strip()
        if not new_name:
            return
        self.profile_service.rename_local_profile(self.selected_local_profile_id, new_name)
        self.device_service.log_i18n_event("log.config.renamed", name=new_name)
        if dpg.does_item_exist("rename_profile_modal"):
            dpg.delete_item("rename_profile_modal")
        self.rebuild_current_screen()

    def export_selected_profile(self) -> None:
        if not self.selected_local_profile_id:
            self.device_service.log_i18n_event("log.config.no_selection_blocked")
            return
        path = self.profile_service.export_local_profile(self.selected_local_profile_id)
        self.device_service.log_i18n_event("log.config.exported", path=path)
        self.refresh_shell()

    def selected_local_profile(self):
        if not self.selected_local_profile_id:
            return None
        return next((profile for profile in self.profile_service.list_local_profiles() if profile.profile_id == self.selected_local_profile_id), None)

    def comparison_reference_label(self) -> str:
        selected = self.selected_local_profile()
        if selected is not None:
            return f"Comparing draft to saved PC config: {selected.display_name}"
        if self.device_service.state.last_read_time:
            return "Comparing draft to the last controller config read"
        return "Comparing draft to the initial local defaults"

    def comparison_lines(self) -> list[str]:
        return self.profile_service.compare_current_draft(self.selected_local_profile())

    def open_import_profile_modal(self) -> None:
        if dpg.does_item_exist("import_profile_modal"):
            dpg.delete_item("import_profile_modal")
        with dpg.window(tag="import_profile_modal", label=t("config.import_modal.title"), modal=True, width=520, height=170, no_resize=True):
            dpg.add_text(t("config.import_modal.body"))
            dpg.add_input_text(
                tag="import_profile_input",
                default_value=self.profile_service.import_path_hint(),
                width=420,
            )
            with dpg.group(horizontal=True):
                dpg.add_button(label=t("config.import_modal.import"), width=100, callback=lambda: self.import_profile_from_modal())
                dpg.add_button(label=t("actions.cancel"), width=100, callback=lambda: dpg.delete_item("import_profile_modal"))

    def import_profile_from_modal(self) -> None:
        if not dpg.does_item_exist("import_profile_input"):
            return
        path = dpg.get_value("import_profile_input").strip()
        if not path:
            self.device_service.log_event("Config import path is empty.")
            return
        try:
            imported = self.profile_service.import_local_profile(path)
        except OSError:
            self.device_service.log_event(f"Could not read config file: {path}")
            return
        except (ValueError, KeyError, TypeError, AttributeError):
            # Malformed / wrong-shape config: import_local_profile ->
            # Profile.from_dict normalizes shape errors to ValueError, but catch
            # the lot so a hostile or corrupt file always surfaces a clean
            # failure here instead of escaping and wedging the modal.
            self.device_service.log_event(f"Config file is not a valid profile: {path}")
            return
        self.selected_local_profile_id = imported.profile_id
        self.device_service.log_i18n_event("log.config.imported", name=imported.display_name)
        if dpg.does_item_exist("import_profile_modal"):
            dpg.delete_item("import_profile_modal")
        self.rebuild_current_screen()

    # --- Safe Import -----------------------------------------------------------
    # Guarded import of external profile files. Nothing is written to disk or
    # the controller before the user picks an action on the preview; the
    # default action is Save as New Profile. See safe_import_model (the UI
    # contract) + screens/safe_import (preview modals).

    def open_safe_import(self) -> None:
        safe_import.open_file_select(self)

    def safe_import_scan_path(self) -> None:
        path = ""
        if dpg.does_item_exist(safe_import.PATH_INPUT):
            path = (dpg.get_value(safe_import.PATH_INPUT) or "").strip()
        if not path:
            safe_import.show_file_error(t("safe_import.file_select.empty_error"))
            return

        existing = {profile.name for profile in self.list_wrapper_profiles()}
        result = safe_import_model.prepare_import(path, existing_names=existing)
        if not result.ok:
            reason = t(result.error_key) if result.error_key else ""
            safe_import.show_file_error(t("safe_import.summary.failed", reason=reason))
            return

        self._safe_import_result = result
        self._safe_import_selected = {
            category
            for category in DEFAULT_CHECKED_CATEGORIES
            if result.categories.get(category)
        }
        self._fill_safe_import_current_values(result)
        # Modal-swap hop: the showing FILE_MODAL must be torn down a
        # rendered frame BEFORE open_preview creates the preview, or DPG
        # never shows it (open_preview's own deletes become no-ops by
        # create time). The error paths above stay
        # synchronous: show_file_error only mutates the open file modal,
        # which is safe on any thread.
        self._defer_modal_swap(
            lambda: safe_import.open_preview(self),
            delete_tags=(safe_import.FILE_MODAL, safe_import.PREVIEW_MODAL),
            key="safe_import_open_preview",
        )

    def _fill_safe_import_current_values(self, result) -> None:
        if self.last_controller_snapshot is None:
            return
        current = snapshot_to_dict(self.last_controller_snapshot)
        for changes in result.categories.values():
            for change in changes:
                change.current_value = current.get(change.key)

    def safe_import_toggle_category(self, category: RiskCategory, value: bool) -> None:
        if value:
            self._safe_import_selected.add(category)
        else:
            self._safe_import_selected.discard(category)

    def safe_import_select_all(self) -> None:
        result = self._safe_import_result
        if result is None:
            return
        self._safe_import_selected = set(result.selectable_present())
        for category in SELECTABLE_CATEGORIES:
            tag = f"safe_import_cat_{category.value}"
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, category in self._safe_import_selected)

    def safe_import_request_apply_to_controller(self) -> None:
        # Save + Apply always confirms and creates a restore point first
        # (by design); the confirm modal emphasizes Device changes when present.
        # Modal-swap hop: two modals can never SHOW stacked (benched
        # 2026-06-11 — the second never renders, any thread), so the
        # preview is HIDDEN for a frame before the confirm is created.
        # Hidden, not deleted: its widget state (the typed profile name
        # safe_import_apply reads from NAME_INPUT) survives, and cancel
        # (safe_import_cancel_apply_confirm) re-shows it in place.
        self._defer_modal_swap(
            lambda: safe_import.open_apply_confirm(self),
            hide_tags=(safe_import.PREVIEW_MODAL,),
            key="safe_import_open_confirm",
        )

    def safe_import_cancel_apply_confirm(self) -> None:
        """Dismiss the Save + Apply confirm modal and bring the preview back.

        The reverse swap of :meth:`safe_import_request_apply_to_controller`,
        under the same modal law: delete CONFIRM in one pass, re-show the
        hidden preview after the rendered frame (the main-hide-roundtrip
        cell in tools/diag_dpg_modal_thread_visibility.py). Wired to both
        the confirm modal's Cancel button and its titlebar close so neither
        path can strand the preview hidden.
        """

        def reshow_preview() -> None:
            if dpg.does_item_exist(safe_import.PREVIEW_MODAL):
                dpg.configure_item(safe_import.PREVIEW_MODAL, show=True)

        self._defer_modal_swap(
            reshow_preview,
            delete_tags=(safe_import.CONFIRM_MODAL,),
            key="safe_import_cancel_confirm",
        )

    def safe_import_apply(self, apply_to_controller: bool = False) -> None:
        """Save the imported profile; optionally apply + verify as a HID job.

        Jobbed like the profile apply: the device
        sequence — pre-apply RP capture, write burst, settle + read-back
        verify — runs as the DPG-free job; the result banner, footer-combo
        refresh, screen rebuild, result modal, and failure modal are the
        on_done half. The disk save stays synchronous on the render thread
        BEFORE the job so its failure path never races the device work.
        Plain Save (``apply_to_controller=False``) is store-only: no busy
        check, no job, today's inline path.
        """

        result = self._safe_import_result
        if result is None or result.profile is None:
            return

        # Save + Apply touches the controller three ways (pre-apply RP
        # capture, the write burst, the post-apply verify read-back).
        # Refuse the whole action while a threaded HID job is in flight,
        # BEFORE the disk save, so a refusal never half-completes (saved
        # but unapplied); the user re-clicks when idle (the confirm modal
        # is left untouched through a refusal). Plain Save
        # (apply_to_controller=False) is store-only and stays available.
        if (
            apply_to_controller
            and self.settings_service is not None
            and not self._hid_available_or_refuse()
        ):
            return

        name = result.generated_name
        if dpg.does_item_exist(safe_import.NAME_INPUT):
            entered = (dpg.get_value(safe_import.NAME_INPUT) or "").strip()
            if entered:
                name = entered

        selected = set(self._safe_import_selected)
        snapshot = filtered_snapshot(result.profile.snapshot, selected)
        profile = WrapperProfile(name=name, snapshot=snapshot)

        audit = result.audit
        audit.selected_categories = [
            category
            for category in SELECTABLE_CATEGORIES
            if category in selected and result.categories.get(category)
        ]
        audit.skipped_categories = [
            category
            for category in SELECTABLE_CATEGORIES
            if category not in selected and result.categories.get(category)
        ]

        try:
            self.wrapper_profile_store.save(profile)
        except WrapperProfileError as exc:
            safe_import.close_modals()
            self._record_settings_apply_result(
                False, t("safe_import.error.save_failed", reason=exc)
            )
            self.refresh_shell()
            return

        if not (apply_to_controller and self.settings_service is not None):
            # Plain Save (or no service wired): store-only — the render
            # thread never waits on the controller here, and the disk save
            # above already happened synchronously. Modal-swap hop: the
            # showing preview is torn down a rendered frame before the
            # tail's open_result creates the result modal (open_result's
            # own deletes become no-ops). The Save + Apply flow below is
            # untouched: it tears its modals down at job start, so by the
            # time its on_done runs this same tail in the _tick completion
            # drain, frames have long since rendered and open_result is a
            # pure create. The save-error path above stays synchronous
            # (delete-only teardown is safe; nothing is created).
            audit.controller_write = "not_performed"
            self._defer_modal_swap(
                lambda: self._finish_safe_import_apply(
                    name, apply_to_controller=apply_to_controller
                ),
                delete_tags=(
                    safe_import.CONFIRM_MODAL,
                    safe_import.PREVIEW_MODAL,
                    safe_import.RESULT_MODAL,
                ),
                key="safe_import_open_result",
            )
            return

        # Job start, render thread: the import modals step aside while the
        # device sequence runs. Closing the confirm modal acknowledges the
        # click; closing the preview underneath removes its store-only Save
        # button, the one non-busy-gated writer that could otherwise mutate
        # this import's audit mid-job. The on_done result modal (open_result)
        # deletes both tags anyway, so this is the same teardown a completed
        # apply always performed — just moved to the start of the window.
        # Feedback during the job is the existing seam machinery (disabled
        # HID-flow buttons + busy refusals + the stall ticker); the result
        # banner and modal land in on_done exactly as before.
        #
        # RESULT_MODAL is torn down here too: a stale result modal from a prior
        # completed apply is otherwise only deleted by open_result(), and the
        # on_done failure path shows the failure modal WITHOUT calling
        # open_result — so without this it would stack two modals (and per the
        # benched modal law the second never renders). Deleting at job start
        # leaves rendered frames before on_done re-creates a modal.
        if self._dpg_context_ready:
            for tag in (
                safe_import.CONFIRM_MODAL,
                safe_import.PREVIEW_MODAL,
                safe_import.RESULT_MODAL,
            ):
                if dpg.does_item_exist(tag):
                    dpg.delete_item(tag)

        def job() -> None:
            # Device sequence, statement-for-statement today's order. The
            # audit / _last_apply_result writes are plain Python state —
            # allowed on the worker; everything DearPyGui is in on_done.
            audit.restore_point_name = self._create_safe_import_restore_point()
            apply_result = self._apply_snapshot_to_controller(snapshot)
            # Phase 2: a Safe Import apply is a profile apply — record it
            # under the saved profile name (best-effort, never affects the
            # apply). include_device is derived: the import's category
            # selection decided whether device-global fields rode along.
            self._record_last_applied_safe(
                name,
                snapshot,
                apply_result,
                include_device=has_device_settings(snapshot),
            )
            self._last_apply_result = apply_result
            if apply_result.failed:
                audit.controller_write = "sent"
                audit.verified = False
            else:
                self._verify_safe_import_write(snapshot, audit)

        def on_done(outcome) -> None:
            if isinstance(outcome, BaseException):
                raise outcome  # today's path has no catch at this level
            self._finish_safe_import_apply(name, apply_to_controller=True)

        self._run_hid_job(job, on_done)

    def _finish_safe_import_apply(
        self, name: str, *, apply_to_controller: bool
    ) -> None:
        """Render-thread tail of :meth:`safe_import_apply`.

        Runs inline for plain Save and as the jobbed flow's on_done for
        Save + Apply — the statement order is today's post-sequence block
        either way.
        """

        self._record_settings_apply_result(True, t("safe_import.result.saved_as", name=name))
        self._refresh_footer_profile_combo(selected_name=name)
        self.rebuild_current_screen()
        apply_failed = (
            apply_to_controller
            and self._last_apply_result is not None
            and self._last_apply_result.failed
        )
        if apply_failed:
            # A partial/failed apply must surface the per-field failure
            # breakdown + Retry. Match the regular profile-apply path, which
            # shows ONLY the failure modal: creating the "Saved as" result
            # modal in this same completion pass too would stack two modals,
            # and per the benched modal law (see _defer_modal_swap) the second
            # never renders — hiding the failure modal the user needs. The
            # "Saved as <name>" outcome is preserved in the result banner above.
            self._show_apply_failure_modal(self._last_apply_result)
        else:
            safe_import.open_result(self)

    def _create_safe_import_restore_point(self) -> str | None:
        """Capture a real Restore Point before Safe Import applies to the controller.

        Migrated from the old WrapperProfileStore-based bridge to the
        :class:`~zd_app.services.restore_point_service.RestorePointService`
        introduced by the restore-point storage rework. The design was explicit: the old
        bridge was a reasonable interim, but should not become the product
        model. The new service owns the fresh-read rule + coverage map +
        atomic-write semantics that the old bridge couldn't provide.

        Returns the restore-point title for ``audit.restore_point_name`` so
        the existing Safe Import result modal still displays it; returns
        ``None`` if no Restore Points service is wired or the capture
        could not produce a snapshot.
        """

        if self.restore_point_service is None:
            return None
        rp = self._capture_restore_point_safe(
            RestorePointTrigger(
                type="before_safe_import_apply",
                source_label="Safe Import",
                reason="Created automatically before applying imported profile to controller",
            ),
        )
        return rp.title if rp is not None else None

    def _verify_safe_import_write(
        self, snapshot: ControllerSnapshot, audit: safe_import_model.ImportAudit
    ) -> None:
        """Earn ``audit.controller_write = "verified"`` by read-back, never by ACKs.

        WriteFile-OK does not mean the firmware committed (the in-burst
        rejection family documented in settings_apply_coordinator), so after a
        fully-ACKed Safe Import apply the device is read back and compared
        against what was applied — the same honesty Restore Points already
        practices. Anything short of a clean comparison downgrades to "sent"
        with the reason stashed on the audit; verify is best-effort and must
        never crash the apply flow.
        """

        audit.verify_mismatched = []
        audit.verify_unverifiable = []
        audit.verify_read_failed = False

        # Firmware wants a quiet interval after a write burst before the next
        # feature-report read batch (same rationale as the post-apply refresh
        # in _apply_wrapper_profile_snapshot).
        time.sleep(POST_APPLY_READ_SETTLE_S)
        try:
            if self.restore_point_service is not None:
                readback, _read_success, _read_errors = (
                    self.restore_point_service.read_current_state_with_provenance()
                )
            else:
                readback = self.settings_service.get_all_settings()
        except Exception:  # noqa: BLE001 - best-effort: downgrade, never raise
            logger.warning(
                "safe import: post-apply verify read failed", exc_info=True
            )
            audit.controller_write = "sent"
            audit.verified = False
            audit.verify_read_failed = True
            return

        mismatched, unverifiable = verify_applied_snapshot(snapshot, readback)
        audit.verify_mismatched = mismatched
        audit.verify_unverifiable = unverifiable
        if mismatched or unverifiable:
            audit.controller_write = "sent"
            audit.verified = False
        else:
            audit.controller_write = "verified"
            audit.verified = True

    def open_support_guide(self, topic: str) -> None:
        guide = support_reference.get_guide(topic)
        if dpg.does_item_exist("support_guide_modal"):
            dpg.delete_item("support_guide_modal")

        with dpg.window(
            tag="support_guide_modal",
            label=guide.title,
            modal=True,
            width=720,
            height=520,
            no_resize=True,
        ):
            screen_title(guide.title)
            dpg.add_text(support_reference.localized_summary(guide), wrap=660)
            dpg.add_spacer(height=8)
            with dpg.child_window(height=340, border=True):
                for bullet in support_reference.localized_bullets(guide):
                    dpg.add_text(f"- {bullet}", wrap=640)
                    dpg.add_spacer(height=4)
            dpg.add_spacer(height=8)
            dpg.add_text(guide.evidence_note, color=self.COLORS["muted"], wrap=660)
            dpg.add_spacer(height=8)
            dpg.add_button(label=t("support.guide.close"), width=120, callback=lambda: dpg.delete_item("support_guide_modal"))

    def log_diagnostic_action(self, message: str) -> None:
        self.diagnostics_service.log_event(message)
        self.refresh_current_screen()

    def clear_diagnostic_logs(self) -> None:
        self.diagnostics_service.clear_event_log()
        self.device_service.clear_event_log()
        self.device_service.log_i18n_event("log.diagnostics.logs_cleared")
        self.refresh_shell()
        self.refresh_current_screen()

    def export_diagnostics_bundle(self) -> None:
        path = self.diagnostics_service.export_bundle(
            self.device_service.state,
            self.device_service.last_read_duration_ms,
            self.device_service.last_write_duration_ms,
            self.settings.diagnostics_bundle_dir,
        )
        self.device_service.log_i18n_event("log.diagnostics.exported_bundle", path=path)
        self.refresh_shell()
        self.refresh_current_screen()

    def export_rich_diagnostic_bundle(self) -> None:
        bundle = self.diagnostic_bundle_service
        if not isinstance(bundle, DiagnosticBundleService):
            self.device_service.log_i18n_event("log.diagnostics.bundle_unavailable")
            self.refresh_shell()
            self.refresh_current_screen()
            return
        try:
            manifest = bundle.preview_bundle_manifest(
                include_archived=True,
                health_report_limit=5,
                wear_ledger_days=90,
                device_identity=self._bundle_device_identity(),
            )
        except Exception:  # noqa: BLE001 — preview failure should not crash
            logger.exception("Diagnostics rich bundle: preview manifest raised")
            self.device_service.log_i18n_event("log.diagnostics.bundle_failed")
            self.refresh_shell()
            self.refresh_current_screen()
            return
        diagnostic_bundle_preview.open_preview_modal(
            self,
            manifest,
            on_export=lambda: self._export_rich_diagnostic_bundle_now(),
        )

    def _export_rich_diagnostic_bundle_now(self) -> None:
        """Export the full DiagnosticBundleService ZIP from the Diagnostics screen.

        Mirrors the Modules-screen export (``modules._generate_zip``): writes a
        ZIP via ``diagnostic_bundle_service.generate_bundle_zip`` (report.md +
        module passports + recent health reports + wear-ledger summary),
        records the wear-ledger event, and best-effort opens the folder. This
        supersedes the thin JSON :meth:`export_diagnostics_bundle` so the
        Support guidance's promise of a log-bearing bundle from Diagnostics
        holds. Guards a missing / wrong-typed service so the button degrades
        instead of crashing.
        """

        bundle = self.diagnostic_bundle_service
        if not isinstance(bundle, DiagnosticBundleService):
            self.device_service.log_i18n_event("log.diagnostics.bundle_unavailable")
            self.refresh_shell()
            self.refresh_current_screen()
            return
        target = (
            bundle.base_dir
            / f"diagnostic_bundle_{time.strftime('%Y-%m-%d_%H%M%S')}.zip"
        )
        try:
            result = bundle.generate_bundle_zip(
                target,
                include_archived=True,
                health_report_limit=5,
                wear_ledger_days=90,
                device_identity=self._bundle_device_identity(),
            )
        except Exception:  # noqa: BLE001 — surface as a failed export, never crash
            logger.exception("Diagnostics rich bundle: generate_bundle_zip raised")
            result = None
        if result is None:
            self.device_service.log_i18n_event("log.diagnostics.bundle_failed")
            self.refresh_shell()
            self.refresh_current_screen()
            return
        bundle.emit_generated_event(
            output_filename=result.name,
            bundle_format="zip",
            include_archived=True,
            health_report_limit=5,
            wear_ledger_days=90,
        )
        self.device_service.log_i18n_event(
            "log.diagnostics.exported_bundle", path=result
        )
        self._try_open_bundle_folder(result)
        self.refresh_shell()
        self.refresh_current_screen()

    def _bundle_device_identity(self) -> dict:
        """Best-effort device identity for the rich bundle's Hardware section.

        Mirrors ``modules._device_identity``: every missing field becomes
        "Unknown" downstream, so partial data is fine.
        """

        try:
            state = self.device_service.state
        except Exception:  # noqa: BLE001 — best-effort
            logger.exception("Diagnostics rich bundle: device state lookup raised")
            return {}
        return {
            "product_string": getattr(state, "product_name", None) or None,
            "firmware_version": getattr(state, "firmware_version", None) or None,
            "connection": getattr(state, "connection_state", None) or None,
            "active_slot": None,
        }

    def _try_open_bundle_folder(self, path) -> None:
        """Best-effort File Explorer open of the bundle's containing folder."""

        if not hasattr(os, "startfile"):
            return
        try:
            os.startfile(str(path.parent))  # type: ignore[attr-defined]
        except OSError:
            logger.exception("Diagnostics rich bundle: failed to open folder")

    def open_diagnostics_bundle_folder(self) -> None:
        """Best-effort File Explorer open for the configured diagnostics folder."""

        if not hasattr(os, "startfile"):
            return
        configured = self.settings.diagnostics_bundle_dir
        if _is_unc_path_value(configured):
            self.device_service.log_i18n_event(
                "log.diagnostics.open_folder_unc_rejected"
            )
            self.refresh_shell()
            self.refresh_current_screen()
            return
        requested = preferences.diagnostics_bundle_dir_open_target(configured)
        try:
            folder = self.diagnostics_service._safe_output_dir(str(requested))
        except Exception:  # noqa: BLE001 — opening a folder should never crash the app
            logger.exception("Diagnostics bundle folder: safe target resolution failed")
            self.device_service.log_i18n_event("log.diagnostics.open_folder_failed")
            self.refresh_shell()
            self.refresh_current_screen()
            return
        opened_fallback = _resolve_non_strict(folder) != _resolve_non_strict(requested)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))  # type: ignore[attr-defined]
            if opened_fallback:
                self.device_service.log_i18n_event(
                    "log.diagnostics.open_folder_safe_fallback"
                )
        except OSError:
            logger.exception("Diagnostics bundle folder: failed to open folder")
            self.device_service.log_i18n_event("log.diagnostics.open_folder_failed")
        finally:
            self.refresh_shell()
            self.refresh_current_screen()

    def update_setting(self, key: str, value) -> None:
        setattr(self.settings, key, value)
        self.settings_store.save(self.settings)
        self._log_setting_updated(key)
        self.refresh_shell()

    def _guarded_settings_save(self, context: str) -> bool:
        """Persist settings, logging (not raising) on OSError.

        A disk-full / locked-file save failure must not abort the caller before
        it applies the matching locale / router / UI changes — otherwise
        in-memory state diverges from what's persisted AND from what's on
        screen. Mirrors the first-run-ack + crash-review guarded saves. Returns
        whether the save succeeded.
        """

        try:
            self.settings_store.save(self.settings)
            return True
        except OSError:
            logger.exception("%s: settings save failed", context)
            return False

    def update_language(self, locale: str) -> None:
        normalized = locale if locale in {"en", "zh-CN"} else "en"
        self.settings.language = normalized
        # Apply the locale regardless of save success so in-memory state, the
        # active locale, and the visible UI stay consistent (A4).
        self._guarded_settings_save("update language")
        self._locale_router.set_locale(normalized)
        self._log_setting_updated("language")

    def restore_app_defaults(self) -> None:
        self.settings = AppSettings()
        self._guarded_settings_save("restore defaults")
        self._locale_router.set_locale(self.settings.language)
        self.device_service.log_i18n_event("log.settings.restored_defaults")
        # LocaleRouter no-ops when the locale is already current, but Restore
        # Defaults flips other settings (developer toggle, logging verbosity,
        # etc.) that the UI surfaces only after a rebuild. Always rebuild here;
        # the rebuild is idempotent so a second-from-_on_locale_changed call
        # on actual locale change is harmless.
        self.rebuild_full_ui()

    def _on_locale_changed(self, old_locale: str, new_locale: str) -> None:
        bind_default_font(new_locale)
        self.rebuild_full_ui()

    def _toggle_legacy_screens(self, value: bool) -> None:
        self.settings.show_legacy_screens = bool(value)
        # Apply the toggle + rebuild regardless of save success (A4).
        self._guarded_settings_save("toggle legacy screens")
        self._log_setting_updated("show_legacy_screens")
        self.rebuild_full_ui()

    def _log_setting_updated(self, key: str) -> None:
        label_key = SETTING_LABEL_KEYS.get(key)
        if label_key is None:
            self.device_service.log_i18n_event("log.setting.updated", label=key)
        else:
            self.device_service.log_i18n_event("log.setting.updated", label_key=label_key)


def _last_apply_result_text(device_service) -> str:
    value = getattr(device_service, "last_apply_result", None)
    if isinstance(value, str) and value:
        return value
    if isinstance(value, LogEntry):
        return render_log_message(value)
    # Pre-apply baseline: friendlier "Ready." than the older
    # "No writes attempted yet." complaint-toned sentinel
    # (operator UX decision).
    return t("apply.idle_ready")


def _top_profile_label(state) -> str:
    active_config_label = home._active_config_label(state)
    if active_config_label == "Not verified":
        return t("status.config.not_verified_label")
    if active_config_label.startswith("Config "):
        return t("status.config.slot", slot=active_config_label.removeprefix("Config "))
    return active_config_label


def _top_profile_not_verified(state) -> bool:
    return home._active_config_label(state) == "Not verified"


def _polling_rate_label(snapshot: ControllerSnapshot | None) -> str:
    if snapshot is None or snapshot.polling_rate is None:
        return t("shell.polling_rate.unknown")
    return POLLING_RATE_LABEL_BY_ENUM.get(snapshot.polling_rate, t("shell.polling_rate.unknown"))


def _apply_status_active(shell: AppShell) -> bool:
    return (
        shell._apply_status_clear_after is not None
        and time.time() < shell._apply_status_clear_after
    )


def _build_nav_themes() -> tuple[int, int, int, int]:
    """Build the four nav-row themes.

    Returns ``(active_button, inactive_button, active_strip, inactive_strip)``.

    Active state reads from a low-alpha accent wash on the row
    (``NAV_ACTIVE_ROW_TINT``) plus the full-accent green left-edge strip — the
    heavy accent fill that used to cover the whole active button is gone. A
    later refinement swapped the active row's neutral bg.raised lift for the
    green wash so the active item reads
    unmistakably on dim monitors where the strip + gray lift was too faint; the
    wash ties the row to the strip's hue without becoming a full accent fill.
    All three button states use the same tint so the active row stays stable
    (no hover/press flicker on the screen you are already on). The inactive
    hover state still lifts to bg.raised so the user gets a clear "this is
    clickable" cue, but the press-flash uses accent.muted instead of the full
    accent.primary so accent-green stays reserved for the active indicator +
    truly actionable signals.
    """

    with dpg.theme() as active_button:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, NAV_ACTIVE_ROW_TINT)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, NAV_ACTIVE_ROW_TINT)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, NAV_ACTIVE_ROW_TINT)
    with dpg.theme() as inactive_button:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, COLORS["bg.surface"])
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, COLORS["bg.raised"])
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COLORS["accent.muted"])
    with dpg.theme() as active_strip:
        with dpg.theme_component(dpg.mvChildWindow):
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, COLORS["accent.primary"])
    with dpg.theme() as inactive_strip:
        with dpg.theme_component(dpg.mvChildWindow):
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, COLORS["bg.surface"])
    return active_button, inactive_button, active_strip, inactive_strip


def _build_sensitivity_plot_themes() -> dict[str, int]:
    """Series themes for the 8-point sensitivity curve plot.

    Built once at theme setup (mirrors :func:`_build_nav_themes`) and bound per
    series by the controller screen: a bright accent line for the live curve,
    matching accent markers on the 8 anchors, and a faint dimmed diagonal for the
    y=x reference so deviation from linear reads at a glance. The controller
    screen no-ops the bind when this is ``None`` (render paths that skip
    ``_setup_theme`` — e.g. unit tests — just draw DPG default colors).
    """

    curve_color = COLORS["accent.primary"]
    marker_color = COLORS["accent.hover"]
    # text.muted at a low alpha so the reference line sits quietly behind the
    # live curve instead of competing with it.
    diagonal_color = (*COLORS["text.muted"][:3], 90)
    with dpg.theme() as curve_theme:
        with dpg.theme_component(dpg.mvLineSeries):
            dpg.add_theme_color(dpg.mvPlotCol_Line, curve_color, category=dpg.mvThemeCat_Plots)
    with dpg.theme() as scatter_theme:
        with dpg.theme_component(dpg.mvScatterSeries):
            dpg.add_theme_color(dpg.mvPlotCol_MarkerFill, marker_color, category=dpg.mvThemeCat_Plots)
            dpg.add_theme_color(dpg.mvPlotCol_MarkerOutline, marker_color, category=dpg.mvThemeCat_Plots)
    with dpg.theme() as diagonal_theme:
        with dpg.theme_component(dpg.mvLineSeries):
            dpg.add_theme_color(dpg.mvPlotCol_Line, diagonal_color, category=dpg.mvThemeCat_Plots)
    return {"curve": curve_theme, "scatter": scatter_theme, "diagonal": diagonal_theme}


def set_window_title_unicode(title: str) -> int:
    """Set this process window title through SetWindowTextW on Windows."""

    user32 = getattr(getattr(ctypes, "windll", None), "user32", None)
    if user32 is None:
        return 0

    target_pid = os.getpid()
    found: list[int] = []
    enum_proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user32.GetWindowThreadProcessId.restype = w.DWORD
    user32.IsWindowVisible.argtypes = [w.HWND]
    user32.IsWindowVisible.restype = w.BOOL
    user32.SetWindowTextW.argtypes = [w.HWND, w.LPCWSTR]
    user32.SetWindowTextW.restype = w.BOOL

    @enum_proc
    def callback(hwnd, _lparam):
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == target_pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    for hwnd in found:
        user32.SetWindowTextW(hwnd, title)
    return len(found)
