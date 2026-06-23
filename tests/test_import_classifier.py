"""Tests for the Safe Import risk classifier."""

from __future__ import annotations

import json
import unittest

from zd_app.models import WrapperProfile
from zd_app.services import import_classifier
from zd_app.services.import_classifier import (
    DEVICE_SETTING_KEYS,
    KNOWN_SNAPSHOT_KEYS,
    ImportPolicy,
    RiskCategory,
    classify_import,
)
from zd_app.services.settings_service import (
    AxisInversion,
    BackPaddleBinding,
    ButtonMapping,
    ButtonSlot,
    ControllerButtonTarget,
    ControllerSnapshot,
    LightingMode,
    LightingSettings,
    LightingZone,
    MacroSlot,
    MotionMappingMode,
    MotionMappingTarget,
    MotionSettings,
    PollingRate,
    RgbColor,
    SensitivityAnchor,
    StickDeadzones,
    TriggerMode,
    TriggerSettings,
    TriggerVibrationMode,
    VibrationSettings,
)
from zd_app.storage.snapshot_codec import snapshot_to_dict
from zd_app.storage.wrapper_profile_store import slugify


def _snapshot_payload() -> dict:
    """A valid WrapperProfile payload with fields spanning several categories.

    ``axis_inversion_right`` / ``sensitivity_right`` / ``trigger_right`` are left
    unset (None) to exercise the "skip unset fields" path.
    """

    snapshot = ControllerSnapshot(
        polling_rate=PollingRate.HZ_8000,
        vibration=VibrationSettings(20, 30, 40, 50, TriggerVibrationMode.NATIVE),
        deadzones=StickDeadzones(5, 6, 90, 91),
        axis_inversion_left=AxisInversion(True, False),
        axis_inversion_right=None,
        sensitivity_left=(
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 72),
            SensitivityAnchor(100, 100),
        ),
        sensitivity_right=None,
        trigger_left=TriggerSettings(3, 77, TriggerMode.SHORT),
        trigger_right=None,
        button_bindings={
            ButtonSlot.A: ButtonMapping.controller_button(ControllerButtonTarget.B),
        },
        lighting_zones={
            LightingZone.HOME: LightingSettings(True, LightingMode.ALWAYS_ON, 180, RgbColor(1, 2, 3)),
        },
        motion_settings=MotionSettings(
            MotionMappingTarget.LEFT_JOYSTICK, 0x06, MotionMappingMode.CONTINUOUS, 50
        ),
        back_paddle_bindings={MacroSlot.M1: BackPaddleBinding(ControllerButtonTarget.A)},
    )
    return WrapperProfile(
        name="Imported",
        created_at="2026-05-22T00:00:00+00:00",
        last_modified_at="2026-05-22T00:00:00+00:00",
        snapshot=snapshot,
    ).to_dict()


class HappyPathClassificationTests(unittest.TestCase):
    def test_valid_profile_ok_and_named(self) -> None:
        result = classify_import(_snapshot_payload(), existing_names=set())

        self.assertTrue(result.ok)
        self.assertIsInstance(result.profile, WrapperProfile)
        self.assertEqual(result.unknown_fields, [])
        self.assertEqual(result.blocked_fields, [])
        self.assertEqual(result.generated_name, "Imported")

    def test_categories_bucket_correctly(self) -> None:
        result = classify_import(_snapshot_payload(), existing_names=set())
        by_cat = {cat: {fc.key for fc in changes} for cat, changes in result.categories.items()}

        self.assertEqual(by_cat[RiskCategory.DEVICE], {"polling_rate"})
        self.assertIn("deadzones", by_cat[RiskCategory.FEEL])
        self.assertIn("sensitivity_left", by_cat[RiskCategory.FEEL])
        self.assertIn("trigger_left.range", by_cat[RiskCategory.FEEL])
        self.assertIn("motion_settings", by_cat[RiskCategory.FEEL])
        # Inversion is FEEL everywhere.
        self.assertIn("axis_inversion_left", by_cat[RiskCategory.FEEL])
        self.assertIn("trigger_left.mode", by_cat[RiskCategory.LAYOUT])
        self.assertIn("button_bindings", by_cat[RiskCategory.LAYOUT])
        self.assertIn("back_paddle_bindings", by_cat[RiskCategory.LAYOUT])
        self.assertIn("vibration", by_cat[RiskCategory.COSMETIC])
        self.assertIn("lighting_zones", by_cat[RiskCategory.COSMETIC])

    def test_unset_fields_not_emitted(self) -> None:
        result = classify_import(_snapshot_payload(), existing_names=set())
        all_keys = {fc.key for changes in result.categories.values() for fc in changes}

        self.assertNotIn("axis_inversion_right", all_keys)
        self.assertNotIn("sensitivity_right", all_keys)
        self.assertNotIn("trigger_right.range", all_keys)
        self.assertNotIn("trigger_right.mode", all_keys)

    def test_field_change_carries_label_and_codec_value(self) -> None:
        payload = _snapshot_payload()
        result = classify_import(payload, existing_names=set())
        feel = {fc.key: fc for fc in result.categories[RiskCategory.FEEL]}

        self.assertEqual(feel["deadzones"].imported_value, payload["snapshot"]["deadzones"])
        self.assertEqual(feel["trigger_left.range"].imported_value, {"range_min": 3, "range_max": 77})
        self.assertEqual(feel["deadzones"].label_key, "import.field.deadzones")


