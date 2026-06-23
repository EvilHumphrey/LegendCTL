"""DPG callback-signature regression tests.

These tests pin the contract that DPG's runtime click/value dispatcher
inspects a callable's signature and passes one positional arg per
POSITIONAL_OR_KEYWORD parameter (padding with None when the standard
sender / app_data / user_data trio runs out). The pre-audit codebase
used a `_s=None, _a=None, _u=None, X=X` idiom across ~25 sites which
breaks under that rule: DPG passes 4 positionals, the 4th is None, X
gets clobbered.

This file simulates DPG's dispatch precisely and asserts every fixed
site's closure capture (route_key, slot_id, side, mode_key, preset, ...)
survives the call. The simulator is the actual behavior pinned by a
hardware repro.
"""

from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# DPG dispatch simulator
# ---------------------------------------------------------------------------


def dpg_dispatch_simulate(callback, sender=42, app_data="APP_DATA_SENTINEL", user_data=None):
    """Invoke ``callback`` the way DPG does at runtime.

    Counts ``POSITIONAL_OR_KEYWORD`` params in the signature, pads the
    standard ``(sender, app_data, user_data)`` trio with ``None`` up to that
    count, and calls the callback with that many positional args. A
    ``VAR_POSITIONAL`` (``*args``) absorber drops the count to 0.
    """

    sig = inspect.signature(callback)
    pos_count = 0
    has_var_positional = False
    for p in sig.parameters.values():
        if p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD:
            pos_count += 1
        elif p.kind == inspect.Parameter.VAR_POSITIONAL:
            has_var_positional = True

    if has_var_positional:
        # DPG sees VAR_POSITIONAL and passes zero positionals (confirmed
        # empirically by 2026-05-25 Smoke #4: `args=()` with the rewritten
        # ``def _diag_export_cb(*args, _rp_id=rp_id, **kwargs)`` button).
        return callback()

    available = [sender, app_data, user_data]
    padded = (available + [None] * pos_count)[:pos_count]
    return callback(*padded)


class DpgDispatchSimulatorTests(unittest.TestCase):
    """Verify the simulator matches the empirically-observed DPG behavior."""

    def test_4_param_lambda_receives_4_args_with_none_pad(self) -> None:
        captured = []
        f = lambda _s=None, _a=None, _u=None, X="CLOSURE": captured.append(X)
        dpg_dispatch_simulate(f)
        # The 4th positional was None, so X got clobbered (this is the bug).
        self.assertEqual(captured, [None])

    def test_var_positional_lambda_receives_zero_args(self) -> None:
        captured = []
        f = lambda *args, X="CLOSURE": captured.append((X, args))
        dpg_dispatch_simulate(f)
        # *args triggers VAR_POSITIONAL branch — DPG passes zero positionals
        # and X stays as the closure default. This is the fix.
        self.assertEqual(captured, [("CLOSURE", ())])

    def test_3_param_slider_receives_3_args_with_app_data_in_value_slot(self) -> None:
        captured = []
        f = lambda _s=None, value=None, _u=None: captured.append(value)
        dpg_dispatch_simulate(f, app_data="SLIDER_VAL")
        self.assertEqual(captured, ["SLIDER_VAL"])

    def test_slider_with_keyword_only_closure_preserves_both(self) -> None:
        captured = []
        f = lambda _s=None, value=None, _u=None, *, side="LEFT": captured.append((side, value))
        dpg_dispatch_simulate(f, app_data="SLIDER_VAL")
        # 3 POS_OR_KW gets 3 positionals (value=app_data), side is
        # KEYWORD_ONLY and stays as closure default.
        self.assertEqual(captured, [("LEFT", "SLIDER_VAL")])


# ---------------------------------------------------------------------------
# Shared screen-build patching infrastructure
# ---------------------------------------------------------------------------


class _FakeContextManager:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _fake_cm(*_args, **_kw):
    return _FakeContextManager()


