"""Forbidden-phrase guard for Restore Point data shape + user-facing copy.

The "Never say Factory Backup" rule mandates that this feature never use the
"factory backup / factory restore / firmware image / clone / etc." vocabulary
that misrepresents the wrapper's coverage. The scan covers:

1. Dataclass attribute names + JSON-exporter output keys (restore-point storage).
2. User-facing strings under any ``restore_points.*`` or ``nav.restore_points``
   i18n key, in both en and zh-CN locale files (restore-point UI integration).
3. Required presence of the verbatim claim-boundary paragraph + its short
   UI variant in the boundary module (restore-point UI integration).

The claim-boundary paragraphs themselves are deliberately whitelisted: they
use the never-words ("factory image", "firmware backup") in *denial* —
saying "Restore Points are NOT factory backups" is the load-bearing
honesty the feature ships. The same pattern the Health Report work follows for its own
claim boundary.
"""

from __future__ import annotations

import dataclasses
import json
import re
import unittest
from pathlib import Path
from typing import Any, Iterator

from zd_app.services.restore_points import (
    CLAIM_BOUNDARY_PARAGRAPH,
    CLAIM_BOUNDARY_SHORT_UI,
)
from zd_app.services.settings_service import (
    AxisInversion,
    ButtonMapping,
    ButtonSlot,
    ControllerButtonTarget,
    ControllerSnapshot,
    LightingMode,
    LightingSettings,
    LightingZone,
    PollingRate,
    RgbColor,
    SensitivityAnchor,
    StickDeadzones,
    TriggerMode,
    TriggerSettings,
    TriggerVibrationMode,
    VibrationSettings,
)
from zd_app.storage import restore_point_models
from zd_app.storage.restore_point_models import (
    CaptureSource,
    CoverageCategory,
    CoverageState,
    DeviceIdentity,
    FieldCoverage,
    IdentityConfidence,
    RestoreAttemptRecord,
    RestoreResultLabel,
    RestorePoint,
    RestorePointCoverage,
    RestorePointTrigger,
    SCHEMA_VERSION,
    KIND,
    restore_point_to_dict,
)


# Forbidden-token blocklist (the "Never say Factory Backup" rule). Tokens are
# lowercased; the scan is case-insensitive.
FORBIDDEN_TOKENS: tuple[str, ...] = (
    "factory_backup",
    "factory_restore",
    "factory_image",
    "full_backup",
    "complete_backup",
    "clone",
    "calibration_backup",
    "firmware_backup",
    "guaranteed_rollback",
    # Reinforcements: bare "factory" and "image" in a field/attr name almost
    # always implies one of the above; the spec forbids the broader
    # ``factory_*``, ``backup_*``, ``image_*``, ``clone_*`` families.
    "factory",
    "backup",
)


def _all_module_classes() -> list[type]:
    classes: list[type] = []
    for attr in dir(restore_point_models):
        value = getattr(restore_point_models, attr)
        if isinstance(value, type) and dataclasses.is_dataclass(value):
            classes.append(value)
    return classes


def _iter_attr_names(cls: type) -> Iterator[str]:
    for field in dataclasses.fields(cls):
        yield field.name


def _walk_keys(payload: Any) -> Iterator[str]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield str(key)
            yield from _walk_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_keys(item)


def _full_snapshot() -> ControllerSnapshot:
    return ControllerSnapshot(
        polling_rate=PollingRate.HZ_1000,
        vibration=VibrationSettings(10, 20, 30, 40, TriggerVibrationMode.NATIVE),
        deadzones=StickDeadzones(5, 6, 90, 91),
        axis_inversion_left=AxisInversion(False, False),
        axis_inversion_right=AxisInversion(False, False),
        sensitivity_left=(
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
        ),
        sensitivity_right=(
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
        ),
        trigger_left=TriggerSettings(0, 100, TriggerMode.LONG),
        trigger_right=TriggerSettings(0, 100, TriggerMode.LONG),
        button_bindings={
            ButtonSlot.A: ButtonMapping.controller_button(ControllerButtonTarget.B),
        },
        lighting_zones={
            LightingZone.HOME: LightingSettings(
                True, LightingMode.ALWAYS_ON, 200, RgbColor(10, 20, 30)
            ),
        },
        motion_settings=None,
        back_paddle_bindings={},
        step_size=128,
    )


