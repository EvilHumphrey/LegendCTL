from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import alias_of, empty_snapshot, make_shell
from zd_app.i18n import t
from zd_app.services.settings_service import (
    ButtonMapping,
    ButtonSlot,
    ControllerButtonTarget,
    MacroSlot,
    MotionMappingMode,
    MotionMappingTarget,
    MotionSettings,
    SensitivityAnchor,
    SetButtonBindingOutcome,
)
from zd_app.ui import components, typography
from zd_app.ui.screens import controller


class ControllerScreenTests(unittest.TestCase):
    def test_controller_renders_tab_bar_with_7_tabs(self) -> None:
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            for tag in ("tab_vibration", "tab_triggers", "tab_sticks", "tab_buttons", "tab_lighting", "tab_motion", "tab_profiles"):
                self.assertTrue(dpg.does_item_exist(tag), tag)
        finally:
            dpg.destroy_context()

    def test_polling_rate_combo_defaults_to_safe_rate_not_8000(self) -> None:
        # Pre-release hardening: the polling-rate combo must NOT default to
        # "8000Hz" (which needs controller fw v1.18+). Before a real device read
        # hydrates it, it shows the safe, universally-supported fallback;
        # _hydrate_polling_rate replaces this with the device's actual rate once
        # a read lands.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = None  # never-read state

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            value = dpg.get_value("usb_polling_rate_combo")
            self.assertEqual(value, controller.POLLING_RATE_DEFAULT_LABEL)
            self.assertNotEqual(value, "8000Hz")
            # The safe default must be a real, universally-supported rate.
            self.assertIn(
                controller.POLLING_RATE_DEFAULT_LABEL, controller.POLLING_RATE_ITEMS
            )
            self.assertEqual(controller.POLLING_RATE_DEFAULT_LABEL, "1000Hz")
        finally:
            dpg.destroy_context()

    def test_controller_titles_use_type_scale_helpers(self) -> None:
        # The screen title renders via screen_title (h1) and each
        # tab section heading via section_title (h2), instead of bare
        # accent-green dpg.add_text. Patch both helpers and assert usage.
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            with patch("zd_app.ui.screens.controller.screen_title") as mock_screen_title, patch(
                "zd_app.ui.screens.controller.section_title"
            ) as mock_section_title:
                controller.build(shell, "content_region")
        finally:
            dpg.destroy_context()

        screen_titles = [call.args[0] for call in mock_screen_title.call_args_list]
        section_titles = [call.args[0] for call in mock_section_title.call_args_list]
        self.assertIn(t("controller.title"), screen_titles)
        self.assertIn(t("controller.vibration.title"), section_titles)
        self.assertIn(t("controller.buttons.title"), section_titles)

    def test_controller_motion_tab_renders_read_only_values(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            motion_settings=MotionSettings(
                target=MotionMappingTarget.LEFT_JOYSTICK,
                trigger_key=7,
                mode=MotionMappingMode.CONTINUOUS,
                sensitivity=42,
            )
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            labels = _collect_labels("tab_motion")
            self.assertIn("Left Joystick", labels)
            self.assertIn("42", labels)
        finally:
            dpg.destroy_context()

    def test_controller_motion_tab_no_apply_button(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            motion_settings=MotionSettings(
                target=MotionMappingTarget.DISABLED,
                trigger_key=0,
                mode=MotionMappingMode.INSTANT,
                sensitivity=0,
            )
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            labels = _collect_labels("tab_motion")
            self.assertFalse(any("Apply" in label for label in labels))
        finally:
            dpg.destroy_context()

    def test_controller_profiles_tab_renders_table_when_profiles_exist(self) -> None:
        profile = SimpleNamespace(name="Apex", last_modified_at="2026-05-05T00:00:00Z")
        shell = make_shell(settings_service=MagicMock(), profiles=[profile])

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            self.assertTrue(dpg.does_item_exist("controller_profiles_table"))
        finally:
            dpg.destroy_context()

    def test_per_row_apply_button_passes_profile_name(self) -> None:
        profile = SimpleNamespace(name="Apex", last_modified_at="2026-05-05T00:00:00Z")
        shell = make_shell(settings_service=MagicMock(), profiles=[profile])
        shell.apply_named_wrapper_profile = MagicMock()

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            button = _first_button_under("controller_profiles_table", "Apply")
            callback = dpg.get_item_callback(button)
            callback("sender", "app_data", dpg.get_item_user_data(button))

            shell.apply_named_wrapper_profile.assert_called_once_with("Apex")
        finally:
            dpg.destroy_context()

    def test_per_row_delete_button_passes_profile_name(self) -> None:
        profile = SimpleNamespace(name="Apex", last_modified_at="2026-05-05T00:00:00Z")
        shell = make_shell(settings_service=MagicMock(), profiles=[profile])
        shell.confirm_delete_named_wrapper_profile = MagicMock()

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            button = _first_button_under("controller_profiles_table", "Delete")
            callback = dpg.get_item_callback(button)
            callback("sender", "app_data", dpg.get_item_user_data(button))

            shell.confirm_delete_named_wrapper_profile.assert_called_once_with("Apex")
        finally:
            dpg.destroy_context()

    def test_sticks_tab_renders_8point_editor_for_left_when_capable(self) -> None:
        # Snapshot presence on the LEFT side flips that side to the 8-point
        # (cat 0x86) editor: 8 anchor pairs with ``_8point`` tags, an 8-pt apply
        # button, and NO 3-point sliders for left. The RIGHT side has no 8-point
        # snapshot field, so it stays on the unchanged 3-point editor.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            for index in range(1, 9):
                self.assertTrue(
                    dpg.does_item_exist(f"sensitivity_left_a{index}x_slider_8point"), index
                )
                self.assertTrue(
                    dpg.does_item_exist(f"sensitivity_left_a{index}y_slider_8point"), index
                )
            # The 3-point left sliders must NOT exist (replace, don't supplement).
            for index in range(1, 4):
                self.assertFalse(
                    dpg.does_item_exist(f"sensitivity_left_a{index}x_slider"), index
                )
                self.assertFalse(
                    dpg.does_item_exist(f"sensitivity_left_a{index}y_slider"), index
                )
            # No 8-point sliders for the right side; its 3-point editor is intact.
            self.assertFalse(dpg.does_item_exist("sensitivity_right_a1x_slider_8point"))
            for index in range(1, 4):
                self.assertTrue(
                    dpg.does_item_exist(f"sensitivity_right_a{index}x_slider"), index
                )

            labels = _collect_labels("tab_sticks")
            self.assertIn("Apply Left Sensitivity (8-pt)", labels)
            # Right side keeps its 3-point apply button.
            self.assertIn("Apply Right Sensitivity", labels)
        finally:
            dpg.destroy_context()

    def test_sticks_tab_renders_8point_editor_for_right_when_capable(self) -> None:
        # Mirror of the left-side test: only the RIGHT side is capable.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_right_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            for index in range(1, 9):
                self.assertTrue(
                    dpg.does_item_exist(f"sensitivity_right_a{index}x_slider_8point"), index
                )
                self.assertTrue(
                    dpg.does_item_exist(f"sensitivity_right_a{index}y_slider_8point"), index
                )
            for index in range(1, 4):
                self.assertFalse(
                    dpg.does_item_exist(f"sensitivity_right_a{index}x_slider"), index
                )
                self.assertFalse(
                    dpg.does_item_exist(f"sensitivity_right_a{index}y_slider"), index
                )
            self.assertFalse(dpg.does_item_exist("sensitivity_left_a1x_slider_8point"))
            for index in range(1, 4):
                self.assertTrue(
                    dpg.does_item_exist(f"sensitivity_left_a{index}x_slider"), index
                )

            labels = _collect_labels("tab_sticks")
            self.assertIn("Apply Right Sensitivity (8-pt)", labels)
            self.assertIn("Apply Left Sensitivity", labels)
        finally:
            dpg.destroy_context()

    def test_sticks_tab_renders_3point_editor_when_not_capable(self) -> None:
        # No 8-point snapshot fields ⇒ both sides render the legacy 3-point
        # editor unchanged: 3 anchor pairs per side, presets row, 3-point apply
        # buttons, and the 3-point explanatory note. No 8-point widgets anywhere.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot()

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            for side in ("left", "right"):
                for index in range(1, 4):
                    self.assertTrue(
                        dpg.does_item_exist(f"sensitivity_{side}_a{index}x_slider"),
                        (side, index),
                    )
                    self.assertTrue(
                        dpg.does_item_exist(f"sensitivity_{side}_a{index}y_slider"),
                        (side, index),
                    )
                self.assertFalse(
                    dpg.does_item_exist(f"sensitivity_{side}_a1x_slider_8point"), side
                )
                self.assertFalse(
                    dpg.does_item_exist(f"sensitivity_{side}_a8x_slider_8point"), side
                )

            labels = _collect_labels("tab_sticks")
            self.assertIn("Apply Left Sensitivity", labels)
            self.assertIn("Apply Right Sensitivity", labels)
            self.assertNotIn("Apply Left Sensitivity (8-pt)", labels)
            # 3-point presets row present (it is hidden only in 8-point mode).
            self.assertIn("Presets", labels)
        finally:
            dpg.destroy_context()

    def test_sticks_tab_renders_8point_editor_when_no_snapshot_falls_back(self) -> None:
        # Defensive: a None snapshot (never-read device) must not crash the
        # branch logic and must fall through to the 3-point editor.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = None

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            self.assertTrue(dpg.does_item_exist("sensitivity_left_a1x_slider"))
            self.assertFalse(dpg.does_item_exist("sensitivity_left_a1x_slider_8point"))
        finally:
            dpg.destroy_context()

    def test_sticks_tab_8point_editor_renders_plot_and_presets(self) -> None:
        # The 8-point editor adds a live curve plot (line + scatter vs a faint
        # diagonal reference) and a 4-button preset row. Only the capable (left)
        # side gets them; the 3-point (right) side gets neither.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            # Plot container + all three series exist on the left side.
            for tag in (
                "sensitivity_left_plot_8point",
                "sensitivity_left_plot_series_8point",
                "sensitivity_left_plot_scatter_8point",
                "sensitivity_left_plot_diagonal_8point",
            ):
                self.assertTrue(dpg.does_item_exist(tag), tag)
            # The 3-point right side has no curve plot.
            self.assertFalse(dpg.does_item_exist("sensitivity_right_plot_8point"))
            self.assertFalse(dpg.does_item_exist("sensitivity_right_plot_series_8point"))

            # All four 8-point preset buttons render.
            labels = _collect_labels("tab_sticks")
            for preset_label in ("Linear", "Aggressive", "Smooth", "Balanced"):
                self.assertIn(preset_label, labels)
        finally:
            dpg.destroy_context()

    def test_sticks_tab_3point_editor_has_no_plot(self) -> None:
        # A non-capable device keeps the legacy 3-point editor: no curve plot on
        # either side. (It keeps its own "Presets" row — asserted in
        # test_sticks_tab_renders_3point_editor_when_not_capable.)
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot()

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            for side in ("left", "right"):
                self.assertFalse(
                    dpg.does_item_exist(f"sensitivity_{side}_plot_8point"), side
                )
                self.assertFalse(
                    dpg.does_item_exist(f"sensitivity_{side}_plot_series_8point"), side
                )
        finally:
            dpg.destroy_context()

    def test_refresh_sensitivity_8point_plot_updates_series_from_sliders(self) -> None:
        # _refresh_sensitivity_8point_plot reads the 16 live sliders into 8 (x,y)
        # points and pushes them into BOTH the line and scatter series.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            xs = [0, 10, 20, 30, 40, 50, 60, 70]
            ys = [0, 5, 15, 28, 40, 55, 75, 90]
            for index in range(1, 9):
                dpg.set_value(f"sensitivity_left_a{index}x_slider_8point", xs[index - 1])
                dpg.set_value(f"sensitivity_left_a{index}y_slider_8point", ys[index - 1])

            shell._refresh_sensitivity_8point_plot("left")

            series = dpg.get_value("sensitivity_left_plot_series_8point")
            scatter = dpg.get_value("sensitivity_left_plot_scatter_8point")
            self.assertEqual(list(series[0]), xs)
            self.assertEqual(list(series[1]), ys)
            self.assertEqual(list(scatter[0]), xs)
            self.assertEqual(list(scatter[1]), ys)
        finally:
            dpg.destroy_context()

    def test_8point_slider_callback_refreshes_plot_live(self) -> None:
        # Dragging any 8-point slider repaints the curve live: DPG sets the new
        # slider value then fires its callback (3-positional form), which reads
        # all 16 sliders back into the series. Pins the live-update wiring + the
        # slider-form callback signature in one shot.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            # DPG updates the slider value, then dispatches its callback.
            dpg.set_value("sensitivity_left_a3y_slider_8point", 42)
            callback = dpg.get_item_callback("sensitivity_left_a3y_slider_8point")
            self.assertIsNotNone(callback)
            callback("sensitivity_left_a3y_slider_8point", 42, None)

            series = dpg.get_value("sensitivity_left_plot_series_8point")
            # Anchor 3 (0-indexed 2) Y now reflects the dragged value.
            self.assertEqual(list(series[1])[2], 42)
        finally:
            dpg.destroy_context()

    def test_8point_plot_binds_series_themes_when_available(self) -> None:
        # When theme setup has run, the plot binds its three series themes. This
        # exercises the themed branch end to end and pins the theme-dict keys the
        # controller screen reads against _build_sensitivity_plot_themes.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            shell._setup_theme()
            self.assertIsNotNone(shell._sensitivity_plot_themes)
            self.assertEqual(
                set(shell._sensitivity_plot_themes), {"curve", "scatter", "diagonal"}
            )
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            # The themed series rendered without a KeyError / bad-bind crash.
            self.assertTrue(dpg.does_item_exist("sensitivity_left_plot_series_8point"))
            self.assertTrue(dpg.does_item_exist("sensitivity_left_plot_scatter_8point"))
            self.assertTrue(dpg.does_item_exist("sensitivity_left_plot_diagonal_8point"))
        finally:
            dpg.destroy_context()

    def test_sticks_tab_8point_plot_is_enlarged(self) -> None:
        # Polish #1: the curve plot is enlarged from 260x220 so it reads as
        # clearly as the vendor editor. Pin a meaningful lower bound (not the
        # exact px, so re-tuning within reason doesn't break the test).
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            config = dpg.get_item_configuration("sensitivity_left_plot_8point")
            self.assertGreaterEqual(config["width"], 400)
            self.assertGreaterEqual(config["height"], 320)
            # The 0-100 axes + the y=x diagonal reference survive the resize.
            self.assertTrue(dpg.does_item_exist("sensitivity_left_plot_diagonal_8point"))
        finally:
            dpg.destroy_context()

    def test_sticks_tab_8point_editor_renders_exact_entry_inputs(self) -> None:
        # Polish #2: each anchor/axis gains an exact-entry numeric input twin
        # next to its slider. Only the capable (left) side gets them.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            for index in range(1, 9):
                for axis in ("x", "y"):
                    self.assertTrue(
                        dpg.does_item_exist(
                            f"sensitivity_left_a{index}{axis}_input_8point"
                        ),
                        (index, axis),
                    )
                    # The slider twin is still present (coarse drag).
                    self.assertTrue(
                        dpg.does_item_exist(
                            f"sensitivity_left_a{index}{axis}_slider_8point"
                        ),
                        (index, axis),
                    )
            # The 3-point right side has no exact-entry inputs.
            self.assertFalse(dpg.does_item_exist("sensitivity_right_a1x_input_8point"))
        finally:
            dpg.destroy_context()

    def test_8point_input_callback_sets_anchor_and_syncs_slider(self) -> None:
        # Polish #2: typing an exact value into an anchor's input (committed on
        # Enter) sets that anchor, syncs the slider twin, and repaints the plot.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            # DPG sets the input value, then dispatches its callback (3-arg form).
            dpg.set_value("sensitivity_left_a3x_input_8point", 35)
            callback = dpg.get_item_callback("sensitivity_left_a3x_input_8point")
            self.assertIsNotNone(callback)
            callback("sensitivity_left_a3x_input_8point", 35, None)

            # Slider twin synced + series reflects the new anchor-3 X.
            self.assertEqual(dpg.get_value("sensitivity_left_a3x_slider_8point"), 35)
            series = dpg.get_value("sensitivity_left_plot_series_8point")
            self.assertEqual(list(series[0])[2], 35)
        finally:
            dpg.destroy_context()

    def test_8point_slider_edit_keeps_curve_monotonic(self) -> None:
        # Polish #3: dragging an anchor below its neighbours auto-pushes them so
        # the curve never becomes non-monotonic (end-to-end through the real
        # slider callback + funnel).
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            # Seed a monotonic X via the sliders, then drag anchor 6 well below
            # anchors 3-5.
            seed = [0, 14, 28, 42, 57, 71, 85, 100]
            for index in range(1, 9):
                dpg.set_value(f"sensitivity_left_a{index}x_slider_8point", seed[index - 1])
            dpg.set_value("sensitivity_left_a6x_slider_8point", 20)
            callback = dpg.get_item_callback("sensitivity_left_a6x_slider_8point")
            callback("sensitivity_left_a6x_slider_8point", 20, None)

            result = [
                dpg.get_value(f"sensitivity_left_a{index}x_slider_8point")
                for index in range(1, 9)
            ]
            # The edit is honoured and the whole X axis stays non-decreasing.
            self.assertEqual(dpg.get_value("sensitivity_left_a6x_slider_8point"), 20)
            self.assertTrue(
                all(result[i] >= result[i - 1] for i in range(1, 8)), result
            )
        finally:
            dpg.destroy_context()

    def test_sticks_tab_8point_editor_renders_draggable_points(self) -> None:
        # The 8-point curve graph gains 8 directly-draggable anchor
        # handles (one DPG drag point per anchor) on the capable side only.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            for index in range(1, 9):
                tag = f"sensitivity_left_drag_a{index}_8point"
                self.assertTrue(dpg.does_item_exist(tag), tag)
                self.assertEqual(
                    dpg.get_item_type(tag), "mvAppItemType::mvDragPoint", tag
                )
            # The 3-point right side has no drag points.
            self.assertFalse(dpg.does_item_exist("sensitivity_right_drag_a1_8point"))
        finally:
            dpg.destroy_context()

    def test_sticks_tab_3point_editor_has_no_drag_points(self) -> None:
        # A non-capable device keeps the legacy 3-point editor: no draggable
        # curve points on either side.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot()

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            for side in ("left", "right"):
                self.assertFalse(
                    dpg.does_item_exist(f"sensitivity_{side}_drag_a1_8point"), side
                )
        finally:
            dpg.destroy_context()

    def test_8point_drag_point_callback_sets_anchor_xy_and_syncs(self) -> None:
        # Dragging a curve point moves that anchor in BOTH axes at once: the drag
        # callback reads the point's (x, y) and funnels each axis through the
        # shared edit path, syncing the slider + input twins and the series.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            # DPG updates the drag point's value, then dispatches its callback.
            dpg.set_value("sensitivity_left_drag_a3_8point", (35.0, 42.0))
            callback = dpg.get_item_callback("sensitivity_left_drag_a3_8point")
            self.assertIsNotNone(callback)
            callback("sensitivity_left_drag_a3_8point", [35.0, 42.0], None)

            # Both slider twins for anchor 3 reflect the dragged position...
            self.assertEqual(dpg.get_value("sensitivity_left_a3x_slider_8point"), 35)
            self.assertEqual(dpg.get_value("sensitivity_left_a3y_slider_8point"), 42)
            # ...as do the exact-entry inputs...
            self.assertEqual(dpg.get_value("sensitivity_left_a3x_input_8point"), 35)
            self.assertEqual(dpg.get_value("sensitivity_left_a3y_input_8point"), 42)
            # ...and the curve series.
            series = dpg.get_value("sensitivity_left_plot_series_8point")
            self.assertEqual(list(series[0])[2], 35)
            self.assertEqual(list(series[1])[2], 42)
        finally:
            dpg.destroy_context()

    def test_8point_slider_edit_repositions_drag_point(self) -> None:
        # Two-way sync the other direction: a slider/input edit (or hydrate /
        # preset) repaints the plot, which repositions the drag point handle to
        # match — so the graph handles never drift from the numeric values.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            dpg.set_value("sensitivity_left_a5x_slider_8point", 60)
            callback = dpg.get_item_callback("sensitivity_left_a5x_slider_8point")
            callback("sensitivity_left_a5x_slider_8point", 60, None)

            point = dpg.get_value("sensitivity_left_drag_a5_8point")
            self.assertEqual(int(point[0]), 60)
        finally:
            dpg.destroy_context()

    def test_8point_plot_axes_have_explicit_unit_and_direction_labels(self) -> None:
        # Readability (#1): the X axis labels "Input % →" and the Y axis
        # "Output % ↑" — the explicit "%" names the 0-100 unit while the arrow
        # keeps the input/output orientation unambiguous.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            x_label = dpg.get_item_label("sensitivity_left_plot_xaxis_8point")
            y_label = dpg.get_item_label("sensitivity_left_plot_yaxis_8point")
            self.assertIn("Input", x_label)
            self.assertIn("%", x_label)
            self.assertIn("→", x_label)
            self.assertIn("Output", y_label)
            self.assertIn("%", y_label)
            self.assertIn("↑", y_label)
        finally:
            dpg.destroy_context()

    def test_sticks_tab_8point_exact_values_tree_holds_anchor_fields(self) -> None:
        # The 16 input+slider twins now live in a collapsible "Exact values"
        # tree node (open by default) so the graph stays the focus while the
        # precise fields remain available.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            tree_tag = "sensitivity_left_exact_values_tree_8point"
            self.assertTrue(dpg.does_item_exist(tree_tag))
            self.assertEqual(dpg.get_item_type(tree_tag), "mvAppItemType::mvTreeNode")
            # The anchor fields still exist (now parented under the tree node).
            self.assertTrue(dpg.does_item_exist("sensitivity_left_a1x_input_8point"))
            self.assertTrue(dpg.does_item_exist("sensitivity_left_a8y_slider_8point"))
        finally:
            dpg.destroy_context()

    def test_sticks_tab_renders_both_sensitivity_curves_when_both_capable(self) -> None:
        # L + R side-by-side deliverable: when both sticks expose an 8-point
        # curve, BOTH graphs (with their own draggable points) render under the
        # "Sensitivity Curves" section, each independently editable. (The literal
        # two-column arrangement is a layout group best confirmed by a hardware
        # smoke; here we pin that both sides build fully.)
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
            sensitivity_right_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            for side in ("left", "right"):
                self.assertTrue(dpg.does_item_exist(f"sensitivity_{side}_plot_8point"), side)
                self.assertTrue(
                    dpg.does_item_exist(f"sensitivity_{side}_drag_a1_8point"), side
                )
                self.assertTrue(
                    dpg.does_item_exist(f"sensitivity_{side}_drag_a8_8point"), side
                )

            labels = _collect_labels("tab_sticks")
            self.assertIn(t("controller.sticks.sensitivity_curves"), labels)
        finally:
            dpg.destroy_context()

    def test_sticks_tab_8point_drag_points_use_accent_fill(self) -> None:
        # #2: the draggable anchor handles render in the app's reserved
        # "draggable/active" accent colour so they read as grabbable.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            accent = tuple(shell.COLORS["accent"])
            for index in range(1, 9):
                color = dpg.get_item_configuration(
                    f"sensitivity_left_drag_a{index}_8point"
                )["color"]
                # DPG stores colors normalised to 0-1 floats; rescale to 0-255.
                as_255 = tuple(round(channel * 255) for channel in color)
                self.assertEqual(as_255, accent, index)
        finally:
            dpg.destroy_context()

    def test_sticks_tab_8point_plot_binds_handle_marker_theme(self) -> None:
        # #2: a marker-size theme is bound to the plot (cascading a larger marker
        # to the scatter so the handles read bigger), and cached on the shell.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            self.assertTrue(dpg.get_item_theme("sensitivity_left_plot_8point"))
            self.assertIsNotNone(shell._sensitivity_handle_theme_id)
            self.assertTrue(dpg.does_item_exist(shell._sensitivity_handle_theme_id))
        finally:
            dpg.destroy_context()

    def test_sticks_tab_8point_renders_point_value_readout(self) -> None:
        # #3: a point-value callout line renders under each capable plot, seeded
        # with the hint text; the 3-point side has none.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            self.assertTrue(
                dpg.does_item_exist("sensitivity_left_drag_readout_8point")
            )
            self.assertEqual(
                dpg.get_value("sensitivity_left_drag_readout_8point"),
                t("controller.sticks.plot_8point.readout_hint"),
            )
            self.assertFalse(
                dpg.does_item_exist("sensitivity_right_drag_readout_8point")
            )
        finally:
            dpg.destroy_context()

    def test_8point_drag_updates_point_value_readout(self) -> None:
        # #3: dragging an anchor refreshes the callout to that point's canonical
        # (input, output) value (not the seed hint).
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            dpg.set_value("sensitivity_left_drag_a3_8point", (35.0, 42.0))
            callback = dpg.get_item_callback("sensitivity_left_drag_a3_8point")
            callback("sensitivity_left_drag_a3_8point", [35.0, 42.0], None)

            readout = dpg.get_value("sensitivity_left_drag_readout_8point")
            self.assertIn("35", readout)
            self.assertIn("42", readout)
            self.assertNotEqual(
                readout, t("controller.sticks.plot_8point.readout_hint")
            )
        finally:
            dpg.destroy_context()

    def test_sticks_tab_8point_exact_values_tree_collapsed_by_default(self) -> None:
        # #4: the numeric exact-values table is demoted — collapsed by default so
        # the graph is the centerpiece (still reachable, one click away).
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            config = dpg.get_item_configuration(
                "sensitivity_left_exact_values_tree_8point"
            )
            self.assertFalse(config["default_open"])
            # The precise fields still exist (demoted, not removed).
            self.assertTrue(dpg.does_item_exist("sensitivity_left_a1x_input_8point"))
            self.assertTrue(dpg.does_item_exist("sensitivity_left_a8y_slider_8point"))
        finally:
            dpg.destroy_context()

    def test_sticks_tab_8point_renders_reset_buttons(self) -> None:
        # #5: each capable side gets "Reset to Linear" + "Reset to Defaults"
        # actions adjacent to its plot; the 3-point side gets neither.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            self.assertTrue(
                dpg.does_item_exist("sensitivity_left_reset_linear_8point")
            )
            self.assertTrue(
                dpg.does_item_exist("sensitivity_left_reset_defaults_8point")
            )
            labels = _collect_labels("tab_sticks")
            self.assertIn("Reset to Linear", labels)
            self.assertIn("Reset to Defaults", labels)
            self.assertFalse(
                dpg.does_item_exist("sensitivity_right_reset_linear_8point")
            )
        finally:
            dpg.destroy_context()

    def test_8point_reset_to_linear_applies_linear_preset(self) -> None:
        # #5: "Reset to Linear" routes through the existing preset apply path with
        # the bundled "Linear" curve (no new service path).
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            sensitivity_left_8point=tuple(SensitivityAnchor(0, 0) for _ in range(8)),
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            shell.apply_left_sensitivity_preset_8point = MagicMock()
            callback = dpg.get_item_callback("sensitivity_left_reset_linear_8point")
            self.assertIsNotNone(callback)
            callback()

            shell.apply_left_sensitivity_preset_8point.assert_called_once_with("Linear")
        finally:
            dpg.destroy_context()

    def test_8point_reset_to_defaults_reverts_to_snapshot_and_applies(self) -> None:
        # #5: "Reset to Defaults" reverts the editor to the controller's last-read
        # curve (the snapshot) and re-applies it — via existing shell methods only.
        anchors = tuple(
            SensitivityAnchor(x, y)
            for x, y in zip(
                (0, 14, 28, 42, 57, 71, 85, 100),
                (0, 10, 22, 38, 58, 74, 88, 100),
            )
        )
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(sensitivity_left_8point=anchors)

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            shell.apply_left_sensitivity_curve_8point = MagicMock()
            callback = dpg.get_item_callback("sensitivity_left_reset_defaults_8point")
            self.assertIsNotNone(callback)
            callback()

            # Editor reverted to the snapshot curve (both widget twins)...
            self.assertEqual(dpg.get_value("sensitivity_left_a2x_slider_8point"), 14)
            self.assertEqual(dpg.get_value("sensitivity_left_a5y_slider_8point"), 58)
            self.assertEqual(dpg.get_value("sensitivity_left_a2x_input_8point"), 14)
            # ...and the curve was re-applied.
            shell.apply_left_sensitivity_curve_8point.assert_called_once_with()
        finally:
            dpg.destroy_context()

    def test_8point_reset_to_defaults_falls_back_to_linear_without_snapshot(self) -> None:
        # #5 defensive: with no last-read curve for the side, "Reset to Defaults"
        # falls back to the linear reset rather than no-op'ing. Exercised directly
        # (the editor only renders when the snapshot side is present).
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot()  # sensitivity_left_8point is None
        shell.apply_left_sensitivity_preset_8point = MagicMock()

        controller._on_reset_sensitivity_8point_defaults(shell, "left")

        shell.apply_left_sensitivity_preset_8point.assert_called_once_with("Linear")

    def test_controller_tab_persists_across_rebuild(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.current_screen = "controller"
        shell.controller_active_tab = "profiles"

        dpg.create_context()
        try:
            shell._setup_theme()
            shell._build_ui()

            self.assertEqual(alias_of(dpg.get_value("controller_tab_bar")), "tab_profiles")
            shell.rebuild_current_screen()
            self.assertEqual(alias_of(dpg.get_value("controller_tab_bar")), "tab_profiles")
        finally:
            dpg.destroy_context()

    def test_buttons_tab_current_section_shows_present_default_muted(self) -> None:
        # A slot mapped to its OWN identity target (A→A) is an unremapped
        # default: it renders the target name with NO "(remapped)" tag.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            button_bindings={
                ButtonSlot.A: ButtonMapping.controller_button(ControllerButtonTarget.A)
            }
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            self.assertTrue(dpg.does_item_exist("current_binding_row_A"))
            self.assertEqual(dpg.get_value("current_binding_target_A"), "A")
            self.assertNotIn(
                t("controller.buttons.current.remapped_tag"),
                _collect_labels("current_binding_row_A"),
            )
        finally:
            dpg.destroy_context()

    def test_buttons_tab_current_section_marks_remap_distinct(self) -> None:
        # A→B is a real remap: target "B", the "(remapped)" tag, and a color
        # distinct from a default (X→X) row. Pins the default-vs-remap surface.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            button_bindings={
                ButtonSlot.A: ButtonMapping.controller_button(ControllerButtonTarget.B),
                ButtonSlot.X: ButtonMapping.controller_button(ControllerButtonTarget.X),
            }
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            # Remap row: new target + the remapped tag.
            self.assertEqual(dpg.get_value("current_binding_target_A"), "B")
            self.assertIn(
                t("controller.buttons.current.remapped_tag"),
                _collect_labels("current_binding_row_A"),
            )
            # Default row: identity target, no remapped tag.
            self.assertEqual(dpg.get_value("current_binding_target_X"), "X")
            self.assertNotIn(
                t("controller.buttons.current.remapped_tag"),
                _collect_labels("current_binding_row_X"),
            )
            # Visually distinct: the remap target color differs from a default's.
            remap_color = dpg.get_item_configuration("current_binding_target_A")["color"]
            default_color = dpg.get_item_configuration("current_binding_target_X")["color"]
            self.assertNotEqual(list(remap_color), list(default_color))
        finally:
            dpg.destroy_context()

    def test_buttons_tab_current_section_absent_slot_shows_honest_unknown(self) -> None:
        # A slot ABSENT from button_bindings (failed to read) must show an honest
        # unknown — the em dash + tooltip — NEVER its identity default. This is
        # the honest-abstention guard (cf. the axis_inversion fake-default bug).
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            button_bindings={
                ButtonSlot.A: ButtonMapping.controller_button(ControllerButtonTarget.A)
            }
        )  # LB intentionally absent from the dict

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            self.assertEqual(
                dpg.get_value("current_binding_target_LB"),
                controller._UNKNOWN_BINDING_GLYPH,
            )
            # The crucial assertion: an unread slot is NOT shown as its default.
            self.assertNotEqual(dpg.get_value("current_binding_target_LB"), "LB")
            # The "couldn't read" tooltip is attached so the unknown is explained.
            self.assertTrue(dpg.does_item_exist("current_binding_tip_LB"))
        finally:
            dpg.destroy_context()

    def test_buttons_tab_current_section_unsupported_kind_not_fabricated(self) -> None:
        # Honest-abstention guard: a slot whose mapping
        # KIND LegendCTL doesn't model (e.g. a keyboard/macro/vendor kind from the
        # official app or a future firmware) must NEVER render as a controller
        # button — even when its target_value byte collides with a known button
        # value. Here kind is 0x02 but value 0x10 == ControllerButtonTarget.B, so
        # the pre-fix renderer showed "B (remapped)" — a fabricated remap. The UI
        # must show the raw bytes instead and make NO "(remapped)" claim.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            button_bindings={
                ButtonSlot.A: ButtonMapping(
                    target_kind=0x02,
                    target_low=0x00,
                    target_value=ControllerButtonTarget.B.value,
                ),
            }
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            value = dpg.get_value("current_binding_target_A")
            # The crucial assertion: an unmodeled kind is NOT named as button "B".
            self.assertNotEqual(value, "B")
            # It shows the honest raw bytes, with the kind surfaced so it reads as
            # an unsupported mapping rather than a controller-button remap.
            self.assertIn("kind=0x02", value)
            self.assertIn("val=0x10", value)
            # And it makes no "(remapped)" claim for a mapping we can't model.
            self.assertNotIn(
                t("controller.buttons.current.remapped_tag"),
                _collect_labels("current_binding_row_A"),
            )
        finally:
            dpg.destroy_context()

    def test_buttons_tab_current_section_none_snapshot_shows_read_hint(self) -> None:
        # Never-read device (None snapshot): show the read hint, fabricate NO
        # rows.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = None

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            labels = _collect_labels("tab_buttons")
            self.assertIn(t("controller.buttons.current.not_read"), labels)
            self.assertFalse(dpg.does_item_exist("current_binding_row_A"))
        finally:
            dpg.destroy_context()

    def test_apply_binding_live_refreshes_current_bindings_without_tab_reentry(
        self,
    ) -> None:
        # Regression guard for the 2026-06-27 hardware bug: applying A→B in the
        # Buttons tab left the read-only Current Bindings list showing "A → A"
        # until the user left and re-entered the tab. The apply path must fold
        # the just-written mapping into the snapshot (no device read) and
        # re-render the list IN PLACE — so this asserts the row flips to
        # "B (remapped)" without the TEST rebuilding the screen.
        settings_service = MagicMock()
        settings_service.set_button_binding.return_value = SimpleNamespace(
            outcome=SetButtonBindingOutcome.OK,
            error_code=None,
            payload_hex="",
        )
        shell = make_shell(settings_service=settings_service)
        # Start from a read where A is unremapped (A→A).
        shell.last_controller_snapshot = empty_snapshot(
            button_bindings={
                ButtonSlot.A: ButtonMapping.controller_button(ControllerButtonTarget.A),
            }
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            # Precondition: the list shows A as an unremapped default.
            self.assertEqual(dpg.get_value("current_binding_target_A"), "A")
            self.assertNotIn(
                t("controller.buttons.current.remapped_tag"),
                _collect_labels("current_binding_row_A"),
            )

            # Pick Source=A / Target=B and apply (writes A→B to the device).
            dpg.set_value("binding_source_combo", "A")
            dpg.set_value("binding_target_combo", "B")
            result = shell.apply_button_binding()

            # The write went through the settings service...
            self.assertIsNotNone(result)
            settings_service.set_button_binding.assert_called_once_with(
                ButtonSlot.A,
                ButtonMapping.controller_button(ControllerButtonTarget.B),
            )
            # ...the snapshot was updated authoritatively, with NO new device read...
            settings_service.get_all_settings.assert_not_called()
            self.assertEqual(
                shell.last_controller_snapshot.button_bindings[ButtonSlot.A],
                ButtonMapping.controller_button(ControllerButtonTarget.B),
            )
            # ...and the Current Bindings list reflects A→B in place — the apply
            # path re-rendered the section; the test never rebuilt the screen.
            self.assertEqual(dpg.get_value("current_binding_target_A"), "B")
            self.assertIn(
                t("controller.buttons.current.remapped_tag"),
                _collect_labels("current_binding_row_A"),
            )
        finally:
            dpg.destroy_context()

    def test_snapshot_refresh_live_refreshes_current_bindings(self) -> None:
        # The async snapshot refresh (read on_done) updates
        # last_controller_snapshot without rebuilding the screen body. Hydrating
        # that snapshot must re-render the Current Bindings list in place, so a
        # Read reflects fresh device bindings without a tab re-entry.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            button_bindings={
                ButtonSlot.A: ButtonMapping.controller_button(ControllerButtonTarget.A),
            }
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            self.assertEqual(dpg.get_value("current_binding_target_A"), "A")

            # A fresh read lands: A is now remapped to B on the device.
            refreshed = empty_snapshot(
                button_bindings={
                    ButtonSlot.A: ButtonMapping.controller_button(
                        ControllerButtonTarget.B
                    ),
                }
            )
            shell.last_controller_snapshot = refreshed
            shell._hydrate_controller_snapshot(refreshed, [])

            self.assertEqual(dpg.get_value("current_binding_target_A"), "B")
            self.assertIn(
                t("controller.buttons.current.remapped_tag"),
                _collect_labels("current_binding_row_A"),
            )
        finally:
            dpg.destroy_context()

    def test_profiles_tab_distinguishes_device_slots_from_local_profiles(self) -> None:
        # The Profiles tab lists LegendCTL-LOCAL profiles. A note must make clear
        # they are separate from the controller's on-device "Profile 1-4" slots,
        # whose names can't be read (slot number only) — so neither namespace is
        # mistaken for the other.
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            joined = " ".join(_collect_labels("tab_profiles"))
            self.assertIn("separate from the controller", joined)
            self.assertIn("slot number only", joined)
        finally:
            dpg.destroy_context()