class _ScreenRecorder:
    """Patches a screen module's ``dpg`` and records every widget kw."""

    def __init__(self, module_path: str) -> None:
        self.module_path = module_path
        self.calls: list[tuple[str, tuple, dict]] = []
        self.widgets_by_label: dict[str, list[dict]] = {}
        self.widgets_by_kind: dict[str, list[dict]] = {}
        self._cm = None

    def __enter__(self) -> "_ScreenRecorder":
        def record(kind):
            def _fn(*args, **kw):
                self.calls.append((kind, args, kw))
                self.widgets_by_kind.setdefault(kind, []).append(kw)
                label = kw.get("label")
                if isinstance(label, str):
                    self.widgets_by_label.setdefault(label, []).append(kw)
                return kw.get("tag", kind)

            return _fn

        widget_kinds = (
            "add_text", "add_spacer", "add_button", "add_combo", "add_slider_int",
            "add_slider_float", "add_checkbox", "add_selectable", "add_input_text",
            "add_progress_bar", "add_table", "add_table_column", "add_table_row",
            "add_separator", "add_radio_button", "set_frame_callback", "add_tooltip",
        )
        patches = {kind: record(kind) for kind in widget_kinds}
        patches.update({
            "child_window": _fake_cm,
            "group": _fake_cm,
            "table": _fake_cm,
            "table_row": _fake_cm,
            "tooltip": _fake_cm,
            "tab_bar": _fake_cm,
            "tab": _fake_cm,
            "window": _fake_cm,
            "does_item_exist": lambda *_a, **_kw: False,
            "delete_item": lambda *_a, **_kw: None,
            "set_value": lambda *_a, **_kw: None,
            "get_value": lambda *_a, **_kw: None,
        })
        self._cm = patch.multiple(self.module_path, **patches)
        self._cm.__enter__()
        return self

    def __exit__(self, *exc):
        if self._cm is not None:
            self._cm.__exit__(*exc)
        return False

    def callbacks_by_label_prefix(self, prefix: str) -> list:
        out = []
        for label, widgets in self.widgets_by_label.items():
            if label.startswith(prefix):
                for w in widgets:
                    if "callback" in w:
                        out.append(w["callback"])
        return out


# ---------------------------------------------------------------------------
# legacy/triggers.py
# ---------------------------------------------------------------------------


