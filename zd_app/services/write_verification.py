"""Shared read-only post-write verification helpers.

The coordinator owns write dispatch.  This module owns the separate, DPG-free
read-back pass used to learn what a controller actually stored afterwards.
Its fresh reader deliberately calls only ``SettingsService.get_*`` methods;
it never writes or mutates controller state.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Optional

from zd_app.services.restore_field_formatting import format_field_value
from zd_app.services.settings_service import (
    SENSITIVITY_STICK_LEFT,
    SENSITIVITY_STICK_RIGHT,
    ButtonSlot,
    ControllerSnapshot,
    LightingZone,
    SensitivityAnchorTuple8,
    SettingsService,
)
from zd_app.storage.restore_point_models import RestoreFieldOutcome, RestoreResultLabel


if TYPE_CHECKING:
    from zd_app.services.settings_apply_coordinator import ApplyResult


logger = logging.getLogger(__name__)


# 8-point (cat 0x86) sensitivity curves use the same left/right logical
# setting as their 3-point hosts.  A sent 8-point rider replaces its host in
# the field outcome set; a downgrade restores the host before verification.
SENSITIVITY_8POINT_RIDERS: tuple[tuple[str, str], ...] = (
    ("sensitivity_left_8point", "sensitivity_left"),
    ("sensitivity_right_8point", "sensitivity_right"),
)

COLLECTION_FIELD_NAMES: frozenset[str] = frozenset(
    {"button_bindings", "back_paddle_bindings", "lighting_zones"}
)

# The controller does not provide a trustworthy read-back channel for paddle
# bindings.  They are therefore could-not-verify by construction, even if a
# caller happens to have a cached value.
WRITE_ONLY_FIELDS: frozenset[str] = frozenset({"back_paddle_bindings"})

# Marker for a getter skipped because the batch budget ran out.  Consumers use
# the ``skipped:`` prefix to distinguish unreadable from legitimately absent.
BATCH_BUDGET_SKIP_ERROR = "skipped: batch read budget exhausted"


@dataclass(frozen=True)
class ApplyReadbackVerification:
    """Per-field read-back outcomes attached to one profile Apply.

    ``None`` is deliberately distinct from an empty tuple. A completed OR
    synthesized sweep carries a tuple of per-field three-valued outcomes;
    ``None`` means no summary was attached at all — the feature was disabled or
    unavailable, or (last resort) the could-not-verify synthesis itself failed
    and was loudly logged. A best-effort sweep that RAISES no longer leaves
    ``None``: it is synthesized into all-could-not-verify outcomes so the
    disclosure can never silently disappear (see
    ``AppShell._attach_all_unverified_readback``).

    ``could_not_verify`` counts only fields whose write SUCCEEDED but could not
    be read back — i.e. "written but unconfirmed". A field whose write FAILED is
    NOT written, so it is excluded here (it is disclosed as a write failure, and
    the profile-Apply footer copy for this count literally says "were written").
    """

    fields: tuple[RestoreFieldOutcome, ...]
    attempted: int
    wrote_succeeded: int
    write_failed: int
    verified_matched: int
    could_not_verify: int
    mismatched: int


def summarize_field_outcomes(
    outcomes: Iterable[RestoreFieldOutcome],
) -> ApplyReadbackVerification:
    """Summarize per-field outcomes into the counters that drive the Apply
    footer disclosure and Verification Details (Phase 2 UI).

    ``verified_matched`` counts every ``verify_matched is True`` field — even one
    whose WRITE failed (a pre-existing value matching the request is genuine
    read-back evidence for the details view). Callers asserting apply-wide
    success (the all-verified footer note) must additionally gate on
    ``write_failed == 0``. ``could_not_verify`` counts only written-but-
    unconfirmed fields; write-failed fields are disclosed as failures instead.
    """

    fields = tuple(outcomes)
    return ApplyReadbackVerification(
        fields=fields,
        attempted=len(fields),
        wrote_succeeded=sum(1 for field in fields if field.write_succeeded),
        write_failed=sum(1 for field in fields if not field.write_succeeded),
        verified_matched=sum(1 for field in fields if field.verify_matched is True),
        # "written but unconfirmed": a field whose write FAILED was not written,
        # so it must not inflate a count the footer describes as "were written"
        # (review fix 2026-07-17). Its failure is disclosed separately;
        # the per-field detail row still shows write=FAIL verify=unverified.
        could_not_verify=sum(
            1
            for field in fields
            if field.write_succeeded and field.verify_matched is None
        ),
        mismatched=sum(1 for field in fields if field.verify_matched is False),
    )


def _safe_call(
    fn: Callable[[], Any],
    name: str,
    *,
    log: logging.Logger,
    log_prefix: str,
) -> tuple[Any, Optional[str]]:
    """Invoke ``fn``; on any exception, return a field-level read error."""

    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001 - one failed getter must not abort a sweep
        log.warning("%s fresh read failed in %s: %s", log_prefix, name, exc)
        return None, f"{type(exc).__name__}: {exc}"


def fresh_read(
    settings_service: SettingsService,
    *,
    batch_read_budget_s: float = 8.0,
    clock: Callable[[], float] = time.monotonic,
    log: logging.Logger | None = None,
    log_prefix: str = "write-verification",
) -> tuple[ControllerSnapshot, dict[str, object], dict[str, str]]:
    """Read every verification field without issuing any controller write.

    Returns ``(snapshot, read_success_map, read_errors)``.  Scalar fields use
    bool success values; collection fields use captured-key sets.  Any getter
    exception or exhausted batch budget becomes a per-field read error so the
    outcome builder can report ``verify_matched=None`` rather than a mismatch.
    """

    service = settings_service
    read_errors: dict[str, str] = {}
    active_log = log or logger

    deadline = (clock() + batch_read_budget_s) if batch_read_budget_s > 0 else None
    issued = 0
    skipped = 0
    exhausted = False

    def _within_budget() -> bool:
        # One guard per getter call. Once the deadline passes, stay exhausted
        # so the whole remaining tail is skipped uniformly.
        nonlocal issued, skipped, exhausted
        if not exhausted and deadline is not None and clock() >= deadline:
            exhausted = True
        if exhausted:
            skipped += 1
            return False
        issued += 1
        return True

    def _mark_skipped(*field_names: str) -> None:
        for field_name in field_names:
            read_errors.setdefault(field_name, BATCH_BUDGET_SKIP_ERROR)

    def _budget_call(fn: Callable[[], Any], name: str, *field_names: str) -> Any:
        if not _within_budget():
            _mark_skipped(*field_names)
            return None
        value, err = _safe_call(fn, name, log=active_log, log_prefix=log_prefix)
        if err:
            for field_name in field_names:
                read_errors[field_name] = err
        return value

    polling_rate = _budget_call(
        service.get_polling_rate, "get_polling_rate", "polling_rate"
    )
    vibration = _budget_call(service.get_vibration, "get_vibration", "vibration")
    deadzones = _budget_call(service.get_deadzones, "get_deadzones", "deadzones")
    axis_inv = _budget_call(
        service.get_axis_inversion,
        "get_axis_inversion",
        "axis_inversion_left",
        "axis_inversion_right",
    )
    sens = _budget_call(
        service.get_sensitivity_curves,
        "get_sensitivity_curves",
        "sensitivity_left",
        "sensitivity_right",
    )

    # Probe capability once, then only read 8-point riders on capable devices.
    # A skipped probe is unknown rather than legacy/not-capable, so mark both
    # riders unreadable instead of silently treating them as absent.
    sens_left_8point: Optional[SensitivityAnchorTuple8] = None
    sens_right_8point: Optional[SensitivityAnchorTuple8] = None
    if _within_budget():
        supports_8point, _ = _safe_call(
            lambda: service.supports_8point_sensitivity(),
            "supports_8point_sensitivity",
            log=active_log,
            log_prefix=log_prefix,
        )
        if supports_8point:
            sens_left_8point = _budget_call(
                lambda: service.get_sensitivity_curve_8point(SENSITIVITY_STICK_LEFT),
                "get_sensitivity_curve_8point(LEFT)",
                "sensitivity_left_8point",
            )
            sens_right_8point = _budget_call(
                lambda: service.get_sensitivity_curve_8point(SENSITIVITY_STICK_RIGHT),
                "get_sensitivity_curve_8point(RIGHT)",
                "sensitivity_right_8point",
            )
    else:
        _mark_skipped("sensitivity_left_8point", "sensitivity_right_8point")

    trig = _budget_call(
        service.get_trigger_settings,
        "get_trigger_settings",
        "trigger_left",
        "trigger_right",
    )
    motion = _budget_call(
        service.get_motion_settings, "get_motion_settings", "motion_settings"
    )
    step_size = _budget_call(service.get_step_size, "get_step_size", "step_size")

    bindings: dict[ButtonSlot, object] = {}
    for slot in ButtonSlot:
        if not _within_budget():
            _mark_skipped("button_bindings")
            continue
        mapping, err = _safe_call(
            lambda slot=slot: service.get_button_binding(slot),
            f"get_button_binding({slot.name})",
            log=active_log,
            log_prefix=log_prefix,
        )
        if mapping is not None:
            bindings[slot] = mapping
        elif err and "button_bindings" not in read_errors:
            read_errors["button_bindings"] = err

    lighting: dict[LightingZone, object] = {}
    for zone in LightingZone:
        if not _within_budget():
            _mark_skipped("lighting_zones")
            continue
        zone_settings, err = _safe_call(
            lambda zone=zone: service.get_zone_lighting(zone),
            f"get_zone_lighting({zone.name})",
            log=active_log,
            log_prefix=log_prefix,
        )
        if zone_settings is not None:
            lighting[zone] = zone_settings
        elif err and "lighting_zones" not in read_errors:
            read_errors["lighting_zones"] = err

    if _within_budget():
        back_paddles, err = _safe_call(
            service.get_all_back_paddle_bindings,
            "get_all_back_paddle_bindings",
            log=active_log,
            log_prefix=log_prefix,
        )
        if back_paddles is None:
            back_paddles = {}
        if err:
            read_errors["back_paddle_bindings"] = err
    else:
        back_paddles = {}
        _mark_skipped("back_paddle_bindings")

    if skipped:
        active_log.warning(
            "%s fresh read: batch read budget (%.1fs) exhausted after %d of %d reads; "
            "skipped fields recorded in read_errors",
            log_prefix,
            batch_read_budget_s,
            issued,
            issued + skipped,
        )

    snapshot = ControllerSnapshot(
        polling_rate=polling_rate,
        vibration=vibration,
        deadzones=deadzones,
        axis_inversion_left=(axis_inv[0] if axis_inv else None),
        axis_inversion_right=(axis_inv[1] if axis_inv else None),
        sensitivity_left=(sens[0] if sens else None),
        sensitivity_right=(sens[1] if sens else None),
        trigger_left=(trig[0] if trig else None),
        trigger_right=(trig[1] if trig else None),
        button_bindings=bindings,  # type: ignore[arg-type]
        lighting_zones=lighting,  # type: ignore[arg-type]
        motion_settings=motion,
        back_paddle_bindings=back_paddles,
        step_size=step_size,
        sensitivity_left_8point=sens_left_8point,
        sensitivity_right_8point=sens_right_8point,
    )

    read_success: dict[str, object] = {
        "polling_rate": polling_rate is not None,
        "step_size": step_size is not None,
        "vibration": vibration is not None,
        "deadzones": deadzones is not None,
        "axis_inversion_left": axis_inv is not None,
        "axis_inversion_right": axis_inv is not None,
        "sensitivity_left": sens is not None,
        "sensitivity_right": sens is not None,
        "trigger_left": trig is not None,
        "trigger_right": trig is not None,
        "motion_settings": motion is not None,
        "button_bindings": set(bindings.keys()),
        "back_paddle_bindings": set(back_paddles.keys()),
        "lighting_zones": set(lighting.keys()),
    }
    return snapshot, read_success, read_errors


def attempted_fields_from_snapshot(snapshot: ControllerSnapshot) -> list[str]:
    """Return the field groups an Apply of ``snapshot`` will attempt."""

    attempted = [
        name
        for name in (
            "polling_rate",
            "step_size",
            "vibration",
            "deadzones",
            "axis_inversion_left",
            "axis_inversion_right",
            "sensitivity_left",
            "sensitivity_right",
            "trigger_left",
            "trigger_right",
        )
        if getattr(snapshot, name) is not None
    ]
    attempted.extend(
        name
        for name in ("button_bindings", "back_paddle_bindings", "lighting_zones")
        if getattr(snapshot, name) or {}
    )
    for rider_name, host_name in SENSITIVITY_8POINT_RIDERS:
        if getattr(snapshot, rider_name) is None:
            continue
        if host_name in attempted:
            attempted[attempted.index(host_name)] = rider_name
        else:
            # The coordinator can write a capable 8-point rider without a
            # 3-point fallback. Keep that real write in the attempted set.
            attempted.append(rider_name)
    return attempted


def attempted_fields_as_sent(
    attempted_fields: list[str],
    sent_snapshot: ControllerSnapshot,
) -> list[str]:
    """Replace downgraded 8-point rider names with their 3-point hosts."""

    hosts_by_rider = {
        rider_name: host_name
        for rider_name, host_name in SENSITIVITY_8POINT_RIDERS
        if getattr(sent_snapshot, rider_name) is None
    }
    fields_as_sent: list[str] = []
    for name in attempted_fields:
        sent_name = hosts_by_rider.get(name, name)
        # An 8-point rider without a 3-point fallback is not a write after an
        # unsupported-capability downgrade, so do not invent a host outcome.
        if sent_name != name and getattr(sent_snapshot, sent_name) is None:
            continue
        fields_as_sent.append(sent_name)
    return fields_as_sent


def readback_differs(field_name: str, applied_value: object, read_value: object) -> bool:
    """Exact inequality with a safe formatting fallback."""

    try:
        return applied_value != read_value
    except Exception:  # noqa: BLE001 - defensive: a custom __eq__ may raise
        return format_field_value(field_name, applied_value) != format_field_value(
            field_name, read_value
        )


def mismatched_collection_entries(
    name: str,
    applied_entries: Mapping[Any, Any],
    read_entries: Mapping[Any, Any],
) -> list[tuple[Any, Any, Any]]:
    """Return differing applied collection entries, ignoring readback extras."""

    mismatches: list[tuple[Any, Any, Any]] = []
    for key, applied_value in applied_entries.items():
        read_value = read_entries.get(key)
        if read_value is None or readback_differs(
            f"{name}[{key.name}]", applied_value, read_value
        ):
            mismatches.append((key, applied_value, read_value))
    return mismatches


def _format_collection_mismatches(
    name: str,
    mismatches: list[tuple[Any, Any, Any]],
) -> tuple[str, str]:
    expected_str = ", ".join(
        f"{key.name}={format_field_value(f'{name}[{key.name}]', applied_value)}"
        for key, applied_value, _read_value in mismatches
    )
    observed_str = ", ".join(
        f"{key.name}={format_field_value(f'{name}[{key.name}]', read_value)}"
        for key, _applied_value, read_value in mismatches
    )
    return expected_str, observed_str


def verify_collection_outcome(
    name: str,
    applied_entries: Mapping[Any, Any],
    read_entries: Mapping[Any, Any],
    read_errors: Mapping[str, str],
) -> tuple[Optional[bool], Optional[str], Optional[str], Optional[str]]:
    """Return the three-valued verification result for one collection field.

    Honesty ordering: an entry that actually read back AND genuinely differs is
    a KNOWN mismatch, and is surfaced even when a sibling entry's getter raised
    mid-sweep. A known-bad must never be masked behind a sibling's
    "could not verify". Only when NO readable entry mismatched does a read error
    or an all-empty read-back degrade the field to could-not-verify — never to a
    firmware mismatch, because the read error is proof we cannot make that call.
    """

    # KNOWN mismatches: entries present in the read-back that genuinely differ.
    # Unambiguous, so they win over any sibling read error. Entries ABSENT from
    # read_entries are deliberately left to the fall-through logic below, where a
    # read error degrades them to could-not-verify and an error-free absence
    # (e.g. an unbound slot read as None) stays a mismatch as it always has.
    readable_mismatches = [
        (key, applied_value, read_entries[key])
        for key, applied_value in applied_entries.items()
        if key in read_entries
        and readback_differs(f"{name}[{key.name}]", applied_value, read_entries[key])
    ]
    if readable_mismatches:
        expected_str, observed_str = _format_collection_mismatches(
            name, readable_mismatches
        )
        return False, "read-back value differs from expected", expected_str, observed_str

    # No readable entry mismatched — the original three-valued logic stands.
    if name in WRITE_ONLY_FIELDS or read_errors.get(name) or not read_entries:
        read_err = read_errors.get(name)
        if read_err:
            return None, f"verify-read failed: {read_err}", None, None
        return None, "read-back did not return a value", None, None
    mismatches = mismatched_collection_entries(name, applied_entries, read_entries)
    if not mismatches:
        return True, None, None, None
    expected_str, observed_str = _format_collection_mismatches(name, mismatches)
    return False, "read-back value differs from expected", expected_str, observed_str


def build_field_outcomes(
    attempted_fields: list[str],
    expected_snapshot: ControllerSnapshot,
    readback: ControllerSnapshot,
    apply_result: ApplyResult,
    read_errors: Mapping[str, str],
) -> list[RestoreFieldOutcome]:
    """Map attempted fields to distinct write and three-valued read outcomes."""

    failures_by_label = {failure.setting_label: failure for failure in apply_result.failed}

    outcomes: list[RestoreFieldOutcome] = []
    for name in attempted_fields:
        labels = apply_labels_for(name, expected_snapshot)
        write_succeeded = True
        write_error: Optional[str] = None
        for label in labels:
            failure = failures_by_label.get(label)
            if failure is not None:
                write_succeeded = False
                write_error = failure.error
                break

        expected_value = getattr(expected_snapshot, name)
        read_value = getattr(readback, name)
        expected_str: Optional[str] = None
        observed_str: Optional[str] = None
        verify_matched: Optional[bool]
        verify_note: Optional[str]
        if name in COLLECTION_FIELD_NAMES:
            (
                verify_matched,
                verify_note,
                expected_str,
                observed_str,
            ) = verify_collection_outcome(
                name, expected_value or {}, read_value or {}, read_errors
            )
        elif read_value is None:
            verify_matched = None
            read_err = read_errors.get(name)
            if read_err:
                verify_note = f"verify-read failed: {read_err}"
            else:
                verify_note = "read-back did not return a value"
        else:
            try:
                verify_matched = read_value == expected_value
            except Exception:  # noqa: BLE001 - a bad comparison is unverifiable
                verify_matched = None
                verify_note = "read-back value could not be compared"
            else:
                if verify_matched:
                    verify_note = None
                else:
                    verify_note = "read-back value differs from expected"
                    expected_str = format_field_value(name, expected_value)
                    observed_str = format_field_value(name, read_value)

        outcomes.append(
            RestoreFieldOutcome(
                field_name=name,
                write_succeeded=write_succeeded,
                write_error=write_error,
                verify_matched=verify_matched,
                verify_note=verify_note,
                expected_value=expected_str,
                observed_value=observed_str,
            )
        )
    return outcomes


def _empty_readback_snapshot() -> ControllerSnapshot:
    """A snapshot whose every field reads as absent (scalars ``None``, collections empty).

    Used as the read-back when the sweep could not read the controller at all,
    so :func:`build_field_outcomes` degrades every attempted field to
    could-not-verify through its normal three-valued path instead of inventing
    a match. Mirrors ``ControllerSnapshot``'s required fields; a new required
    field would fail this constructor loudly rather than silently mis-report.
    """

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


def all_unverified_outcomes(
    attempted_fields: list[str],
    sent_snapshot: ControllerSnapshot,
    apply_result: "ApplyResult",
    *,
    reason: str,
) -> list[RestoreFieldOutcome]:
    """Outcomes for a sweep that could not run: every attempted field is
    could-not-verify, with the write results preserved.

    Built through the same :func:`build_field_outcomes` path a real
    all-unreadable sweep takes — an empty read-back plus a read error per
    attempted field — so a whole-sweep failure presents identically to a sweep
    that reached the controller but could read nothing back. This is the honest
    fallback for a verifier that raised before it could read: it can never let a
    successful write burst display as a clean, caveat-free "verified" success.
    """

    read_errors = {name: reason for name in attempted_fields}
    return build_field_outcomes(
        attempted_fields,
        sent_snapshot,
        _empty_readback_snapshot(),
        apply_result,
        read_errors,
    )


def apply_labels_for(name: str, snapshot: ControllerSnapshot) -> list[str]:
    """Map a snapshot field to the coordinator's existing write labels."""

    if name == "polling_rate":
        return ["polling"]
    if name == "step_size":
        return ["step_size"]
    if name == "vibration":
        return ["vibration"]
    if name == "deadzones":
        return ["deadzones"]
    if name == "axis_inversion_left":
        return ["axis_inv_left"]
    if name == "axis_inversion_right":
        return ["axis_inv_right"]
    if name in {"sensitivity_left", "sensitivity_left_8point"}:
        return ["sens_left"]
    if name in {"sensitivity_right", "sensitivity_right_8point"}:
        return ["sens_right"]
    if name == "trigger_left":
        return ["trigger_left"]
    if name == "trigger_right":
        return ["trigger_right"]
    if name == "button_bindings":
        return [f"binding_{slot.name}" for slot in (snapshot.button_bindings or {})]
    if name == "lighting_zones":
        return [f"lighting_{zone.name}" for zone in (snapshot.lighting_zones or {})]
    if name == "back_paddle_bindings":
        return [
            f"back_paddle_{slot.name}" for slot in (snapshot.back_paddle_bindings or {})
        ]
    return []


def unverified_writes_after_final_read(
    unverified_writes: Iterable[str],
    field_outcomes: Iterable[RestoreFieldOutcome],
    sent_snapshot: ControllerSnapshot,
) -> tuple[str, ...]:
    """Drop setter disclosures superseded by a matching final read-back."""

    labels_with_final_match = {
        label
        for outcome in field_outcomes
        if outcome.verify_matched is True
        for label in apply_labels_for(outcome.field_name, sent_snapshot)
    }
    return tuple(label for label in unverified_writes if label not in labels_with_final_match)


def label_for_restore_result(
    *, attempted: int, write_failed: int, mismatched: int, could_not_verify: int
) -> RestoreResultLabel:
    """Preserve Restore's existing counter-to-label semantics."""

    if attempted == 0:
        return RestoreResultLabel.RESTORE_FAILED
    if write_failed >= attempted:
        return RestoreResultLabel.RESTORE_FAILED
    if write_failed > 0:
        return RestoreResultLabel.PARTIALLY_RESTORED
    if mismatched > 0:
        return RestoreResultLabel.MISMATCH_AFTER_RESTORE
    if could_not_verify > 0:
        return RestoreResultLabel.RESTORED_WITH_WARNINGS
    return RestoreResultLabel.VERIFIED