class ControllerDiagramTests(unittest.TestCase):
    """The Buttons-tab back-view paddle diagram (v1): code-drawn hotspots for the
    6 BACK macro slots, highlight-on-select, honest framing, and i18n parity.
    Pure UI — no device I/O, no snapshot dependency."""

    # The 6 BACK slots the v1 diagram draws (M3/M4 are FRONT — not drawn).
    BACK_SLOTS = (
        MacroSlot.LM, MacroSlot.RM,
        MacroSlot.M1, MacroSlot.M2,
        MacroSlot.LK, MacroSlot.RK,
    )

    @staticmethod
    def _as_255(color) -> tuple:
        # DPG draw-item colors come back normalised to 0-1 floats; rescale to
        # 0-255 to compare against the shell COLORS palette (mirrors the
        # drag-point colour assertion). Defensive against an already-255 build.
        chans = [float(c) for c in color]
        if chans and max(chans) <= 1.0:
            return tuple(round(c * 255) for c in chans)
        return tuple(round(c) for c in chans)

    def test_diagram_renders_back_slot_hotspots_only(self) -> None:
        # Each of the 6 BACK slots gets a hotspot + label tag; the 2 FRONT slots
        # (M3/M4) are deliberately NOT drawn — a note points to them instead.
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            self.assertTrue(dpg.does_item_exist("diagram_back_drawlist"))
            for slot in self.BACK_SLOTS:
                self.assertTrue(
                    dpg.does_item_exist(f"diagram_paddle_{slot.name}"), slot.name
                )
                self.assertTrue(
                    dpg.does_item_exist(f"diagram_paddle_label_{slot.name}"), slot.name
                )
            # M3/M4 are front buttons — never drawn on the back view.
            for slot in (MacroSlot.M3, MacroSlot.M4):
                self.assertFalse(
                    dpg.does_item_exist(f"diagram_paddle_{slot.name}"), slot.name
                )
            # The position map carries exactly the 6 back slots.
            self.assertEqual(set(controller._BACK_PADDLE_POS), set(self.BACK_SLOTS))
        finally:
            dpg.destroy_context()

    def test_diagram_position_map_matches_confirmed_mirror(self) -> None:
        # Lock the CONFIRMED (spec §6) MIRRORED map: image-LEFT = player's RIGHT,
        # so each LEFT-side slot's x is GREATER than its RIGHT-side pair's x —
        # including the verified, counter-intuitive split M1 = LEFT-grip lower /
        # M2 = RIGHT-grip lower.
        pos = controller._BACK_PADDLE_POS
        self.assertGreater(pos[MacroSlot.LM][0], pos[MacroSlot.RM][0])  # paddles
        self.assertGreater(pos[MacroSlot.M1][0], pos[MacroSlot.M2][0])  # rear buttons
        self.assertGreater(pos[MacroSlot.LK][0], pos[MacroSlot.RK][0])  # top claws
        # Vertical stack: claws (top edge) above paddles above lower buttons.
        self.assertLess(pos[MacroSlot.LK][1], pos[MacroSlot.LM][1])
        self.assertLess(pos[MacroSlot.LM][1], pos[MacroSlot.M1][1])
        # Every hotspot (with its radius) fits inside the canvas.
        r = controller._PADDLE_R
        for slot, (x, y) in pos.items():
            self.assertTrue(r <= x <= controller._DIAGRAM_W - r, slot.name)
            self.assertTrue(r <= y <= controller._DIAGRAM_H - r, slot.name)

    def test_diagram_highlight_sets_selected_accent_others_muted(self) -> None:
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            accent = tuple(shell.COLORS["accent"])
            muted = tuple(shell.COLORS["muted"])

            # Default (pre-interaction) state: all hotspots muted.
            self.assertEqual(
                self._as_255(dpg.get_item_configuration("diagram_paddle_M1")["color"]),
                muted,
            )

            controller.refresh_diagram_paddle_highlight(shell, MacroSlot.M1)

            # Selected slot -> accent; every other back slot -> muted.
            self.assertEqual(
                self._as_255(dpg.get_item_configuration("diagram_paddle_M1")["color"]),
                accent,
            )
            for other in self.BACK_SLOTS:
                if other is MacroSlot.M1:
                    continue
                self.assertEqual(
                    self._as_255(
                        dpg.get_item_configuration(f"diagram_paddle_{other.name}")["color"]
                    ),
                    muted,
                    other.name,
                )

            # Clearing (None) returns the selection to muted.
            controller.refresh_diagram_paddle_highlight(shell, None)
            self.assertEqual(
                self._as_255(dpg.get_item_configuration("diagram_paddle_M1")["color"]),
                muted,
            )
        finally:
            dpg.destroy_context()

    def test_diagram_highlight_guarded_no_throw_when_unmounted(self) -> None:
        # The diagram isn't built (no Buttons tab mounted) — refresh must be a
        # silent no-op, never raising on the absent tags. Mirrors
        # _apply_current_binding_target_state's does_item_exist guard.
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            controller.refresh_diagram_paddle_highlight(shell, MacroSlot.M1)
            controller.refresh_diagram_paddle_highlight(shell, None)
        finally:
            dpg.destroy_context()

    def test_diagram_combo_touch_highlights_matching_slot(self) -> None:
        # The back-paddle row's combo callback drives the highlight: touching
        # M1's dropdown lights M1 (accent) and leaves the others muted. Invoked
        # the way DPG dispatches a combo: (sender, app_data, user_data=slot).
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            combo = "back_paddle_combo_M1"
            callback = dpg.get_item_callback(combo)
            self.assertIsNotNone(callback)
            # user_data carries the slot, exactly as the apply button does.
            self.assertEqual(dpg.get_item_user_data(combo), MacroSlot.M1)
            callback("sender", "A", dpg.get_item_user_data(combo))

            self.assertEqual(
                self._as_255(dpg.get_item_configuration("diagram_paddle_M1")["color"]),
                tuple(shell.COLORS["accent"]),
            )
            self.assertEqual(
                self._as_255(dpg.get_item_configuration("diagram_paddle_M2")["color"]),
                tuple(shell.COLORS["muted"]),
            )
        finally:
            dpg.destroy_context()

    def test_diagram_renders_honest_and_front_notes(self) -> None:
        # Honest framing (always) + the M3/M4-are-front note + the approx caption
        # (rendered because _BACK_PADDLE_APPROX is non-empty) all show in the tab.
        self.assertTrue(controller._BACK_PADDLE_APPROX)  # precondition for approx note
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            labels = _collect_labels("tab_buttons")
            self.assertIn(t("controller.buttons.diagram.title"), labels)
            self.assertIn(t("controller.buttons.diagram.note"), labels)
            self.assertIn(t("controller.buttons.diagram.front_note"), labels)
            self.assertIn(t("controller.buttons.diagram.approx_note"), labels)
        finally:
            dpg.destroy_context()

    def test_diagram_i18n_keys_present_in_both_locales(self) -> None:
        # Parity + non-empty for the new keys (the global
        # test_locale_jsons_have_matching_keys covers set equality; this pins the
        # specific keys, the honest-abstention content, and ASCII glyph-safety).
        locale_dir = Path("zd_app/i18n/locales")
        en = json.loads((locale_dir / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((locale_dir / "zh-CN.json").read_text(encoding="utf-8"))
        keys = (
            "controller.buttons.diagram.title",
            "controller.buttons.diagram.note",
            "controller.buttons.diagram.front_note",
            "controller.buttons.diagram.approx_note",
        )
        for key in keys:
            with self.subTest(key=key):
                self.assertIn(key, en)
                self.assertIn(key, zh)
                self.assertTrue(en[key])
                self.assertTrue(zh[key])
        # Honest abstention: the framing note never implies a device read.
        self.assertIn("not a read of the controller", en["controller.buttons.diagram.note"])
        # The front note names the two front buttons.
        self.assertIn("M3", en["controller.buttons.diagram.front_note"])
        self.assertIn("M4", en["controller.buttons.diagram.front_note"])
        # EN strings stay free of glyphs the Inter font can't render (em-dash,
        # ellipsis, check, arrow) — they would surface as "?".
        for key in keys:
            for ch in ("—", "…", "✓", "→"):
                self.assertNotIn(ch, en[key], (key, ch))


def _collect_labels(root) -> list[str]:
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


def _first_button_under(root, label: str):
    stack = [root]
    while stack:
        item = stack.pop()
        if (
            dpg.get_item_type(item) == "mvAppItemType::mvButton"
            and dpg.get_item_label(item) == label
        ):
            return item
        for slot in range(4):
            stack.extend(dpg.get_item_children(item, slot) or [])
    raise AssertionError(f"No button labeled {label!r} under {root!r}")


class _ControllerChildWindowRecorder:
    """Patches ``controller.dpg.child_window`` to record kwargs without
    requiring a real DPG context — mirrors the pattern in
    ``test_screens_diagnostics.py``."""

    class _CM:
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False

    def __init__(self) -> None:
        self.child_windows: list[dict] = []
        self._cm = None
        self._cm_typo = None
        self._cm_components = None

    def __enter__(self) -> "_ControllerChildWindowRecorder":
        def record_child_window(*_args, **kw):
            self.child_windows.append(kw)
            return self._CM()

        def passthrough(*_args, **_kw):
            return self._CM()

        fake_dpg = MagicMock()
        fake_dpg.child_window = record_child_window
        fake_dpg.group = passthrough
        fake_dpg.tab_bar = passthrough
        fake_dpg.tab = passthrough
        fake_dpg.tree_node = passthrough
        fake_dpg.drawlist = passthrough
        fake_dpg.table = passthrough
        fake_dpg.table_row = passthrough
        # Items that return tags must echo something usable.
        fake_dpg.add_text = MagicMock(return_value="t")
        fake_dpg.add_button = MagicMock(return_value="b")
        fake_dpg.add_spacer = MagicMock(return_value="s")
        fake_dpg.add_separator = MagicMock(return_value="sep")
        fake_dpg.add_checkbox = MagicMock(return_value="cb")
        fake_dpg.add_combo = MagicMock(return_value="c")
        fake_dpg.add_slider_int = MagicMock(return_value="sl")
        fake_dpg.add_table_column = MagicMock()
        fake_dpg.add_selectable = MagicMock(return_value="sel")
        fake_dpg.draw_rectangle = MagicMock()
        fake_dpg.does_item_exist = MagicMock(return_value=False)
        fake_dpg.set_value = MagicMock()
        fake_dpg.bind_item_theme = MagicMock()
        self._cm = patch.object(controller, "dpg", fake_dpg)
        self._cm.__enter__()
        # The typography helpers (screen_title/section_title/helper_text)
        # use their own module-level ``dpg``; patch it too so title rendering
        # routes through the fake instead of hitting a real (absent) context.
        self._cm_typo = patch.object(typography, "dpg", fake_dpg)
        self._cm_typo.__enter__()
        # The Profiles tab now routes its table through components.table() /
        # action_button(), which use components' own module-level ``dpg``.
        # patch.object rebinds names (it does not mutate the shared dearpygui
        # module the way patch.multiple does), so without this the table call
        # would hit the real, context-less components.dpg and segfault (exit
        # 139). Point it at the same fake. (The other 4 screen recorders use
        # patch.multiple on the shared module, so they cover components for free.)
        self._cm_components = patch.object(components, "dpg", fake_dpg)
        self._cm_components.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        if self._cm_components is not None:
            self._cm_components.__exit__(*exc)
        if self._cm_typo is not None:
            self._cm_typo.__exit__(*exc)
        if self._cm is not None:
            self._cm.__exit__(*exc)


class ControllerLoadingHintHeightTests(unittest.TestCase):
    """Regression for the DPG card-clip lane (2026-06-21).

    The Controller screen's ``_render_loading_hint`` renders when no
    controller snapshot has been read yet: a "Loading…" label plus a 16px
    skeleton rectangle. It used to pin a hand-measured fixed height (80), which a
    real-viewport probe (tools/diag_dpg_card_clip.py) still found clipping the
    skeleton by 6px at the shipped fonts. It now fits its content via the DPG-2.x
    ``auto_resize_y`` flag (with the legacy fill flag ``autosize_y`` suppressed),
    so it can't clip and carries no magic number. Mirrors how the Live Verify
    tests assert ``auto_resize_y``.
    """

    def test_loading_hint_card_fits_content_no_fixed_height(self) -> None:
        # No controller snapshot ⇒ loading hint renders.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = None
        with _ControllerChildWindowRecorder() as rec:
            controller.build(shell, "content_region")
        # The loading-hint card is the bordered, untagged, content-fit card with
        # no width set (the tab containers come after and carry tags/widths).
        loading_hints = [
            kw for kw in rec.child_windows
            if kw.get("auto_resize_y") is True
            and kw.get("width") is None
            and kw.get("tag") is None
        ]
        self.assertGreater(
            len(loading_hints), 0,
            f"Expected a content-fit (auto_resize_y) loading-hint card; got "
            f"child_windows {rec.child_windows}",
        )
        for kw in loading_hints:
            # Fit, not fixed: no hand-measured height, and the legacy fill flag
            # is suppressed so the card shrinks to content instead of filling.
            self.assertIsNone(
                kw.get("height"),
                f"Loading-hint card should fit content, not pin a height: {kw}",
            )
            self.assertFalse(
                kw.get("autosize_y", False),
                f"Loading-hint card must suppress the legacy fill flag: {kw}",
            )


class SensitivityPlotAxisTests(unittest.TestCase):
    """The 8-point curve plot's axis constants (unclip the endpoint dots)."""

    def test_plot_axis_view_pad_is_positive(self) -> None:
        # A positive pad widens the plotted view a few % past 0-100 so the
        # (0,0)/(100,100) endpoint dots aren't half-clipped at the axis edges.
        self.assertIsInstance(controller._PLOT_AXIS_VIEW_PAD, (int, float))
        self.assertGreater(controller._PLOT_AXIS_VIEW_PAD, 0)

    def test_plot_axis_ticks_unchanged(self) -> None:
        # The gridline labels stay at 0/25/50/75/100 -- only the view limits are
        # padded, never the ticks.
        self.assertEqual(
            controller._PLOT_AXIS_TICKS,
            (("0", 0.0), ("25", 25.0), ("50", 50.0), ("75", 75.0), ("100", 100.0)),
        )


if __name__ == "__main__":
    unittest.main()
