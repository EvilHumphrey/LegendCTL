"""Cross-registry drift gate for the ControllerSnapshot field model.

``ControllerSnapshot`` (zd_app/services/settings_service.py) is declared once,
but its field list is re-encoded in three registries that history shows drift
independently — the 2026-06-09 review found the 8-point sensitivity fields
known to the import classifier yet missing from Safe Import's UI map, so
imported curves rendered no preview row AND survived FEEL deselection. This
module pins KEY-SET parity so the next snapshot field fails loudly in every
registry that does not register it:

* ``snapshot_codec.snapshot_to_dict`` — the serialized profile shape;
* ``restore_point_service`` coverage tables (``_SCALAR_FIELDS`` +
  ``_COLLECTION_FIELDS`` + ``_SENSITIVITY_8POINT_RIDERS``) — capture/restore;
* ``import_classifier.KNOWN_SNAPSHOT_KEYS`` — the classifier's risk classification;
* ``safe_import_model.SNAPSHOT_FIELD_CATEGORY`` — Safe-Import preview/filter UI.

The parity gates above are KEY presence only. Category VALUES converged on a
single rule (axis_inversion = FEEL everywhere, vibration = COSMETIC everywhere)
and are pinned by ``CategoryValueAgreementTests`` — the
only intentional residual disagreements are the classifier's trigger ``.mode`` split
(LAYOUT half of a FEEL frame) and the restore registry's apply-side
``motion_settings = UNSUPPORTED`` (no write path; the import maps carry its
FEEL risk bucket instead).
"""

from __future__ import annotations

import dataclasses
import json
import unittest
from pathlib import Path

from zd_app.services import import_classifier, restore_point_service
from zd_app.services.settings_service import ControllerSnapshot
from zd_app.storage.restore_point_models import CoverageCategory
from zd_app.storage.snapshot_codec import snapshot_to_dict
from zd_app.ui import safe_import_model

# Source of truth: the dataclass's own field names.
SNAPSHOT_FIELDS: frozenset[str] = frozenset(
    f.name for f in dataclasses.fields(ControllerSnapshot)
)

# --- Intentional exceptions (named so the gate documents the model) ---------

# The 8-point curves are riders on their 3-point host fields in the
# restore-point coverage tables — deliberately NOT standalone scalar rows, so
# legacy (non-cat-0x86) controllers see no new "not captured" noise and
# ``total_supported_count`` stays stable per device (rationale comment at
# ``restore_point_service._SENSITIVITY_8POINT_RIDERS``). The rider table
# therefore counts as coverage in the restore parity check below, and
# ``test_8point_rider_exception_is_exact`` keeps this exception honest.
SENSITIVITY_8POINT_FIELDS: frozenset[str] = frozenset(
    {"sensitivity_left_8point", "sensitivity_right_8point"}
)

# The classifier splits each trigger frame into ``.range`` (FEEL) + ``.mode`` (LAYOUT)
# because one frame carries both a feel value and a layout-ish mode; the UI map
# categorizes the unsplit frame FEEL. The ``.mode`` halves are therefore the
# only intentional classifier / UI-map category disagreement
# (``test_trigger_mode_exception_is_exact`` keeps this exception honest).
CLASSIFIER_TRIGGER_MODE_KEYS: frozenset[str] = frozenset(
    {"trigger_left.mode", "trigger_right.mode"}
)


def _empty_snapshot() -> ControllerSnapshot:
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


def _collapse(preview_key: str) -> str:
    """Collapse a split preview key to its snapshot field (``trigger_left.range``
    -> ``trigger_left``), mirroring ``import_classifier._SNAPSHOT_KEY_ORDER``."""

    return preview_key.split(".", 1)[0]


def _restore_tables() -> tuple[set[str], set[str], set[str]]:
    scalars = {name for name, _category, _writable in restore_point_service._SCALAR_FIELDS}
    collections = {
        name for name, _category, _writable, _size in restore_point_service._COLLECTION_FIELDS
    }
    riders = {rider for rider, _host in restore_point_service._SENSITIVITY_8POINT_RIDERS}
    return scalars, collections, riders


