"""Single source of truth for the Restore Points claim-boundary paragraphs.

Both paragraphs are the required Restore Points claim-boundary statement.
They must appear unchanged in:

- the Restore Points detail view (long paragraph at top);
- every JSON export of a restore point (long paragraph as the
  ``claim_boundary_paragraph`` field — long-form for support / audit);
- the list-view footer caveat (short paragraph).

The forbidden-phrase test in
``tests/test_restore_point_forbidden_phrases.py`` deliberately whitelists
both paragraphs because they intentionally use the never-words
(``factory image``, ``firmware backup``) in denial — the same pattern the Health Report
follows for its own claim boundary.
"""

from __future__ import annotations


# Long-form claim-boundary paragraph. Per the "Required claim-boundary
# statement" rule — verbatim. Do not paraphrase, do not split, do not localize.
CLAIM_BOUNDARY_PARAGRAPH = (
    "Restore Points save the controller settings this app can read through "
    "its supported HID paths at the time the snapshot is created. They are "
    "useful for returning supported settings — such as polling rate, "
    "step-size, deadzones, sensitivity curves, trigger ranges, vibration, "
    "supported button/back-paddle bindings, and supported lighting fields — "
    "to an earlier app-visible state. They are not a factory image, firmware "
    "backup, hidden calibration dump, or guarantee that every controller "
    "setting was captured. Some fields may be unavailable, partially "
    "readable, intermittently readable, or not writable on restore; after a "
    "restore, the app reads back what it can and reports any missing, "
    "failed, or mismatched fields."
)


# Short UI-footer variant. Per the "Required claim-boundary statement" rule —
# verbatim. Used in list-view footer + confirmation-modal copy.
CLAIM_BOUNDARY_SHORT_UI = (
    "Restore Points capture app-supported, app-readable settings. They are "
    "not factory backups. After restore, the app verifies what it can and "
    "reports any fields that failed, could not be read, or did not match."
)


__all__ = [
    "CLAIM_BOUNDARY_PARAGRAPH",
    "CLAIM_BOUNDARY_SHORT_UI",
]
