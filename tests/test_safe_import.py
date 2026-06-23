"""Tests for Safe Import: classifier mock, badges, preview flow."""

from __future__ import annotations

import dataclasses
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n
from zd_app.models import WrapperProfile
from zd_app.services.settings_apply_coordinator import ApplyFailure
from zd_app.ui import app_shell as app_shell_module
from zd_app.services.settings_service import (
    ButtonMapping,
    ButtonSlot,
    ControllerSnapshot,
    MotionMappingMode,
    MotionMappingTarget,
    MotionSettings,
    PollingRate,
    SensitivityAnchor,
    StickDeadzones,
    TriggerMode,
    TriggerSettings,
    TriggerVibrationMode,
    VibrationSettings,
)
from zd_app.ui import safe_import_badges
from zd_app.ui.safe_import_badges import BadgeKind
from zd_app.ui import safe_import_model as model
from zd_app.ui.safe_import_model import RiskCategory
from zd_app.ui.screens import controller, safe_import


def _snapshot(**overrides) -> ControllerSnapshot:
    payload = dict(
        polling_rate=PollingRate.HZ_8000,
        vibration=VibrationSettings(15, 15, 15, 15, TriggerVibrationMode.NATIVE),
        deadzones=StickDeadzones(5, 5, 95, 95),
        axis_inversion_left=None,
        axis_inversion_right=None,
        sensitivity_left=None,
        sensitivity_right=None,
        trigger_left=TriggerSettings(0, 100, TriggerMode.SHORT),
        trigger_right=None,
        # 01 00 TT controller-button remap (TT=0x10 -> B); the codec now
        # validates this shape at decode, so the fixture uses a real target.
        button_bindings={ButtonSlot.A: ButtonMapping(0x01, 0x00, 0x10)},
        lighting_zones={},
    )
    payload.update(overrides)
    return ControllerSnapshot(**payload)


def _export_dict(name="Apex Feel", **snap_overrides) -> dict:
    return WrapperProfile(name=name, snapshot=_snapshot(**snap_overrides)).to_dict()


# A valid monotonic 8-anchor curve (1.2.9 / fw-1.24 cat-0x86 shape). The codec
# range-checks x/y as percents; curve-shape semantics live in the apply path.
_ANCHORS_8 = tuple(
    SensitivityAnchor(x=x, y=y)
    for x, y in (
        (0, 5), (14, 20), (29, 35), (43, 50), (57, 65), (71, 80), (86, 90), (100, 100),
    )
)


class ClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_happy_path_buckets_categories(self) -> None:
        result = model.classify_import(_export_dict(), existing_names=set())
        self.assertTrue(result.ok)
        cats = {c.value: [fc.key for fc in v] for c, v in result.categories.items()}
        self.assertIn("polling_rate", cats["device"])
        self.assertIn("deadzones", cats["feel"])
        self.assertIn("vibration", cats["cosmetic"])
        self.assertIn("button_bindings", cats["layout"])

    def test_automation_fields_blocked_and_discarded(self) -> None:
        raw = _export_dict()
        raw["turbo_enabled"] = True
        raw["snapshot"]["macro_binding"] = {"keys": [1, 2, 3]}
        result = model.classify_import(raw, existing_names=set())

        self.assertTrue(result.ok)  # valid core profile, automation just blocked
        self.assertEqual(result.audit.blocked_automation_count, 2)
        self.assertIn("turbo_enabled", result.blocked_fields)
        self.assertIn("macro_binding", result.blocked_fields)
        # Discarded: the saved profile round-trips without automation payload.
        round_tripped = result.profile.to_dict()
        self.assertNotIn("turbo_enabled", round_tripped)
        self.assertNotIn("macro_binding", json.dumps(round_tripped))

    def test_automation_keys_never_reach_saved_json_or_write_payload(self) -> None:
        # End-to-end security invariant: even automation keys named to BYPASS the
        # classifier (``myturbo`` — 'turbo' is mid-token; ``weird.rapid`` — and a
        # ``turbo_enabled`` nested in a VALID button binding) are stripped by the
        # deny-by-default codec, so NONE of them appear in the saved profile JSON
        # and the persisted button mapping carries only the three allowlisted
        # keys. The codec — not the classifier — is the real boundary.
        raw = _export_dict()
        raw["myturbo"] = {"keys": [1, 2, 3]}
        raw["weird.rapid"] = True
        raw["snapshot"]["auto_fire_extra"] = [1, 2, 3]
        slot_key = next(iter(raw["snapshot"]["button_bindings"]))
        raw["snapshot"]["button_bindings"][slot_key]["turbo_enabled"] = True

        result = model.classify_import(raw, existing_names=set())
        self.assertTrue(result.ok)  # valid core profile still imports
        self.assertIsNotNone(result.profile)

        # classify_import -> filtered_snapshot -> WrapperProfile.to_dict: the
        # exact JSON that would be persisted for this import.
        selected = {
            RiskCategory.FEEL,
            RiskCategory.LAYOUT,
            RiskCategory.COSMETIC,
            RiskCategory.DEVICE,
        }
        snap = model.filtered_snapshot(result.profile.snapshot, selected)
        saved = WrapperProfile(name="x", snapshot=snap).to_dict()
        saved_json = json.dumps(saved)
        for forbidden in (
            "myturbo",
            "weird.rapid",
            "auto_fire_extra",
            "turbo_enabled",
            "turbo",
            "macro",
        ):
            self.assertNotIn(forbidden, saved_json)

        # The persisted button mappings carry only the allowlisted target bytes.
        for mapping in saved["snapshot"]["button_bindings"].values():
            self.assertEqual(set(mapping), {"target_kind", "target_low", "target_value"})

    def test_safety_sensitive_keys_blocked_not_counted_as_automation(self) -> None:
        raw = _export_dict()
        raw["script"] = "do_evil()"
        raw["snapshot"]["hid_raw"] = "deadbeef"
        result = model.classify_import(raw, existing_names=set())
        self.assertIn("script", result.blocked_fields)
        self.assertIn("hid_raw", result.blocked_fields)
        self.assertEqual(result.audit.blocked_automation_count, 0)

    def test_unknown_harmless_metadata_ignored_with_warning(self) -> None:
        raw = _export_dict()
        raw["author"] = "someone"
        raw["snapshot"]["notes"] = "hello"
        result = model.classify_import(raw, existing_names=set())
        self.assertIn("author", result.unknown_fields)
        self.assertIn("notes", result.unknown_fields)

    def test_hard_fail_non_object_root(self) -> None:
        result = model.classify_import([1, 2, 3], existing_names=set())
        self.assertFalse(result.ok)
        self.assertEqual(result.error_key, "safe_import.error.not_object")

    def test_hard_fail_bad_schema_version(self) -> None:
        raw = _export_dict()
        raw["schema_version"] = 999
        result = model.classify_import(raw, existing_names=set())
        self.assertFalse(result.ok)
        self.assertEqual(result.error_key, "safe_import.error.schema")

    def test_hard_fail_out_of_range_value(self) -> None:
        raw = _export_dict()
        raw["snapshot"]["deadzones"]["left_center"] = 250  # percent > 100
        result = model.classify_import(raw, existing_names=set())
        self.assertFalse(result.ok)
        self.assertEqual(result.error_key, "safe_import.error.invalid")

    def test_generated_name_uniquified_and_sanitized(self) -> None:
        raw = _export_dict(name="A\x00pex\x1f")
        result = model.classify_import(raw, existing_names={"Apex"})
        self.assertNotIn("\x00", result.generated_name)
        # slug collides with existing "Apex" -> disambiguated id
        self.assertTrue(result.audit.generated_profile_id.startswith("apex"))

    def test_filtered_snapshot_clears_unselected_device(self) -> None:
        result = model.classify_import(_export_dict(), existing_names=set())
        selected = {RiskCategory.FEEL, RiskCategory.LAYOUT, RiskCategory.COSMETIC}
        snap = model.filtered_snapshot(result.profile.snapshot, selected)
        self.assertIsNone(snap.polling_rate)  # device cleared
        self.assertIsNotNone(snap.deadzones)  # feel kept


