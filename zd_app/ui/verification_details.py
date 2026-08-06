"""Localized, honest rendering for per-field write verification details."""

from __future__ import annotations

import re

from zd_app.i18n import t
from zd_app.services.write_verification import SENSITIVITY_8POINT_RIDERS
from zd_app.storage.restore_point_models import RestoreFieldOutcome


_SENSITIVITY_8POINT_HOSTS = dict(SENSITIVITY_8POINT_RIDERS)
_INDEXED_FIELD_RE = re.compile(
    r"^(?P<prefix>button_bindings|lighting_zones)\[(?P<item>[^\[\]]+)\]$"
)
_VERIFY_NOTE_KEYS = {
    "read-back did not return a value": "verification_details.note.no_value",
    "read-back value could not be compared": "verification_details.note.could_not_compare",
    "read-back value differs from expected": "verification_details.note.differs",
    "write-only": "verification_details.note.write_only",
}
_VERIFY_READ_FAILED_PREFIX = "verify-read failed: "


def _translate_or_raw(key: str, raw: str) -> str:
    translated = t(key)
    return raw if translated == f"[{key}]" else translated


def verification_field_label(field_name: str) -> str:
    """Localize known field identifiers while preserving unknown identifiers."""

    host_name = _SENSITIVITY_8POINT_HOSTS.get(field_name)
    if host_name is not None:
        return t(
            "verification_details.field.eight_point",
            field=_translate_or_raw(f"field.label.{host_name}", host_name),
        )

    indexed = _INDEXED_FIELD_RE.fullmatch(field_name)
    if indexed is not None:
        prefix = indexed.group("prefix")
        item = indexed.group("item")
        if prefix == "lighting_zones":
            item = _translate_or_raw(
                f"controller.choice.lighting_zone.{item.lower()}",
                item,
            )
        return t(f"verification_details.field_prefix.{prefix}", item=item)

    return _translate_or_raw(f"field.label.{field_name}", field_name)


def _verification_note(note: str) -> str:
    key = _VERIFY_NOTE_KEYS.get(note)
    if key is not None:
        return t(key)
    if note.startswith(_VERIFY_READ_FAILED_PREFIX):
        # The prefix is UI vocabulary; the remainder is device/OS diagnostic
        # payload and must stay byte-for-byte honest.
        return t(
            "verification_details.note.verify_read_failed",
            reason=note.removeprefix(_VERIFY_READ_FAILED_PREFIX),
        )
    return note


def format_verification_outcome(
    outcome: RestoreFieldOutcome,
) -> tuple[str, str | None]:
    """Return a localized main row and optional expected/observed detail row.

    Known UI statuses, labels, and service-authored verification notes are
    localized. Unknown error/note strings and the expected/observed values are
    controller or OS payload, so they are intentionally preserved verbatim.
    """

    write_status_key = (
        "verification_details.status.write_ok"
        if outcome.write_succeeded
        else "verification_details.status.write_failed"
    )
    if outcome.verify_matched is True:
        verify_status_key = "verification_details.status.verify_matched"
    elif outcome.verify_matched is False:
        verify_status_key = "verification_details.status.verify_mismatch"
    else:
        verify_status_key = "verification_details.status.verify_unverified"

    row = t(
        "verification_details.row",
        field=verification_field_label(outcome.field_name),
        write_prefix=t("verification_details.prefix.write"),
        write_status=t(write_status_key),
        verify_prefix=t("verification_details.prefix.verify"),
        verify_status=t(verify_status_key),
    )

    detail: str | None = None
    if outcome.write_error:
        detail = _translate_or_raw(outcome.write_error, outcome.write_error)
    elif outcome.verify_matched is None and outcome.verify_note:
        detail = _verification_note(outcome.verify_note)
    if detail:
        row = t("verification_details.row_with_detail", row=row, detail=detail)

    expected_observed: str | None = None
    if (
        outcome.verify_matched is False
        and outcome.expected_value is not None
        and outcome.observed_value is not None
    ):
        expected_observed = t(
            "verification_details.expected_observed",
            expected_prefix=t("verification_details.prefix.expected"),
            expected=outcome.expected_value,
            observed_prefix=t("verification_details.prefix.observed"),
            observed=outcome.observed_value,
        )
    return row, expected_observed


__all__ = ["format_verification_outcome", "verification_field_label"]
