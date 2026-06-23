"""Read-only advanced-session preflight checks for app guidance."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable


logger = logging.getLogger(__name__)

UNKNOWN_PREFLIGHT_STATE_LABEL = "Unknown"
DEFAULT_PREFLIGHT_ACTION_HINT = "Recheck the environment before advanced controller actions."

# Gate-decision reason tokens. Kept in sync with the healthy-session
# gate policy table below and with the label map in
# zd_app/i18n/labels.gate_decision_label. Changes to either side require
# touching both; the Unknown-fallback-plus-warning-log discipline catches
# silent drift.
GATE_REASON_ALLOWED = "gate_allowed"
GATE_REASON_ALLOWED_UI_MISMATCH = "gate_allowed_ui_mismatch"
GATE_REASON_FORCED_ALLOW = "gate_forced_allow"
GATE_REASON_FORCED_BLOCK = "gate_forced_block"
GATE_REASON_BLOCKED_PUBLIC_VISIBLE = "gate_blocked_public_visible"
GATE_REASON_BLOCKED_DEVICE_MISSING = "gate_blocked_device_missing"
GATE_REASON_BLOCKED_NO_PUBLIC_DEVICE = "gate_blocked_no_public_device"
GATE_REASON_BLOCKED_NO_APP_NO_DEVICE = "gate_blocked_no_app_no_device"
GATE_REASON_BLOCKED_UNCLEAR = "gate_blocked_unclear"
GATE_REASON_BLOCKED_NOT_CHECKED = "gate_blocked_not_checked"
GATE_REASON_BLOCKED_PREFLIGHT_ERROR = "gate_blocked_preflight_error"
GATE_REASON_BLOCKED_UNKNOWN = "gate_blocked_unknown"

# Policy table: preflight state token -> (blocked, reason_token).
# public_visible is treated as block (the conservative default): acting on
# settings while the vendor UI is only partially up risks writing to a
# half-initialized device. This default is intentional; revisit only with
# targeted testing of the public_visible state.
_DEFAULT_GATE_POLICY_TABLE: dict[str, tuple[bool, str]] = {
    "ready_for_settings": (False, GATE_REASON_ALLOWED),
    "public_visible": (True, GATE_REASON_BLOCKED_PUBLIC_VISIBLE),
    "public_visible_ui_mismatch": (False, GATE_REASON_ALLOWED_UI_MISMATCH),
    "device_missing": (True, GATE_REASON_BLOCKED_DEVICE_MISSING),
    "app_only_no_public_device": (True, GATE_REASON_BLOCKED_NO_PUBLIC_DEVICE),
    "no_app_no_device": (True, GATE_REASON_BLOCKED_NO_APP_NO_DEVICE),
    "unclear": (True, GATE_REASON_BLOCKED_UNCLEAR),
    "not_checked": (True, GATE_REASON_BLOCKED_NOT_CHECKED),
    "error": (True, GATE_REASON_BLOCKED_PREFLIGHT_ERROR),
}


@dataclass(frozen=True)
class SessionPreflightSnapshot:
    state: str
    state_label: str
    summary: str
    action_hint: str
    last_checked_label: str
    official_ui_label: str
    public_probe_label: str
    hidden_visibility_label: str


class PreflightService:
    def __init__(self, runner: Callable[[], object] | None = None):
        self._runner = runner or _default_runner
        self.last_snapshot = SessionPreflightSnapshot(
            state="not_checked",
            state_label="Not Checked Yet",
            summary=(
                "Session features depend on the official Controller Settings window plus an active wrapper controller session. "
                "Run the preflight check before treating a failed advanced action as a controller fault."
            ),
            action_hint="Run Session Preflight before advanced controller actions.",
            last_checked_label="Not Checked Yet",
            official_ui_label="Unknown",
            public_probe_label="Unknown",
            hidden_visibility_label="Unknown",
        )

    def run_session_preflight(self) -> SessionPreflightSnapshot:
        checked_at = time.strftime("%H:%M:%S")
        try:
            result = self._runner()
        except Exception as exc:
            self.last_snapshot = SessionPreflightSnapshot(
                state="error",
                state_label="Preflight Failed",
                summary=(
                    "The preflight check could not complete, so the app cannot classify the current advanced-session state."
                ),
                action_hint="Retry the preflight check before drawing any transport conclusion.",
                last_checked_label=checked_at,
                official_ui_label="Probe Failed",
                public_probe_label="Probe Failed",
                hidden_visibility_label=str(exc),
            )
            return self.last_snapshot

        self.last_snapshot = SessionPreflightSnapshot(
            state=str(result.state),
            state_label=_state_label(str(result.state)),
            summary=str(result.summary),
            action_hint=_action_hint(str(result.state)),
            last_checked_label=checked_at,
            official_ui_label=_official_ui_label(result.official_ui),
            public_probe_label=_public_probe_label(result.public_probe, result.public_paths),
            hidden_visibility_label=_hidden_visibility_label(result.hidden_visible_paths),
        )
        return self.last_snapshot


def _state_label(state: str) -> str:
    label = {
        "ready_for_settings": "Ready For Settings",
        "public_visible": "Public Ready, UI Not Ready",
        "public_visible_ui_mismatch": "Windows/App Mismatch",
        "device_missing": "No Device Visible",
        "app_only_no_public_device": "App Open, No Public Device",
        "no_app_no_device": "No App, No Device",
        "unclear": "Mixed Visibility",
    }.get(state)
    if label is not None:
        return label
    logger.warning("Unmapped preflight state label requested: %s", state)
    return UNKNOWN_PREFLIGHT_STATE_LABEL


def _action_hint(state: str) -> str:
    hint = {
        "ready_for_settings": "This is a good time to try Activate Config or a guarded Lighting action.",
        "public_visible": "Get the official app to a usable Device Settings state before testing session features.",
        "public_visible_ui_mismatch": "Treat this as an app/device visibility mismatch, not a hidden-transport negative.",
        "device_missing": "Reconnect the controller before treating any advanced-feature failure as protocol evidence.",
        "app_only_no_public_device": "Fix Windows-side device visibility before drawing transport conclusions.",
        "no_app_no_device": "Open the official app and reconnect the controller first.",
        "unclear": "Check the raw readiness rows before interpreting a session-feature failure.",
    }.get(state)
    if hint is not None:
        return hint
    logger.warning("Unmapped preflight action hint requested: %s", state)
    return DEFAULT_PREFLIGHT_ACTION_HINT


def _official_ui_label(official_ui: object) -> str:
    settings_window = getattr(official_ui, "settings_window", None)
    device_settings_button = bool(getattr(official_ui, "device_settings_button", False))
    no_device_connected = bool(getattr(official_ui, "no_device_connected", False))
    app_running = bool(getattr(official_ui, "app_running", False))

    if settings_window:
        return "Controller Settings Open"
    if device_settings_button:
        return "Device Settings Visible"
    if no_device_connected:
        return "No Device Connected"
    if app_running:
        return "Official App Open"
    return "Official App Closed"


def _public_probe_label(public_probe: object, public_paths: tuple[str, ...] | list[str]) -> str:
    if bool(getattr(public_probe, "ok", False)):
        return "Public Identify OK"
    if public_paths:
        return "Public Path Visible"
    return "No Public Path"


def _hidden_visibility_label(hidden_visible_paths: tuple[str, ...] | list[str]) -> str:
    if not hidden_visible_paths:
        return "Hidden Not Visible"
    count = len(hidden_visible_paths)
    noun = "path" if count == 1 else "paths"
    return f"Hidden Visible ({count} {noun})"


def _default_runner():
    from zd_app.protocol import preflight_visibility as ptv

    official_ui = ptv.run_official_ui_probe(False)
    public_paths = ptv.public_hid_paths()
    # Retry-aware variant of the public-identify probe. Eliminates a
    # false-positive "unclear" preflight classification seen in local
    # testing: the single-shot
    # public identify probe failed once with ERROR_FILE_NOT_FOUND in an
    # otherwise-healthy Controller Settings session and drove the gate
    # to block. The retry ladder retries on RETRYABLE_OPEN_ERRORS
    # membership with 100 ms + 250 ms backoffs, worst-case 350 ms added
    # latency only on genuine failures, zero added latency on the common
    # healthy-session path.
    public_probe = ptv.run_public_identify_probe_candidates_with_retry(
        public_paths,
        mode_name="read_write",
        share_mode=ptv.PUBLIC_IDENTIFY_OPEN_PROFILE.share_mode,
        creation_disposition=ptv.PUBLIC_IDENTIFY_OPEN_PROFILE.creation_disposition,
        flags_and_attributes=ptv.PUBLIC_IDENTIFY_OPEN_PROFILE.flags_and_attributes,
    )
    hidden_visible_paths = ptv.filter_hid_paths(ptv.HIDDEN_VENDOR_ID, ptv.HIDDEN_PRODUCT_ID)
    return ptv.summarize_preflight(
        launch_official=False,
        official_ui=official_ui,
        public_paths=public_paths,
        public_probe=public_probe,
        hidden_visible_paths=hidden_visible_paths,
    )


@dataclass(frozen=True)
class GateDecision:
    """Result of evaluating the healthy-session gate against a preflight snapshot.

    ``blocked`` is the operative field: callers skip the gated operation
    when ``blocked`` is True. ``reason`` is a stable token (one of the
    ``GATE_REASON_*`` constants above) suitable for log correlation.
    ``label`` is the human-readable form resolved through
    ``zd_app.i18n.labels.gate_decision_label`` with the
    Unknown-fallback discipline; it is for diagnostic surfaces only and
    is not primary user-facing copy (screens rendering a blocked gate
    use the existing preflight ``state_label`` / ``action_hint`` strings).
    """

    blocked: bool
    reason: str
    label: str


class GatePolicy:
    """Maps ``SessionPreflightSnapshot.state`` to a ``GateDecision``.

    Instantiate via ``default_gate_policy()`` for the table-driven
    healthy-session gate policy, or via
    ``allow_all_gate_policy()`` / ``block_all_gate_policy()`` for the
    operator-override modes selected by ``ZD_HIDDEN_OPENER_GATE``. A
    custom table can be passed for unit tests that want to pin a
    particular policy without depending on the default.
    """

    def __init__(
        self,
        *,
        force: str | None = None,
        table: dict[str, tuple[bool, str]] | None = None,
    ) -> None:
        if force not in (None, "allow", "block"):
            raise ValueError(
                f"GatePolicy force must be one of None/'allow'/'block', got: {force!r}"
            )
        self._force = force
        self._table = table if table is not None else _DEFAULT_GATE_POLICY_TABLE

    def evaluate(self, snapshot: SessionPreflightSnapshot) -> GateDecision:
        # Deferred import: the label registry is the single source of truth
        # for human-readable gate-decision text, but importing it at module
        # scope would tie services-package import time to i18n locale init.
        from zd_app.i18n.labels import gate_decision_label

        if self._force == "allow":
            reason = GATE_REASON_FORCED_ALLOW
            blocked = False
        elif self._force == "block":
            reason = GATE_REASON_FORCED_BLOCK
            blocked = True
        else:
            state = snapshot.state
            entry = self._table.get(state)
            if entry is None:
                logger.warning(
                    "Unmapped preflight state for gate evaluation: %s", state
                )
                reason = GATE_REASON_BLOCKED_UNKNOWN
                blocked = True
            else:
                blocked, reason = entry

        return GateDecision(blocked=blocked, reason=reason, label=gate_decision_label(reason))


def default_gate_policy() -> GatePolicy:
    """Table-driven healthy-session gate policy."""
    return GatePolicy()


def allow_all_gate_policy() -> GatePolicy:
    """Operator-override policy that allows every snapshot.

    Selected by ``ZD_HIDDEN_OPENER_GATE=allow``. Intended for
    degraded-state validation trials and debugging only; do not ship as
    a default.
    """
    return GatePolicy(force="allow")


def block_all_gate_policy() -> GatePolicy:
    """Operator-override policy that blocks every snapshot.

    Selected by ``ZD_HIDDEN_OPENER_GATE=block``. Intended for testing
    the gate-block UI surface without physically reproducing a degraded
    state.
    """
    return GatePolicy(force="block")