class Sensitivity8PointImportTests(unittest.TestCase):
    """The 8-point curves must ride the full Safe Import surface.

    The keys were known to the classifier but missing
    from the UI's ``SNAPSHOT_FIELD_CATEGORY``, so imported curves produced no
    preview row (R1) and survived FEEL deselection in ``filtered_snapshot``
    (R2) — unreviewed curve data could reach the device.
    """

    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_imported_8point_curve_produces_feel_row(self) -> None:
        # R1: an import carrying 8 anchors gets a visible FEEL diff row.
        raw = _export_dict(sensitivity_left_8point=_ANCHORS_8)
        result = model.classify_import(raw, existing_names=set())

        self.assertTrue(result.ok)
        feel_rows = {fc.key: fc for fc in result.categories.get(RiskCategory.FEEL, [])}
        self.assertIn("sensitivity_left_8point", feel_rows)
        row = feel_rows["sensitivity_left_8point"]
        self.assertEqual(row.label_key, "safe_import.field.sensitivity_left_8point")
        self.assertEqual(len(row.imported_value), 8)
        self.assertNotIn("sensitivity_left_8point", result.unknown_fields)

    def test_filtered_snapshot_clears_8point_when_feel_deselected(self) -> None:
        # R2: deselecting FEEL clears the curves — they are neither saved nor
        # applied. This is the assertion that fails without the map fix.
        snap = _snapshot(
            sensitivity_left_8point=_ANCHORS_8,
            sensitivity_right_8point=_ANCHORS_8,
        )
        cleared = model.filtered_snapshot(snap, {RiskCategory.LAYOUT})
        self.assertIsNone(cleared.sensitivity_left_8point)
        self.assertIsNone(cleared.sensitivity_right_8point)

    def test_filtered_snapshot_preserves_8point_when_feel_selected(self) -> None:
        # R3: keeping FEEL selected keeps the curves intact.
        snap = _snapshot(
            sensitivity_left_8point=_ANCHORS_8,
            sensitivity_right_8point=_ANCHORS_8,
        )
        kept = model.filtered_snapshot(snap, {RiskCategory.FEEL})
        self.assertEqual(kept.sensitivity_left_8point, snap.sensitivity_left_8point)
        self.assertEqual(kept.sensitivity_right_8point, snap.sensitivity_right_8point)

    def test_summarize_8point_value_uses_point_count_form(self) -> None:
        serialized = [{"x": a.x, "y": a.y} for a in _ANCHORS_8]
        self.assertEqual(
            model.summarize_value("sensitivity_left_8point", serialized),
            i18n.t("safe_import.value.points", count=8),
        )


class PrepareImportGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n.set_locale("en")
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, text: str) -> str:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_missing_file_unreadable(self) -> None:
        result = model.prepare_import(str(self.dir / "nope.json"), existing_names=set())
        self.assertFalse(result.ok)
        self.assertEqual(result.error_key, "safe_import.error.unreadable")

    def test_invalid_json(self) -> None:
        path = self._write("bad.json", "{ not json")
        result = model.prepare_import(path, existing_names=set())
        self.assertFalse(result.ok)
        self.assertEqual(result.error_key, "safe_import.error.invalid_json")

    def test_too_deep_rejected_before_parse(self) -> None:
        deep = "[" * 70 + "]" * 70
        path = self._write("deep.json", deep)
        result = model.prepare_import(path, existing_names=set())
        self.assertFalse(result.ok)
        self.assertEqual(result.error_key, "safe_import.error.too_deep")

    def test_oversize_rejected(self) -> None:
        path = self._write("big.json", "0" * (model.MAX_IMPORT_BYTES + 1))
        result = model.prepare_import(path, existing_names=set())
        self.assertFalse(result.ok)
        self.assertEqual(result.error_key, "safe_import.error.too_large")

    def test_happy_path_sets_source_filename(self) -> None:
        path = self._write("apex.json", json.dumps(_export_dict()))
        result = model.prepare_import(path, existing_names=set())
        self.assertTrue(result.ok)
        self.assertEqual(result.audit.source_filename, "apex.json")


class BadgeLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n.set_locale("en")

    def test_profile_badge_plain_is_no_automation(self) -> None:
        # Plain profile (no Device overrides) shows only the No Automation badge.
        profile = WrapperProfile(name="x", snapshot=_snapshot(polling_rate=None))
        self.assertEqual(
            safe_import_badges.badges_for_profile(profile),
            [BadgeKind.NO_AUTOMATION],
        )

    def test_profile_badge_includes_device_settings_when_overrides_present(self) -> None:
        # Device settings: a profile carrying Device overrides also gets the Device badge.
        profile = WrapperProfile(name="x", snapshot=_snapshot())  # polling set
        self.assertEqual(
            safe_import_badges.badges_for_profile(profile),
            [BadgeKind.NO_AUTOMATION, BadgeKind.DEVICE_SETTINGS],
        )

    def test_profile_badge_step_size_alone_triggers_device_badge(self) -> None:
        profile = WrapperProfile(
            name="x", snapshot=_snapshot(polling_rate=None, step_size=80)
        )
        self.assertIn(
            BadgeKind.DEVICE_SETTINGS,
            safe_import_badges.badges_for_profile(profile),
        )

    def test_profile_badge_tolerates_profile_without_snapshot(self) -> None:
        # SimpleNamespace test stand-ins lack a ``snapshot`` attribute — the
        # render path must not raise.
        profile = SimpleNamespace(name="x")
        self.assertEqual(
            safe_import_badges.badges_for_profile(profile),
            [BadgeKind.NO_AUTOMATION],
        )

    def test_import_result_badges_full_set(self) -> None:
        raw = _export_dict()
        raw["turbo"] = True
        raw["author"] = "z"
        result = model.classify_import(raw, existing_names=set())
        kinds = safe_import_badges.badges_for_import_result(result)
        self.assertIn(BadgeKind.AUTOMATION_BLOCKED, kinds)
        self.assertIn(BadgeKind.NO_AUTOMATION, kinds)  # saved profile is clean
        self.assertIn(BadgeKind.DEVICE_SETTINGS, kinds)  # polling present
        self.assertIn(BadgeKind.NEEDS_REVIEW, kinds)  # unknown author ignored


# An untranslated i18n key renders as "[some.dotted.key]"; badge chips like
# "[ No Automation ]" (spaces, capitals) must not match.
_KEY_PLACEHOLDER = re.compile(r"\[[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\]")