class NameGenerationTests(unittest.TestCase):
    def test_collision_generates_unique_name(self) -> None:
        result = classify_import(_snapshot_payload(), existing_names={"Imported"})

        self.assertNotEqual(slugify(result.generated_name), "imported")
        self.assertEqual(result.generated_name, "Imported (2)")

    def test_hostile_name_sanitized(self) -> None:
        payload = _snapshot_payload()
        payload["name"] = "X" * 5000 + "\x00\x1b"
        result = classify_import(payload, existing_names=set())

        self.assertLessEqual(len(result.generated_name), 64)
        self.assertNotIn("\x00", result.generated_name)


class UnknownKeyTests(unittest.TestCase):
    def test_foreign_top_level_and_snapshot_keys_reported(self) -> None:
        payload = _snapshot_payload()
        payload["vendor_extra"] = {"a": 1}
        payload["snapshot"]["experimental"] = 7
        result = classify_import(payload, existing_names=set())

        self.assertIn("vendor_extra", result.unknown_fields)
        self.assertIn("snapshot.experimental", result.unknown_fields)
        self.assertTrue(result.ok)
        self.assertEqual(result.blocked_fields, [])

    def test_unknown_key_does_not_break_validation(self) -> None:
        payload = _snapshot_payload()
        payload["snapshot"]["totally_unknown"] = {"x": 1}
        result = classify_import(payload, existing_names=set())

        self.assertIsInstance(result.profile, WrapperProfile)

    def test_safety_sensitive_top_level_keys_blocked(self) -> None:
        # Covers the always-blocked list (raw HID, script/command,
        # path/file, plugin, reserved, device/firmware override).
        for bad in (
            "hid_raw_report",
            "script",
            "exec_path",
            "shell_command",
            "dll_inject",
            "plugin_loader",
            "reserved",
            "firmware_override",
            "device_override",
            "feature_report",
        ):
            with self.subTest(bad=bad):
                payload = _snapshot_payload()
                payload[bad] = "rm -rf /"
                result = classify_import(payload, existing_names=set())

                self.assertIn(bad, result.blocked_fields)
                self.assertFalse(result.ok)
                blocked_keys = {fc.key for fc in result.categories[RiskCategory.BLOCKED]}
                self.assertIn(bad, blocked_keys)

    def test_benign_words_with_dangerous_substrings_not_blocked(self) -> None:
        # Token-boundary matching: these legit metadata keys merely contain a
        # dangerous substring and must NOT block (description ⊅ script, etc.).
        for benign in ("description_text", "account_id", "subscription", "notes", "author"):
            with self.subTest(benign=benign):
                payload = _snapshot_payload()
                payload[benign] = "value"
                result = classify_import(payload, existing_names=set())

                self.assertNotIn(benign, result.blocked_fields)
                self.assertTrue(result.ok)

    def test_safety_sensitive_snapshot_key_blocked(self) -> None:
        payload = _snapshot_payload()
        payload["snapshot"]["raw_hid_payload"] = "deadbeef"
        result = classify_import(payload, existing_names=set())

        self.assertIn("snapshot.raw_hid_payload", result.blocked_fields)
        self.assertIn("snapshot.raw_hid_payload", result.unknown_fields)
        self.assertFalse(result.ok)

    def test_fail_on_unknown_policy_blocks_benign(self) -> None:
        payload = _snapshot_payload()
        payload["author"] = "someone"
        result = classify_import(
            payload, existing_names=set(), policy=ImportPolicy(fail_on_unknown=True)
        )

        self.assertIn("author", result.blocked_fields)
        self.assertFalse(result.ok)