class FieldRegistryDriftTests(unittest.TestCase):
    maxDiff = None

    def _assert_parity(self, registry_name: str, covered: set[str]) -> None:
        problems = []
        missing = sorted(SNAPSHOT_FIELDS - covered)
        extra = sorted(covered - SNAPSHOT_FIELDS)
        # Plain ASCII in the failure text: it prints through consoles whose
        # codepage mangles em-dashes.
        if missing:
            problems.append(
                f"{registry_name} is MISSING snapshot field(s): {missing} - register"
                " them (or add a documented exception set in this module)"
            )
        if extra:
            problems.append(
                f"{registry_name} carries key(s) that are not ControllerSnapshot"
                f" fields: {extra} - remove stale entries or rename to match the dataclass"
            )
        self.assertFalse(problems, "; ".join(problems))

    def test_codec_serializes_every_dataclass_field(self) -> None:
        # The two candidate sources of truth must agree before anything else:
        # dataclass field names == keys snapshot_to_dict emits.
        self._assert_parity(
            "snapshot_codec.snapshot_to_dict", set(snapshot_to_dict(_empty_snapshot()))
        )

    def test_restore_point_tables_cover_every_field(self) -> None:
        scalars, collections, riders = _restore_tables()
        self._assert_parity(
            "restore_point_service (_SCALAR_FIELDS + _COLLECTION_FIELDS +"
            " _SENSITIVITY_8POINT_RIDERS)",
            scalars | collections | riders,
        )

    def test_import_classifier_knows_every_field(self) -> None:
        self._assert_parity(
            "import_classifier.KNOWN_SNAPSHOT_KEYS",
            set(import_classifier.KNOWN_SNAPSHOT_KEYS),
        )
        # KNOWN_SNAPSHOT_KEYS is derived from _CATEGORY_MAP by collapsing the
        # split trigger keys; pin the derivation assumption explicitly.
        collapsed = {_collapse(key) for key in import_classifier._CATEGORY_MAP}
        self.assertEqual(
            collapsed,
            set(import_classifier.KNOWN_SNAPSHOT_KEYS),
            "import_classifier._CATEGORY_MAP no longer collapses to KNOWN_SNAPSHOT_KEYS",
        )

    def test_safe_import_ui_map_covers_every_field(self) -> None:
        # The UI map keeps the trigger frames unsplit today; collapse anyway so
        # the comparison stays valid if it ever adopts the classifier's split keys.
        collapsed = {_collapse(key) for key in safe_import_model.SNAPSHOT_FIELD_CATEGORY}
        self._assert_parity("safe_import_model.SNAPSHOT_FIELD_CATEGORY", collapsed)

    def test_8point_rider_exception_is_exact(self) -> None:
        """Keeps the named exception honest: the rider table covers exactly the
        declared 8-point fields, every rider host is itself a snapshot field,
        and no rider double-registers as a scalar coverage row."""

        scalars, _collections, riders = _restore_tables()
        hosts = {host for _rider, host in restore_point_service._SENSITIVITY_8POINT_RIDERS}
        self.assertEqual(
            riders,
            set(SENSITIVITY_8POINT_FIELDS),
            "restore_point_service._SENSITIVITY_8POINT_RIDERS does not cover exactly"
            " the declared SENSITIVITY_8POINT_FIELDS exception",
        )
        bad_hosts = sorted(hosts - SNAPSHOT_FIELDS)
        self.assertFalse(
            bad_hosts,
            f"_SENSITIVITY_8POINT_RIDERS host(s) are not snapshot fields: {bad_hosts}",
        )
        double = sorted(riders & scalars)
        self.assertFalse(
            double,
            f"8-point rider(s) double-registered as scalar coverage rows: {double}",
        )

    def test_restore_tables_are_disjoint(self) -> None:
        scalars, collections, riders = _restore_tables()
        overlap = sorted(
            (scalars & collections) | (scalars & riders) | (collections & riders)
        )
        self.assertFalse(
            overlap,
            "restore_point_service field(s) registered in more than one coverage"
            f" table: {overlap}",
        )

    def test_every_ui_field_label_exists_in_locales(self) -> None:
        """A key in the UI map without its locale label renders as a bracketed
        placeholder in the diff/result screens. en is the fallback locale and
        test_i18n pins en<->zh key parity, so checking en suffices."""

        en = json.loads(
            Path("zd_app/i18n/locales/en.json").read_text(encoding="utf-8")
        )
        missing = sorted(
            label_key
            for label_key in safe_import_model.FIELD_LABEL_KEYS.values()
            if label_key not in en
        )
        self.assertFalse(
            missing, f"en.json is missing safe_import field label(s): {missing}"
        )


