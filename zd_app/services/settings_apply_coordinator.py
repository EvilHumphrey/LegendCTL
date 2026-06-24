"""Coordinates the apply pipeline: per-setting dispatch, result aggregation,
retry-on-transient bookkeeping. DPG-free; the UI layer (AppShell) wires up
callbacks and renders modals.

Extracted from zd_app.ui.app_shell as part of decomposing AppShell into
DPG-free service helpers.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from zd_app.i18n import t
from zd_app.services.settings_service import (
    BackPaddleBinding,
    ControllerSnapshot,
    MacroSlot,
    SettingsService,
)


logger = logging.getLogger(__name__)


def _safe_invoke(callback: Callable[..., object] | None, *args, _name: str = "callback") -> None:
    # A misbehaving listener must not abort the apply pipeline; downstream
    # writes need to run regardless of upstream callback failures.
    if callback is None:
        return
    try:
        callback(*args)
    except Exception as exc:
        logger.warning("Coordinator listener %s raised: %s", _name, exc)


def outcome_is_success(outcome) -> bool:
    # None means the write_fn returned no outcome at all (a miswired stub or
    # service, e.g. a bare None return). That is a wiring bug, not a success —
    # count it as a failure so it can't silently masquerade as applied.
    if outcome is None:
        return False
    name = getattr(outcome, "name", None)
    value = getattr(outcome, "value", None)
    return name in {"OK", "OK_WITH_RETRY"} or value in {"ok", "ok_with_retry"}


def outcome_used_retry(outcome) -> bool:
    name = getattr(outcome, "name", None)
    value = getattr(outcome, "value", None)
    return name == "OK_WITH_RETRY" or value == "ok_with_retry"


def outcome_label(outcome) -> str:
    if outcome is None:
        return "ok"
    return str(getattr(outcome, "value", outcome))


def result_error_text(result) -> str:
    outcome = getattr(result, "outcome", None)
    if outcome is None:
        # outcome_is_success(None) is False, so a no-outcome result lands on
        # the failure path; name the wiring bug instead of labelling it "ok".
        return "write returned no outcome"
    label = outcome_label(outcome)
    if label == "write_failed":
        message = t("apply.error.write_failed")
    elif label == "verify_failed":
        message = t("apply.error.verify_failed")
    elif label == "device_not_found":
        message = t("apply.error.device_not_found")
    elif label == "open_failed":
        message = t("apply.error.open_failed")
    else:
        message = label
    error_code = getattr(result, "error_code", None)
    if error_code is not None:
        message = t("apply.error.with_code", message=message, code=error_code)
    return message


def result_is_transient(result) -> bool:
    outcome = getattr(result, "outcome", None)
    return outcome_label(outcome) == "write_failed"


@dataclass
class ApplyFailure:
    setting_label: str
    error: str
    is_transient: bool
    retry_fn: Callable[[], object] | None = field(default=None, repr=False, compare=False)
    on_success: Callable[[], None] | None = field(default=None, repr=False, compare=False)


@dataclass
class ApplyResult:
    total_attempted: int = 0
    succeeded: int = 0
    retry_recoveries: int = 0
    failed: list[ApplyFailure] = field(default_factory=list)


class SettingsApplyCoordinator:
    """Drives the apply pipeline against a SettingsService and aggregates outcomes.

    The coordinator is i18n-aware (via ``zd_app.i18n.t``) but DPG-free; rendering
    failure modals + recording log entries lives in the UI layer.
    """

    def __init__(
        self,
        settings_service: SettingsService | None,
        on_apply_started: Callable[[], None] | None = None,
        on_apply_finished: Callable[[ApplyResult], None] | None = None,
        step_size_trailer_delay_s: float = 0.1,
        field_trailer_delay_s: float = 0.1,
    ) -> None:
        self._settings_service = settings_service
        self._on_apply_started = on_apply_started
        self._on_apply_finished = on_apply_finished
        # Firmware quirk: 8 of 9 testable in-burst-eligible fields (vibration,
        # deadzones, sensitivities, triggers, button bindings, lighting zones —
        # and originally step_size) commit only intermittently (14-90%) inside
        # a multi-field burst. The cascade is broad, not specific to one cat.
        # The fix is a per-field trailer: each vulnerable write is preceded by
        # ~100 ms of firmware-quiet time. Hardware-validated at 100% commit per
        # field. A bulk-batch alternative was rejected after
        # the same cascade re-triggered inside a back-to-back batch
        # (sensitivity_right dropped to 14% at trailer position 4). Configurable
        # so tests can drive trailers with zero delay.
        self._step_size_trailer_delay_s = step_size_trailer_delay_s
        self._field_trailer_delay_s = field_trailer_delay_s

    def set_settings_service(self, settings_service: SettingsService | None) -> None:
        """Late-bind the settings service when the controller connects after startup."""

        self._settings_service = settings_service

    def apply_snapshot(
        self,
        snapshot: ControllerSnapshot,
        on_back_paddle_apply: Callable[[MacroSlot, BackPaddleBinding], None] | None = None,
    ) -> ApplyResult:
        _safe_invoke(self._on_apply_started, _name="on_apply_started")

        if self._settings_service is None:
            result = ApplyResult(
                total_attempted=0,
                succeeded=0,
                failed=[
                    ApplyFailure(
                        setting_label="settings_service",
                        error="apply.failure.error.not_available",
                        is_transient=False,
                    )
                ],
            )
            _safe_invoke(self._on_apply_finished, result, _name="on_apply_finished")
            return result

        result = ApplyResult()
        write = self._make_writer(result)
        settings_service = self._settings_service
        field_delay = self._field_trailer_delay_s

        # Probe the 8-point capability ONCE, up front, before any write in this
        # apply can disconnect + _invalidate_cached_handles (which nulls the
        # cached verdict). A lazy mid-pipeline probe — after the burst — would,
        # on a flaky just-reconnected device, fire a fresh ~500ms device probe
        # that could return a DIFFERENT verdict than the one the snapshot was
        # built against, selecting the wrong sensitivity-curve branch. The
        # short-circuit is preserved: a snapshot with no 8-point curve never
        # touches the device probe (keeps the 3-point path byte-for-byte and
        # leaves mock services unburdened — see the sensitivity dispatch below).
        has_8point_curve = (
            snapshot.sensitivity_left_8point is not None
            or snapshot.sensitivity_right_8point is not None
        )
        use_8point = has_8point_curve and settings_service.supports_8point_sensitivity()

        def trailer_write(label, fn, *, on_success=None):
            # Per-field trailer: sleep first so the firmware is in a quiet
            # state before the next write hits, then fire the write.
            if field_delay > 0:
                time.sleep(field_delay)
            write(label, fn, on_success=on_success)

        # --- Main burst (back-to-back, no inter-write settle) ---
        # Only polling_rate and back_paddle_bindings remain in the burst.
        # polling_rate is empirically clean in-burst (100% commit rate — first
        # write into a quiet firmware). back_paddle_bindings is write-only (no
        # read-back channel; field characterization never pinned it down) so we
        # can't observe whether it shares the burst-rejection quirk; left in
        # its current position to minimize unverifiable changes.
        if snapshot.polling_rate is not None:
            write(
                "polling",
                lambda: settings_service.set_polling_rate(snapshot.polling_rate),
            )
        for slot, binding in (snapshot.back_paddle_bindings or {}).items():
            slot_label = slot.name if hasattr(slot, "name") else str(slot)
            write(
                f"back_paddle_{slot_label}",
                lambda slot=slot, binding=binding: settings_service.set_back_paddle_binding(
                    slot,
                    binding.target,
                ),
                on_success=(
                    (lambda slot=slot, binding=binding: on_back_paddle_apply(slot, binding))
                    if on_back_paddle_apply is not None
                    else None
                ),
            )

        # --- Vulnerable-field trailers (each preceded by field_delay sleep) ---
        # Hardware-validated safe pattern: one isolated write per ~100 ms
        # firmware-quiet window. A bulk-batch alternative was rejected — the
        # back-to-back cascade dropped sensitivity_right to 14% commit. Order
        # within the trailer is stable for predictable diagnostics; no field
        # is written in both the main burst and the trailer.
        #
        # axis_inversion is included here as of the axis_inversion
        # investigation: the prior "axis_inv is 0% always"
        # framing was a get_axis_inversion read-offset bug, not a write
        # failure. Post-fix re-validation showed axis_inv writes share the
        # same in-burst-cascade band (20-27% commit) as the other vulnerable
        # fields, so axis_inv joins the trailer at its original burst-order
        # position (after deadzones, before sensitivities).
        if snapshot.vibration is not None:
            trailer_write(
                "vibration",
                lambda: settings_service.set_vibration(snapshot.vibration),
            )
        if snapshot.deadzones is not None:
            trailer_write(
                "deadzones",
                lambda: settings_service.set_all_deadzones(snapshot.deadzones),
            )
        if snapshot.axis_inversion_left is not None:
            trailer_write(
                "axis_inv_left",
                lambda: settings_service.set_left_stick_inversion(
                    snapshot.axis_inversion_left
                ),
            )
        if snapshot.axis_inversion_right is not None:
            trailer_write(
                "axis_inv_right",
                lambda: settings_service.set_right_stick_inversion(
                    snapshot.axis_inversion_right
                ),
            )
        # Sensitivity writer dispatch (additive, capability-gated). On a capable
        # 1.2.9 / fw-1.24 device the snapshot carries the richer 8-point curve
        # alongside the legacy 3-point one; we write 0x86 then. The capability
        # probe (device-touching, cached on the service) is consulted ONLY when
        # the snapshot actually has an 8-point curve to write — short-circuit so
        # a legacy snapshot / mock service is never asked about 0x86 and the
        # 3-point path stays byte-for-byte unchanged. Either path goes through
        # trailer_write, so the per-field burst-rejection settle is preserved.
        # Per-stick fallback to 3-point if a stick lacks an 8-point curve.
        left_8point = snapshot.sensitivity_left_8point
        right_8point = snapshot.sensitivity_right_8point
        # ``use_8point`` was fixed at the start of this apply (see the up-front
        # capability probe) so a mid-burst disconnect can't flip the verdict
        # under us here.

        if use_8point and left_8point is not None:
            trailer_write(
                "sens_left",
                lambda: settings_service.set_left_stick_sensitivity_curve_8point(
                    left_8point
                ),
            )
        elif snapshot.sensitivity_left is not None:
            trailer_write(
                "sens_left",
                lambda: settings_service.set_left_stick_sensitivity_curve(
                    snapshot.sensitivity_left
                ),
            )

        if use_8point and right_8point is not None:
            trailer_write(
                "sens_right",
                lambda: settings_service.set_right_stick_sensitivity_curve_8point(
                    right_8point
                ),
            )
        elif snapshot.sensitivity_right is not None:
            trailer_write(
                "sens_right",
                lambda: settings_service.set_right_stick_sensitivity_curve(
                    snapshot.sensitivity_right
                ),
            )
        if snapshot.trigger_left is not None:
            trailer_write(
                "trigger_left",
                lambda: settings_service.set_left_trigger_settings(
                    snapshot.trigger_left
                ),
            )
        if snapshot.trigger_right is not None:
            trailer_write(
                "trigger_right",
                lambda: settings_service.set_right_trigger_settings(
                    snapshot.trigger_right
                ),
            )
        for slot, mapping in (snapshot.button_bindings or {}).items():
            slot_label = slot.name if hasattr(slot, "name") else str(slot)
            trailer_write(
                f"binding_{slot_label}",
                lambda slot=slot, mapping=mapping: settings_service.set_button_binding(
                    slot,
                    mapping,
                ),
            )
        for zone, settings_obj in (snapshot.lighting_zones or {}).items():
            zone_label = zone.name if hasattr(zone, "name") else str(zone)
            trailer_write(
                f"lighting_{zone_label}",
                lambda zone=zone, settings_obj=settings_obj: settings_service.set_zone_lighting(
                    zone,
                    settings_obj,
                ),
            )

        # --- step_size trailer (unchanged from prior work) ---
        # step_size (cat 0x0d) was the first field characterized with this
        # quirk; 100% commit rate at 100 ms settle, hardware-validated
        # across N=60 trials. Kept as its own configurable delay
        # so the step_size work's separate hardware envelope (100/200/500 ms)
        # remains the source of truth for that field's settle.
        if snapshot.step_size is not None:
            if self._step_size_trailer_delay_s > 0:
                time.sleep(self._step_size_trailer_delay_s)
            # Verified write: the firmware silently rejects a fraction of
            # step_size writes (WriteFile OK, device never commits), and this is
            # the LAST write of the apply -- it follows the full vibration /
            # deadzone / axis-inv / sensitivity / trigger / 16-button / lighting
            # burst, the noisiest moment for the in-burst-rejection quirk. A
            # single un-verified write here is exactly where the revert-to-1 bug
            # bites, so read back and re-write on mismatch; a real reject now
            # surfaces as a failed apply row instead of a silent revert. The
            # default verify settle (100 ms) matches the hardware-validated
            # step_size envelope.
            write(
                "step_size",
                lambda: settings_service.set_step_size_verified(snapshot.step_size),
            )

        _safe_invoke(self._on_apply_finished, result, _name="on_apply_finished")
        return result

    def retry_failures(
        self,
        failures: list[ApplyFailure],
    ) -> ApplyResult:
        retry_failures = list(failures)
        retry_result = ApplyResult(total_attempted=len(retry_failures))
        if not retry_failures:
            return retry_result

        for failure in retry_failures:
            if failure.retry_fn is None:
                retry_result.failed.append(failure)
                continue
            try:
                result = failure.retry_fn()
            except Exception as exc:
                retry_result.failed.append(
                    ApplyFailure(
                        setting_label=failure.setting_label,
                        error=str(exc),
                        is_transient=False,
                        retry_fn=failure.retry_fn,
                        on_success=failure.on_success,
                    )
                )
                continue

            outcome = getattr(result, "outcome", None)
            if outcome_is_success(outcome):
                retry_result.succeeded += 1
                if outcome_used_retry(outcome):
                    retry_result.retry_recoveries += 1
                _safe_invoke(failure.on_success, _name="failure.on_success")
                continue

            retry_result.failed.append(
                ApplyFailure(
                    setting_label=failure.setting_label,
                    error=result_error_text(result),
                    is_transient=result_is_transient(result),
                    retry_fn=failure.retry_fn,
                    on_success=failure.on_success,
                )
            )

        return retry_result

    def _make_writer(
        self,
        result: ApplyResult,
    ) -> Callable[..., bool]:
        def write(
            label: str,
            write_fn: Callable[[], object],
            *,
            on_success: Callable[[], None] | None = None,
        ) -> bool:
            result.total_attempted += 1
            try:
                outcome_result = write_fn()
            except Exception as exc:
                result.failed.append(
                    ApplyFailure(
                        setting_label=label,
                        error=str(exc),
                        is_transient=False,
                        retry_fn=write_fn,
                        on_success=on_success,
                    )
                )
                return False

            outcome = getattr(outcome_result, "outcome", None)
            if outcome_is_success(outcome):
                result.succeeded += 1
                if outcome_used_retry(outcome):
                    result.retry_recoveries += 1
                _safe_invoke(on_success, _name="on_success")
                return True
            result.failed.append(
                ApplyFailure(
                    setting_label=label,
                    error=result_error_text(outcome_result),
                    is_transient=result_is_transient(outcome_result),
                    retry_fn=write_fn,
                    on_success=on_success,
                )
            )
            return False

        return write