class AutomationKeyTests(unittest.TestCase):
    """Design decision 2026-05-22: automation keys block + discard like safety-sensitive."""

    def test_automation_foreign_keys_blocked_and_bucketed(self) -> None:
        for auto in ("turbo", "macro_sequence", "rapid_fire", "repeat_delay", "automation"):
            with self.subTest(auto=auto):
                payload = _snapshot_payload()
                payload["snapshot"][auto] = {"enabled": True}
                result = classify_import(payload, existing_names=set())

                qualified = f"snapshot.{auto}"
                self.assertIn(qualified, result.blocked_fields)
                self.assertFalse(result.ok)
                auto_keys = {fc.key for fc in result.categories[RiskCategory.AUTOMATION]}
                self.assertIn(qualified, auto_keys)

    def test_automation_blocks_by_default(self) -> None:
        payload = _snapshot_payload()
        payload["turbo"] = True
        result = classify_import(payload, existing_names=set())

        self.assertIn("turbo", result.blocked_fields)
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_automation_count, 1)

    def test_blocked_field_change_carries_no_payload(self) -> None:
        payload = _snapshot_payload()
        payload["macro_binding"] = "A->B->A->B"
        payload["raw_report"] = [1, 2, 3, 255]
        result = classify_import(payload, existing_names=set())

        blocked_changes = (
            result.categories[RiskCategory.AUTOMATION] + result.categories[RiskCategory.BLOCKED]
        )
        self.assertTrue(blocked_changes)
        self.assertTrue(all(change.imported_value is None for change in blocked_changes))

    def test_nested_turbo_enabled_blocked_and_discarded(self) -> None:
        payload = _snapshot_payload()
        slot_key = next(iter(payload["snapshot"]["button_bindings"]))
        payload["snapshot"]["button_bindings"][slot_key]["turbo_enabled"] = True
        result = classify_import(payload, existing_names=set())

        self.assertTrue(any("turbo_enabled" in name for name in result.blocked_fields))
        self.assertFalse(result.ok)
        self.assertGreaterEqual(result.blocked_automation_count, 1)
        # Discarded: the codec-validated profile must not carry the key.
        self.assertIsNotNone(result.profile)
        self.assertNotIn("turbo_enabled", json.dumps(result.profile.to_dict()))

    def test_macro_binding_blocked_and_discarded(self) -> None:
        payload = _snapshot_payload()
        payload["snapshot"]["macro_binding"] = "x"
        result = classify_import(payload, existing_names=set())

        self.assertIn("snapshot.macro_binding", result.blocked_fields)
        self.assertFalse(result.ok)
        self.assertNotIn("macro_binding", json.dumps(result.profile.to_dict()))

    def test_blocked_counts_split_automation_and_safety(self) -> None:
        payload = _snapshot_payload()
        payload["turbo"] = True          # automation
        payload["macro_binding"] = "x"   # automation
        payload["script"] = "y"          # safety-sensitive
        result = classify_import(payload, existing_names=set())

        self.assertEqual(result.blocked_automation_count, 2)
        self.assertEqual(result.blocked_safety_count, 1)
        self.assertEqual(len(result.blocked_fields), 3)
        self.assertFalse(result.ok)

    def test_camelcase_dangerous_keys_blocked(self) -> None:
        """B2: a dangerous token preceded by a lowercase letter (camelCase)
        previously slipped the scan because matching lowercased first, losing the
        word boundary. ``runShellCmd`` / ``fastTurbo`` / ``evilExec`` must block."""
        for bad in ("runShellCmd", "fastTurbo", "evilExec"):
            with self.subTest(bad=bad):
                payload = _snapshot_payload()
                payload[bad] = "x"
                result = classify_import(payload, existing_names=set())

                self.assertIn(bad, result.blocked_fields)
                self.assertFalse(result.ok)

    def test_camelcase_dangerous_key_nested_in_button_binding_blocked(self) -> None:
        """B2: a camelCase dangerous key smuggled inside a known field
        (``button_bindings.<slot>.runShellCmd``) is surfaced by the recursive
        danger scan — previously it was reported nowhere."""
        payload = _snapshot_payload()
        slot_key = next(iter(payload["snapshot"]["button_bindings"]))
        payload["snapshot"]["button_bindings"][slot_key]["runShellCmd"] = True
        result = classify_import(payload, existing_names=set())

        self.assertTrue(any("runShellCmd" in name for name in result.blocked_fields))
        self.assertFalse(result.ok)

    def test_snake_case_detection_still_works(self) -> None:
        """B2 guard: the camelCase broadening must not regress snake_case /
        leading-token detection (``raw_report``, ``turbo_enabled``) or the
        benign-substring exclusions (``description`` should not match)."""
        payload = _snapshot_payload()
        payload["raw_report"] = "x"        # blocked (safety)
        payload["turbo_enabled"] = True    # blocked (automation; "turbo" leads)
        payload["description"] = "ok"      # benign: contains "script" mid-word, must not block
        result = classify_import(payload, existing_names=set())

        self.assertIn("raw_report", result.blocked_fields)
        self.assertIn("turbo_enabled", result.blocked_fields)
        self.assertNotIn("description", result.blocked_fields)


