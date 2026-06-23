"""Triggers screen."""

from __future__ import annotations

import dearpygui.dearpygui as dpg


def build(shell, parent: str) -> None:
    draft = shell.profile_service.current_draft
    left = draft.left_trigger
    right = draft.right_trigger

    with dpg.child_window(parent=parent, autosize_x=True, autosize_y=True, border=False):
        dpg.add_text("Triggers", color=shell.COLORS["text"])
        dpg.add_text("This legacy screen edits a local draft only. Use the Triggers tab on the main Controller screen to apply trigger changes to the controller.")
        if shell.device_service.state.last_read_time is None:
            dpg.add_text("Live meters still work before a controller read, but settings are local defaults until then.", color=shell.COLORS["warn"])
        dpg.add_spacer(height=10)

        dpg.add_checkbox(label="Linked Edit Mode", default_value=shell.link_triggers, callback=lambda _s, value: shell.set_link_triggers(value))
        dpg.add_spacer(height=10)

        with dpg.group(horizontal=True):
            _trigger_card(shell, "left", left)
            _trigger_card(shell, "right", right)


def _trigger_card(shell, side: str, settings) -> None:
    with dpg.child_window(width=420, height=320, border=True):
        dpg.add_text(f"{side.title()} Trigger", color=shell.COLORS["muted"])
        dpg.add_combo(
            items=["Standard", "Linear", "Hair Trigger"],
            default_value=settings.mode,
            width=170,
            label="Mode",
            callback=lambda _s=None, value=None, _u=None, *, selected_side=side: shell.update_trigger_settings(selected_side, {"mode": value}),
        )
        dpg.add_slider_int(
            label="Threshold",
            min_value=0,
            max_value=100,
            default_value=settings.threshold,
            callback=lambda _s=None, value=None, _u=None, *, selected_side=side: shell.update_trigger_settings(selected_side, {"threshold": value}),
        )
        dpg.add_slider_int(
            label="Actuation Point",
            min_value=0,
            max_value=100,
            default_value=settings.actuation_point,
            callback=lambda _s=None, value=None, _u=None, *, selected_side=side: shell.update_trigger_settings(selected_side, {"actuation_point": value}),
        )
        dpg.add_checkbox(
            label="Hair Trigger",
            default_value=settings.hair_trigger,
            callback=lambda _s=None, value=None, _u=None, *, selected_side=side: shell.update_trigger_settings(selected_side, {"hair_trigger": value}),
        )
        dpg.add_spacer(height=6)
        dpg.add_progress_bar(default_value=0.0, width=240, tag=f"triggers_live_{side}")
        dpg.add_text(f"{side.title()} Trigger Live: 0", tag=f"triggers_live_{side}_label")
        dpg.add_spacer(height=10)
        with dpg.group(horizontal=True):
            dpg.add_button(label="Apply", width=100, callback=lambda: shell.apply_trigger_changes())
            dpg.add_button(label="Reset", width=100, callback=lambda *args, selected_side=side: shell.reset_trigger_settings(selected_side))