def _sample_restore_point() -> RestorePoint:
    coverage = RestorePointCoverage(
        captured_supported_count=2,
        total_supported_count=13,
        capture_source=CaptureSource.FRESH_READ,
        fields={
            "polling_rate": FieldCoverage(
                state=CoverageState.CAPTURED,
                readable=True,
                writable=True,
                category=CoverageCategory.DEVICE,
            ),
            "lighting_zones": FieldCoverage(
                state=CoverageState.PARTIAL,
                readable=True,
                writable=True,
                category=CoverageCategory.COSMETIC,
                note="Only the home zone was captured during this read.",
            ),
        },
    )
    return RestorePoint(
        schema_version=SCHEMA_VERSION,
        kind=KIND,
        id="rp_20260524_190355_9b42de",
        created_at="2026-05-24T19:03:55Z",
        app_version="2.0.0",
        app_build_commit=None,
        title="Before Safe Import — 2026-05-24 19:03",
        trigger=RestorePointTrigger(
            type="before_safe_import_apply",
            source_label="Safe Import",
            reason="Created before applying imported profile to controller",
        ),
        device_identity=DeviceIdentity(
            vid="413D",
            pid="2104",
            product_string="ZD Ultimate Legend",
            firmware_version="1.18",
            identity_confidence=IdentityConfidence.READABLE,
        ),
        snapshot=_full_snapshot(),
        coverage=coverage,
        last_restore_attempt=RestoreAttemptRecord(
            attempted_at="2026-05-24T19:05:11Z",
            label=RestoreResultLabel.VERIFIED,
            attempted=3,
            wrote_succeeded=3,
            write_failed=0,
            verified_matched=3,
            could_not_verify=0,
            mismatched=0,
        ),
    )


class ForbiddenPhraseAttributeTests(unittest.TestCase):
    """No dataclass attribute (in storage/restore_point_models) may use a
    forbidden token."""

    def test_dataclass_attrs_have_no_forbidden_token(self) -> None:
        offenders: list[str] = []
        for cls in _all_module_classes():
            for attr in _iter_attr_names(cls):
                lowered = attr.lower()
                for token in FORBIDDEN_TOKENS:
                    if token in lowered:
                        offenders.append(f"{cls.__name__}.{attr} contains {token!r}")
        self.assertEqual(
            offenders,
            [],
            f"Forbidden tokens in dataclass attribute names: {offenders}",
        )


class ForbiddenPhraseJsonTests(unittest.TestCase):
    """No JSON exporter key may use a forbidden token."""

    def test_restore_point_json_keys_have_no_forbidden_token(self) -> None:
        payload = restore_point_to_dict(_sample_restore_point())
        offenders: list[str] = []
        for key in _walk_keys(payload):
            lowered = key.lower()
            for token in FORBIDDEN_TOKENS:
                if token in lowered:
                    offenders.append(f"JSON key {key!r} contains {token!r}")
        self.assertEqual(
            offenders,
            [],
            f"Forbidden tokens in JSON exporter output keys: {offenders}",
        )


