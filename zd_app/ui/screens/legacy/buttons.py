"""Buttons screen."""

from __future__ import annotations

import dearpygui.dearpygui as dpg

from zd_app.ui.components import Column, table


def build(shell, parent: str) -> None:
    draft = shell.profile_service.current_draft
    selected_id = shell.selected_button_input or draft.button_mappings[0].physical_input_id
    selected_mapping = next(item for item in draft.button_mappings if item.physical_input_id == selected_id)
    selected_dirty = shell.profile_service.is_button_dirty(selected_mapping.physical_input_id)

    with dpg.child_window(parent=parent, autosize_x=True, autosize_y=True, border=False):
        dpg.add_text("Buttons", color=shell.COLORS["text"])
        dpg.add_text("Remap physical controls into a local draft. Device writes stay explicit and visible.")
        if shell.device_service.state.last_read_time is None:
            dpg.add_text("Current values are local defaults until you read the controller.", color=shell.COLORS["warn"])
        dpg.add_spacer(height=10)

        with dpg.group(horizontal=True):
            with dpg.child_window(width=520, height=-1, border=True):
                dpg.add_text("Physical Controls", color=shell.COLORS["muted"])
                with table(
                    [
                        Column("Input", width=170),
                        Column("Mapped Action", width=170),
                        Column("Status", width=90),
                        Column("Live", width=90),
                    ]
                ):
                    for mapping in draft.button_mappings:
                        dirty = shell.profile_service.is_button_dirty(mapping.physical_input_id)
                        with dpg.table_row():
                            dpg.add_selectable(
                                label=mapping.physical_label,
                                default_value=mapping.physical_input_id == selected_id,
                                callback=lambda *args, input_id=mapping.physical_input_id: shell.select_button_mapping(input_id),
                            )
                            dpg.add_text(mapping.action, tag=f"buttons_action_{mapping.physical_input_id}")
                            dpg.add_text(
                                "Dirty" if dirty else "Clean",
                                tag=f"buttons_dirty_{mapping.physical_input_id}",
                                color=shell.COLORS["warn"] if dirty else shell.COLORS["muted"],
                            )
                            dpg.add_text("Idle", tag=f"buttons_live_{mapping.physical_input_id}", color=shell.COLORS["muted"])

            with dpg.child_window(width=-1, height=-1, border=True):
                dpg.add_text("Selected Control", color=shell.COLORS["muted"])
                with dpg.group(horizontal=True):
                    dpg.add_text("Physical:")
                    dpg.add_text(selected_mapping.physical_label)
                with dpg.group(horizontal=True):
                    dpg.add_text("Current:")
                    dpg.add_text(selected_mapping.action, tag="buttons_editor_current")
                with dpg.group(horizontal=True):
                    dpg.add_text("Default:")
                    dpg.add_text(selected_mapping.default_action)
                with dpg.group(horizontal=True):
                    dpg.add_text("Status:")
                    dpg.add_text("Dirty" if selected_dirty else "Clean", color=shell.COLORS["warn"] if selected_dirty else shell.COLORS["muted"])
                dpg.add_spacer(height=8)
                dpg.add_combo(
                    items=shell.button_actions,
                    default_value=selected_mapping.action,
                    width=240,
                    label="Target Action",
                    callback=lambda _s=None, value=None, _u=None, *, input_id=selected_mapping.physical_input_id: shell.update_button_mapping(input_id, value),
                )
                dpg.add_spacer(height=10)
                with dpg.group(horizontal=True):
                    dpg.add_button(
                        label="Reset Mapping",
                        width=140,
                        callback=lambda *args, input_id=selected_mapping.physical_input_id: shell.reset_button_mapping(input_id),
                    )
                    dpg.add_button(label="Apply to Controller", width=150, callback=lambda: shell.apply_button_changes())
                dpg.add_spacer(height=8)
                dpg.add_text(
                    "This legacy screen edits a local draft only. Use the Buttons tab on the main Controller screen to apply remaps to the controller.",
                    wrap=340,
                    color=shell.COLORS["warn"],
                )