class _DpgTestCase(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")
        dpg.create_context()

    def tearDown(self) -> None:
        dpg.destroy_context()

    def _full_ui(self, shell) -> None:
        shell._setup_theme()
        shell._build_ui()

    def _assert_no_placeholders(self, root) -> None:
        bad = [lbl for lbl in self._labels(root) if _KEY_PLACEHOLDER.search(lbl)]
        self.assertEqual(bad, [], f"Untranslated keys in {root}: {bad}")

    def _labels(self, root) -> list[str]:
        labels: list[str] = []
        stack = [root]
        while stack:
            item = stack.pop()
            label = dpg.get_item_label(item)
            value = dpg.get_value(item) if dpg.get_item_type(item) == "mvAppItemType::mvText" else None
            if label:
                labels.append(str(label))
            if value:
                labels.append(str(value))
            for slot in range(4):
                stack.extend(dpg.get_item_children(item, slot) or [])
        return labels

    def _scan(self, shell, raw) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "p.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            shell.open_safe_import()
            dpg.set_value(safe_import.PATH_INPUT, str(path))
            shell.safe_import_scan_path()


class PreviewRenderTests(_DpgTestCase):
    def test_file_select_modal_renders_without_placeholders(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.open_safe_import()
        self.assertTrue(dpg.does_item_exist(safe_import.FILE_MODAL))
        self._assert_no_placeholders(safe_import.FILE_MODAL)

    def test_preview_renders_summary_checklist_diff(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        self._scan(shell, _export_dict())
        self.assertTrue(dpg.does_item_exist(safe_import.PREVIEW_MODAL))
        self.assertTrue(dpg.does_item_exist("safe_import_summary_card"))
        self.assertTrue(dpg.does_item_exist("safe_import_diff_region"))
        self.assertTrue(dpg.does_item_exist("safe_import_raw_json"))
        self.assertTrue(dpg.does_item_exist("safe_import_save_new_button"))
        self._assert_no_placeholders(safe_import.PREVIEW_MODAL)

    def test_preview_renders_8point_row_without_placeholders(self) -> None:
        # End-to-end: an imported 8-point curve renders a labeled diff row
        # (i18n key resolves; no "[safe_import.field...]" placeholder).
        shell = make_shell(settings_service=MagicMock())
        self._scan(shell, _export_dict(sensitivity_left_8point=_ANCHORS_8))
        self.assertTrue(dpg.does_item_exist(safe_import.PREVIEW_MODAL))
        joined = "\n".join(self._labels(safe_import.PREVIEW_MODAL))
        self.assertIn(i18n.t("safe_import.field.sensitivity_left_8point"), joined)
        self._assert_no_placeholders(safe_import.PREVIEW_MODAL)

    def test_preview_motion_row_carries_never_applied_note(self) -> None:
        # Motion stays importable/preserved, but the wrapper has no motion
        # write path — the diff row must say so, instead of implying the
        # setting gets applied.
        shell = make_shell(settings_service=MagicMock())
        self._scan(
            shell,
            _export_dict(
                motion_settings=MotionSettings(
                    MotionMappingTarget.LEFT_JOYSTICK,
                    0x06,
                    MotionMappingMode.CONTINUOUS,
                    50,
                )
            ),
        )
        self.assertTrue(dpg.does_item_exist(safe_import.PREVIEW_MODAL))
        joined = "\n".join(self._labels(safe_import.PREVIEW_MODAL))
        self.assertIn(i18n.t("safe_import.diff.motion_never_applied"), joined)
        self._assert_no_placeholders(safe_import.PREVIEW_MODAL)

        # The note is per-row honesty, not boilerplate: absent motion, absent note.
        self._scan(shell, _export_dict())
        joined = "\n".join(self._labels(safe_import.PREVIEW_MODAL))
        self.assertNotIn(i18n.t("safe_import.diff.motion_never_applied"), joined)

    def test_device_category_unchecked_by_default(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        self._scan(shell, _export_dict())
        self.assertNotIn(RiskCategory.DEVICE, shell._safe_import_selected)
        self.assertIn(RiskCategory.FEEL, shell._safe_import_selected)
        self.assertFalse(dpg.get_value("safe_import_cat_device"))
        self.assertTrue(dpg.get_value("safe_import_cat_feel"))

    def test_select_all_checks_device_too(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        self._scan(shell, _export_dict())
        shell.safe_import_select_all()
        self.assertIn(RiskCategory.DEVICE, shell._safe_import_selected)
        self.assertTrue(dpg.get_value("safe_import_cat_device"))

    def test_save_as_new_is_default_and_does_not_write_controller(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        self._full_ui(shell)
        self._scan(shell, _export_dict())
        shell.safe_import_apply(apply_to_controller=False)

        shell.wrapper_profile_store.save.assert_called()
        self.assertEqual(shell._safe_import_result.audit.controller_write, "not_performed")
        self.assertTrue(dpg.does_item_exist(safe_import.RESULT_MODAL))
        self._assert_no_placeholders(safe_import.RESULT_MODAL)

    def test_save_and_apply_requires_confirm_then_writes(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = _snapshot()
        applied: dict = {}

        def fake_apply(snapshot):
            applied["snapshot"] = snapshot
            return SimpleNamespace(failed=[])

        shell._apply_snapshot_to_controller = MagicMock(side_effect=fake_apply)
        # "verified" is earned by read-back now — stub a read-back that echoes
        # exactly what was applied (the contract lives in ApplyVerifyTests).
        shell.restore_point_service.read_current_state_with_provenance = MagicMock(
            side_effect=lambda: (applied["snapshot"], {}, {})
        )
        self._full_ui(shell)
        self._scan(shell, _export_dict())

        shell.safe_import_request_apply_to_controller()
        self.assertTrue(dpg.does_item_exist(safe_import.CONFIRM_MODAL))

        with patch("zd_app.ui.app_shell.time.sleep"):
            shell.safe_import_apply(apply_to_controller=True)
        shell._apply_snapshot_to_controller.assert_called_once()
        self.assertEqual(shell._safe_import_result.audit.controller_write, "verified")
        # A restore point was saved before applying (current snapshot present).
        self.assertIsNotNone(shell._safe_import_result.audit.restore_point_name)

    def test_scan_empty_path_shows_error(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.open_safe_import()
        shell.safe_import_scan_path()
        self.assertTrue(dpg.get_item_configuration(safe_import.FILE_ERROR)["show"])


class ModalThreadDeferralTests(_DpgTestCase):
    """The modal-open hops ride the swap seam.

    DPG's benched modal law (thread-independent; matrix in
    tools/diag_dpg_modal_thread_visibility.py): a modal created in the
    same pass another modal was showing — deleted, hidden, or left up —
    never becomes visible, and two modals can never SHOW stacked. The
    Safe Import hops therefore run through AppShell._defer_modal_swap:
    teardown pass → rendered frame → create pass. These tests arm the
    seam the way run()'s render loop does and pin the per-hop
    choreography: nothing modal-shaped happens on the callback pass,
    teardown and create land on separate passes, the create runs exactly
    once, error paths stay synchronous, the preview is hidden (state
    intact) under the confirm and restored on cancel, and the
    already-render-threaded Save + Apply on_done path is NOT routed
    through the seam. The seam's own mechanics (FIFO, pass bounds,
    containment, unarmed inline mode the rest of this file relies on)
    live in test_app_shell_ui_deferral.py.
    """

    def _scan_armed(self, shell, raw) -> None:
        """The base ``_scan`` steps, without assuming a synchronous preview.

        ``prepare_import`` reads the file inside the scan callback itself,
        so the tempdir may be gone by the time a test drains the seam.
        """

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "p.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            shell.open_safe_import()
            dpg.set_value(safe_import.PATH_INPUT, str(path))
            shell.safe_import_scan_path()

    def _drain_swap(self, shell) -> None:
        """Both halves of a pending swap (production renders a frame between)."""

        shell._drain_deferred_ui_calls()
        shell._drain_deferred_ui_calls()

    def _preview_shown(self) -> bool:
        return bool(dpg.get_item_configuration(safe_import.PREVIEW_MODAL)["show"])

    def test_scan_swaps_preview_in_over_two_passes(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell._defer_ui_armed = True
        with patch.object(
            safe_import, "open_preview", wraps=safe_import.open_preview
        ) as spy:
            self._scan_armed(shell, _export_dict())

            # Callback pass: scan returned with the swap enqueued, untouched.
            spy.assert_not_called()
            self.assertFalse(dpg.does_item_exist(safe_import.PREVIEW_MODAL))
            self.assertTrue(dpg.does_item_exist(safe_import.FILE_MODAL))
            self.assertFalse(shell._deferred_ui_calls.empty())

            shell._drain_deferred_ui_calls()  # pass 1: teardown only

            spy.assert_not_called()
            self.assertFalse(dpg.does_item_exist(safe_import.FILE_MODAL))
            self.assertFalse(dpg.does_item_exist(safe_import.PREVIEW_MODAL))

            shell._drain_deferred_ui_calls()  # pass 2: create, post-frame

            spy.assert_called_once()
            self.assertTrue(dpg.does_item_exist(safe_import.PREVIEW_MODAL))
            self.assertTrue(shell._deferred_ui_calls.empty())

    def test_scan_error_paths_stay_synchronous_and_enqueue_nothing(self) -> None:
        # show_file_error mutates the OPEN file modal — no create, safe on
        # any thread — so the error paths keep today's synchronous feedback.
        shell = make_shell(settings_service=MagicMock())
        shell._defer_ui_armed = True

        shell.open_safe_import()
        shell.safe_import_scan_path()  # empty path
        self.assertTrue(dpg.get_item_configuration(safe_import.FILE_ERROR)["show"])
        self.assertTrue(shell._deferred_ui_calls.empty())

        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.json"
            bad.write_text("{ not json", encoding="utf-8")
            dpg.set_value(safe_import.PATH_INPUT, str(bad))
            shell.safe_import_scan_path()
        self.assertTrue(dpg.get_item_configuration(safe_import.FILE_ERROR)["show"])
        self.assertFalse(dpg.does_item_exist(safe_import.PREVIEW_MODAL))
        self.assertTrue(shell._deferred_ui_calls.empty())

    def test_plain_save_swaps_result_modal_in_over_two_passes(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        self._full_ui(shell)
        shell._defer_ui_armed = True
        self._scan_armed(shell, _export_dict())
        self._drain_swap(shell)  # land the preview first
        self.assertTrue(dpg.does_item_exist(safe_import.PREVIEW_MODAL))

        shell.safe_import_apply(apply_to_controller=False)

        # The disk save happened on the callback pass; the modal swap
        # (preview teardown, then open_result's pure create) waits for
        # the render-thread passes.
        shell.wrapper_profile_store.save.assert_called()
        self.assertEqual(
            shell._safe_import_result.audit.controller_write, "not_performed"
        )
        self.assertFalse(dpg.does_item_exist(safe_import.RESULT_MODAL))
        self.assertTrue(dpg.does_item_exist(safe_import.PREVIEW_MODAL))
        self.assertFalse(shell._deferred_ui_calls.empty())

        shell._drain_deferred_ui_calls()  # pass 1: preview torn down

        self.assertFalse(dpg.does_item_exist(safe_import.PREVIEW_MODAL))
        self.assertFalse(dpg.does_item_exist(safe_import.RESULT_MODAL))

        shell._drain_deferred_ui_calls()  # pass 2: result created

        self.assertTrue(dpg.does_item_exist(safe_import.RESULT_MODAL))
        self.assertTrue(shell._deferred_ui_calls.empty())
        self._assert_no_placeholders(safe_import.RESULT_MODAL)

    def test_request_apply_confirm_hides_preview_then_creates_confirm(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell._defer_ui_armed = True
        self._scan_armed(shell, _export_dict())
        self._drain_swap(shell)
        self.assertTrue(dpg.does_item_exist(safe_import.PREVIEW_MODAL))
        self.assertTrue(self._preview_shown())

        shell.safe_import_request_apply_to_controller()

        self.assertFalse(dpg.does_item_exist(safe_import.CONFIRM_MODAL))
        self.assertTrue(self._preview_shown())  # untouched on the callback pass

        shell._drain_deferred_ui_calls()  # pass 1: preview hidden, NOT deleted

        self.assertFalse(dpg.does_item_exist(safe_import.CONFIRM_MODAL))
        self.assertTrue(dpg.does_item_exist(safe_import.PREVIEW_MODAL))
        self.assertFalse(self._preview_shown())

        shell._drain_deferred_ui_calls()  # pass 2: confirm created

        self.assertTrue(dpg.does_item_exist(safe_import.CONFIRM_MODAL))
        self.assertTrue(dpg.does_item_exist(safe_import.PREVIEW_MODAL))
        self.assertFalse(self._preview_shown())  # never two showing modals

    def test_cancel_apply_confirm_restores_preview(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell._defer_ui_armed = True
        self._scan_armed(shell, _export_dict())
        self._drain_swap(shell)
        shell.safe_import_request_apply_to_controller()
        self._drain_swap(shell)
        self.assertTrue(dpg.does_item_exist(safe_import.CONFIRM_MODAL))

        shell.safe_import_cancel_apply_confirm()

        shell._drain_deferred_ui_calls()  # pass 1: confirm deleted
        self.assertFalse(dpg.does_item_exist(safe_import.CONFIRM_MODAL))
        self.assertFalse(self._preview_shown())  # re-show waits for the frame

        shell._drain_deferred_ui_calls()  # pass 2: preview re-shown
        self.assertTrue(self._preview_shown())
        self.assertTrue(shell._deferred_ui_calls.empty())

    def test_confirm_keeps_typed_profile_name_for_apply(self) -> None:
        # The preview is hidden (not deleted) under the confirm precisely so
        # the operator's typed profile name survives to the apply read.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = _snapshot()
        applied: dict = {}

        def fake_apply(snapshot):
            applied["snapshot"] = snapshot
            return SimpleNamespace(failed=[])

        shell._apply_snapshot_to_controller = MagicMock(side_effect=fake_apply)
        shell.restore_point_service.read_current_state_with_provenance = MagicMock(
            side_effect=lambda: (applied["snapshot"], {}, {})
        )
        self._full_ui(shell)
        shell._defer_ui_armed = True
        self._scan_armed(shell, _export_dict())
        self._drain_swap(shell)
        dpg.set_value(safe_import.NAME_INPUT, "Smoke Custom Name")
        shell.safe_import_request_apply_to_controller()
        self._drain_swap(shell)
        self.assertFalse(self._preview_shown())

        with patch("zd_app.ui.app_shell.time.sleep"):
            shell.safe_import_apply(apply_to_controller=True)

        saved_profile = shell.wrapper_profile_store.save.call_args.args[0]
        self.assertEqual(saved_profile.name, "Smoke Custom Name")

    def test_save_apply_on_done_bypasses_deferral_seam(self) -> None:
        # The jobbed flow's on_done already runs on the render thread (the
        # _tick completion drain in threaded mode; inline in sync mode),
        # frames after the job-start modal teardown — that path
        # must NOT pick up an extra deferral hop from this fix.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = _snapshot()
        applied: dict = {}

        def fake_apply(snapshot):
            applied["snapshot"] = snapshot
            return SimpleNamespace(failed=[])

        shell._apply_snapshot_to_controller = MagicMock(side_effect=fake_apply)
        shell.restore_point_service.read_current_state_with_provenance = MagicMock(
            side_effect=lambda: (applied["snapshot"], {}, {})
        )
        self._full_ui(shell)
        shell._defer_ui_armed = True
        self._scan_armed(shell, _export_dict())
        self._drain_swap(shell)
        shell.safe_import_request_apply_to_controller()
        self._drain_swap(shell)
        self.assertTrue(dpg.does_item_exist(safe_import.CONFIRM_MODAL))

        with patch("zd_app.ui.app_shell.time.sleep"):
            shell.safe_import_apply(apply_to_controller=True)

        # Sync mode runs job + on_done inline: the result modal is already
        # up and the deferred-UI queue was never touched.
        self.assertTrue(dpg.does_item_exist(safe_import.RESULT_MODAL))
        self.assertTrue(shell._deferred_ui_calls.empty())
        self.assertEqual(
            shell._safe_import_result.audit.controller_write, "verified"
        )

    def test_failed_save_apply_tears_down_stale_result_modal(self) -> None:
        # A5 (follow-up): a prior completed apply can leave RESULT_MODAL up. A
        # subsequent Save+Apply that FAILS shows the failure modal and (rightly)
        # never calls open_result() — the only deleter of RESULT_MODAL. Without
        # the job-start RESULT_MODAL teardown, the stale result modal stacks
        # under the failure modal (and per the modal law the failure modal then
        # never renders). Assert the stale modal is gone and only failure shows.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = _snapshot()
        applied: dict = {}

        def fake_apply(snapshot):
            applied["snapshot"] = snapshot
            return SimpleNamespace(
                failed=[ApplyFailure("polling", "boom", False)], succeeded=0
            )

        shell._apply_snapshot_to_controller = MagicMock(side_effect=fake_apply)
        shell.restore_point_service.read_current_state_with_provenance = MagicMock(
            side_effect=lambda: (applied.get("snapshot") or _snapshot(), {}, {})
        )
        self._full_ui(shell)
        shell._defer_ui_armed = True
        self._scan_armed(shell, _export_dict())
        self._drain_swap(shell)
        dpg.set_value(safe_import.NAME_INPUT, "Stale Modal Guard")
        shell.safe_import_request_apply_to_controller()
        self._drain_swap(shell)

        # Simulate a leftover result modal from a previous completed apply.
        with dpg.window(tag=safe_import.RESULT_MODAL, modal=True):
            dpg.add_text("stale result")
        self.assertTrue(dpg.does_item_exist(safe_import.RESULT_MODAL))

        with patch("zd_app.ui.app_shell.time.sleep"):
            shell.safe_import_apply(apply_to_controller=True)

        # Failure modal is up; the stale result modal was torn down at job start
        # and the failure path did not recreate it.
        self.assertTrue(dpg.does_item_exist("apply_failure_modal"))
        self.assertFalse(dpg.does_item_exist(safe_import.RESULT_MODAL))

    def test_file_select_modal_autosizes_to_content(self) -> None:
        # Hardware testing: the old fixed height=210 clipped the
        # Browse/Scan/Cancel row behind an internal scrollbar, and the
        # hidden error text grows the content further when shown.
        shell = make_shell(settings_service=MagicMock())
        shell.open_safe_import()
        config = dpg.get_item_configuration(safe_import.FILE_MODAL)
        self.assertTrue(config["autosize"])


class ApplyVerifyTests(_DpgTestCase):
    """Save + Apply must earn "verified" from a post-apply read-back.

    The audit used to claim
    ``controller_write="verified"`` from write ACKs alone, which this project
    has hardware-proven insufficient (the firmware in-burst rejection family —
    WriteFile says OK, device never commits). The contract now: ACKed writes
    alone are "sent"; "verified" exists only after a read-back comparison with
    no mismatched and no unverifiable applied fields.
    """

    def _apply_shell(self) -> tuple:
        """A scanned shell whose apply stub records the applied snapshot."""

        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = _snapshot()
        applied: dict = {}

        def fake_apply(snapshot):
            applied["snapshot"] = snapshot
            return SimpleNamespace(failed=[])

        shell._apply_snapshot_to_controller = MagicMock(side_effect=fake_apply)
        self._full_ui(shell)
        self._scan(shell, _export_dict())
        return shell, applied

    def test_matching_readback_yields_verified(self) -> None:
        shell, applied = self._apply_shell()
        shell.restore_point_service.read_current_state_with_provenance = MagicMock(
            side_effect=lambda: (applied["snapshot"], {}, {})
        )
        with patch("zd_app.ui.app_shell.time.sleep"):
            shell.safe_import_apply(apply_to_controller=True)

        audit = shell._safe_import_result.audit
        self.assertEqual(audit.controller_write, "verified")
        self.assertTrue(audit.verified)
        self.assertEqual(audit.verify_mismatched, [])
        self.assertEqual(audit.verify_unverifiable, [])
        self.assertFalse(audit.verify_read_failed)

    def test_mismatched_readback_downgrades_to_sent_and_names_field(self) -> None:
        shell, applied = self._apply_shell()

        def differing_readback():
            altered = dataclasses.replace(
                applied["snapshot"], deadzones=StickDeadzones(9, 9, 90, 90)
            )
            return altered, {}, {}

        shell.restore_point_service.read_current_state_with_provenance = MagicMock(
            side_effect=differing_readback
        )
        with patch("zd_app.ui.app_shell.time.sleep"):
            shell.safe_import_apply(apply_to_controller=True)

        audit = shell._safe_import_result.audit
        self.assertEqual(audit.controller_write, "sent")
        self.assertFalse(audit.verified)
        self.assertEqual(audit.verify_mismatched, ["deadzones"])
        self.assertEqual(audit.verify_unverifiable, [])
        # The result modal names the differing field instead of a bare "sent".
        self.assertTrue(dpg.does_item_exist(safe_import.RESULT_MODAL))
        joined = "\n".join(self._labels(safe_import.RESULT_MODAL))
        self.assertIn(
            i18n.t("safe_import.result.write_sent_mismatch", fields="deadzones"),
            joined,
        )
        self._assert_no_placeholders(safe_import.RESULT_MODAL)

    def test_readback_failure_downgrades_to_sent_flow_completes(self) -> None:
        shell, applied = self._apply_shell()
        shell.restore_point_service.read_current_state_with_provenance = MagicMock(
            side_effect=OSError("HID read timed out after 1000ms")
        )
        with patch("zd_app.ui.app_shell.time.sleep"):
            shell.safe_import_apply(apply_to_controller=True)

        audit = shell._safe_import_result.audit
        self.assertEqual(audit.controller_write, "sent")
        self.assertFalse(audit.verified)
        self.assertTrue(audit.verify_read_failed)
        self.assertEqual(audit.verify_mismatched, [])
        self.assertEqual(audit.verify_unverifiable, [])
        # Verify is best-effort: the flow completed — profile saved, modal up.
        shell.wrapper_profile_store.save.assert_called()
        self.assertTrue(dpg.does_item_exist(safe_import.RESULT_MODAL))
        joined = "\n".join(self._labels(safe_import.RESULT_MODAL))
        self.assertIn(i18n.t("safe_import.result.write_sent_verify_failed"), joined)
        self._assert_no_placeholders(safe_import.RESULT_MODAL)

    def test_failed_apply_keeps_legacy_sent_and_skips_readback(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = _snapshot()
        failure = ApplyFailure(
            setting_label="deadzones", error="write failed", is_transient=False
        )
        shell._apply_snapshot_to_controller = MagicMock(
            return_value=SimpleNamespace(
                failed=[failure], succeeded=0, total_attempted=1, retry_recoveries=0
            )
        )
        readback = MagicMock()
        shell.restore_point_service.read_current_state_with_provenance = readback
        self._full_ui(shell)
        self._scan(shell, _export_dict())

        with patch("zd_app.ui.app_shell.time.sleep") as sleep_mock:
            shell.safe_import_apply(apply_to_controller=True)

        audit = shell._safe_import_result.audit
        self.assertEqual(audit.controller_write, "sent")
        self.assertFalse(audit.verified)
        self.assertFalse(audit.verify_read_failed)
        self.assertEqual(audit.verify_mismatched, [])
        self.assertEqual(audit.verify_unverifiable, [])
        readback.assert_not_called()
        sleep_mock.assert_not_called()

    def test_failed_apply_shows_only_failure_modal_not_result_modal(self) -> None:
        # A5: on a partial/failed Save + Apply, the per-field failure modal
        # (with Retry) must be the single visible modal. Creating the "Saved as"
        # result modal in the same completion pass too stacks two modals, and
        # per the benched modal law (see _defer_modal_swap) the second never
        # renders — which would hide the failure breakdown the user needs. This
        # matches the regular profile-apply path, which shows only the failure
        # modal. The "Saved as <name>" outcome stays in the result banner.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = _snapshot()
        failure = ApplyFailure(
            setting_label="deadzones", error="write failed", is_transient=False
        )
        shell._apply_snapshot_to_controller = MagicMock(
            return_value=SimpleNamespace(
                failed=[failure], succeeded=0, total_attempted=1, retry_recoveries=0
            )
        )
        shell.restore_point_service.read_current_state_with_provenance = MagicMock()
        self._full_ui(shell)
        self._scan(shell, _export_dict())

        with patch("zd_app.ui.app_shell.time.sleep"):
            shell.safe_import_apply(apply_to_controller=True)

        # The failure modal (with Retry) is the single visible modal; the
        # "Saved as" result modal must NOT be created alongside it.
        self.assertTrue(dpg.does_item_exist("apply_failure_modal"))
        self.assertFalse(dpg.does_item_exist(safe_import.RESULT_MODAL))
        joined = "\n".join(self._labels("apply_failure_modal"))
        self.assertIn(i18n.t("apply.retry_failed_button"), joined)

    def test_settle_sleep_precedes_readback(self) -> None:
        # The verify read must give the firmware its post-burst quiet interval
        # (POST_APPLY_READ_SETTLE_S) BEFORE the feature-report read batch.
        shell, applied = self._apply_shell()
        order: list = []

        def record_read():
            order.append("read")
            return applied["snapshot"], {}, {}

        shell.restore_point_service.read_current_state_with_provenance = MagicMock(
            side_effect=record_read
        )
        with patch(
            "zd_app.ui.app_shell.time.sleep",
            side_effect=lambda s: order.append(("sleep", s)),
        ):
            shell.safe_import_apply(apply_to_controller=True)

        settle = ("sleep", app_shell_module.POST_APPLY_READ_SETTLE_S)
        self.assertIn(settle, order)
        self.assertLess(order.index(settle), order.index("read"))

    def test_fallback_readback_via_settings_service(self) -> None:
        # No RestorePointService wired -> the verify read falls back to
        # settings_service.get_all_settings() and can still earn "verified".
        shell, applied = self._apply_shell()
        shell.restore_point_service = None
        shell.settings_service.get_all_settings = MagicMock(
            side_effect=lambda: applied["snapshot"]
        )
        with patch("zd_app.ui.app_shell.time.sleep"):
            shell.safe_import_apply(apply_to_controller=True)

        audit = shell._safe_import_result.audit
        self.assertEqual(audit.controller_write, "verified")
        self.assertTrue(audit.verified)
        shell.settings_service.get_all_settings.assert_called_once()


class ProfilesTabTests(_DpgTestCase):
    def test_import_button_opens_safe_import(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.open_safe_import = MagicMock()
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass
            controller.build(shell, "content_region")
        self.assertTrue(dpg.does_item_exist("controller_profiles_import_button"))
        dpg.get_item_callback("controller_profiles_import_button")()
        shell.open_safe_import.assert_called_once()

    def test_profile_rows_render_no_automation_badge(self) -> None:
        profile = SimpleNamespace(name="Apex", last_modified_at="2026-05-05T00:00:00Z")
        shell = make_shell(settings_service=MagicMock(), profiles=[profile])
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass
            controller.build(shell, "content_region")
        self.assertTrue(dpg.does_item_exist("profile_row_0_badge_no_automation"))


class CopyDisciplineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.locale_dir = Path("zd_app/i18n/locales")
        self.en = json.loads((self.locale_dir / "en.json").read_text(encoding="utf-8"))
        self.zh = json.loads((self.locale_dir / "zh-CN.json").read_text(encoding="utf-8"))

    def test_safe_import_and_badge_namespaces_have_parity(self) -> None:
        for prefix in ("safe_import.", "badge."):
            en_keys = {k for k in self.en if k.startswith(prefix)}
            zh_keys = {k for k in self.zh if k.startswith(prefix)}
            self.assertEqual(en_keys, zh_keys)
            self.assertGreater(len(en_keys), 0)

    def test_no_banned_tournament_or_bansafe_claims(self) -> None:
        # Per the no-claims policy: no positive tournament/ban/anti-cheat *claims*. The
        # approved disclaimer "...not a tournament or anti-cheat certification"
        # is a negation and is allowed, so we match specific banned phrases.
        banned = (
            "tournament-safe",
            "tournament safe",
            "tournament-approved",
            "tournament approved",
            "tournament-certified",
            "ban-safe",
            "ban safe",
            "anti-cheat safe",
            "ban-proof",
            "competitive-approved",
            "guaranteed safe",
            "cannot get you banned",
        )
        for data in (self.en, self.zh):
            for key, value in data.items():
                if not (key.startswith("safe_import.") or key.startswith("badge.")):
                    continue
                lowered = value.lower()
                for phrase in banned:
                    self.assertNotIn(phrase, lowered, f"{key}: {value!r}")

    def test_trust_statement_present_in_both_locales(self) -> None:
        self.assertIn("safe_import.trust_statement", self.en)
        self.assertIn("safe_import.trust_statement", self.zh)
        self.assertNotEqual(self.en["safe_import.trust_statement"], self.zh["safe_import.trust_statement"])


class AboutTrustStatementTests(_DpgTestCase):
    def test_about_renders_security_trust_statement(self) -> None:
        from zd_app.ui.screens import about

        shell = make_shell(settings_service=MagicMock())
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass
            about.build(shell, "content_region")
        self.assertTrue(dpg.does_item_exist("about_security_trust_statement"))
        self.assertEqual(
            dpg.get_value("about_security_trust_statement"),
            i18n.t("safe_import.trust_statement"),
        )


class DeviceSettingsHelpersTests(unittest.TestCase):
    """Device settings: shared device-settings helpers + maps."""

    def test_device_setting_keys_match_snapshot_field_category(self) -> None:
        # The UI's field-category map must mark exactly the shared DEVICE keys
        # as DEVICE (Save As exclusion + per-card badge agree).
        device_in_map = {
            key
            for key, category in model.SNAPSHOT_FIELD_CATEGORY.items()
            if category is RiskCategory.DEVICE
        }
        self.assertEqual(device_in_map, set(model.DEVICE_SETTING_KEYS))

    def test_has_device_settings_true_when_polling_set(self) -> None:
        self.assertTrue(model.has_device_settings(_snapshot()))

    def test_has_device_settings_true_when_only_step_size_set(self) -> None:
        self.assertTrue(model.has_device_settings(_snapshot(polling_rate=None, step_size=80)))

    def test_has_device_settings_false_when_all_device_fields_none(self) -> None:
        self.assertFalse(
            model.has_device_settings(_snapshot(polling_rate=None, step_size=None))
        )

    def test_has_device_settings_false_for_none_snapshot(self) -> None:
        self.assertFalse(model.has_device_settings(None))

    def test_without_device_settings_clears_polling_and_step_size(self) -> None:
        original = _snapshot(step_size=120)
        cleared = model.without_device_settings(original)
        self.assertIsNone(cleared.polling_rate)
        self.assertIsNone(cleared.step_size)
        # Non-Device fields are preserved.
        self.assertEqual(cleared.deadzones, original.deadzones)
        self.assertEqual(cleared.button_bindings, original.button_bindings)

    def test_without_device_settings_returns_a_copy(self) -> None:
        original = _snapshot()
        _ = model.without_device_settings(original)
        # Original snapshot is not mutated.
        self.assertIsNotNone(original.polling_rate)

    def test_filtered_snapshot_clears_step_size_with_device(self) -> None:
        # Adding step_size → DEVICE means filtered_snapshot also nulls it when
        # DEVICE is excluded.
        original = _snapshot(step_size=120)
        keep = {RiskCategory.FEEL, RiskCategory.LAYOUT, RiskCategory.COSMETIC}
        cleared = model.filtered_snapshot(original, keep)
        self.assertIsNone(cleared.step_size)


if __name__ == "__main__":
    unittest.main()