class SeparatorVariantTokenizationTests(unittest.TestCase):
    """P2a: ``_matches`` tokenizes on separator + camelCase boundaries, so every
    spelling of an automation/blocked token collapses onto the canonical pattern
    (catching ``rapid-fire`` / ``rapid fire`` that the old substring scan missed)
    while benign words that merely contain a pattern mid-token never match."""

    # Spelled with hyphen / space / dot — the old ``str.find`` scan missed all of
    # these because the separator broke the glued ``rapidfire`` / ``rapid_fire``
    # substrings.
    AUTOMATION_VARIANTS = (
        "rapid-fire",
        "rapid fire",
        "rapid.fire",
        "auto-fire",
        "auto fire",
        "macro-sequence",
        "turbo mode",
    )
    BLOCKED_VARIANTS = ("auto-exec", "raw report", "shell-command")
    BENIGN_NON_MATCHES = ("subscription", "account", "description")

    def test_separator_variant_automation_keys_blocked_top_level(self) -> None:
        for auto in self.AUTOMATION_VARIANTS:
            with self.subTest(auto=auto):
                payload = _snapshot_payload()
                payload[auto] = True
                result = classify_import(payload, existing_names=set())

                self.assertIn(auto, result.blocked_fields)
                self.assertFalse(result.ok)
                self.assertGreaterEqual(result.blocked_automation_count, 1)
                auto_keys = {fc.key for fc in result.categories[RiskCategory.AUTOMATION]}
                self.assertIn(auto, auto_keys)

    def test_separator_variant_automation_keys_blocked_when_nested(self) -> None:
        # Same variants smuggled INSIDE a known field (a button binding dict):
        # exercises the recursive ``_scan_dangerous_keys`` walk, and the codec
        # still drops the foreign key so the profile validates but ok=False.
        for auto in self.AUTOMATION_VARIANTS:
            with self.subTest(auto=auto):
                payload = _snapshot_payload()
                slot_key = next(iter(payload["snapshot"]["button_bindings"]))
                payload["snapshot"]["button_bindings"][slot_key][auto] = True
                result = classify_import(payload, existing_names=set())

                self.assertTrue(
                    any(auto in name for name in result.blocked_fields),
                    msg=f"{auto!r} not in {result.blocked_fields!r}",
                )
                self.assertFalse(result.ok)
                self.assertGreaterEqual(result.blocked_automation_count, 1)
                # Discarded by the codec allowlist: never written back.
                self.assertIsNotNone(result.profile)
                self.assertNotIn(auto, json.dumps(result.profile.to_dict()))

    def test_separator_variant_blocked_list_keys_blocked(self) -> None:
        for bad in self.BLOCKED_VARIANTS:
            with self.subTest(bad=bad):
                payload = _snapshot_payload()
                payload[bad] = "x"
                result = classify_import(payload, existing_names=set())

                self.assertIn(bad, result.blocked_fields)
                self.assertFalse(result.ok)
                self.assertGreaterEqual(result.blocked_safety_count, 1)
                blocked_keys = {fc.key for fc in result.categories[RiskCategory.BLOCKED]}
                self.assertIn(bad, blocked_keys)

    def test_benign_words_do_not_match_any_pattern(self) -> None:
        for benign in self.BENIGN_NON_MATCHES:
            with self.subTest(benign=benign):
                payload = _snapshot_payload()
                payload[benign] = "value"
                result = classify_import(payload, existing_names=set())

                self.assertNotIn(benign, result.blocked_fields)
                self.assertTrue(result.ok)

    def test_matches_unit_separator_and_boundary_behavior(self) -> None:
        from zd_app.services.import_classifier import (
            _AUTOMATION_KEY_PATTERNS,
            _BLOCKED_KEY_PATTERNS,
            _matches,
        )

        for key in self.AUTOMATION_VARIANTS:
            with self.subTest(automation=key):
                self.assertTrue(_matches(key, _AUTOMATION_KEY_PATTERNS))
        for key in self.BLOCKED_VARIANTS:
            with self.subTest(blocked=key):
                self.assertTrue(_matches(key, _BLOCKED_KEY_PATTERNS))
        # Benign words match neither pattern set (no mid-token false positives).
        for key in self.BENIGN_NON_MATCHES:
            with self.subTest(benign=key):
                self.assertFalse(_matches(key, _AUTOMATION_KEY_PATTERNS))
                self.assertFalse(_matches(key, _BLOCKED_KEY_PATTERNS))
        # ``macros`` (plural) matches by token-prefix; ``account`` ⊅ ``count``.
        self.assertTrue(_matches("macros", _AUTOMATION_KEY_PATTERNS))
        self.assertFalse(_matches("account", _AUTOMATION_KEY_PATTERNS))


