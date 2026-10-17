"""Safe Import picker lifecycle without a native context or device services."""

import ast
from contextlib import nullcontext
from pathlib import Path
import queue
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

from zd_app.ui import app_shell
from zd_app.ui.screens import safe_import


class _Shell:
    """A light stand-in that runs the REAL seam, arbiter included.

    Every seam method is the AppShell function itself, not a stub, so these
    tests exercise the real pacing, coalescing and create-slot arbitration.
    The instance state below mirrors what ``AppShell.__init__`` sets up for
    the seam; ``FakeShellContractTests`` fails if the seam starts reading an
    attribute this fake does not carry.
    """

    COLORS = {"muted": (0, 0, 0), "error": (255, 0, 0)}
    SCREEN_ALIASES = app_shell.AppShell.SCREEN_ALIASES
    _canonical_screen_id = app_shell.AppShell._canonical_screen_id
    _defer_ui_call = app_shell.AppShell._defer_ui_call
    _drain_deferred_ui_calls = app_shell.AppShell._drain_deferred_ui_calls
    _defer_modal_swap = app_shell.AppShell._defer_modal_swap
    _release_modal_swap_slot = app_shell.AppShell._release_modal_swap_slot
    _cancel_screen_owned_modal_swap = app_shell.AppShell._cancel_screen_owned_modal_swap
    _run_modal_swap_teardown = staticmethod(app_shell.AppShell._run_modal_swap_teardown)

    def __init__(self):
        self._defer_ui_armed = True
        self._deferred_ui_calls = queue.Queue()
        self._pending_modal_swap_keys = set()
        self._modal_swap_lock = threading.Lock()
        self._modal_swap_generation = 0
        self._modal_swap_pending = False
        self._modal_swap_teardown_done = False
        self._modal_swap_owner_screen = None
        self.current_screen = "controller"


def _code_names(code: types.CodeType) -> set[str]:
    """Every attribute/global name a function's code reads, closures included."""

    names = set(code.co_names)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _code_names(const)
    return names


class FakeShellContractTests(unittest.TestCase):
    """The fake must carry everything the real seam touches.

    The previous version of ``_Shell`` bound ``_defer_modal_swap`` but none of
    the create-slot arbiter's state, and every seam call died with
    ``AttributeError: _modal_swap_lock``. Rather than hand-maintaining a list,
    walk the code of every AppShell function the fake binds, collect each name
    that is also an attribute of a real AppShell, and require the fake to have
    it. Methods found that way are walked too, so a helper the seam starts
    calling later is caught as well.

    Scope, stated so nobody over-reads it: the walk sees direct attribute reads
    (``co_names``). It does NOT see a string ``getattr(self, "x", default)``,
    and it skips names a real shell only sets after ``__init__``. The seam uses
    no string getattr on ``self`` today. The screen does --
    ``safe_import.open_file_select`` reads shell state with a defaulted
    ``getattr`` -- and a default there would quietly turn a check off in these
    tests, so those literals get their own test below.
    """

    def test_fake_carries_every_shell_attribute_the_seam_reads(self):
        from tests.r2_shell_test_helpers import make_shell

        real = make_shell()
        fake = _Shell()

        def _function(value):
            return value.__func__ if isinstance(value, staticmethod) else value

        # The AppShell functions the fake borrows verbatim (its own __init__
        # is not one of them).
        bound = {
            name
            for name, value in vars(_Shell).items()
            if isinstance(value, (types.FunctionType, staticmethod))
            and _function(value) is _function(vars(app_shell.AppShell).get(name))
        }
        pending = sorted(bound)
        seen: set[str] = set()
        missing: list[str] = []
        while pending:
            method_name = pending.pop()
            if method_name in seen:
                continue
            seen.add(method_name)
            function = vars(app_shell.AppShell)[method_name]
            if isinstance(function, staticmethod):
                function = function.__func__
            for name in sorted(_code_names(function.__code__)):
                if not hasattr(real, name):
                    continue
                if not hasattr(fake, name):
                    missing.append(f"{method_name} reads self.{name}")
                    continue
                if isinstance(
                    vars(app_shell.AppShell).get(name), (types.FunctionType, staticmethod)
                ):
                    pending.append(name)
        self.assertEqual(missing, [], "the picker fake no longer carries the seam's contract")
        # Anti-vacuity: the walk really reached the arbiter through the seam.
        self.assertIn("_defer_modal_swap", seen)
        self.assertIn("_release_modal_swap_slot", seen, "the walk must reach the arbiter")

        # State the fake carries must also have the real shell's types, so a
        # stand-in such as a dict for the lock or a list for the key set
        # cannot pass by name alone.
        for name in vars(fake):
            if hasattr(real, name):
                self.assertIs(
                    type(getattr(fake, name)),
                    type(getattr(real, name)),
                    f"_Shell.{name} has a different type from AppShell.{name}",
                )

    def test_fake_carries_every_shell_attribute_the_screen_reads_by_name(self):
        from tests.r2_shell_test_helpers import make_shell

        real = make_shell()
        fake = _Shell()
        # Only the functions these tests drive; the rest of the screen reads
        # Safe Import result state the picker fake has no reason to carry.
        tree = ast.parse(Path(safe_import.__file__).read_text(encoding="utf-8"))
        driven = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {"open_file_select", "open_browse"}
        ]
        self.assertEqual(len(driven), 2)
        literals = sorted(
            {
                node.args[1].value
                for function in driven
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "shell"
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            }
        )
        # Anti-vacuity: these are the reads the slot check depends on.
        self.assertIn("_modal_swap_pending", literals)
        self.assertIn("_pending_modal_swap_keys", literals)
        missing = [
            name for name in literals if hasattr(real, name) and not hasattr(fake, name)
        ]
        self.assertEqual(
            missing, [], "the picker fake lacks shell state safe_import reads by name"
        )


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
