from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import empty_snapshot, make_shell
from zd_app.i18n import t
from zd_app.services.settings_service import (
    BackPaddleBinding,
    ControllerButtonTarget,
    MacroSlot,
)
from zd_app.ui.screens import controller


class BackPaddleScreenTests(unittest.TestCase):
    def test_back_paddles_section_renders_8_rows(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            back_paddle_bindings={MacroSlot.M1: BackPaddleBinding(ControllerButtonTarget.A)}
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            for slot in MacroSlot:
                self.assertTrue(dpg.does_item_exist(f"back_paddle_row_{slot.name}"))
                self.assertTrue(dpg.does_item_exist(f"back_paddle_combo_{slot.name}"))
                self.assertTrue(dpg.does_item_exist(f"back_paddle_apply_{slot.name}"))
            self.assertEqual(dpg.get_value("back_paddle_combo_M1"), "A")
        finally:
            dpg.destroy_context()

    def test_back_paddle_combo_callback_passes_slot_via_user_data(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        shell.apply_back_paddle_binding_from_combo = MagicMock()

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            button = "back_paddle_apply_LM"
            callback = dpg.get_item_callback(button)
            callback("sender", "app_data", dpg.get_item_user_data(button))

            shell.apply_back_paddle_binding_from_combo.assert_called_once_with(MacroSlot.LM)
        finally:
            dpg.destroy_context()

    def test_compatibility_note_visible_in_section(self) -> None:
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            labels = _collect_labels("tab_buttons")
            self.assertIn("Back Paddles", labels)
            self.assertTrue(
                any("1-step button bindings only" in label for label in labels)
            )
        finally:
            dpg.destroy_context()

    def test_unread_paddle_shows_placeholder_not_unbound(self) -> None:
        # M1 is set by the user this session; every other paddle is unread. An
        # unread paddle has NO LegendCTL value and we can't read the device's real
        # state, so it must show the honest placeholder — never "Unbound", which
        # would falsely imply the device paddle is empty.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot(
            back_paddle_bindings={MacroSlot.M1: BackPaddleBinding(ControllerButtonTarget.A)}
        )

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            placeholder = t("controller.back_paddles.not_set_here")
            unbound = t("controller.back_paddles.unbound")
            self.assertEqual(dpg.get_value("back_paddle_combo_M1"), "A")
            for slot in MacroSlot:
                if slot is MacroSlot.M1:
                    continue
                value = dpg.get_value(f"back_paddle_combo_{slot.name}")
                self.assertEqual(value, placeholder)
                self.assertNotEqual(value, unbound)
        finally:
            dpg.destroy_context()

    def test_cannot_read_note_renders_in_section(self) -> None:
        # The honest can't-read + overwrite note frames the whole section.
        shell = make_shell(settings_service=MagicMock())

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            labels = _collect_labels("tab_buttons")
            self.assertTrue(any("already on your controller" in label for label in labels))
            self.assertTrue(any("overwrites" in label for label in labels))
        finally:
            dpg.destroy_context()

    def test_unbound_is_a_dropdown_action_placeholder_is_not(self) -> None:
        # "Unbound" stays a selectable clear ACTION in the dropdown; the unread
        # placeholder is only ever a default, never a pickable target option.
        options = controller._back_paddle_target_options()
        self.assertIn(t("controller.back_paddles.unbound"), options)
        self.assertNotIn(t("controller.back_paddles.not_set_here"), options)

    def test_apply_on_unread_placeholder_writes_nothing(self) -> None:
        # Clicking Apply while the unread placeholder is still selected must NOT
        # write — we won't overwrite the device's unreadable paddle state with a
        # blank. It records a friendly "left unchanged" hint instead.
        shell = make_shell(settings_service=MagicMock())
        shell.last_controller_snapshot = empty_snapshot()  # all paddles unread

        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            controller.build(shell, "content_region")

            self.assertEqual(
                dpg.get_value("back_paddle_combo_M1"),
                t("controller.back_paddles.not_set_here"),
            )
            shell._hid_available_or_refuse = MagicMock(return_value=True)
            shell._record_settings_apply_result = MagicMock()
            shell.refresh_shell = MagicMock()

            shell.apply_back_paddle_binding_from_combo(MacroSlot.M1)

            shell.settings_service.set_back_paddle_binding.assert_not_called()
            shell._record_settings_apply_result.assert_called_once()
            ok, message = shell._record_settings_apply_result.call_args.args
            self.assertFalse(ok)
            self.assertIn("left unchanged", message)
        finally:
            dpg.destroy_context()


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


if __name__ == "__main__":
    unittest.main()
