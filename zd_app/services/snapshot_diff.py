"""Pure field-by-field diff of two :class:`ControllerSnapshot` instances.

Powers the **Device vs Profile** screen (Phase 1): the *live device read*
(Current) vs a *selected wrapper profile* (Selected). UI-free and fully
unit-tested — the screen layer only renders the :class:`SnapshotDiff` this
module returns.

Honest-measurement is the whole point, so the differ never guesses:

* ``None`` is overloaded (read-failure / timeout vs legitimately-absent). The
  Current column distinguishes "unreadable" from "absent" via the **provenance
  maps** (``current_read_errors`` / ``current_read_success`` from
  :meth:`RestorePointService._do_fresh_read`), never from a raw ``None``. An
  unreadable field is ``current_unreadable`` — never ``changed``.
* ``back_paddle_bindings`` is a write-only HID surface (no honest device read)
  → every slot row is ``write_only``, current rendered as a dash.
* ``motion_settings`` is read-only / unsupported (the app never writes it) →
  ``read_only_unsupported``; the row shows values but never implies a change the
  app could act on.
* 8-point sensitivity folds onto its 3-point host: when either side carries a
  ``sensitivity_*_8point`` curve the comparison is emitted under the rider name
  and the 3-point host row is suppressed — one sensitivity row per stick, never
  both encodings. When only ONE side carries the rider and the other side holds
  a 3-point value, the encodings are not directly comparable: the row shows
  BOTH values as ``encoding_differs`` (informational) instead of hiding the
  3-point side behind a dash.

Phase 2 adds an optional third side — the **Last-Applied** record (what the
wrapper last sent to the controller). It is an *annotation*, never a row
source: the row set and every Phase-1 column/status are computed exactly as
before, then each row gains ``last_applied_value`` / ``drift`` /
``last_applied_failed``. Drift compares **Current vs Last-Applied** (the
"has my controller drifted since I applied?" question) and is only ever
asserted when both sides are honestly known:

* ``drift`` is ``None`` (unknowable) when there is no record, the field's
  apply failed (``last_applied_failed`` marks it), the current side is
  unreadable or absent, the record lacks the field, the row is write-only
  (``back_paddle_bindings``) or never-applied (``motion_settings``), or the
  two sides hold different sensitivity encodings (8-point vs 3-point).
* ``last_applied_failed`` rows still render their recorded value — it is what
  was *sent* — but the flag tells the UI to caveat it ("failed at apply")
  and the value is never compared against the device.
* The 8-point rider fold applies to the last-applied side exactly as to the
  other two: one sensitivity value per stick (the record's richest encoding),
  attached to whichever single sensitivity row exists for that stick.

``last_applied_failed`` (the parameter) carries the apply coordinator's
``setting_label`` tokens verbatim (``"vibration"``, ``"sens_left"``,
``"binding_A"``, …) as persisted by
:class:`zd_app.storage.last_applied_store.LastAppliedStore`; the mapping from
diff row names to those labels lives here.

Reuses the canonical field registry + formatter + compare idiom from
:mod:`zd_app.services.restore_point_service` /
:mod:`zd_app.services.restore_field_formatting` so the strings and grouping
match the rest of the app (CONFIRM modal, restore-preview, result page).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Optional

from zd_app.services.restore_field_formatting import format_field_value
from zd_app.services.restore_point_service import (
    _COLLECTION_FIELDS,
    _PREVIEW_EXCLUDED_FIELDS,
    _SCALAR_FIELDS,
    _SENSITIVITY_8POINT_RIDERS,
)
from zd_app.services.settings_service import ControllerSnapshot
from zd_app.storage.restore_point_models import CoverageCategory


# -- status vocabulary -------------------------------------------------------
# Plain string constants (not an Enum) so the screen + tests compare against a
# stable, JSON-friendly token without importing an enum type.
STATUS_CHANGED = "changed"
STATUS_SAME = "same"
STATUS_CURRENT_UNREADABLE = "current_unreadable"
STATUS_WRITE_ONLY = "write_only"
STATUS_READ_ONLY_UNSUPPORTED = "read_only_unsupported"
STATUS_ONLY_IN_PROFILE = "only_in_profile"
STATUS_ONLY_ON_DEVICE = "only_on_device"
STATUS_ENCODING_DIFFERS = "encoding_differs"

# -- drift vocabulary (Phase 2: Current vs Last-Applied) ----------------------
DRIFT_DRIFTED = "drifted"
DRIFT_MATCHES = "matches"

# Statuses that roll into ``n_informational`` (everything that is neither a
# clean change, a clean match, nor an honest read failure).
_INFORMATIONAL_STATUSES = frozenset(
    {
        STATUS_WRITE_ONLY,
        STATUS_READ_ONLY_UNSUPPORTED,
        STATUS_ONLY_IN_PROFILE,
        STATUS_ONLY_ON_DEVICE,
        STATUS_ENCODING_DIFFERS,
    }
)

# Deterministic category grouping order for the output rows. Device-global
# fields (polling_rate / step_size) sort first under "device"; the read-only
# motion row sorts last under "unsupported".
_CATEGORY_ORDER: tuple[CoverageCategory, ...] = (
    CoverageCategory.DEVICE,
    CoverageCategory.FEEL,
    CoverageCategory.LAYOUT,
    CoverageCategory.COSMETIC,
    CoverageCategory.UNSUPPORTED,
)
_CATEGORY_RANK: dict[str, int] = {
    cat.value: rank for rank, cat in enumerate(_CATEGORY_ORDER)
}

# Human notes for the informational / unreadable rows.
_NOTE_WRITE_ONLY = "cannot read from device"
_NOTE_READ_ONLY = "read-only; this app does not write this"
_NOTE_UNREADABLE_GENERIC = "could not read from device"
_NOTE_ENCODING_DIFFERS = "different curve formats - not directly comparable"


# Diff-row-name → apply-coordinator ``setting_label`` mapping for the
# last_applied_failed membership test. The labels are the exact strings
# ``SettingsApplyCoordinator.apply_snapshot`` passes to its writer (and that
# the failure modal / LastAppliedRecord.failed_fields carry). Both sensitivity
# encodings of a stick share the host's label — the coordinator writes
# whichever encoding under ``sens_left`` / ``sens_right``. ``motion_settings``
# has no label: the coordinator never writes it.
_SCALAR_APPLY_LABELS: dict[str, str] = {
    "polling_rate": "polling",
    "step_size": "step_size",
    "vibration": "vibration",
    "deadzones": "deadzones",
    "axis_inversion_left": "axis_inv_left",
    "axis_inversion_right": "axis_inv_right",
    "sensitivity_left": "sens_left",
    "sensitivity_right": "sens_right",
    "sensitivity_left_8point": "sens_left",
    "sensitivity_right_8point": "sens_right",
    "trigger_left": "trigger_left",
    "trigger_right": "trigger_right",
}
_COLLECTION_APPLY_LABEL_PREFIXES: dict[str, str] = {
    "button_bindings": "binding",
    "lighting_zones": "lighting",
    "back_paddle_bindings": "back_paddle",
}

# Stick-pairing lookups for the last-applied sensitivity aspect.
_RIDER_HOSTS: dict[str, str] = dict(_SENSITIVITY_8POINT_RIDERS)  # rider -> host
_HOST_RIDERS: dict[str, str] = {host: rider for rider, host in _SENSITIVITY_8POINT_RIDERS}


@dataclass(frozen=True)
class SnapshotFieldDiff:
    """One field/slot row of the Current-vs-Selected diff."""

    field_name: str  # e.g. "polling_rate", "button_bindings[A]"
    category: str  # CoverageCategory.value: device|feel|layout|cosmetic|unsupported
    current_value: Optional[str]  # format_field_value(...) or None when absent/unreadable
    selected_value: Optional[str]  # format_field_value(...) or None when the profile omits it
    status: str  # one of the STATUS_* constants above
    note: Optional[str] = None  # reason for unreadable / write-only / read-only rows
    # -- Phase 2: the Last-Applied annotation (defaults = no record) ---------
    last_applied_value: Optional[str] = None  # format_field_value(...) of the record's value
    drift: Optional[str] = None  # DRIFT_* comparing Current vs Last-Applied; None = unknowable
    last_applied_failed: bool = False  # this field was attempted but not ACKed at apply time


@dataclass(frozen=True)
class SnapshotDiff:
    """The whole diff plus aggregate counts for the summary line."""

    fields: tuple[SnapshotFieldDiff, ...]  # stable order, grouped by category
    n_changed: int
    n_same: int
    n_unreadable: int  # the current side could not be read for this field
    n_informational: int  # write-only / read-only-unsupported / profile-absent rows
    n_drifted: int = 0  # rows where Current verifiably differs from Last-Applied


def compute_snapshot_diff(
    current: ControllerSnapshot,
    selected: ControllerSnapshot,
    *,
    current_read_success: Optional[Mapping[str, object]] = None,
    current_read_errors: Optional[Mapping[str, str]] = None,
    last_applied: Optional[ControllerSnapshot] = None,
    last_applied_failed: frozenset[str] = frozenset(),
) -> SnapshotDiff:
    """Diff the live ``current`` device snapshot against a ``selected`` profile.

    ``current_read_success`` / ``current_read_errors`` are the provenance maps
    from :meth:`RestorePointService.read_current_state_with_provenance` — they
    are what makes the Current column distinguish "unreadable" from
    "legitimately absent" instead of guessing from a raw ``None``. Pass ``None``
    for both when no provenance is available (best-effort: a ``None`` current
    value is then treated as a legitimate absence, not a read failure).

    ``last_applied`` (Phase 2) is the snapshot the wrapper last applied (from
    :class:`~zd_app.storage.last_applied_store.LastAppliedStore`);
    ``last_applied_failed`` is the matching record's coordinator
    ``setting_label`` set. When ``last_applied`` is ``None`` the output is
    byte-for-byte the Phase-1 diff (every row keeps its annotation defaults
    and ``n_drifted`` is 0).
    """

    rows: list[SnapshotFieldDiff] = []

    # Which 3-point hosts fold onto an 8-point curve this comparison: a host
    # folds when *either* side carries a non-None rider value (§3b.4). The
    # folded row is emitted under the rider name and the 3-point host row is
    # suppressed — one sensitivity row per stick, never both encodings.
    fold_hosts: dict[str, str] = {}
    for rider_name, host_name in _SENSITIVITY_8POINT_RIDERS:
        if (
            getattr(current, rider_name, None) is not None
            or getattr(selected, rider_name, None) is not None
        ):
            fold_hosts[host_name] = rider_name

    # Scalar fields (stable registry order).
    for name, category, _writable in _SCALAR_FIELDS:
        if name == "motion_settings":
            rows.append(_motion_row(current, selected, category))
        elif name in fold_hosts:
            rider = fold_hosts[name]
            rows.append(
                _folded_sensitivity_row(
                    name, rider, category, current, selected, current_read_errors, current_read_success
                )
            )
        else:
            rows.append(
                _scalar_row(
                    name, category, current, selected, current_read_errors, current_read_success
                )
            )

    # Collection fields (button_bindings, back_paddle_bindings, lighting_zones)
    # expand to one row per union slot/zone present in either side.
    for name, category, _writable, _expected in _COLLECTION_FIELDS:
        cur_dict = getattr(current, name) or {}
        sel_dict = getattr(selected, name) or {}
        union = sorted(set(cur_dict) | set(sel_dict), key=lambda slot: slot.name)
        write_only = name in _PREVIEW_EXCLUDED_FIELDS

        success_set: Optional[set] = None
        if (
            current_read_success is not None
            and name in current_read_success
            and isinstance(current_read_success[name], set)
        ):
            success_set = current_read_success[name]  # type: ignore[assignment]
        aggregate_error = (
            current_read_errors.get(name) if current_read_errors is not None else None
        )

        for slot in union:
            field_name = f"{name}[{slot.name}]"
            if write_only:
                rows.append(_write_only_row(field_name, category, sel_dict.get(slot)))
            else:
                rows.append(
                    _collection_row(
                        field_name,
                        category,
                        slot,
                        cur_dict,
                        sel_dict,
                        success_set,
                        aggregate_error,
                    )
                )

    ordered = _grouped_by_category(rows)
    if last_applied is not None:
        ordered = _annotate_last_applied(
            ordered,
            current,
            last_applied,
            last_applied_failed,
            current_read_errors,
            current_read_success,
        )
    n_changed = sum(1 for r in ordered if r.status == STATUS_CHANGED)
    n_same = sum(1 for r in ordered if r.status == STATUS_SAME)
    n_unreadable = sum(1 for r in ordered if r.status == STATUS_CURRENT_UNREADABLE)
    n_informational = sum(1 for r in ordered if r.status in _INFORMATIONAL_STATUSES)
    n_drifted = sum(1 for r in ordered if r.drift == DRIFT_DRIFTED)
    return SnapshotDiff(
        fields=tuple(ordered),
        n_changed=n_changed,
        n_same=n_same,
        n_unreadable=n_unreadable,
        n_informational=n_informational,
        n_drifted=n_drifted,
    )


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _scalar_row(
    field_name: str,
    category: CoverageCategory,
    current: ControllerSnapshot,
    selected: ControllerSnapshot,
    errors: Optional[Mapping[str, str]],
    success: Optional[Mapping[str, object]],
) -> SnapshotFieldDiff:
    cur_raw = getattr(current, field_name, None)
    sel_raw = getattr(selected, field_name, None)
    hint, note = _resolve_current(field_name, cur_raw, errors, success)
    sel_present = sel_raw is not None
    sel_str = format_field_value(field_name, sel_raw) if sel_present else None

    if hint == "unreadable":
        return SnapshotFieldDiff(
            field_name,
            category.value,
            None,
            sel_str,
            STATUS_CURRENT_UNREADABLE,
            note=note or _NOTE_UNREADABLE_GENERIC,
        )
    if hint == "present":
        cur_str = format_field_value(field_name, cur_raw)
        if sel_present:
            status = STATUS_CHANGED if _values_differ(cur_raw, sel_raw, cur_str, sel_str) else STATUS_SAME
            return SnapshotFieldDiff(field_name, category.value, cur_str, sel_str, status)
        return SnapshotFieldDiff(field_name, category.value, cur_str, None, STATUS_ONLY_ON_DEVICE)
    # hint == "absent" — a legitimately-absent current value (provenance did not
    # flag it as a read failure).
    if sel_present:
        return SnapshotFieldDiff(field_name, category.value, None, sel_str, STATUS_ONLY_IN_PROFILE)
    return SnapshotFieldDiff(field_name, category.value, None, None, STATUS_SAME)


def _folded_sensitivity_row(
    host_name: str,
    rider_name: str,
    category: CoverageCategory,
    current: ControllerSnapshot,
    selected: ControllerSnapshot,
    errors: Optional[Mapping[str, str]],
    success: Optional[Mapping[str, object]],
) -> SnapshotFieldDiff:
    """The one sensitivity row for a stick whose 3-point host is folded.

    The fold precondition guarantees at least one side carries the 8-point
    rider. When BOTH do, this is an ordinary rider-vs-rider compare. When only
    ONE does and the other side holds a 3-point host value, the encodings are
    not directly comparable — the row shows both values under
    ``STATUS_ENCODING_DIFFERS`` instead of hiding the 3-point side behind an
    only-on-one-side dash. Provenance still wins: an unreadable current
    host/rider renders ``current_unreadable``, never a guessed verdict.
    """

    cur_rider = getattr(current, rider_name, None)
    sel_rider = getattr(selected, rider_name, None)

    # Both sides carry the rider — plain 8-point vs 8-point compare.
    if cur_rider is not None and sel_rider is not None:
        return _scalar_row(rider_name, category, current, selected, errors, success)

    if sel_rider is not None:
        # Rider only in the profile. The current side answers with its 3-point
        # host, is unreadable, or has nothing for this stick at all.
        sel_str = format_field_value(rider_name, sel_rider)
        rider_hint, rider_note = _resolve_current(rider_name, cur_rider, errors, success)
        if rider_hint == "unreadable":
            return SnapshotFieldDiff(
                rider_name,
                category.value,
                None,
                sel_str,
                STATUS_CURRENT_UNREADABLE,
                note=rider_note or _NOTE_UNREADABLE_GENERIC,
            )
        cur_host = getattr(current, host_name, None)
        host_hint, host_note = _resolve_current(host_name, cur_host, errors, success)
        if host_hint == "unreadable":
            return SnapshotFieldDiff(
                rider_name,
                category.value,
                None,
                sel_str,
                STATUS_CURRENT_UNREADABLE,
                note=host_note or _NOTE_UNREADABLE_GENERIC,
            )
        if host_hint == "present":
            return SnapshotFieldDiff(
                rider_name,
                category.value,
                format_field_value(host_name, cur_host),
                sel_str,
                STATUS_ENCODING_DIFFERS,
                note=_NOTE_ENCODING_DIFFERS,
            )
        return SnapshotFieldDiff(
            rider_name, category.value, None, sel_str, STATUS_ONLY_IN_PROFILE
        )

    # Rider only on the device (the fold precondition makes cur_rider non-None
    # here). An explicit read error on the rider still wins over its value.
    rider_hint, rider_note = _resolve_current(rider_name, cur_rider, errors, success)
    if rider_hint == "unreadable":
        return SnapshotFieldDiff(
            rider_name,
            category.value,
            None,
            None,
            STATUS_CURRENT_UNREADABLE,
            note=rider_note or _NOTE_UNREADABLE_GENERIC,
        )
    cur_str = format_field_value(rider_name, cur_rider)
    sel_host = getattr(selected, host_name, None)
    if sel_host is not None:
        return SnapshotFieldDiff(
            rider_name,
            category.value,
            cur_str,
            format_field_value(host_name, sel_host),
            STATUS_ENCODING_DIFFERS,
            note=_NOTE_ENCODING_DIFFERS,
        )
    return SnapshotFieldDiff(
        rider_name, category.value, cur_str, None, STATUS_ONLY_ON_DEVICE
    )


def _collection_row(
    field_name: str,
    category: CoverageCategory,
    slot,
    cur_dict: Mapping,
    sel_dict: Mapping,
    success_set: Optional[set],
    aggregate_error: Optional[str],
) -> SnapshotFieldDiff:
    sel_raw = sel_dict.get(slot)
    sel_present = sel_raw is not None
    sel_str = format_field_value(field_name, sel_raw) if sel_present else None

    # Current-side resolution. When a per-slot success set is available (the
    # normal fresh-read path), a slot missing from it is an honest read miss —
    # current_unreadable, NOT a phantom change. Without a success set, fall back
    # to dict membership + the aggregate read error.
    cur_raw = cur_dict.get(slot)
    if success_set is not None:
        cur_unreadable = slot not in success_set
    else:
        cur_unreadable = slot not in cur_dict and aggregate_error is not None
    if cur_unreadable:
        return SnapshotFieldDiff(
            field_name,
            category.value,
            None,
            sel_str,
            STATUS_CURRENT_UNREADABLE,
            note=aggregate_error or _NOTE_UNREADABLE_GENERIC,
        )

    cur_present = cur_raw is not None
    if cur_present:
        cur_str = format_field_value(field_name, cur_raw)
        if sel_present:
            status = STATUS_CHANGED if _values_differ(cur_raw, sel_raw, cur_str, sel_str) else STATUS_SAME
            return SnapshotFieldDiff(field_name, category.value, cur_str, sel_str, status)
        return SnapshotFieldDiff(field_name, category.value, cur_str, None, STATUS_ONLY_ON_DEVICE)
    if sel_present:
        return SnapshotFieldDiff(field_name, category.value, None, sel_str, STATUS_ONLY_IN_PROFILE)
    return SnapshotFieldDiff(field_name, category.value, None, None, STATUS_SAME)


def _write_only_row(
    field_name: str, category: CoverageCategory, sel_raw
) -> SnapshotFieldDiff:
    """A ``back_paddle_bindings`` slot: write-only, no honest device read."""

    sel_str = format_field_value(field_name, sel_raw) if sel_raw is not None else None
    return SnapshotFieldDiff(
        field_name,
        category.value,
        None,
        sel_str,
        STATUS_WRITE_ONLY,
        note=_NOTE_WRITE_ONLY,
    )


def _motion_row(
    current: ControllerSnapshot, selected: ControllerSnapshot, category: CoverageCategory
) -> SnapshotFieldDiff:
    """``motion_settings`` row: always read-only/unsupported (app never writes it)."""

    cur = current.motion_settings
    sel = selected.motion_settings
    cur_str = format_field_value("motion_settings", cur) if cur is not None else None
    sel_str = format_field_value("motion_settings", sel) if sel is not None else None
    return SnapshotFieldDiff(
        "motion_settings",
        category.value,
        cur_str,
        sel_str,
        STATUS_READ_ONLY_UNSUPPORTED,
        note=_NOTE_READ_ONLY,
    )


# ---------------------------------------------------------------------------
# Last-Applied annotation (Phase 2)
# ---------------------------------------------------------------------------
# A pure post-pass over the finished Phase-1 rows: the row set, ordering and
# every Phase-1 column/status are untouched; each row only gains its
# (last_applied_value, drift, last_applied_failed) triple. Keeping this out of
# the row builders pins Phase-1 behavior byte-for-byte and keeps the drift
# rules in one place.


def _annotate_last_applied(
    rows: list[SnapshotFieldDiff],
    current: ControllerSnapshot,
    last_applied: ControllerSnapshot,
    failed_labels: frozenset[str],
    errors: Optional[Mapping[str, str]],
    success: Optional[Mapping[str, object]],
) -> list[SnapshotFieldDiff]:
    out: list[SnapshotFieldDiff] = []
    for row in rows:
        value, drift, failed = _last_applied_aspect(
            row.field_name, current, last_applied, failed_labels, errors, success
        )
        out.append(
            replace(
                row,
                last_applied_value=value,
                drift=drift,
                last_applied_failed=failed,
            )
        )
    return out


def _last_applied_aspect(
    field_name: str,
    current: ControllerSnapshot,
    la: ControllerSnapshot,
    failed_labels: frozenset[str],
    errors: Optional[Mapping[str, str]],
    success: Optional[Mapping[str, object]],
) -> tuple[Optional[str], Optional[str], bool]:
    """Resolve one row's ``(last_applied_value, drift, last_applied_failed)``."""

    if "[" in field_name and field_name.endswith("]"):
        coll, _, slot_token = field_name.partition("[")
        return _collection_aspect(
            coll, slot_token[:-1], current, la, failed_labels, errors, success
        )
    if field_name == "motion_settings":
        # The apply pipeline never writes motion_settings; the record may
        # carry a value it *received*, but presenting it as "last applied"
        # would overclaim. No coordinator label exists for it either.
        return None, None, False
    if field_name in _RIDER_HOSTS or field_name in _HOST_RIDERS:
        return _sensitivity_aspect(field_name, current, la, failed_labels, errors, success)
    return _scalar_aspect(field_name, current, la, failed_labels, errors, success)