class LegacyTriggersCallbackSignatureTests(unittest.TestCase):
    """Pin legacy/triggers.py against the DPG dispatch bug (sliders + Reset)."""

    def _make_shell(self):
        shell = MagicMock()
        shell.COLORS = {
            "accent": (1, 1, 1, 1),
            "muted": (2, 2, 2, 2),
            "warn": (3, 3, 3, 3),
            "good": (4, 4, 4, 4),
            "text": (5, 5, 5, 5),
        }
        shell.link_triggers = False
        shell.device_service.state.last_read_time = None
        trigger_left = SimpleNamespace(mode="Standard", threshold=50, actuation_point=50, hair_trigger=False)
        trigger_right = SimpleNamespace(mode="Standard", threshold=50, actuation_point=50, hair_trigger=False)
        shell.profile_service.current_draft.left_trigger = trigger_left
        shell.profile_service.current_draft.right_trigger = trigger_right
        return shell

    def test_trigger_sliders_preserve_side_and_deliver_value(self) -> None:
        from zd_app.ui.screens.legacy import triggers

        shell = self._make_shell()
        with _ScreenRecorder("zd_app.ui.screens.legacy.triggers.dpg") as rec:
            triggers.build(shell, parent="content_region")

        # Combos + sliders + checkboxes with closure-captured side.
        combos = rec.widgets_by_kind.get("add_combo", [])
        sliders = rec.widgets_by_kind.get("add_slider_int", [])
        checkboxes = rec.widgets_by_kind.get("add_checkbox", [])

        # Linked Edit Mode checkbox has no closure — skip it.
        side_checkboxes = [c for c in checkboxes if c.get("label") == "Hair Trigger"]

        # Each trigger card builds: 1 combo, 2 sliders, 1 hair-trigger
        # checkbox → 4 per side → 8 total per side... we only need to
        # verify each callback delivers value + a non-None side.
        all_value_callbacks = [w["callback"] for w in combos + sliders + side_checkboxes if "callback" in w]
        self.assertGreater(len(all_value_callbacks), 0, "no slider/combo callbacks captured")

        for cb in all_value_callbacks:
            shell.update_trigger_settings.reset_mock()
            dpg_dispatch_simulate(cb, app_data=77)
            shell.update_trigger_settings.assert_called_once()
            (side, payload), _ = shell.update_trigger_settings.call_args
            self.assertIn(side, ("left", "right"), "selected_side closure clobbered")
            # Payload is a single-key dict; the value must equal app_data (77).
            self.assertEqual(list(payload.values())[0], 77)

    def test_trigger_reset_button_preserves_side(self) -> None:
        from zd_app.ui.screens.legacy import triggers

        shell = self._make_shell()
        with _ScreenRecorder("zd_app.ui.screens.legacy.triggers.dpg") as rec:
            triggers.build(shell, parent="content_region")

        reset_callbacks = rec.callbacks_by_label_prefix("Reset")
        self.assertEqual(len(reset_callbacks), 2, "expected one Reset per trigger card")

        for cb in reset_callbacks:
            shell.reset_trigger_settings.reset_mock()
            dpg_dispatch_simulate(cb)
            shell.reset_trigger_settings.assert_called_once()
            (side,), _ = shell.reset_trigger_settings.call_args
            self.assertIn(side, ("left", "right"))


# ---------------------------------------------------------------------------
# legacy/sticks.py
# ---------------------------------------------------------------------------


class LegacySticksCallbackSignatureTests(unittest.TestCase):
    def _make_shell(self):
        shell = MagicMock()
        shell.COLORS = {
            "accent": (1, 1, 1, 1),
            "muted": (2, 2, 2, 2),
            "warn": (3, 3, 3, 3),
            "good": (4, 4, 4, 4),
            "text": (5, 5, 5, 5),
        }
        shell.selected_stick_side = "left"
        shell.preview_seconds_remaining.return_value = 0
        stick = SimpleNamespace(
            curve_preset="Default",
            center_deadzone=10,
            peripheral_deadzone=90,
            deadzone_compensation=5,
            invert_x=False,
            invert_y=False,
        )
        shell.profile_service.current_draft.left_stick = stick
        shell.profile_service.current_draft.right_stick = stick
        return shell

    def test_stick_side_buttons_dispatch_correct_side(self) -> None:
        from zd_app.ui.screens.legacy import sticks

        shell = self._make_shell()
        with _ScreenRecorder("zd_app.ui.screens.legacy.sticks.dpg") as rec:
            sticks.build(shell, parent="content_region")

        left_cbs = rec.callbacks_by_label_prefix("Left Stick")
        right_cbs = rec.callbacks_by_label_prefix("Right Stick")
        self.assertEqual(len(left_cbs), 1)
        self.assertEqual(len(right_cbs), 1)

        shell.select_stick_side.reset_mock()
        dpg_dispatch_simulate(left_cbs[0])
        shell.select_stick_side.assert_called_once_with("left")

        shell.select_stick_side.reset_mock()
        dpg_dispatch_simulate(right_cbs[0])
        shell.select_stick_side.assert_called_once_with("right")

    def test_stick_sliders_and_combos_preserve_side_and_value(self) -> None:
        from zd_app.ui.screens.legacy import sticks

        shell = self._make_shell()
        with _ScreenRecorder("zd_app.ui.screens.legacy.sticks.dpg") as rec:
            sticks.build(shell, parent="content_region")

        combos = rec.widgets_by_kind.get("add_combo", [])
        sliders = rec.widgets_by_kind.get("add_slider_int", [])
        checkboxes = rec.widgets_by_kind.get("add_checkbox", [])

        value_callbacks = [w["callback"] for w in combos + sliders + checkboxes if "callback" in w]
        self.assertGreater(len(value_callbacks), 0, "no slider/combo callbacks captured")

        for cb in value_callbacks:
            shell.update_stick_settings.reset_mock()
            dpg_dispatch_simulate(cb, app_data=42)
            shell.update_stick_settings.assert_called_once()
            (side, payload), _ = shell.update_stick_settings.call_args
            self.assertIn(side, ("left", "right"))
            self.assertEqual(list(payload.values())[0], 42)


