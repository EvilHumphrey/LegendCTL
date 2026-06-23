"""UI-neutral label-mapping helpers shared by services and UI.

These return human-readable strings without touching DPG, so services can
call them for diagnostic evidence + capture attribution without taking
on a UI-tier dependency. Pure-string formatting only; no widget code lives
here.

Moved here from the UI layer so services can build these labels without a
UI-tier dependency: ``preflight_service`` (a service) previously had to reach
into the UI layer to get ``gate_decision_label``.
"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


UNKNOWN_GATE_DECISION_LABEL = "Unknown"


def gate_decision_label(reason_token: str) -> str:
    """Map a healthy-session gate reason token to a human-readable label.

    Tokens are produced by GatePolicy.evaluate in
    zd_app/services/preflight_service.py per the gate policy table there.
    Labels here are for
    internal diagnostic evidence and capture attribution; they are
    not intended as primary user-facing copy (screens should use the
    existing preflight state_label / action_hint strings when rendering
    gate-blocked states to the user).
    """
    label = {
        "gate_allowed": "Allowed",
        "gate_allowed_ui_mismatch": "Allowed (Windows/App Mismatch)",
        "gate_forced_allow": "Allowed (Forced)",
        "gate_forced_block": "Blocked (Forced)",
        "gate_blocked_public_visible": "Blocked (Public Ready, UI Not Ready)",
        "gate_blocked_device_missing": "Blocked (No Device Visible)",
        "gate_blocked_no_public_device": "Blocked (App Open, No Public Device)",
        "gate_blocked_no_app_no_device": "Blocked (No App, No Device)",
        "gate_blocked_unclear": "Blocked (Mixed Visibility)",
        "gate_blocked_not_checked": "Blocked (Preflight Not Run)",
        "gate_blocked_preflight_error": "Blocked (Preflight Failed)",
        "gate_blocked_unknown": "Blocked (Unknown Preflight State)",
    }.get(reason_token)
    if label is not None:
        return label
    logger.warning("Unmapped gate decision label requested: %s", reason_token)
    return UNKNOWN_GATE_DECISION_LABEL