class FieldCoverageVocabularyTests(unittest.TestCase):
    """The :class:`CoverageState` / :class:`CoverageCategory` /
    :class:`CaptureSource` enums must not use forbidden values, since they
    serialize directly into JSON.
    """

    def test_coverage_state_values_have_no_forbidden_token(self) -> None:
        for state in CoverageState:
            lowered = state.value.lower()
            for token in FORBIDDEN_TOKENS:
                self.assertNotIn(
                    token,
                    lowered,
                    f"CoverageState.{state.name} = {state.value!r} contains {token!r}",
                )

    def test_coverage_category_values_have_no_forbidden_token(self) -> None:
        for category in CoverageCategory:
            lowered = category.value.lower()
            for token in FORBIDDEN_TOKENS:
                self.assertNotIn(
                    token,
                    lowered,
                    f"CoverageCategory.{category.name} = {category.value!r} contains {token!r}",
                )

    def test_capture_source_values_have_no_forbidden_token(self) -> None:
        for source in CaptureSource:
            lowered = source.value.lower()
            for token in FORBIDDEN_TOKENS:
                self.assertNotIn(
                    token,
                    lowered,
                    f"CaptureSource.{source.name} = {source.value!r} contains {token!r}",
                )

    def test_restore_result_label_values_have_no_forbidden_token(self) -> None:
        for label in RestoreResultLabel:
            lowered = label.value.lower()
            for token in FORBIDDEN_TOKENS:
                self.assertNotIn(
                    token,
                    lowered,
                    f"RestoreResultLabel.{label.name} = {label.value!r} contains {token!r}",
                )


# ---------------------------------------------------------------------------
# RPU6 — user-facing copy hygiene + claim-boundary presence
# ---------------------------------------------------------------------------


class I18nUserFacingForbiddenPhrasesTests(unittest.TestCase):
    """No localized ``restore_points.*`` or ``nav.restore_points`` string
    may contain a forbidden token.

    The verbatim claim-boundary copy is whitelisted (per the test docstring)
    because it uses the never-words in denial. Both
    :data:`CLAIM_BOUNDARY_PARAGRAPH` and :data:`CLAIM_BOUNDARY_SHORT_UI`
    are stripped before the scan.
    """

    def _load_locale(self, name: str) -> dict[str, str]:
        path = Path("zd_app/i18n/locales") / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _restore_points_keys(self, data: dict[str, str]) -> dict[str, str]:
        return {
            key: value
            for key, value in data.items()
            if key.startswith("restore_points.") or key == "nav.restore_points"
        }

    # The empty_state + footer_caveat copy is intentionally a mini
    # claim-boundary statement that says "they are not factory backups" /
    # "并非出厂备份". The denial language is exactly what the design mandates;
    # whitelisting these specific keys lets the scan still catch any
    # ACCIDENTAL use of forbidden vocabulary elsewhere. A separate test
    # below (test_denial_keys_actually_contain_denial_language) enforces
    # that the whitelisted keys really do contain the denial.
    _DENIAL_COPY_KEYS = frozenset(
        {
            "restore_points.list.empty_state",
            "restore_points.list.footer_caveat",
        }
    )

    def _assert_no_forbidden_in_values(self, locale_name: str) -> None:
        data = self._load_locale(locale_name)
        rp_values = self._restore_points_keys(data)
        for key, value in rp_values.items():
            if key in self._DENIAL_COPY_KEYS:
                continue
            lower = value.lower()
            for token in FORBIDDEN_TOKENS:
                with self.subTest(locale=locale_name, key=key, token=token):
                    self.assertNotIn(
                        token,
                        lower,
                        msg=(
                            f"Locale {locale_name} key {key!r} value {value!r} "
                            f"contains forbidden token {token!r}"
                        ),
                    )

    def test_en_restore_points_strings_have_no_forbidden_tokens(self) -> None:
        self._assert_no_forbidden_in_values("en")

    def test_zh_cn_restore_points_strings_have_no_forbidden_tokens(self) -> None:
        self._assert_no_forbidden_in_values("zh-CN")

    def test_has_at_least_one_restore_points_key(self) -> None:
        # Sanity: filter above isn't silently matching nothing.
        data = self._load_locale("en")
        keys = self._restore_points_keys(data)
        self.assertGreater(len(keys), 30)

    def test_en_zh_have_identical_restore_points_keys(self) -> None:
        en = self._restore_points_keys(self._load_locale("en"))
        zh = self._restore_points_keys(self._load_locale("zh-CN"))
        self.assertEqual(set(en.keys()), set(zh.keys()))

    def test_denial_keys_actually_contain_denial_language(self) -> None:
        # Companion to the whitelist above: if the empty_state /
        # footer_caveat copy ever loses its denial language, we silently
        # would lose the load-bearing honesty disclaimer. This test
        # asserts the denial actually exists in both locales.
        for locale_name, denial_marker in (
            ("en", "not factory backups"),
            ("zh-CN", "并非出厂备份"),
        ):
            data = self._load_locale(locale_name)
            for key in self._DENIAL_COPY_KEYS:
                with self.subTest(locale=locale_name, key=key):
                    self.assertIn(
                        denial_marker,
                        data[key],
                        msg=(
                            f"Locale {locale_name} key {key!r} no longer "
                            f"contains denial marker {denial_marker!r}"
                        ),
                    )