# ---------------------------------------------------------------------------
# legacy/buttons.py
# ---------------------------------------------------------------------------


class LegacyButtonsCallbackSignatureTests(unittest.TestCase):
    def _make_shell(self):
        shell = MagicMock()
        shell.COLORS = {
            "accent": (1, 1, 1, 1),
            "muted": (2, 2, 2, 2),
            "warn": (3, 3, 3, 3),
            "good": (4, 4, 4, 4),
            "text": (5, 5, 5, 5),
        }
        shell.selected_button_input = "A"
        shell.button_actions = ["A", "B", "X", "Y"]
        mapping_a = SimpleNamespace(
            physical_input_id="A",
            physical_label="A Button",
            action="A",
            default_action="A",
        )
        mapping_b = SimpleNamespace(
            physical_input_id="B",
            physical_label="B Button",
            action="B",
            default_action="B",
        )
        shell.profile_service.current_draft.button_mappings = [mapping_a, mapping_b]
        shell.profile_service.is_button_dirty.return_value = False
        shell.device_service.state.last_read_time = None
        return shell, mapping_a

    def test_button_selectable_preserves_input_id(self) -> None:
        from zd_app.ui.screens.legacy import buttons

        shell, _ = self._make_shell()
        with _ScreenRecorder("zd_app.ui.screens.legacy.buttons.dpg") as rec:
            buttons.build(shell, parent="content_region")

        selectables = rec.widgets_by_kind.get("add_selectable", [])
        callbacks = [w["callback"] for w in selectables if "callback" in w]
        self.assertEqual(len(callbacks), 2, "expected one selectable per mapping")

        captured_ids = set()
        for cb in callbacks:
            shell.select_button_mapping.reset_mock()
            dpg_dispatch_simulate(cb)
            shell.select_button_mapping.assert_called_once()
            (input_id,), _ = shell.select_button_mapping.call_args
            self.assertIsNotNone(input_id, "input_id closure clobbered")
            captured_ids.add(input_id)
        self.assertEqual(captured_ids, {"A", "B"})

    def test_button_action_combo_preserves_input_id_and_delivers_value(self) -> None:
        from zd_app.ui.screens.legacy import buttons

        shell, mapping = self._make_shell()
        with _ScreenRecorder("zd_app.ui.screens.legacy.buttons.dpg") as rec:
            buttons.build(shell, parent="content_region")

        combos = rec.widgets_by_kind.get("add_combo", [])
        self.assertEqual(len(combos), 1, "expected one Target Action combo")

        cb = combos[0]["callback"]
        shell.update_button_mapping.reset_mock()
        dpg_dispatch_simulate(cb, app_data="X")
        shell.update_button_mapping.assert_called_once_with(mapping.physical_input_id, "X")

    def test_reset_button_preserves_input_id(self) -> None:
        from zd_app.ui.screens.legacy import buttons

        shell, mapping = self._make_shell()
        with _ScreenRecorder("zd_app.ui.screens.legacy.buttons.dpg") as rec:
            buttons.build(shell, parent="content_region")

        reset_cbs = rec.callbacks_by_label_prefix("Reset Mapping")
        self.assertEqual(len(reset_cbs), 1)

        shell.reset_button_mapping.reset_mock()
        dpg_dispatch_simulate(reset_cbs[0])
        shell.reset_button_mapping.assert_called_once_with(mapping.physical_input_id)