def _scalar_aspect(
    name: str,
    current: ControllerSnapshot,
    la: ControllerSnapshot,
    failed_labels: frozenset[str],
    errors: Optional[Mapping[str, str]],
    success: Optional[Mapping[str, object]],
) -> tuple[Optional[str], Optional[str], bool]:
    label = _SCALAR_APPLY_LABELS.get(name)
    failed = label is not None and label in failed_labels
    la_raw = getattr(la, name, None)
    la_str = format_field_value(name, la_raw) if la_raw is not None else None
    if failed:
        # The value was *sent* but never ACKed — render it, never compare it.
        return la_str, None, True
    if la_raw is None:
        return None, None, False
    cur_raw = getattr(current, name, None)
    hint, _note = _resolve_current(name, cur_raw, errors, success)
    if hint != "present":
        # Unreadable current → unknowable; legitimately-absent current is not
        # called a drift either (mirrors Phase 1 never calling absence a change).
        return la_str, None, False
    cur_str = format_field_value(name, cur_raw)
    drifted = _values_differ(cur_raw, la_raw, cur_str, la_str)
    return la_str, DRIFT_DRIFTED if drifted else DRIFT_MATCHES, False


def _sensitivity_aspect(
    row_name: str,
    current: ControllerSnapshot,
    la: ControllerSnapshot,
    failed_labels: frozenset[str],
    errors: Optional[Mapping[str, str]],
    success: Optional[Mapping[str, object]],
) -> tuple[Optional[str], Optional[str], bool]:
    """The one last-applied sensitivity value for a stick's single row.

    The row is the rider when the Phase-1 fold fired (either Current or
    Selected carried the 8-point curve) and the 3-point host otherwise. The
    last-applied side independently contributes its richest encoding for the
    stick; drift is computed only when Current and Last-Applied hold the SAME
    encoding (cross-encoding values are not directly comparable — the same
    honesty as ``STATUS_ENCODING_DIFFERS``).
    """

    if row_name in _RIDER_HOSTS:
        rider, host = row_name, _RIDER_HOSTS[row_name]
    else:
        host, rider = row_name, _HOST_RIDERS[row_name]
    # Both encodings of a stick write under the host's coordinator label.
    failed = _SCALAR_APPLY_LABELS[host] in failed_labels

    la_rider = getattr(la, rider, None)
    la_host = getattr(la, host, None)
    if la_rider is not None:
        la_name, la_raw = rider, la_rider
    elif la_host is not None:
        la_name, la_raw = host, la_host
    else:
        la_name, la_raw = None, None
    la_str = format_field_value(la_name, la_raw) if la_raw is not None else None

    if failed:
        return la_str, None, True
    if la_raw is None:
        return None, None, False

    # Current side: the rider wins when present; provenance wins over both.
    cur_rider = getattr(current, rider, None)
    rider_hint, _ = _resolve_current(rider, cur_rider, errors, success)
    if rider_hint == "unreadable":
        return la_str, None, False
    if cur_rider is not None:
        cur_name, cur_raw = rider, cur_rider
    else:
        cur_host = getattr(current, host, None)
        host_hint, _ = _resolve_current(host, cur_host, errors, success)
        if host_hint == "unreadable" or cur_host is None:
            return la_str, None, False
        cur_name, cur_raw = host, cur_host

    if cur_name != la_name:
        return la_str, None, False
    cur_str = format_field_value(cur_name, cur_raw)
    drifted = _values_differ(cur_raw, la_raw, cur_str, la_str)
    return la_str, DRIFT_DRIFTED if drifted else DRIFT_MATCHES, False


