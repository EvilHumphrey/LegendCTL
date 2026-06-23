"""Profile and draft management."""

from __future__ import annotations

from pathlib import Path

from zd_app.models import (
    ButtonMapping,
    OnboardSlot,
    Profile,
    StickSettings,
    TriggerSettings,
    create_default_profile,
    deep_copy_profile,
    diff_profile_count,
    utc_now_iso,
)
from zd_app.storage.profile_store import ProfileStore


class ProfileService:
    def __init__(self, profile_store: ProfileStore):
        self.profile_store = profile_store
        self.current_draft = create_default_profile()
        self._baseline_profile = deep_copy_profile(self.current_draft)
        self.onboard_slots = [OnboardSlot(slot_id=index + 1, display_name=f"Config {index + 1}") for index in range(4)]
        self.last_controller_read_scope = "No controller read yet."
        self.last_controller_read_source = "Not verified"

    @property
    def baseline_profile(self) -> Profile | None:
        return self._baseline_profile

    def read_from_controller(self, active_slot: int = 1, active_slot_source: str = "Not verified") -> Profile:
        self.current_draft = create_default_profile(name=f"Draft aligned to Config {active_slot}", origin="draft")
        self.current_draft.active_on_controller = True
        self.current_draft.last_modified = utc_now_iso()
        self._baseline_profile = deep_copy_profile(self.current_draft)
        self.last_controller_read_source = active_slot_source
        if active_slot_source in {"Official App UI", "Controller Protocol"}:
            self.last_controller_read_scope = (
                f"Active config {active_slot} confirmed from {active_slot_source}. "
                "The draft now follows that onboard config, but button, stick, trigger, and macro contents still use local defaults until protocol-backed config reads land."
            )
        else:
            self.last_controller_read_scope = (
                f"Working draft aligned to config {active_slot}, but the active onboard config is not verified yet. "
                "Button, stick, trigger, and macro contents still use local defaults until protocol-backed config reads land."
            )
        for slot in self.onboard_slots:
            slot.active = slot.slot_id == active_slot
            slot.status = (
                "slot_active_confirmed"
                if slot.active and active_slot_source in {"Official App UI", "Controller Protocol"}
                else "slot_working_assumption"
                if slot.active
                else "slot_not_read"
            )
            slot.target_selected = False
            slot.source_profile_id = self.current_draft.profile_id if slot.active else None
        return self.current_draft

    def pending_changes_count(self) -> int:
        return diff_profile_count(self.current_draft, self._baseline_profile)

    def revert_unsaved_changes(self) -> None:
        if self._baseline_profile is not None:
            self.current_draft = self._baseline_profile.clone()
            self.current_draft.dirty = False

    def save_draft_locally(self) -> Profile:
        profile = self.current_draft.clone()
        profile.origin = "desktop"
        profile.dirty = False
        profile.last_modified = utc_now_iso()
        self.profile_store.save(profile)
        return profile

    def list_local_profiles(self) -> list[Profile]:
        return self.profile_store.list_profiles()

    def load_local_profile(self, profile_id: str) -> Profile:
        loaded = self.profile_store.load(profile_id)
        self.current_draft = loaded.clone()
        self.current_draft.origin = "draft"
        self.current_draft.dirty = False
        self._baseline_profile = deep_copy_profile(self.current_draft)
        return self.current_draft

    def duplicate_local_profile(self, profile_id: str) -> Profile:
        source = self.profile_store.load(profile_id)
        return self.profile_store.duplicate(profile_id, f"{source.display_name} Copy")

    def rename_local_profile(self, profile_id: str, new_name: str) -> Profile:
        return self.profile_store.rename(profile_id, new_name)

    def export_local_profile(self, profile_id: str) -> str:
        return str(self.profile_store.export_profile(profile_id))

    def import_local_profile(self, path: str) -> Profile:
        return self.profile_store.import_profile(path)

    def set_button_mapping(self, input_id: str, action: str) -> None:
        for mapping in self.current_draft.button_mappings:
            if mapping.physical_input_id == input_id:
                mapping.action = action
                break
        self.current_draft.dirty = self.pending_changes_count() > 0
        self.current_draft.last_modified = utc_now_iso()

    def reset_button_mapping(self, input_id: str) -> None:
        for mapping in self.current_draft.button_mappings:
            if mapping.physical_input_id == input_id:
                mapping.action = mapping.default_action
                mapping.turbo_enabled = False
                mapping.macro_binding = None
                break
        self.current_draft.dirty = self.pending_changes_count() > 0
        self.current_draft.last_modified = utc_now_iso()

    def set_stick_settings(self, side: str, patch: dict) -> None:
        target = self.current_draft.left_stick if side == "left" else self.current_draft.right_stick
        for key, value in patch.items():
            if hasattr(target, key):
                setattr(target, key, value)
        self.current_draft.dirty = self.pending_changes_count() > 0
        self.current_draft.last_modified = utc_now_iso()

    def set_trigger_settings(self, side: str, patch: dict) -> None:
        target = self.current_draft.left_trigger if side == "left" else self.current_draft.right_trigger
        for key, value in patch.items():
            if hasattr(target, key):
                setattr(target, key, value)
        self.current_draft.dirty = self.pending_changes_count() > 0
        self.current_draft.last_modified = utc_now_iso()

    def select_onboard_target(self, slot_id: int) -> None:
        for slot in self.onboard_slots:
            slot.target_selected = slot.slot_id == slot_id

    def restore_safe_defaults(self, active_slot: int = 1) -> Profile:
        self.current_draft = create_default_profile(name="Safe Defaults Draft", origin="draft")
        self.current_draft.active_on_controller = False
        self.current_draft.last_modified = utc_now_iso()
        self._baseline_profile = deep_copy_profile(self.current_draft)
        self.last_controller_read_source = "Not verified"
        self.last_controller_read_scope = "Local safe defaults restored. No controller config contents are loaded."
        for slot in self.onboard_slots:
            slot.active = slot.slot_id == active_slot
            slot.status = "slot_loaded" if slot.active else "slot_unknown"
            slot.target_selected = False
            slot.source_profile_id = self.current_draft.profile_id if slot.active else None
        return self.current_draft

    def apply_button_changes(self) -> tuple[bool, str]:
        return False, "Writing button remaps directly to the controller isn't supported yet. Draft saved locally."

    def apply_stick_changes(self) -> tuple[bool, str]:
        return False, "Writing stick settings directly to the controller isn't supported yet. Draft saved locally."

    def apply_trigger_changes(self) -> tuple[bool, str]:
        return False, "Writing trigger settings directly to the controller isn't supported yet. Draft saved locally."

    def is_button_dirty(self, input_id: str) -> bool:
        current = next((item for item in self.current_draft.button_mappings if item.physical_input_id == input_id), None)
        previous = self._baseline_button_mapping(input_id)
        if current is None or previous is None:
            return False
        return current.action != previous.action or current.turbo_enabled != previous.turbo_enabled or current.macro_binding != previous.macro_binding

    def compare_current_draft(self, reference: Profile | None = None) -> list[str]:
        baseline = reference or self._baseline_profile
        if baseline is None:
            return ["No comparison reference is available yet."]

        lines: list[str] = []
        current_map = {item.physical_input_id: item for item in self.current_draft.button_mappings}
        baseline_map = {item.physical_input_id: item for item in baseline.button_mappings}
        for input_id, mapping in current_map.items():
            previous = baseline_map.get(input_id)
            if previous is None:
                lines.append(f"{mapping.physical_label}: new mapping {mapping.action}")
                continue
            if mapping.action != previous.action:
                lines.append(f"{mapping.physical_label}: {previous.action} -> {mapping.action}")

        lines.extend(self._stick_diff_lines("Left Stick", baseline.left_stick, self.current_draft.left_stick))
        lines.extend(self._stick_diff_lines("Right Stick", baseline.right_stick, self.current_draft.right_stick))
        lines.extend(self._trigger_diff_lines("Left Trigger", baseline.left_trigger, self.current_draft.left_trigger))
        lines.extend(self._trigger_diff_lines("Right Trigger", baseline.right_trigger, self.current_draft.right_trigger))

        return lines or ["No differences from the current reference."]

    def import_path_hint(self) -> str:
        return str(Path("zd_data") / "exports" / "example_profile.json")

    def _baseline_button_mapping(self, input_id: str) -> ButtonMapping | None:
        if self._baseline_profile is None:
            return None
        return next((item for item in self._baseline_profile.button_mappings if item.physical_input_id == input_id), None)

    @staticmethod
    def _stick_diff_lines(label: str, before: StickSettings, after: StickSettings) -> list[str]:
        lines: list[str] = []
        before_dict = before.to_dict()
        after_dict = after.to_dict()
        for key, previous in before_dict.items():
            current = after_dict.get(key)
            if current != previous:
                friendly = key.replace("_", " ").title()
                lines.append(f"{label} {friendly}: {previous} -> {current}")
        return lines

    @staticmethod
    def _trigger_diff_lines(label: str, before: TriggerSettings, after: TriggerSettings) -> list[str]:
        lines: list[str] = []
        before_dict = before.to_dict()
        after_dict = after.to_dict()
        for key, previous in before_dict.items():
            current = after_dict.get(key)
            if current != previous:
                friendly = key.replace("_", " ").title()
                lines.append(f"{label} {friendly}: {previous} -> {current}")
        return lines