class HostileInputTests(unittest.TestCase):
    def test_non_dict_payload(self) -> None:
        result = classify_import([1, 2, 3], existing_names=set())

        self.assertFalse(result.ok)
        self.assertIsNone(result.profile)
        self.assertTrue(result.generated_name)

    def test_bad_schema_version(self) -> None:
        payload = _snapshot_payload()
        payload["schema_version"] = 2
        result = classify_import(payload, existing_names=set())

        self.assertIsNone(result.profile)
        self.assertFalse(result.ok)

    def test_out_of_range_value_invalidates_without_crash(self) -> None:
        payload = _snapshot_payload()
        payload["snapshot"]["deadzones"]["left_center"] = 9999
        result = classify_import(payload, existing_names=set())

        self.assertIsNone(result.profile)
        self.assertFalse(result.ok)
        self.assertEqual(result.categories[RiskCategory.FEEL], [])

    def test_bad_button_mapping_invalidates_profile(self) -> None:
        # An in-range but unsupported button target (0x05 is not a
        # ControllerButtonTarget) fails codec validation now, so the classifier
        # reports no profile / ok=False instead of a mapping that would only
        # fail later at apply.
        payload = _snapshot_payload()
        slot_key = next(iter(payload["snapshot"]["button_bindings"]))
        payload["snapshot"]["button_bindings"][slot_key]["target_value"] = 0x05
        result = classify_import(payload, existing_names=set())

        self.assertIsNone(result.profile)
        self.assertFalse(result.ok)

    def test_blocked_key_detected_even_when_profile_invalid(self) -> None:
        payload = _snapshot_payload()
        payload["schema_version"] = 7
        payload["evil_script"] = "x"
        result = classify_import(payload, existing_names=set())

        self.assertIsNone(result.profile)
        self.assertIn("evil_script", result.blocked_fields)
        self.assertFalse(result.ok)


