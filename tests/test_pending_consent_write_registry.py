"""Non-vacuous inventory of public UI roots that write to the controller."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n
from zd_app.services.settings_service import MacroSlot, StickDeadzones
import zd_app.ui.app_shell as app_shell_module
from zd_app.ui.app_shell import AppShell
from zd_app.ui.screens import restore_points


# Each positive sink twin proves the registered path still reaches a real write
# continuation; otherwise deleting/stubbing a root could make a refusal-only
# test pass vacuously.
_WRITE_ROOTS = {
    "_retry_failed_settings": ("self._apply_coordinator.retry_failures",),
    "apply_polling_rate": ("self._do_write_polling_rate",),
    "apply_step_size": ("self._do_write_step_size",),
    "apply_back_paddle_binding_from_combo": (
        "self.settings_service.set_back_paddle_binding",
    ),
    "apply_vibration_settings": ("self.settings_service.set_vibration",),
    "_apply_trigger_settings": (
        "self.settings_service.set_left_trigger_settings",
        "self.settings_service.set_right_trigger_settings",
    ),
    "apply_deadzone_settings": ("self.settings_service.set_all_deadzones",),
    "apply_diagnostics_deadzone": ("self._do_write_deadzones",),
    "_apply_sensitivity_preset": (
        "self._set_widget",
        "self.apply_left_sensitivity_curve",
        "self.apply_right_sensitivity_curve",
    ),
    "_apply_sensitivity_curve": (
        "self.settings_service.set_left_stick_sensitivity_curve",
        "self.settings_service.set_right_stick_sensitivity_curve",
    ),
    "_apply_sensitivity_preset_8point": (
        "self._set_sensitivity_8point_anchor_widgets",
        "self.apply_left_sensitivity_curve_8point",
        "self.apply_right_sensitivity_curve_8point",
    ),
    "_apply_sensitivity_curve_8point": (
        "self.settings_service.set_left_stick_sensitivity_curve_8point",
        "self.settings_service.set_right_stick_sensitivity_curve_8point",
    ),
    "_apply_axis_inversion": (
        "self.settings_service.set_left_stick_inversion",
        "self.settings_service.set_right_stick_inversion",
    ),
    "apply_button_binding": ("self.settings_service.set_button_binding",),
    "apply_lighting": ("self.settings_service.set_zone_lighting",),
    "safe_import_apply": ("self._run_hid_job",),
}

_MODELED_CONTROLLER_SINKS = frozenset(
    {
        "self._apply_coordinator.apply_snapshot",
        "self._apply_coordinator.retry_failures",
        "self._run_hid_job",
    }
)
_DIRECTLY_GUARDED_SINK_OWNERS = frozenset(
    {
        "_retry_failed_settings",
        "apply_back_paddle_binding_from_combo",
        "apply_vibration_settings",
        "_apply_trigger_settings",
        "apply_deadzone_settings",
        "_apply_sensitivity_curve",
        "_apply_sensitivity_curve_8point",
        "_apply_axis_inversion",
        "apply_button_binding",
        "apply_lighting",
        "safe_import_apply",
    }
)
_SEPARATELY_GUARDED_SINK_OWNERS = frozenset({"_apply_wrapper_profile_snapshot"})
_INTERNAL_GUARDED_SINK_CALLERS = {
    "_apply_snapshot_to_controller": {
        "_apply_wrapper_profile_snapshot",
        "safe_import_apply",
    },
    "_do_write_polling_rate": {"apply_polling_rate", "_flush_slider_throttle"},
    "_do_write_step_size": {"apply_step_size", "_flush_slider_throttle"},
    "_do_write_deadzones": {
        "apply_diagnostics_deadzone",
        "_flush_slider_throttle",
    },
}
_EXEMPT_READ_JOB_OWNERS = frozenset(
    {
        "refresh_from_controller",
        "_apply_wrapper_profile_resolved",
        "_schedule_deadzone_readback_verify",
    }
)

_BLOCKED_CALLBACKS = (
    ("apply_polling_rate", ("1000Hz",)),
    ("apply_step_size", (120,)),
    ("apply_back_paddle_binding_from_combo", (MacroSlot.M1,)),
    ("apply_vibration_settings", ()),
    ("apply_left_trigger_settings", ()),
    ("apply_right_trigger_settings", ()),
    ("apply_deadzone_settings", ()),
    ("apply_diagnostics_deadzone", (StickDeadzones(1, 2, 3, 4),)),
    ("apply_left_sensitivity_curve", ()),
    ("apply_right_sensitivity_curve", ()),
    ("apply_left_sensitivity_preset", ("Linear",)),
    ("apply_right_sensitivity_preset", ("Linear",)),
    ("apply_left_sensitivity_curve_8point", ()),
    ("apply_right_sensitivity_curve_8point", ()),
    ("apply_left_sensitivity_preset_8point", ("Linear",)),
    ("apply_right_sensitivity_preset_8point", ("Linear",)),
    ("apply_left_axis_inversion", ()),
    ("apply_right_axis_inversion", ()),
    ("apply_button_binding", ()),
    ("apply_lighting", ()),
)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _functions(path: Path, class_name: str | None = None) -> dict[str, ast.AST]:
    body = ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body
    if class_name:
        body = next(
            node.body
            for node in body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
    return {
        node.name: node
        for node in body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _calls(function: ast.AST) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            found.setdefault(_call_name(node.func), []).append(node.lineno)
    return found


def _controller_sinks(calls: dict[str, list[int]]) -> set[str]:
    return {
        name
        for name in calls
        if name.startswith("self.settings_service.set_")
        or name in _MODELED_CONTROLLER_SINKS
    }


class PendingConsentWriteRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.methods = _functions(
            Path(app_shell_module.__file__).resolve(), AppShell.__name__
        )

    def test_discovered_sink_owners_match_guarded_and_exempt_inventory(self) -> None:
        discovered = {
            method_name: sinks
            for method_name, method in self.methods.items()
            if (sinks := _controller_sinks(_calls(method)))
        }
        expected = (
            _DIRECTLY_GUARDED_SINK_OWNERS
            | _SEPARATELY_GUARDED_SINK_OWNERS
            | set(_INTERNAL_GUARDED_SINK_CALLERS)
            | _EXEMPT_READ_JOB_OWNERS
        )
        self.assertTrue(discovered)
        self.assertEqual(set(discovered), expected)

        for method_name in _DIRECTLY_GUARDED_SINK_OWNERS:
            calls = _calls(self.methods[method_name])
            guard = calls.get("self._hid_write_available_or_refuse", [])
            self.assertEqual(len(guard), 1, method_name)
            first_sink = min(
                line
                for sink in discovered[method_name]
                for line in calls[sink]
            )
            self.assertLess(guard[0], first_sink, method_name)

        for method_name in _SEPARATELY_GUARDED_SINK_OWNERS:
            calls = _calls(self.methods[method_name])
            for guard_name in (
                "self._zd_write_allowed_or_refuse",
                "self._consent_pending_write_allowed_or_refuse",
            ):
                guard = calls.get(guard_name, [])
                self.assertEqual(len(guard), 1, method_name)
                first_sink = min(
                    line
                    for sink in discovered[method_name]
                    for line in calls[sink]
                )
                self.assertLess(guard[0], first_sink, method_name)

        for sink_owner, expected_callers in _INTERNAL_GUARDED_SINK_CALLERS.items():
            call_name = f"self.{sink_owner}"
            actual_callers = {
                method_name
                for method_name, method in self.methods.items()
                if call_name in _calls(method)
            }
            self.assertEqual(actual_callers, expected_callers, sink_owner)

        for method_name in _EXEMPT_READ_JOB_OWNERS:
            self.assertEqual(
                discovered[method_name],
                {"self._run_hid_job"},
                method_name,
            )

    def test_every_registered_root_gates_before_positive_sink_twins(self) -> None:
        self.assertEqual(len(_WRITE_ROOTS), 16)
        for method_name, sinks in _WRITE_ROOTS.items():
            with self.subTest(root=method_name):
                calls = _calls(self.methods[method_name])
                guard_lines = calls.get("self._hid_write_available_or_refuse", [])
                self.assertEqual(len(guard_lines), 1)
                for sink in sinks:
                    self.assertIn(sink, calls)
                    self.assertLess(guard_lines[0], min(calls[sink]))

    def test_pending_consent_blocks_callbacks_before_widgets_or_sinks(self) -> None:
        for method_name, args in _BLOCKED_CALLBACKS:
            with self.subTest(root=method_name):
                service = MagicMock()
                shell = make_shell(settings_service=service)
                service.reset_mock()
                shell.device_service.record_apply_result.reset_mock()
                shell._consent_pending_verify = True
                shell._show_first_run_acknowledgment_modal_if_needed = MagicMock()
                shell._set_widget = MagicMock()
                shell._set_sensitivity_8point_anchor_widgets = MagicMock()

                with patch("zd_app.ui.app_shell.dpg.get_value") as get_value, patch(
                    "zd_app.ui.app_shell.dpg.set_value"
                ) as set_value, patch(
                    "zd_app.ui.app_shell.dpg.configure_item"
                ) as configure_item:
                    returned = getattr(shell, method_name)(*args)

                self.assertIsNone(returned)
                self.assertEqual(service.method_calls, [])
                get_value.assert_not_called()
                set_value.assert_not_called()
                configure_item.assert_not_called()
                shell._set_widget.assert_not_called()
                shell._set_sensitivity_8point_anchor_widgets.assert_not_called()
                shell._show_first_run_acknowledgment_modal_if_needed.assert_called_once_with()
                shell.device_service.record_apply_result.assert_called_once_with(
                    False, i18n.t("first_run.pending_write_blocked")
                )

    def test_write_helper_leaves_read_capable_gate_consent_free(self) -> None:
        write_calls = _calls(self.methods["_hid_write_available_or_refuse"])
        hid = write_calls.get("self._hid_available_or_refuse", [])
        consent = write_calls.get("self._consent_pending_write_allowed_or_refuse", [])
        self.assertEqual(len(hid), 1)
        self.assertEqual(len(consent), 1)
        self.assertLess(hid[0], consent[0])
        self.assertNotIn(
            "self._consent_pending_write_allowed_or_refuse",
            _calls(self.methods["_hid_available_or_refuse"]),
        )
        self.assertIn(
            "self._hid_available_or_refuse",
            _calls(self.methods["manual_save_restore_point"]),
        )

    def test_jobbed_profile_and_restore_guards_remain_separate(self) -> None:
        for method_name in ("apply_named_wrapper_profile", "_apply_wrapper_profile_snapshot"):
            calls = _calls(self.methods[method_name])
            self.assertIn("self._zd_write_allowed_or_refuse", calls)
            self.assertIn("self._consent_pending_write_allowed_or_refuse", calls)

        restore_path = Path(restore_points.__file__).resolve()
        restore_source = ast.get_source_segment(
            restore_path.read_text(encoding="utf-8"),
            _functions(restore_path)["_execute_restore"],
        )
        self.assertIn("_consent_pending_write_allowed_or_refuse", restore_source)
        self.assertIn("shell._run_hid_job", restore_source)


if __name__ == "__main__":
    unittest.main()