def _collection_aspect(
    coll: str,
    slot_name: str,
    current: ControllerSnapshot,
    la: ControllerSnapshot,
    failed_labels: frozenset[str],
    errors: Optional[Mapping[str, str]],
    success: Optional[Mapping[str, object]],
) -> tuple[Optional[str], Optional[str], bool]:
    field_name = f"{coll}[{slot_name}]"
    prefix = _COLLECTION_APPLY_LABEL_PREFIXES.get(coll)
    failed = prefix is not None and f"{prefix}_{slot_name}" in failed_labels

    la_by_name = {
        getattr(slot, "name", slot): value
        for slot, value in (getattr(la, coll, None) or {}).items()
    }
    la_raw = la_by_name.get(slot_name)
    la_str = format_field_value(field_name, la_raw) if la_raw is not None else None
    if failed:
        return la_str, None, True
    if coll in _PREVIEW_EXCLUDED_FIELDS:
        # back_paddle_bindings: write-only — the record is the only honest
        # source for the value, but with no device read there is no drift.
        return la_str, None, False
    if la_raw is None:
        return None, None, False

    # Current-side readability mirrors _collection_row exactly: a per-slot
    # success set wins; otherwise dict-absence + an aggregate error means
    # unreadable, and plain absence is a legitimate absence.
    cur_by_name = {
        getattr(slot, "name", slot): value
        for slot, value in (getattr(current, coll, None) or {}).items()
    }
    success_set: Optional[set] = None
    if success is not None and coll in success and isinstance(success[coll], set):
        success_set = {getattr(slot, "name", slot) for slot in success[coll]}
    aggregate_error = errors.get(coll) if errors is not None else None
    if success_set is not None:
        cur_unreadable = slot_name not in success_set
    else:
        cur_unreadable = slot_name not in cur_by_name and aggregate_error is not None
    if cur_unreadable:
        return la_str, None, False
    cur_raw = cur_by_name.get(slot_name)
    if cur_raw is None:
        return la_str, None, False
    cur_str = format_field_value(field_name, cur_raw)
    drifted = _values_differ(cur_raw, la_raw, cur_str, la_str)
    return la_str, DRIFT_DRIFTED if drifted else DRIFT_MATCHES, False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_current(
    field_name: str,
    current_value: object,
    errors: Optional[Mapping[str, str]],
    success: Optional[Mapping[str, object]],
) -> tuple[str, Optional[str]]:
    """Resolve the current side to ``("unreadable"|"present"|"absent", note)``.

    Drives the unreadable-vs-absent distinction from the provenance maps, never
    from the raw ``None`` (§3b.2):

    1. an explicit read error → ``unreadable`` (note = the error string);
    2. else a provenance ``success`` entry that says the field was not captured
       → ``unreadable``;
    3. else a ``None`` value is a legitimate absence and a non-``None`` value is
       present.
    """

    if errors is not None and field_name in errors:
        return "unreadable", errors[field_name]
    if success is not None and field_name in success:
        captured = success[field_name]
        if isinstance(captured, bool) and not captured:
            return "unreadable", None
    if current_value is None:
        return "absent", None
    return "present", None