class CategoryValueAgreementTests(unittest.TestCase):
    """Category VALUES agree across the registries (axis_inversion = FEEL
    everywhere, vibration = COSMETIC everywhere). Key-set parity is
    ``FieldRegistryDriftTests``' job; this class pins the assignments and
    keeps the named exceptions exact."""

    maxDiff = None

    def test_category_values_agree_classifier_vs_ui_map(self) -> None:
        """The classifier (services) and the Safe-Import UI map assign
        the SAME category to every key they share, after collapsing the classifier's
        ``trigger_*.range``/``.mode`` split onto the UI map's unsplit frames.

        By rule: axis_inversion = FEEL everywhere, vibration = COSMETIC
        everywhere. Both maps use the same ``RiskCategory``
        enum (re-exported), so identity comparison is exact.
        """

        disagreements = []
        for key, classifier_category in import_classifier._CATEGORY_MAP.items():
            if key in CLASSIFIER_TRIGGER_MODE_KEYS:
                continue  # the one intentional classifier refinement, pinned below
            ui_map_category = safe_import_model.SNAPSHOT_FIELD_CATEGORY[_collapse(key)]
            if classifier_category is not ui_map_category:
                disagreements.append(
                    f"{key}: classifier={classifier_category.name}"
                    f" ui-map={ui_map_category.name}"
                )
        self.assertFalse(
            disagreements,
            "classifier / Safe-Import-UI category assignment(s) diverged (policy"
            " 2026-06-10: axis_inversion=FEEL, vibration=COSMETIC): "
            + "; ".join(disagreements),
        )

    def test_trigger_mode_exception_is_exact(self) -> None:
        """Keeps the trigger-split exception honest: the ONLY split keys in
        the classifier are the two trigger frames, only their ``.mode`` halves diverge
        from the UI map (LAYOUT vs the frame's FEEL), and the ``.range`` halves
        agree as FEEL."""

        split_keys = {key for key in import_classifier._CATEGORY_MAP if "." in key}
        self.assertEqual(
            split_keys,
            {"trigger_left.range", "trigger_left.mode",
             "trigger_right.range", "trigger_right.mode"},
            "import_classifier._CATEGORY_MAP grew/lost split preview keys -"
            " update the trigger-split exception here deliberately",
        )
        for key in CLASSIFIER_TRIGGER_MODE_KEYS:
            self.assertIs(
                import_classifier._CATEGORY_MAP[key],
                import_classifier.RiskCategory.LAYOUT,
                f"{key} is the documented LAYOUT half of the trigger split",
            )

    def test_category_values_agree_restore_registry_vs_ui_map(self) -> None:
        """The restore-point coverage registry agrees with the Safe-Import map
        on every field's category, compared by enum ``.value`` because the
        vocabularies differ (``CoverageCategory`` has UNSUPPORTED/METADATA;
        ``RiskCategory`` has AUTOMATION/BLOCKED — the shared names carry
        identical values).

        ``motion_settings`` is the documented exception: the restore registry
        encodes the apply-side truth (UNSUPPORTED — no write path, and the
        Safe-Import diff says so on its preview row) while the import maps
        carry its FEEL risk bucket.
        """

        coverage_categories: dict[str, CoverageCategory] = {
            name: category
            for name, category, _writable in restore_point_service._SCALAR_FIELDS
        }
        coverage_categories.update(
            (name, category)
            for name, category, _writable, _size in restore_point_service._COLLECTION_FIELDS
        )

        motion = coverage_categories.pop("motion_settings")
        self.assertIs(
            motion,
            CoverageCategory.UNSUPPORTED,
            "motion_settings must stay UNSUPPORTED in the restore registry -"
            " it is the apply-side truth (no supported write path)",
        )

        disagreements = []
        for name, coverage_category in coverage_categories.items():
            ui_category = safe_import_model.SNAPSHOT_FIELD_CATEGORY[name]
            if coverage_category.value != ui_category.value:
                disagreements.append(
                    f"{name}: restore={coverage_category.value}"
                    f" ui-map={ui_category.value}"
                )
        self.assertFalse(
            disagreements,
            "restore registry / Safe-Import category value(s) diverged (policy"
            " 2026-06-10: axis_inversion=FEEL, vibration=COSMETIC): "
            + "; ".join(disagreements),
        )


if __name__ == "__main__":
    unittest.main()