class ClaimBoundaryRequiredPresenceTests(unittest.TestCase):
    """The verbatim claim-boundary paragraph + short variant must appear in
    the boundary module exports. The Restore Points screen + JSON exports
    re-render them from these constants, so as long as the constants are
    correct everything downstream stays honest.
    """

    def test_claim_boundary_paragraph_uses_never_words_in_denial(self) -> None:
        # Per the "Required claim-boundary statement" rule — the long paragraph
        # is the load-bearing denial. Confirm it actually contains the
        # disclaimer language the test whitelists it for.
        lowered = CLAIM_BOUNDARY_PARAGRAPH.lower()
        self.assertIn("factory image", lowered)
        self.assertIn("firmware", lowered)
        self.assertIn("not a factory", lowered)

    def test_short_ui_boundary_denies_factory_backup(self) -> None:
        # Short UI variant must also carry the "not a factory backup" denial.
        lowered = CLAIM_BOUNDARY_SHORT_UI.lower()
        self.assertIn("not factory backups", lowered)

    def test_paragraph_to_dict_round_trip_appears_in_exports(self) -> None:
        # Lock the boundary constants against accidental whitespace /
        # punctuation drift — any change here breaks the verbatim contract
        # downstream (UI detail view + JSON exports both render from these
        # constants). Schema v2 wires the paragraph through the JSON
        # exporter; the dedicated assertion lives in
        # :class:`V2JsonExportClaimBoundaryTests` below.
        self.assertTrue(CLAIM_BOUNDARY_PARAGRAPH.startswith("Restore Points save the controller settings"))
        self.assertTrue(CLAIM_BOUNDARY_PARAGRAPH.endswith("missing, failed, or mismatched fields."))
        self.assertTrue(CLAIM_BOUNDARY_SHORT_UI.startswith("Restore Points capture app-supported, app-readable settings."))
        self.assertTrue(CLAIM_BOUNDARY_SHORT_UI.endswith("could not be read, or did not match."))


class V2JsonExportClaimBoundaryTests(unittest.TestCase):
    """Schema v2 closes the restore-point UI-integration acceptance criterion #81 by
    embedding the verbatim claim-boundary paragraph in every JSON
    export. This test pins that the field is present and the value is
    actually the load-bearing denial paragraph (not an empty string, not
    truncated, not paraphrased).
    """

    def test_v2_json_export_includes_verbatim_claim_boundary(self) -> None:
        payload = restore_point_to_dict(_sample_restore_point())
        self.assertEqual(payload["schema_version"], 2)
        self.assertIn("claim_boundary_paragraph", payload)
        self.assertIn("claim_boundary_short", payload)
        self.assertEqual(payload["claim_boundary_paragraph"], CLAIM_BOUNDARY_PARAGRAPH)
        self.assertEqual(payload["claim_boundary_short"], CLAIM_BOUNDARY_SHORT_UI)
        # Spot-check the load-bearing denial language is actually in the
        # exported paragraph — the whole point of the field is that JSON
        # consumers see the denial without cross-referencing the UI.
        paragraph_value = payload["claim_boundary_paragraph"]
        self.assertTrue(paragraph_value.startswith("Restore Points save the controller settings"))
        self.assertIn("not a factory image", paragraph_value)
        self.assertIn("firmware backup", paragraph_value)


if __name__ == "__main__":
    unittest.main()