def _values_differ(a: object, b: object, a_str: str, b_str: str) -> bool:
    """Exact inequality with a string-compare fallback for an ``__eq__`` that raises.

    Every snapshot field is int-valued (or a frozen dataclass of ints), so exact
    ``!=`` is correct — no float epsilon. Mirrors ``_scalar_field_delta`` in
    :mod:`zd_app.services.restore_point_service`.
    """

    try:
        return a != b
    except Exception:  # noqa: BLE001 - defensive: any dataclass whose __eq__ raises
        return a_str != b_str


def _grouped_by_category(rows: list[SnapshotFieldDiff]) -> list[SnapshotFieldDiff]:
    """Stable-sort rows into the fixed category order, preserving walk order within."""

    return sorted(
        rows,
        key=lambda r: _CATEGORY_RANK.get(r.category, len(_CATEGORY_ORDER)),
    )


__all__ = [
    "STATUS_CHANGED",
    "STATUS_SAME",
    "STATUS_CURRENT_UNREADABLE",
    "STATUS_WRITE_ONLY",
    "STATUS_READ_ONLY_UNSUPPORTED",
    "STATUS_ONLY_IN_PROFILE",
    "STATUS_ONLY_ON_DEVICE",
    "STATUS_ENCODING_DIFFERS",
    "DRIFT_DRIFTED",
    "DRIFT_MATCHES",
    "SnapshotFieldDiff",
    "SnapshotDiff",
    "compute_snapshot_diff",
]
