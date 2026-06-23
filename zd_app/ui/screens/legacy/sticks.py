"""Sticks screen."""

from __future__ import annotations

import dearpygui.dearpygui as dpg


def build(shell, parent: str) -> None:
    side = shell.selected_stick_side
    settings = shell.profile_service.current_draft.left_stick if side == "left" else shell.profile_service.current_draft.right_stick

    with dpg.child_window(parent=parent, autosize_x=True, autosize_y=True, border=False):
        dpg.add_text("Sticks", color=shell.COLORS["text"])
        dpg.add_text("This legacy screen edits a local draft only. Use the Sticks tab on the main Controller screen to apply deadzone changes to the controller.")
        dpg.add_spacer(height=10)

        with dpg.group(horizontal=True):
            dpg.add_button(label="Left Stick", callback=lambda *args: shell.select_stick_side("left"), width=120)
            dpg.add_button(label="Right Stick", callback=lambda *args: shell.select_stick_side("right"), width=120)
            dpg.add_spacer(width=14)
            dpg.add_text("Preset")
            dpg.add_combo(
                items=["Default", "Safe", "Competitive", "Custom"],
                default_value=settings.curve_preset,
                width=160,
                callback=lambda _s=None, value=None, _u=None, *, selected_side=side: shell.update_stick_settings(selected_side, {"curve_preset": value}),
            )

        if shell.preview_seconds_remaining() > 0:
            dpg.add_text(
                f"Temporary preview active for {shell.preview_seconds_remaining()}s. Revert is automatic unless you confirm.",
                color=shell.COLORS["warn"],
            )
            dpg.add_button(label="Keep Preview as Draft", width=180, callback=lambda: shell.confirm_stick_preview())

        with dpg.group(horizontal=True):
            with dpg.child_window(width=380, height=320, border=True):
                dpg.add_text(f"{side.title()} Stick Controls", color=shell.COLORS["muted"])
                dpg.add_slider_int(
                    label="Center Deadzone",
                    min_value=0,
                    max_value=40,
                    default_value=settings.center_deadzone,
                    callback=lambda _s=None, value=None, _u=None, *, selected_side=side: shell.update_stick_settings(selected_side, {"center_deadzone": value}),
                )
                dpg.add_slider_int(
                    label="Peripheral Deadzone",
                    min_value=70,
                    max_value=100,
                    default_value=settings.peripheral_deadzone,
                    callback=lambda _s=None, value=None, _u=None, *, selected_side=side: shell.update_stick_settings(selected_side, {"peripheral_deadzone": value}),
                )
                dpg.add_slider_int(
                    label="Deadzone Compensation",
                    min_value=0,
                    max_value=20,
                    default_value=settings.deadzone_compensation,
                    callback=lambda _s=None, value=None, _u=None, *, selected_side=side: shell.update_stick_settings(selected_side, {"deadzone_compensation": value}),
                )
                dpg.add_checkbox(
                    label="Invert X",
                    default_value=settings.invert_x,
                    callback=lambda _s=None, value=None, _u=None, *, selected_side=side: shell.update_stick_settings(selected_side, {"invert_x": value}),
                )
                dpg.add_checkbox(
                    label="Invert Y",
                    default_value=settings.invert_y,
                    callback=lambda _s=None, value=None, _u=None, *, selected_side=side: shell.update_stick_settings(selected_side, {"invert_y": value}),
                )
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Apply Temporarily", width=140, callback=lambda: shell.start_stick_preview())
                    dpg.add_button(label="Apply and Save", width=140, callback=lambda: shell.apply_stick_changes())
                    dpg.add_button(label="Revert", width=100, callback=lambda: shell.revert_unsaved_changes())

            with dpg.child_window(width=-1, height=320, border=True):
                dpg.add_text("Explanation", color=shell.COLORS["muted"])
                dpg.add_text(
                    "Default keeps the stock curve.\n"
                    "Safe widens the deadzone slightly.\n"
                    "Competitive reduces center deadzone while keeping outer protection.\n"
                    "Custom is unlocked as soon as you tweak any slider.",
                    wrap=360,
                )
                dpg.add_spacer(height=10)
                dpg.add_text("Current Values", color=shell.COLORS["muted"])
                dpg.add_text(
                    f"Center: {settings.center_deadzone}\n"
                    f"Peripheral: {settings.peripheral_deadzone}\n"
                    f"Compensation: {settings.deadzone_compensation}\n"
                    f"Invert X: {settings.invert_x}\n"
                    f"Invert Y: {settings.invert_y}",
                    tag="sticks_current_values",
                )
