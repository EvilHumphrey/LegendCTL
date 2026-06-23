"""Shared user-facing trust labels.

UI-tier labels live here. The DPG-free ``gate_decision_label`` was moved to
``zd_app.i18n.labels`` so services can call it without a UI dependency.
"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)

UNKNOWN_CONNECTION_STATE_LABEL = "Unknown"


def active_config_label(state) -> str:
    if state.summary_sources.get("active_profile") == "unknown":
        return "Not verified"
    return f"Config {state.active_onboard_profile}"


def connection_state_label(connection_state: str) -> str:
    label = {
        "connected": "Connected",
        "connecting": "Checking",
        "no_device": "No Device",
        "unsupported_firmware": "Unsupported Firmware",
        "wrong_mode": "Wrong Mode",
        "device_error": "Device Error",
    }.get(connection_state)
    if label is not None:
        return label
    logger.warning("Unmapped connection state label requested: %s", connection_state)
    return UNKNOWN_CONNECTION_STATE_LABEL
