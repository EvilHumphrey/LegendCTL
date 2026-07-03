"""Wide-window context rail for core screens.

The rail is deliberately display/navigation only. It reads the shell's cached
device/profile state and routes buttons to existing shell navigation/actions;
it never touches device services that would perform a fresh read.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator

import dearpygui.dearpygui as dpg

from zd_app.i18n import t
from zd_app.ui import trust_front_door, trust_labels
from zd_app.ui.themes import COLORS
from zd_app.ui.typography import section_title


WIDE_VIEWPORT_THRESHOLD = 1600
RAIL_WIDTH = 280
RAIL_GAP = 12
WORK_COLUMN_WIDTH = 1040
LIVE_VERIFY_WORK_WIDTH = 1220
RAIL_SCREEN_IDS = frozenset({"home", "controller", "diagnostics", "live_verify"})
_SHELL_HORIZONTAL_RESERVE = 250
_RAIL_WRAP = RAIL_WIDTH - 32

RAIL_TAG = "right_rail"
DEVICE_VALUE_TAG = "right_rail_device_model"
CONNECTION_VALUE_TAG = "right_rail_connection"
FIRMWARE_VALUE_TAG = "right_rail_firmware"
PROFILE_VALUE_TAG = "right_rail_active_profile"
PENDING_VALUE_TAG = "right_rail_pending_changes"
TRUST_VALUE_TAG = "right_rail_trust_posture"


def is_wide(shell) -> bool:
    """Return True when the current viewport should use the wide rail layout."""

    width = _viewport_width(shell)
    return width >= WIDE_VIEWPORT_THRESHOLD


def screen_uses_rail(screen_id: str) -> bool:
    return screen_id in RAIL_SCREEN_IDS


def wide_state(shell) -> bool:
    screen = getattr(shell, "current_screen", "")
    canonical = getattr(shell, "_canonical_screen_id", lambda value: value)
    return screen_wide_state(shell, canonical(screen))


def screen_wide_state(shell, screen_id: str) -> bool:
    if not screen_uses_rail(screen_id):
        return False
    return can_fit_rail(shell, work_width_for_screen(screen_id))


def work_width_for_screen(screen_id: str) -> int:
    if screen_id == "live_verify":
        return LIVE_VERIFY_WORK_WIDTH
    return WORK_COLUMN_WIDTH


def can_fit_rail(shell, work_width: int = WORK_COLUMN_WIDTH) -> bool:
    if not is_wide(shell):
        return False
    return _content_width(shell) >= work_width + RAIL_GAP + RAIL_WIDTH


def trailing_spacer_width(shell, work_width: int) -> int:
    """Best-effort spacer that places the rail in the freed right-side space."""

    content_width = _content_width(shell)
    if content_width <= 0:
        return RAIL_GAP
    used = work_width + RAIL_GAP + RAIL_WIDTH
    return RAIL_GAP + max(0, content_width - used)


@contextmanager
def rail_screen(
    shell,
    parent: str,
    *,
    screen_id: str,
    root_tag: str,
    work_tag: str,
    work_width: int = WORK_COLUMN_WIDTH,
) -> Iterator[None]:
    """Open a screen root and, on wide viewports, add the right rail."""

    with dpg.child_window(
        parent=parent,
        tag=root_tag,
        width=-1,
        autosize_y=True,
        border=False,
    ):
        if not screen_wide_state(shell, screen_id):
            yield
            return
        with dpg.group(horizontal=True, tag=f"{root_tag}_wide_layout"):
            with dpg.child_window(
                tag=work_tag,
                width=work_width,
                border=False,
                auto_resize_y=True,
                autosize_y=False,
            ):
                yield
            spacer = trailing_spacer_width(shell, work_width)
            dpg.add_spacer(width=spacer)
            build(shell)


def build(shell) -> None:
    with dpg.child_window(
        tag=RAIL_TAG,
        width=RAIL_WIDTH,
        border=True,
        auto_resize_y=True,
        autosize_y=False,
    ):
        section_title(t("right_rail.title"))
        _metric(t("right_rail.device_model"), _device_model(shell), DEVICE_VALUE_TAG)
        _metric(t("right_rail.connection"), _connection_value(shell), CONNECTION_VALUE_TAG)
        _metric(
            t("right_rail.firmware"),
            _firmware_value(shell),
            FIRMWARE_VALUE_TAG,
            value_color=_firmware_value_color(shell),
        )
        _metric(
            t("right_rail.active_profile"),
            _active_profile_value(shell),
            PROFILE_VALUE_TAG,
            value_color=_active_profile_value_color(shell),
        )
        _metric(
            t("right_rail.pending_changes"),
            str(_pending_count(shell)),
            PENDING_VALUE_TAG,
        )
        dpg.add_spacer(height=8)
        dpg.add_text(
            t("right_rail.trust_posture"),
            tag=TRUST_VALUE_TAG,
            color=shell.COLORS["muted"],
            wrap=_RAIL_WRAP,
        )
        dpg.add_spacer(height=10)
        dpg.add_button(
            label=t("footer.read"),
            tag="right_rail_read",
            width=-1,
            callback=lambda: shell.refresh_from_controller(),
        )
        dpg.add_button(
            label=t("nav.health_report"),
            tag="right_rail_health",
            width=-1,
            callback=lambda: shell.switch_screen("health_report"),
        )
        dpg.add_button(
            label=t("trust_front_door.link.self_check"),
            tag="right_rail_trust",
            width=-1,
            callback=lambda: trust_front_door.open_trust_surface(shell, "self_check"),
        )


def refresh(shell) -> None:
    if not dpg.does_item_exist(RAIL_TAG):
        return
    _set(DEVICE_VALUE_TAG, _device_model(shell))
    _set(CONNECTION_VALUE_TAG, _connection_value(shell))
    _set(FIRMWARE_VALUE_TAG, _firmware_value(shell), _firmware_value_color(shell))
    _set(
        PROFILE_VALUE_TAG,
        _active_profile_value(shell),
        _active_profile_value_color(shell),
    )
    _set(PENDING_VALUE_TAG, str(_pending_count(shell)))
    _set(TRUST_VALUE_TAG, t("right_rail.trust_posture"))


def _metric(label: str, value: str, tag: str, *, value_color=None) -> None:
    dpg.add_text(label, color=(144, 153, 170, 255), wrap=_RAIL_WRAP)
    kwargs = {"tag": tag, "wrap": _RAIL_WRAP}
    if value_color is not None:
        kwargs["color"] = value_color
    dpg.add_text(value, **kwargs)
    dpg.add_spacer(height=5)


def _set(tag: str, value: str, color=None) -> None:
    if dpg.does_item_exist(tag):
        dpg.set_value(tag, value)
        if color is not None:
            dpg.configure_item(tag, color=color)


def _viewport_width(shell) -> int:
    getter: Callable[[], int] | None = getattr(shell, "_viewport_client_width", None)
    if callable(getter):
        original_bound_getter = (
            getattr(getter, "__self__", None) is shell
            and getattr(getattr(getter, "__func__", None), "__name__", "")
            == "_viewport_client_width"
        )
        if original_bound_getter and not getattr(shell, "_dpg_context_ready", False):
            return -1
        try:
            width = int(getter())
        except Exception:  # noqa: BLE001 - layout fallback only
            width = -1
        return width
    return -1


def _content_width(shell) -> int:
    viewport_width = _viewport_width(shell)
    if viewport_width <= 0:
        return 0
    return max(0, viewport_width - _SHELL_HORIZONTAL_RESERVE)


def _device_model(shell) -> str:
    state = shell.device_service.state
    if getattr(state, "connection_state", "") == "no_device":
        return t("status.no_device")
    return str(getattr(state, "product_name", "") or t("common.unknown"))


def _connection_value(shell) -> str:
    state = shell.device_service.state
    connection_state = getattr(state, "connection_state", "")
    if connection_state == "connected":
        mode = getattr(state, "connection_mode", "") or t("common.unknown")
        return t("right_rail.connection.connected", mode=mode)
    if connection_state == "no_device":
        return t("status.no_device")
    label = trust_labels.connection_state_label(connection_state)
    if label == "Unknown":
        return t("common.unknown")
    return label


def _firmware_value(shell) -> str:
    formatter = getattr(shell.device_service, "format_firmware_version", None)
    if callable(formatter):
        value = str(formatter())
    else:
        firmware = getattr(shell.device_service.state, "firmware_version", "")
        value = str(firmware or t("common.unknown"))
    if _has_retained_firmware(shell.device_service.state) and not _is_connected(shell):
        return t("device.value.last_read", value=value)
    return value


def _active_profile_value(shell) -> str:
    state = shell.device_service.state
    profile = trust_labels.active_config_label(state)
    if profile == "Not verified":
        profile = t("profile.config_state.not_verified")
    elif profile.startswith("Config "):
        profile = t("profile.config_state.config", n=profile.removeprefix("Config "))
    if _has_retained_active_profile(state) and not _is_connected(shell):
        profile = t("device.value.last_read", value=profile)
    source_fn = getattr(shell.device_service, "summary_source_label_for", None)
    source = source_fn("active_profile") if callable(source_fn) else t("common.unknown")
    return t("right_rail.active_profile.value", profile=profile, source=source)


def _firmware_value_color(shell):
    if _has_retained_firmware(shell.device_service.state) and not _is_connected(shell):
        return COLORS["text.secondary"]
    return COLORS["text.primary"]


def _active_profile_value_color(shell):
    if _has_retained_active_profile(shell.device_service.state) and not _is_connected(
        shell
    ):
        return COLORS["text.secondary"]
    return COLORS["text.primary"]


def _is_connected(shell) -> bool:
    return getattr(shell.device_service.state, "connection_state", "") == "connected"


def _has_retained_firmware(state) -> bool:
    firmware = str(getattr(state, "firmware_version", "") or "").strip()
    return bool(firmware and firmware != "Unknown")


def _has_retained_active_profile(state) -> bool:
    sources = getattr(state, "summary_sources", {}) or {}
    return sources.get("active_profile", "unknown") != "unknown"


def _pending_count(shell) -> int:
    pending_fn = getattr(shell.profile_service, "pending_changes_count", None)
    if not callable(pending_fn):
        return 0
    try:
        return int(pending_fn())
    except Exception:  # noqa: BLE001 - display fallback only
        return 0