class CategoryMapDriftTests(unittest.TestCase):
    def test_known_snapshot_keys_match_codec(self) -> None:
        """Guards the seam: if the codec gains a field (e.g. the flagged
        step_size gap is closed) this fails until the category map adds it."""

        reference = ControllerSnapshot(
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

        self.assertEqual(set(snapshot_to_dict(reference).keys()), set(KNOWN_SNAPSHOT_KEYS))

    def test_device_setting_keys_match_category_map(self) -> None:
        """Device settings: the shared ``DEVICE_SETTING_KEYS`` set must agree with the
        DEVICE entries of the classifier's category map (single source of truth)."""

        device_in_map = {
            key
            for key, category in import_classifier._CATEGORY_MAP.items()
            if category is RiskCategory.DEVICE
        }
        self.assertEqual(device_in_map, set(DEVICE_SETTING_KEYS))

    def test_step_size_classified_device(self) -> None:
        """Device settings: a profile carrying ``step_size`` reports it in the DEVICE bucket."""

        snapshot = ControllerSnapshot(
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
            step_size=80,
        )
        payload = WrapperProfile(
            name="StepOnly",
            created_at="2026-05-22T00:00:00+00:00",
            last_modified_at="2026-05-22T00:00:00+00:00",
            snapshot=snapshot,
        ).to_dict()

        result = classify_import(payload, existing_names=set())

        self.assertTrue(result.ok)
        device_keys = [change.key for change in result.categories[RiskCategory.DEVICE]]
        self.assertIn("step_size", device_keys)


class CompactAndHomoglyphDangerScanTests(unittest.TestCase):
    """P3: glued compounds, Unicode homoglyphs/fullwidth, and the ``count``
    prefix false-positive. All audit-only — the codec already drops these
    unknown keys before save/apply — but the danger scan must still count them
    so the Safe-Import block totals aren't understated, and ``count`` must stop
    tripping on ``country_code``."""

    def test_glued_compound_danger_keys_blocked(self) -> None:
        # Single-token compounds (no separator / camelCase hump) slip the
        # token-anchored scan; the compact-key boundary check catches them.
        for bad in ("runshellcmd", "evilexec"):
            with self.subTest(bad=bad):
                payload = _snapshot_payload()
                payload[bad] = "x"
                result = classify_import(payload, existing_names=set())

                self.assertIn(bad, result.blocked_fields)
                self.assertFalse(result.ok)
                self.assertGreater(result.blocked_safety_count, 0)
                # Discarded by the codec allowlist: never written back.
                self.assertIsNotNone(result.profile)
                self.assertNotIn(bad, json.dumps(result.profile.to_dict()))

    def test_homoglyph_and_fullwidth_danger_keys_blocked(self) -> None:
        # NFKC folds fullwidth to ASCII (then the token scan catches it);
        # Cyrillic / Greek confusables don't fold and fail closed to BLOCKED.
        cases = {
            "macro_cyrillic": "mаcro",          # Cyrillic 'а' (U+0430)
            "turbo_greek": "turbοMode",         # Greek 'ο' (U+03BF)
            "rapidfire_fullwidth": "rapidＦire",  # fullwidth 'Ｆ' (U+FF26)
            "script_cyrillic": "scrіpt",        # Cyrillic 'і' (U+0456)
        }
        for label, bad in cases.items():
            with self.subTest(case=label):
                payload = _snapshot_payload()
                payload[bad] = "x"
                result = classify_import(payload, existing_names=set())

                self.assertIn(bad, result.blocked_fields)
                self.assertFalse(result.ok)
                self.assertGreater(
                    result.blocked_automation_count + result.blocked_safety_count,
                    0,
                )
                # Still dropped by the codec — never echoed into the profile.
                self.assertIsNotNone(result.profile)
                self.assertNotIn(bad, json.dumps(result.profile.to_dict()))

    def test_country_code_not_blocked(self) -> None:
        # ``count`` is now an exact-token match, so ``country_code``
        # (``country`` startswith ``count``) no longer false-positives.
        payload = _snapshot_payload()
        payload["country_code"] = "US"
        result = classify_import(payload, existing_names=set())

        self.assertNotIn("country_code", result.blocked_fields)
        self.assertIn("country_code", result.unknown_fields)
        self.assertTrue(result.ok)

    def test_count_token_still_blocks_when_whole_token(self) -> None:
        # Guard against over-narrowing: a literal ``count`` token still routes to
        # automation (``click_count`` has no other danger token).
        payload = _snapshot_payload()
        payload["snapshot"]["click_count"] = 5
        result = classify_import(payload, existing_names=set())

        self.assertIn("snapshot.click_count", result.blocked_fields)
        self.assertFalse(result.ok)

    def test_codec_still_drops_unknown_keys_invariant(self) -> None:
        # The deny-by-default codec must keep dropping every foreign key —
        # glued, homoglyph, or benign — so none round-trips into the profile.
        payload = _snapshot_payload()
        payload["runshellcmd"] = "x"
        payload["scrіpt"] = "y"
        payload["country_code"] = "US"
        payload["snapshot"]["vendor_blob"] = {"z": 1}
        result = classify_import(payload, existing_names=set())

        self.assertIsNotNone(result.profile)
        serialized = json.dumps(result.profile.to_dict())
        for foreign in ("runshellcmd", "scrіpt", "country_code", "vendor_blob"):
            self.assertNotIn(foreign, serialized)


if __name__ == "__main__":
    unittest.main()