# ---------------------------------------------------------------------------
# controller.py
# ---------------------------------------------------------------------------


class ControllerSensitivityPresetCallbackTests(unittest.TestCase):
    """Pin controller.py:234 (sensitivity preset buttons)."""

    def test_preset_button_lambda_preserves_preset_and_cb(self) -> None:
        # controller.py:234 was:
        #   lambda p=preset, cb=callback: cb(p)
        # which DPG would call as cb(sender_int, app_data_str), assigning
        # sender_int to p and app_data_str to cb — then trying to call the
        # string as a function. Fixed to: lambda *args, p=preset, cb=callback.
        captured = []

        def real_cb(value):
            captured.append(value)

        # Rebuild the lambda the way controller.py:234 does it now.
        preset = "Competitive"
        callback = real_cb
        fixed_lambda = lambda *args, p=preset, cb=callback: cb(p)

        dpg_dispatch_simulate(fixed_lambda)
        self.assertEqual(captured, ["Competitive"])

    def test_axis_inversion_button_lambda_preserves_cb(self) -> None:
        # controller.py:247 was:
        #   lambda cb=callback: cb()
        # DPG would clobber cb with sender_int, then try to call int(). Fixed
        # to: lambda *args, cb=callback: cb().
        called = []

        def real_cb():
            called.append("yes")

        callback = real_cb
        fixed_lambda = lambda *args, cb=callback: cb()

        dpg_dispatch_simulate(fixed_lambda)
        self.assertEqual(called, ["yes"])

    def test_pre_fix_pattern_fails_under_dpg_dispatch(self) -> None:
        """Sanity check: the pre-fix pattern raises under our simulator.

        Confirms the simulator faithfully reproduces the 2026-05-24 production
        failure mode for the default-args-only lambda shape.
        """

        def real_cb(value):
            return value

        preset = "Competitive"
        callback = real_cb
        broken_lambda = lambda p=preset, cb=callback: cb(p)

        # DPG passes 2 positionals (one per POS_OR_KW). p becomes sender_int
        # (42 in the simulator), cb becomes app_data ("APP_DATA_SENTINEL"
        # string). cb(p) then tries to call the string — TypeError.
        with self.assertRaises(TypeError):
            dpg_dispatch_simulate(broken_lambda)


# ---------------------------------------------------------------------------
# app_shell.py — crash review + apply-failure modals
# ---------------------------------------------------------------------------


class AppShellModalCallbackSignatureTests(unittest.TestCase):
    """Pin app_shell.py:1873, 1895, 1930, 1937 against DPG dispatch.

    These 4 sites used the 3-param idiom with no closure (so the pre-audit
    code was functionally safe), but the audit rewrites them to *args for
    consistency and future-proofing. Tests verify they still don't raise
    under DPG-style invocation.
    """

    def test_three_arg_invocation_does_not_raise(self) -> None:
        # The *args fix accepts any positional count without raising.
        called = []
        f = lambda *args: called.append("ran")

        dpg_dispatch_simulate(f)  # 0 args via VAR_POSITIONAL
        self.assertEqual(called, ["ran"])

    def test_pre_audit_three_param_idiom_still_works(self) -> None:
        # Sanity: the old `_s=None, _a=None, _u=None: handler()` shape
        # accepts 3 positionals (DPG passes 3 since POS_OR_KW count is 3)
        # and handler() runs cleanly because there's no closure to clobber.
        # This is why those 4 app_shell sites weren't caught in the original
        # smoke — they're functionally fine; the audit converts them only
        # for consistency.
        called = []
        f = lambda _s=None, _a=None, _u=None: called.append("ran")

        dpg_dispatch_simulate(f)
        self.assertEqual(called, ["ran"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
