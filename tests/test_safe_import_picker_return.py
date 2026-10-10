"""Safe Import picker lifecycle without a native context or device services."""

from contextlib import nullcontext
import queue
import unittest
from unittest.mock import MagicMock, patch

from zd_app.ui import app_shell
from zd_app.ui.screens import safe_import


class _Shell:
    COLORS = {"muted": (0, 0, 0), "error": (255, 0, 0)}
    _defer_ui_call = app_shell.AppShell._defer_ui_call
    _drain_deferred_ui_calls = app_shell.AppShell._drain_deferred_ui_calls
    _defer_modal_swap = app_shell.AppShell._defer_modal_swap

    def __init__(self):
        self._defer_ui_armed = True
        self._deferred_ui_calls = queue.Queue()
        self._pending_modal_swap_keys = set()
        self.current_screen = "controller"


class SafeImportPickerReturnTests(unittest.TestCase):
    def setUp(self):
        self.shell = _Shell()
        self.items = {}
        self.ids = {}
        self.next_id = 1
        self.buttons = []
        self.fake_dpg = MagicMock()
        self.fake_dpg.window.side_effect = self._window
        self.fake_dpg.group.side_effect = lambda **kw: nullcontext()
        self.fake_dpg.file_dialog.side_effect = self._file_dialog
        self.fake_dpg.add_input_text.side_effect = self._input
        self.fake_dpg.add_button.side_effect = lambda **kw: self.buttons.append(kw)
        self.fake_dpg.does_item_exist.side_effect = lambda tag: self._tag(tag) in self.items
        self.fake_dpg.get_alias_id.side_effect = lambda tag: self.ids[tag]
        self.fake_dpg.delete_item.side_effect = self._delete
        self.fake_dpg.configure_item.side_effect = lambda tag, **kw: self.items[self._tag(tag)].update(kw)
        self.fake_dpg.set_value.side_effect = lambda tag, value: self.items[tag].update(value=value)
        for module in (safe_import, app_shell):
            patcher = patch.object(module, "dpg", self.fake_dpg)
            patcher.start()
            self.addCleanup(patcher.stop)
        safe_import.open_file_select(self.shell)
        self.shell._drain_deferred_ui_calls()
        self.shell._drain_deferred_ui_calls()
        self.items[safe_import.PATH_INPUT]["value"] = "original.json"

    def _window(self, **kwargs):
        self.ids[kwargs["tag"]] = self.next_id
        self.next_id += 1
        self.items[kwargs["tag"]] = {"show": True, **kwargs}
        return nullcontext()

    def _tag(self, tag):
        if isinstance(tag, int):
            return next((name for name, ident in self.ids.items() if ident == tag), None)
        return tag

    def _input(self, **kwargs):
        self.items[kwargs["tag"]] = {"value": kwargs.get("default_value", "")}

    def _file_dialog(self, **kwargs):
        # Model the observed DPG behavior: the picker hides the standing modal.
        self.items[safe_import.FILE_MODAL]["show"] = False
        self.ids[kwargs["tag"]] = self.next_id
        self.next_id += 1
        self.items[kwargs["tag"]] = kwargs
        return nullcontext(self.ids[kwargs["tag"]])

    def _delete(self, tag):
        tag = self._tag(tag)
        self.items.pop(tag, None)
        self.ids.pop(tag, None)
        if tag == safe_import.FILE_MODAL:
            self.items.pop(safe_import.PATH_INPUT, None)

    def _browse(self):
        button = next(button for button in self.buttons if button["label"] == safe_import.t("safe_import.file_select.browse"))
        button["callback"]()
        return self.items[safe_import.FILE_DIALOG]

    def _finish(self):
        self.shell._drain_deferred_ui_calls()
        self.assertNotIn(safe_import.FILE_DIALOG, self.items)
        self.assertFalse(self.items[safe_import.FILE_MODAL]["show"])
        self.shell._drain_deferred_ui_calls()
        self.assertTrue(self.items[safe_import.FILE_MODAL]["show"])

    def test_picking_preserves_selected_path_and_restores_after_frame_gap(self):
        dialog = self._browse()
        dialog["callback"](safe_import.FILE_DIALOG, {"file_path_name": "chosen.json"})
        self.assertEqual(self.items[safe_import.PATH_INPUT]["value"], "chosen.json")
        self.assertFalse(self.items[safe_import.FILE_MODAL]["show"])
        self._finish()

    def test_cancel_preserves_typed_path_and_restores_after_frame_gap(self):
        dialog = self._browse()
        self.assertTrue(callable(dialog.get("cancel_callback")), "Browse must restore the import flow on cancel")
        dialog["cancel_callback"](safe_import.FILE_DIALOG, {})
        self._finish()
        self.assertEqual(self.items[safe_import.PATH_INPUT]["value"], "original.json")

    def test_late_picker_exit_does_not_recreate_closed_import(self):
        dialog = self._browse()
        dialog["callback"](safe_import.FILE_DIALOG, {})
        self._delete(safe_import.FILE_MODAL)
        self.shell._drain_deferred_ui_calls()
        self.shell._drain_deferred_ui_calls()
        self.assertNotIn(safe_import.FILE_MODAL, self.items)
        self.assertNotIn(safe_import.PATH_INPUT, self.items)
        self.assertNotIn(safe_import.FILE_DIALOG, self.items)

    def test_duplicate_picker_exit_coalesces(self):
        dialog = self._browse()
        callback = dialog.get("cancel_callback")
        self.assertTrue(callable(callback), "Browse needs a cancel return path")
        callback()
        callback()
        self.assertEqual(self.shell._deferred_ui_calls.qsize(), 1)
        self._finish()

    def test_reopen_splits_existing_modal_teardown_and_create(self):
        previous = self.items[safe_import.FILE_MODAL]
        previous["show"] = False
        safe_import.open_file_select(self.shell)
        self.assertIs(self.items[safe_import.FILE_MODAL], previous)
        self.shell._drain_deferred_ui_calls()
        self.assertNotIn(safe_import.FILE_MODAL, self.items)
        self.shell._drain_deferred_ui_calls()
        self.assertTrue(self.items[safe_import.FILE_MODAL]["show"])

    def test_reopen_abandoned_if_screen_changes(self):
        safe_import.open_file_select(self.shell)
        self.shell.current_screen = "home"
        self.shell._drain_deferred_ui_calls()
        self.shell._drain_deferred_ui_calls()
        self.assertNotIn(safe_import.FILE_MODAL, self.items)

    def test_late_picker_selection_does_not_change_replacement_form(self):
        dialog = self._browse()
        self._delete(safe_import.FILE_MODAL)
        safe_import.open_file_select(self.shell)
        self.items[safe_import.PATH_INPUT]["value"] = "new-flow.json"
        self.items[safe_import.FILE_MODAL]["show"] = False
        dialog["callback"](safe_import.FILE_DIALOG, {"file_path_name": "old-flow.json"})
        self.shell._drain_deferred_ui_calls()
        self.shell._drain_deferred_ui_calls()
        self.assertEqual(self.items[safe_import.PATH_INPUT]["value"], "new-flow.json")
        self.assertFalse(self.items[safe_import.FILE_MODAL]["show"])

    def test_reopen_during_teardown_gap_coalesces(self):
        safe_import.open_file_select(self.shell)
        self.shell._drain_deferred_ui_calls()
        safe_import.open_file_select(self.shell)
        self.assertNotIn(safe_import.FILE_MODAL, self.items)
        self.shell._drain_deferred_ui_calls()
        self.assertTrue(self.items[safe_import.FILE_MODAL]["show"])
        self.assertEqual(self.shell._deferred_ui_calls.qsize(), 0)

    def test_old_picker_callback_does_not_close_replacement_picker(self):
        old_dialog = self._browse()
        new_dialog = self._browse()
        old_dialog["callback"](safe_import.FILE_DIALOG, {"file_path_name": "old-flow.json"})
        self.shell._drain_deferred_ui_calls()
        self.shell._drain_deferred_ui_calls()
        self.assertIs(self.items[safe_import.FILE_DIALOG], new_dialog)
        self.assertEqual(self.items[safe_import.PATH_INPUT]["value"], "original.json")

    def test_shell_without_seam_restores_inline(self):
        safe_import.open_browse()
        dialog = self.items[safe_import.FILE_DIALOG]
        dialog["callback"](safe_import.FILE_DIALOG, {"file_path_name": "chosen.json"})
        self.assertNotIn(safe_import.FILE_DIALOG, self.items)
        self.assertTrue(self.items[safe_import.FILE_MODAL]["show"])
        self.assertEqual(self.items[safe_import.PATH_INPUT]["value"], "chosen.json")


if __name__ == "__main__":
    unittest.main()
