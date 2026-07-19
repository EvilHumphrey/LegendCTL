"""Restore-point capture and restore service.

Implements the apply-coordinator-interaction and fresh-read-rule flows for
restore points.

Capture flow:
    1. Try a fresh read by invoking the individual ``settings_service.get_*``
       methods (per-call None signal = read failure; this is the per-field
       success signal that the coverage map needs).
    2. If fresh read produces enough fields, build a snapshot at
       ``CaptureSource.FRESH_READ``.
    3. Otherwise, if a recent cached snapshot was provided (within
       ``fresh_read_max_age_s``), use it at ``CaptureSource.CACHED_SNAPSHOT``.
    4. Otherwise log and return ``None``.

Restore flow:
    1. Create a ``before_restore`` restore point of current state.
    2. Filter the captured snapshot to writable + (optionally) selected
       fields.
    3. Apply through the injected :class:`SettingsApplyCoordinator`.
    4. Read controller back via fresh reads.
    5. Compare expected vs read-back per attempted field.
    6. Build a :class:`RestoreResult` and persist a
       :class:`RestoreAttemptRecord` onto the restore point.

The service is UI-free and takes the apply coordinator as a constructor
dependency so the restore-point UI-integration follow-up work can share one coordinator
instance with the rest of the apply pipeline.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Optional

from zd_app.services.restore_field_formatting import format_field_value
from zd_app.services.settings_apply_coordinator import (
    SettingsApplyCoordinator,
    snapshot_as_sent,
)
from zd_app.services.write_verification import (
    WRITE_ONLY_FIELDS as _PREVIEW_EXCLUDED_FIELDS,
    SENSITIVITY_8POINT_RIDERS as _SENSITIVITY_8POINT_RIDERS,
    apply_labels_for as _apply_labels_for,
    attempted_fields_as_sent as _attempted_fields_as_sent,
    build_field_outcomes as _build_field_outcomes,
    fresh_read,
    label_for_restore_result as _label_for,
    mismatched_collection_entries as _mismatched_collection_entries,
    readback_differs as _readback_differs,
    unverified_writes_after_final_read as _unverified_writes_after_final_restore_read,
)
from zd_app.services.wear_ledger import WearLedgerService
from zd_app.services.wear_ledger.models import RP_CAPTURE, RP_DELETE, RP_RESTORE
from zd_app.services.settings_service import (
    ButtonSlot,
    ControllerSnapshot,
    LightingZone,
    MacroSlot,
    SettingsService,
)
from zd_app.storage.restore_point_models import (
    KIND,
    SCHEMA_VERSION,
    CaptureSource,
    CoverageCategory,
    CoverageState,
    DeviceIdentity,
    FieldCoverage,
    IdentityConfidence,
    RestoreAttemptRecord,
    RestoreFieldDelta,
    RestorePoint,
    RestorePointCoverage,
    RestorePointTrigger,
    RestorePreview,
    RestoreResult,
    SkippedFile,
)
from zd_app.storage.restore_point_store import RestorePointStore


logger = logging.getLogger(__name__)


FRESH_READ_MAX_AGE_S = 30.0
"""Default staleness threshold per the "Staleness threshold" rule."""


# Static field classification (per the "Field category model" rule). Restore
# eligibility uses ``writable`` — ``motion_settings`` is captured for record
# but never restored because the wrapper has no supported write path
# (documented at ``ControllerSnapshot.motion_settings``).
_SCALAR_FIELDS: tuple[tuple[str, CoverageCategory, bool], ...] = (
    ("polling_rate", CoverageCategory.DEVICE, True),
    ("step_size", CoverageCategory.DEVICE, True),
    # Rumble strength is presentation, so
    # vibration sits in COSMETIC matching the Safe-Import buckets. This is
    # what groups the RP-detail and Device-vs-Profile rows under Cosmetic.
    ("vibration", CoverageCategory.COSMETIC, True),
    ("deadzones", CoverageCategory.FEEL, True),
    ("axis_inversion_left", CoverageCategory.FEEL, True),
    ("axis_inversion_right", CoverageCategory.FEEL, True),
    ("sensitivity_left", CoverageCategory.FEEL, True),
    ("sensitivity_right", CoverageCategory.FEEL, True),
    ("trigger_left", CoverageCategory.FEEL, True),
    ("trigger_right", CoverageCategory.FEEL, True),
    ("motion_settings", CoverageCategory.UNSUPPORTED, False),
)

_COLLECTION_FIELDS: tuple[tuple[str, CoverageCategory, bool, int], ...] = (
    # name, category, writable, expected_size
    ("button_bindings", CoverageCategory.LAYOUT, True, len(ButtonSlot)),
    ("back_paddle_bindings", CoverageCategory.LAYOUT, True, len(MacroSlot)),
    ("lighting_zones", CoverageCategory.COSMETIC, True, len(LightingZone)),
)

def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with ``Z`` suffix to match the documented JSON shape."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _generate_id(now: Optional[datetime] = None) -> str:
    """Compute a restore-point id: ``rp_YYYYMMDD_HHMMSS_<6char>``.

    The 6-char tail is a cryptographically random hex string so two captures
    that land in the same second don't collide on filename.
    """

    moment = now if now is not None else datetime.now(timezone.utc)
    stamp = moment.strftime("%Y%m%d_%H%M%S")
    return f"rp_{stamp}_{secrets.token_hex(3)}"


class RestorePointService:
    """Owns the fresh-read rule for captures and the apply+verify flow for
    restores. UI-free — callers in the restore-point UI wire the buttons.
    """

    def __init__(
        self,
        *,
        store: RestorePointStore,
        settings_service: SettingsService,
        apply_coordinator: SettingsApplyCoordinator,
        app_version: str,
        app_build_commit: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        id_factory: Callable[[datetime], str] = _generate_id,
        wear_ledger: Optional[WearLedgerService] = None,
        prune_on_capture: bool = True,
        batch_read_budget_s: float = 8.0,
        post_apply_read_settle_s: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store = store
        self._settings_service = settings_service
        self._apply_coordinator = apply_coordinator
        self._app_version = app_version
        self._app_build_commit = app_build_commit
        self._clock = clock
        self._utc_now = utc_now
        self._id_factory = id_factory
        self._wear_ledger = wear_ledger
        # Retention: capture() runs store.prune() with the store's default
        # caps after every successful save (prune() otherwise has no callers,
        # so the vault would grow unbounded). Tests / bulk flows can opt out.
        self._prune_on_capture = prune_on_capture
        # Wall-clock cap for one _do_fresh_read batch — same rationale as
        # SettingsService.get_all_settings (a misbehaving device burns the
        # full per-read timeout on every getter). Skipped fields are recorded
        # in read_errors (see _BATCH_BUDGET_SKIP_ERROR) so coverage and the
        # Device-vs-Profile screen see them as unreadable, not absent.
        # 0 or negative disables the budget.
        self._batch_read_budget_s = batch_read_budget_s
        # Settle between restore()'s apply burst and its verify fresh-read.
        # The firmware wants a quiet interval after write bursts before it
        # answers feature-report reads again (see the per-field trailer
        # rationale in SettingsApplyCoordinator); without it the first verify
        # read can hit a transient HID timeout (observed on hardware
        # 2026-06-10) and pollute the result with a spurious
        # could-not-verify. 0 or negative disables. ``sleep`` is injectable
        # for tests, mirroring SettingsService's seam.
        self._post_apply_read_settle_s = post_apply_read_settle_s
        self._sleep = sleep
        # Construction-identity logging while we investigate the load-by-id
        # bug behind the "service unavailable" / "not found" wrapper
        # symptom. Correlates with RestorePointStore's construction log
        # and the _safe_load identity print in the screen module.
        logger.info(
            "RestorePointService constructed: service_id=%s store_id=%s "
            "base_dir=%r",
            id(self),
            id(self._store),
            str(getattr(self._store, "base_dir", None)),
        )

    # -- read-only -----------------------------------------------------------

    def list_with_skipped(self) -> tuple[list[RestorePoint], list[SkippedFile]]:
        """Pass-through to :meth:`RestorePointStore.list`."""

        return self._store.list()

    def read_current_state_with_provenance(
        self,
    ) -> tuple[ControllerSnapshot, dict[str, object], dict[str, str]]:
        """Public wrapper over :meth:`_do_fresh_read` for non-capture callers.

        The Device-vs-Profile diff screen needs the ``(snapshot, read_success,
        read_errors)`` triple — not just the snapshot — so it can honestly
        distinguish a field that was *unreadable* (HID timeout / error) from one
        that is *legitimately absent*. ``_do_fresh_read`` already produces the
        triple; this is a no-behavior-change passthrough so the screen does not
        reach into a private method.
        """

        return self._do_fresh_read()

    # -- delete --------------------------------------------------------------

    def delete(self, rp_id: str) -> bool:
        """Pass-through to :meth:`RestorePointStore.delete`.

        Returns True when a stored restore point with this id was removed.
        On success, appends an ``rp_delete`` wear-ledger event (when a
        ledger is wired) so the maintenance log keeps a record of the vault
        edit. The title is resolved before the unlink purely for the ledger
        summary; a missing or corrupt file falls back to the raw id.
        """

        title: Optional[str] = None
        try:
            title = self._store.load(rp_id).title
        except Exception:  # noqa: BLE001 - summary falls back to the id
            title = None
        deleted = self._store.delete(rp_id)
        if deleted and self._wear_ledger is not None:
            self._wear_ledger.append(
                RP_DELETE,
                summary=f"Deleted restore point: {title or rp_id}",
                details={"rp_id": rp_id, "title": title},
            )
        return deleted

    # -- capture -------------------------------------------------------------

    def capture(
        self,
        trigger: RestorePointTrigger,
        *,
        title: Optional[str] = None,
        device_identity: Optional[DeviceIdentity] = None,
        fresh_read_max_age_s: float = FRESH_READ_MAX_AGE_S,
        cached_snapshot: Optional[ControllerSnapshot] = None,
        cached_snapshot_ts: Optional[float] = None,
    ) -> Optional[RestorePoint]:
        """Capture a restore point. Returns ``None`` if no usable snapshot.

        Tries a fresh-read first (calls each public ``settings_service.get_*``
        method and tracks per-field success). If the fresh read returns
        anything at all, the restore point uses
        :attr:`CaptureSource.FRESH_READ` even when some fields fail (the
        coverage map honestly reflects which fields didn't read). If every
        fresh-read attempt fails AND a cached snapshot was provided that is
        within ``fresh_read_max_age_s``, falls back to
        :attr:`CaptureSource.CACHED_SNAPSHOT`. Otherwise logs + returns None
        per the "Fresh-read rule".

        When ``prune_on_capture`` is enabled (default), store retention
        pruning runs after the save + wear-ledger append; a prune failure is
        logged and never fails the capture.
        """

        snapshot, read_success, _read_errors = self._do_fresh_read()
        capture_source = CaptureSource.FRESH_READ
        if not _any_read_succeeded(read_success):
            if cached_snapshot is None or cached_snapshot_ts is None:
                logger.warning(
                    "restore-point capture: fresh read returned nothing and no "
                    "cached snapshot available (trigger=%s)",
                    trigger.type,
                )
                return None
            age = self._clock() - cached_snapshot_ts
            if age > fresh_read_max_age_s:
                logger.warning(
                    "restore-point capture: fresh read failed and cached "
                    "snapshot is %.1fs old (> %.1fs threshold) — refusing to "
                    "save a stale snapshot (trigger=%s)",
                    age,
                    fresh_read_max_age_s,
                    trigger.type,
                )
                return None
            snapshot = cached_snapshot
            read_success = _read_success_from_snapshot(snapshot)
            capture_source = CaptureSource.CACHED_SNAPSHOT

        moment = self._utc_now()
        rp_id = self._id_factory(moment)
        rp_title = title if title else _default_title(trigger, moment)
        coverage = _build_coverage(snapshot, read_success, capture_source)
        rp = RestorePoint(
            schema_version=SCHEMA_VERSION,
            kind=KIND,
            id=rp_id,
            created_at=_iso_from_datetime(moment),
            app_version=self._app_version,
            app_build_commit=self._app_build_commit,
            title=rp_title,
            trigger=trigger,
            device_identity=device_identity or _unknown_device_identity(),
            snapshot=snapshot,
            coverage=coverage,
            last_restore_attempt=None,
        )
        self._store.save(rp)
        if self._wear_ledger is not None:
            self._wear_ledger.append(
                RP_CAPTURE,
                summary=f"Captured restore point: {rp_title}",
                details={
                    "rp_id": rp.id,
                    "title": rp_title,
                    "trigger_type": trigger.type,
                    "capture_source": coverage.capture_source.value,
                    "captured_supported_count": coverage.captured_supported_count,
                    "total_supported_count": coverage.total_supported_count,
                },
            )
        if self._prune_on_capture:
            # Retention runs at capture level (not store.save) so every hook
            # path + restore's internal before_restore capture share one
            # enforcement point. No ``protect`` arg: the fresh capture is the
            # newest file (phase-1 pruning is auto-RPs oldest-first above
            # max_count) and prune() itself protects the newest
            # first_readable_connect per device. A prune failure must never
            # fail or roll back the capture that just succeeded.
            try:
                pruned = self._store.prune()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "restore-point retention prune failed after capture %s",
                    rp.id,
                    exc_info=True,
                )
            else:
                if pruned:
                    logger.info(
                        "restore-point retention pruned %d restore point(s): %s",
                        len(pruned),
                        ", ".join(pruned),
                    )
        return rp

    def _do_fresh_read(
        self,
    ) -> tuple[ControllerSnapshot, dict[str, object], dict[str, str]]:
        """Delegate the shared fresh-read implementation with Restore's seam."""

        return fresh_read(
            self._settings_service,
            batch_read_budget_s=self._batch_read_budget_s,
            clock=self._clock,
            log=logger,
            log_prefix="restore-point",
        )

    # -- restore -------------------------------------------------------------

    def restore(
        self,
        rp_id: str,
        *,
        excluded_fields: Optional[Iterable[str]] = None,
    ) -> RestoreResult:
        """Restore a previously captured restore point.

        Flow per the "Apply coordinator interaction" design:

        1. Save a ``before_restore`` restore point of current state.
        2. Filter the captured snapshot to writable + (not-excluded) fields
           that the original capture actually recorded.
        3. Apply via the injected :class:`SettingsApplyCoordinator`.
        4. Read controller back with fresh reads (the same per-field success
           pass used in :meth:`capture`).
        5. Compare expected vs read-back per attempted field.
        6. Persist the summary as :class:`RestoreAttemptRecord` on the
           restore point.

        Returns a :class:`RestoreResult` describing the outcome.
        """

        excluded = set(excluded_fields or ())
        target_rp = self._store.load(rp_id)

        # 1. before_restore safety snapshot.
        before_trigger = RestorePointTrigger(
            type="before_restore",
            source_label="Before restore",
            reason=f"Created automatically before restoring '{target_rp.title}'",
        )
        before_rp = self.capture(
            before_trigger,
            title=f"Before restoring {target_rp.title}",
            device_identity=target_rp.device_identity,
        )
        before_id = before_rp.id if before_rp is not None else None

        if before_rp is None:
            # Flaky-controller case: capture() returned None (live read failed,
            # no fresh cached snapshot), so this restore will overwrite the
            # controller with NO recoverable before-snapshot. Surface it BEFORE
            # the apply (it was previously only visible post-apply). We do NOT
            # abort — restore proceeding is by design — but the caller/UI can
            # warn: the returned RestoreResult's before_restore_point_id is None,
            # the machine-readable "no rollback point" signal it already keys on.
            logger.warning(
                "restore: before-restore safety snapshot could not be captured "
                "for restore of '%s'; proceeding WITHOUT a rollback point (no "
                "recoverable pre-restore controller state).",
                target_rp.title,
            )

        # 2. Filter the snapshot to writable + captured + not-excluded fields.
        filtered, attempted_fields = _filter_snapshot_for_restore(
            target_rp.snapshot,
            target_rp.coverage,
            excluded,
        )

        # 3. Apply through the coordinator.
        apply_result = self._apply_coordinator.apply_snapshot(filtered)

        # Quiet interval between the apply burst and the verify read — see
        # the ``post_apply_read_settle_s`` constructor comment.
        if self._post_apply_read_settle_s > 0:
            self._sleep(self._post_apply_read_settle_s)

        # 4. Read controller back via fresh reads.
        readback, _, read_errors = self._do_fresh_read()

        # 5. Build per-field outcomes. An unconfirmed capability may have
        # substituted a 3-point host write for an 8-point rider, so compare the
        # snapshot and field names the coordinator actually sent, not the
        # captured intent.
        verified_snapshot = snapshot_as_sent(filtered, apply_result)
        verified_fields = _attempted_fields_as_sent(
            attempted_fields, verified_snapshot
        )
        field_outcomes = _build_field_outcomes(
            verified_fields,
            verified_snapshot,
            readback,
            apply_result,
            read_errors,
        )
        unverified_writes = _unverified_writes_after_final_restore_read(
            getattr(apply_result, "unverified_writes", ()),
            field_outcomes,
            verified_snapshot,
        )

        wrote_succeeded = sum(1 for f in field_outcomes if f.write_succeeded)
        write_failed = sum(1 for f in field_outcomes if not f.write_succeeded)
        verified_matched = sum(1 for f in field_outcomes if f.verify_matched is True)
        could_not_verify = sum(1 for f in field_outcomes if f.verify_matched is None)
        mismatched = sum(1 for f in field_outcomes if f.verify_matched is False)
        label = _label_for(
            attempted=len(field_outcomes),
            write_failed=write_failed,
            mismatched=mismatched,
            could_not_verify=could_not_verify,
        )

        completed_at = _iso_from_datetime(self._utc_now())
        result = RestoreResult(
            label=label,
            attempted=len(field_outcomes),
            wrote_succeeded=wrote_succeeded,
            write_failed=write_failed,
            verified_matched=verified_matched,
            could_not_verify=could_not_verify,
            mismatched=mismatched,
            fields=tuple(field_outcomes),
            before_restore_point_id=before_id,
            completed_at=completed_at,
            sensitivity_downgrades=apply_result.sensitivity_downgrades,
            unverified_writes=unverified_writes,
        )

        # 6. Persist RestoreAttemptRecord onto the restore point.
        record = RestoreAttemptRecord(
            attempted_at=completed_at,
            label=label,
            attempted=len(field_outcomes),
            wrote_succeeded=wrote_succeeded,
            write_failed=write_failed,
            verified_matched=verified_matched,
            could_not_verify=could_not_verify,
            mismatched=mismatched,
        )
        updated_rp = replace(target_rp, last_restore_attempt=record)
        # The firmware apply already committed above; persisting the attempt
        # record is best-effort bookkeeping. An OSError here must NOT propagate
        # and discard the RestoreResult (the caller needs before_restore_point_id
        # for rollback), so log and continue.
        try:
            self._store.save(updated_rp)
        except OSError:
            logger.exception(
                "restore: persisting RestoreAttemptRecord failed after a "
                "committed apply (rp_id=%s); returning the result regardless",
                rp_id,
            )
        if self._wear_ledger is not None:
            self._wear_ledger.append(
                RP_RESTORE,
                summary=f"Restored '{target_rp.title}' ({label.value})",
                details={
                    "rp_id": rp_id,
                    "title": target_rp.title,
                    "result_label": label.value,
                    "attempted": len(field_outcomes),
                    "wrote_succeeded": wrote_succeeded,
                    "write_failed": write_failed,
                    "verified_matched": verified_matched,
                    "could_not_verify": could_not_verify,
                    "mismatched": mismatched,
                    "before_restore_point_id": before_id,
                },
            )
        return result

    # -- pre-restore preview -------------------------------------------------

    def compute_restore_preview(self, rp_id: str) -> RestorePreview:
        """Compute a pre-restore preview comparing the captured RP snapshot
        to the live controller state.

        Reads the live controller via the same :meth:`_do_fresh_read`
        machinery used by :meth:`capture` and :meth:`restore`, then walks
        the same writable-and-captured field set :func:`_filter_snapshot_for_restore`
        identifies for the restore — minus the write-only fields the device
        can't read back (see :data:`_PREVIEW_EXCLUDED_FIELDS`).

        On a per-field read failure (HID timeout, exception, or a clean
        ``None`` return), the impacted entry's ``current_value`` is ``None``
        and ``note`` carries the short exception text (mirroring the
        :class:`RestoreFieldOutcome.verify_note` pattern). The preview NEVER
        raises — read failures are reflected in field-level notes, not
        propagated to the caller. Symmetric with the result-page UX in
        :func:`zd_app.ui.screens.restore_points._build_result`.
        """

        target_rp = self._store.load(rp_id)
        current_snapshot, _read_success, read_errors = self._do_fresh_read()
        return _build_restore_preview(target_rp, current_snapshot, read_errors)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _any_read_succeeded(read_success: Mapping[str, object]) -> bool:
    for value in read_success.values():
        if isinstance(value, bool):
            if value:
                return True
        elif isinstance(value, set):
            if value:
                return True
    return False


def _read_success_from_snapshot(snapshot: ControllerSnapshot) -> dict[str, object]:
    """Derive a per-field success map from an already-built snapshot.

    Used when falling back to a cached snapshot: we can't tell from the
    snapshot alone whether a None was "didn't read" vs "legitimate None", so
    we conservatively treat non-None as captured and None as not_captured.
    This is the explicit honesty trade-off the cached-snapshot mode makes —
    documented at :attr:`CaptureSource.CACHED_SNAPSHOT`.
    """

    bindings = snapshot.button_bindings or {}
    paddles = snapshot.back_paddle_bindings or {}
    zones = snapshot.lighting_zones or {}
    return {
        "polling_rate": snapshot.polling_rate is not None,
        "step_size": snapshot.step_size is not None,
        "vibration": snapshot.vibration is not None,
        "deadzones": snapshot.deadzones is not None,
        "axis_inversion_left": snapshot.axis_inversion_left is not None,
        "axis_inversion_right": snapshot.axis_inversion_right is not None,
        "sensitivity_left": snapshot.sensitivity_left is not None,
        "sensitivity_right": snapshot.sensitivity_right is not None,
        "trigger_left": snapshot.trigger_left is not None,
        "trigger_right": snapshot.trigger_right is not None,
        "motion_settings": snapshot.motion_settings is not None,
        "button_bindings": set(bindings.keys()),
        "back_paddle_bindings": set(paddles.keys()),
        "lighting_zones": set(zones.keys()),
    }


def _build_coverage(
    snapshot: ControllerSnapshot,
    read_success: Mapping[str, object],
    capture_source: CaptureSource,
) -> RestorePointCoverage:
    fields: dict[str, FieldCoverage] = {}
    for name, category, writable in _SCALAR_FIELDS:
        captured = bool(read_success.get(name, False))
        if category == CoverageCategory.UNSUPPORTED:
            state = CoverageState.CAPTURED if captured else CoverageState.UNSUPPORTED
            note = (
                "Captured for record only; no supported write path in this app."
                if captured
                else "No supported write path in this app."
            )
        else:
            state = CoverageState.CAPTURED if captured else CoverageState.NOT_CAPTURED
            note = None
        fields[name] = FieldCoverage(
            state=state,
            readable=captured,
            writable=writable,
            category=category,
            note=note,
        )

    for name, category, writable, expected in _COLLECTION_FIELDS:
        captured_keys = read_success.get(name, set())
        if not isinstance(captured_keys, set):
            captured_keys = set()
        captured_count = len(captured_keys)
        if captured_count == 0:
            state = CoverageState.NOT_CAPTURED
            note = None
        elif captured_count < expected:
            state = CoverageState.PARTIAL
            note = (
                f"Captured {captured_count} of {expected} supported entries."
            )
        else:
            state = CoverageState.CAPTURED
            note = None
        fields[name] = FieldCoverage(
            state=state,
            readable=(captured_count > 0),
            writable=writable,
            category=category,
            note=note,
        )

    total_supported = sum(
        1 for coverage in fields.values()
        if coverage.category not in (CoverageCategory.UNSUPPORTED, CoverageCategory.METADATA)
    )
    captured_supported = sum(
        1 for coverage in fields.values()
        if coverage.category not in (CoverageCategory.UNSUPPORTED, CoverageCategory.METADATA)
        and coverage.state in (CoverageState.CAPTURED, CoverageState.PARTIAL)
    )
    return RestorePointCoverage(
        captured_supported_count=captured_supported,
        total_supported_count=total_supported,
        fields=fields,
        capture_source=capture_source,
    )


def _filter_snapshot_for_restore(
    snapshot: ControllerSnapshot,
    coverage: RestorePointCoverage,
    excluded: set[str],
) -> tuple[ControllerSnapshot, list[str]]:
    """Drop fields that aren't writable, weren't captured, or are excluded.

    Returns the filtered snapshot and the list of attempted field names so
    the verification pass knows which fields to compare.
    """

    def _keep(name: str) -> bool:
        if name in excluded:
            return False
        field_coverage = coverage.fields.get(name)
        if field_coverage is None or not field_coverage.writable:
            return False
        return field_coverage.state in (CoverageState.CAPTURED, CoverageState.PARTIAL)

    attempted: list[str] = []
    kwargs: dict[str, object] = {
        "polling_rate": None,
        "vibration": None,
        "deadzones": None,
        "axis_inversion_left": None,
        "axis_inversion_right": None,
        "sensitivity_left": None,
        "sensitivity_right": None,
        "sensitivity_left_8point": None,
        "sensitivity_right_8point": None,
        "trigger_left": None,
        "trigger_right": None,
        "button_bindings": {},
        "lighting_zones": {},
        "motion_settings": None,
        "back_paddle_bindings": {},
        "step_size": None,
    }
    for name, _, _ in _SCALAR_FIELDS:
        if not _keep(name):
            continue
        value = getattr(snapshot, name)
        if value is None:
            continue
        kwargs[name] = value
        attempted.append(name)
    for name, _, _, _ in _COLLECTION_FIELDS:
        if not _keep(name):
            continue
        value = getattr(snapshot, name) or {}
        if not value:
            continue
        kwargs[name] = value
        attempted.append(name)

    # 8-point sensitivity riders (see _SENSITIVITY_8POINT_RIDERS). A rider is
    # carried and initially verified only when its 3-point host is restore-eligible
    # (writable, captured, not excluded — i.e. present in ``attempted``) AND the
    # capture actually recorded an 8-point curve for that stick. When it applies,
    # the rider REPLACES the host in ``attempted`` so the verify pass compares
    # the 0x86 read-back the coordinator normally writes; a later recorded
    # downgrade converts the verification target back to the host. The host's
    # 3-point value already sits in ``kwargs`` as the coordinator's per-stick
    # fallback. Honoring the rider name in ``excluded`` lets a caller force the
    # 3-point path for a stick.
    for rider_name, host_name in _SENSITIVITY_8POINT_RIDERS:
        if rider_name in excluded:
            continue
        if host_name not in attempted:
            continue
        rider_value = getattr(snapshot, rider_name)
        if rider_value is None:
            continue
        kwargs[rider_name] = rider_value
        attempted[attempted.index(host_name)] = rider_name

    filtered = ControllerSnapshot(**kwargs)  # type: ignore[arg-type]
    return filtered, attempted


def verify_applied_snapshot(
    applied: ControllerSnapshot,
    readback: ControllerSnapshot,
) -> tuple[list[str], list[str]]:
    """Compare a just-applied snapshot against a post-apply device read-back.

    The reusable honest-measurement core for "verified" claims outside
    ``restore()``: WriteFile success does NOT mean the firmware committed (the
    in-burst rejection family documented in
    :mod:`zd_app.services.settings_apply_coordinator`), so a caller that
    applied ``applied`` and re-read the device as ``readback`` calls this to
    learn which written fields actually took.

    Walks exactly the fields the apply pipeline would have written: writable
    :data:`_SCALAR_FIELDS` with a non-``None`` applied value plus non-empty
    :data:`_COLLECTION_FIELDS` entries. ``motion_settings`` is not writable
    and is never compared. 8-point riders dispatch like the coordinator: a
    stick whose ``applied`` carries a ``sensitivity_*_8point`` curve was
    written via cat 0x86, so the rider is compared and the 3-point host
    compare for that stick is skipped (mirrors the attempted-set substitution
    in :func:`_filter_snapshot_for_restore`).

    Returns ``(mismatched_fields, unverifiable_fields)``:

    * mismatched — the device read back a value that differs from what was
      applied (or a readable collection is missing an applied slot/zone: the
      read path enumerates every entry, so an absent one means the device
      answered with nothing where the write burst should have left a value);
    * unverifiable — no honest read-back exists: a scalar (or rider) read
      back ``None``, or a ``back_paddle_bindings`` entry — a write-only HID
      surface (see :data:`_PREVIEW_EXCLUDED_FIELDS`) the device cannot echo.

    Both lists empty == every written field was positively confirmed.
    """

    mismatched: list[str] = []
    unverifiable: list[str] = []

    # Sticks written via cat 0x86 this apply: rider present in ``applied``.
    riders_by_host = {
        host_name: rider_name
        for rider_name, host_name in _SENSITIVITY_8POINT_RIDERS
        if getattr(applied, rider_name) is not None
    }

    def _compare_scalar(name: str) -> None:
        read_value = getattr(readback, name, None)
        if read_value is None:
            unverifiable.append(name)
        elif _readback_differs(name, getattr(applied, name), read_value):
            mismatched.append(name)

    for name, _category, writable in _SCALAR_FIELDS:
        if not writable:
            continue  # motion_settings: captured for record, never written
        rider_name = riders_by_host.get(name)
        if rider_name is not None:
            _compare_scalar(rider_name)
        elif getattr(applied, name) is not None:
            _compare_scalar(name)

    for name, _category, _writable, _expected in _COLLECTION_FIELDS:
        applied_entries = getattr(applied, name) or {}
        if not applied_entries:
            continue
        read_entries = getattr(readback, name, None) or {}
        write_only = name in _PREVIEW_EXCLUDED_FIELDS
        for key, _applied_value, read_value in _mismatched_collection_entries(
            name, applied_entries, read_entries
        ):
            entry_name = f"{name}[{key.name}]"
            if read_value is None:
                (unverifiable if write_only else mismatched).append(entry_name)
            else:
                mismatched.append(entry_name)

    return mismatched, unverifiable


def _build_restore_preview(
    rp: RestorePoint,
    current_snapshot: ControllerSnapshot,
    read_errors: Mapping[str, str],
) -> RestorePreview:
    """Walk the field surface restore() would write and compare RP vs live.

    Excludes :data:`_PREVIEW_EXCLUDED_FIELDS` (write-only HID surfaces
    that can't be read back). Collections (``button_bindings``,
    ``lighting_zones``) expand to one delta per RP-captured slot/zone so
    the modal can call out exactly which binding or zone changed —
    aggregate-level "button_bindings: <huge dict> -> <huge dict>" would
    be visually useless. Unreadable entries carry ``current_value=None``
    + a ``note`` (read-error text or "device not readable" fallback).
    """

    target_snapshot = rp.snapshot
    _, attempted_fields = _filter_snapshot_for_restore(
        target_snapshot,
        rp.coverage,
        excluded=set(_PREVIEW_EXCLUDED_FIELDS),
    )

    deltas: list[RestoreFieldDelta] = []
    for name in attempted_fields:
        if name == "button_bindings":
            target_collection = target_snapshot.button_bindings or {}
            current_collection = current_snapshot.button_bindings or {}
            agg_err = read_errors.get("button_bindings")
            for slot, target_value in target_collection.items():
                deltas.append(
                    _collection_field_delta(
                        field_name=f"button_bindings[{slot.name}]",
                        target_value=target_value,
                        current_value=current_collection.get(slot),
                        current_present=slot in current_collection,
                        aggregate_read_error=agg_err,
                    )
                )
        elif name == "lighting_zones":
            target_collection = target_snapshot.lighting_zones or {}
            current_collection = current_snapshot.lighting_zones or {}
            agg_err = read_errors.get("lighting_zones")
            for zone, target_value in target_collection.items():
                deltas.append(
                    _collection_field_delta(
                        field_name=f"lighting_zones[{zone.name}]",
                        target_value=target_value,
                        current_value=current_collection.get(zone),
                        current_present=zone in current_collection,
                        aggregate_read_error=agg_err,
                    )
                )
        else:
            deltas.append(
                _scalar_field_delta(
                    field_name=name,
                    target_value=getattr(target_snapshot, name),
                    current_value=getattr(current_snapshot, name),
                    read_error=read_errors.get(name),
                )
            )

    fields_changing = sum(1 for d in deltas if d.will_change)
    fields_unreadable = sum(1 for d in deltas if d.current_value is None)
    fields_unchanged = len(deltas) - fields_changing - fields_unreadable
    return RestorePreview(
        fields=tuple(deltas),
        fields_changing=fields_changing,
        fields_unchanged=fields_unchanged,
        fields_unreadable=fields_unreadable,
    )


def _scalar_field_delta(
    *,
    field_name: str,
    target_value: object,
    current_value: object,
    read_error: Optional[str],
) -> RestoreFieldDelta:
    target_str = (
        format_field_value(field_name, target_value)
        if target_value is not None
        else None
    )
    if current_value is None or read_error:
        note = (
            f"device read failed: {read_error}"
            if read_error
            else "device not readable"
        )
        return RestoreFieldDelta(
            field_name=field_name,
            will_change=False,
            current_value=None,
            target_value=target_str,
            note=note,
        )
    current_str = format_field_value(field_name, current_value)
    try:
        will_change = current_value != target_value
    except Exception:  # noqa: BLE001
        # Defensive fallback for any dataclass whose __eq__ raises — compare
        # the rendered string forms instead so the preview still produces
        # a usable answer.
        will_change = current_str != target_str
    return RestoreFieldDelta(
        field_name=field_name,
        will_change=will_change,
        current_value=current_str,
        target_value=target_str,
        note=None,
    )


def _collection_field_delta(
    *,
    field_name: str,
    target_value: object,
    current_value: object,
    current_present: bool,
    aggregate_read_error: Optional[str],
) -> RestoreFieldDelta:
    target_str = (
        format_field_value(field_name, target_value)
        if target_value is not None
        else None
    )
    if not current_present or current_value is None:
        note = (
            f"device read failed: {aggregate_read_error}"
            if aggregate_read_error
            else "device not readable"
        )
        return RestoreFieldDelta(
            field_name=field_name,
            will_change=False,
            current_value=None,
            target_value=target_str,
            note=note,
        )
    current_str = format_field_value(field_name, current_value)
    try:
        will_change = current_value != target_value
    except Exception:  # noqa: BLE001
        will_change = current_str != target_str
    return RestoreFieldDelta(
        field_name=field_name,
        will_change=will_change,
        current_value=current_str,
        target_value=target_str,
        note=None,
    )


def _default_title(trigger: RestorePointTrigger, moment: datetime) -> str:
    stamp = moment.strftime("%Y-%m-%d %H:%M")
    label = trigger.source_label or trigger.type.replace("_", " ").title()
    return f"{label} — {stamp}"


def _iso_from_datetime(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unknown_device_identity() -> DeviceIdentity:
    return DeviceIdentity(
        vid=None,
        pid=None,
        product_string=None,
        firmware_version=None,
        identity_confidence=IdentityConfidence.UNKNOWN,
    )


__all__ = [
    "FRESH_READ_MAX_AGE_S",
    "RestorePointService",
    "verify_applied_snapshot",
]
