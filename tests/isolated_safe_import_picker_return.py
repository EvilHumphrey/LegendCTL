"""Real-render picker return regression; run only on the quiet desktop.

One viewport, no AppShell construction, device services, or real profile reads.
Callbacks are invoked directly after the picker is rendered; native input is
not injected. Visibility is checked after real frames, not inferred from show.
"""

from contextlib import contextmanager
import unittest
from unittest.mock import patch

import dearpygui.dearpygui as dpg

from tests.test_safe_import_picker_return import _Shell
from zd_app.ui.screens import safe_import


class SafeImportPickerRenderTests(unittest.TestCase):
    def test_picker_returns_and_reentry_stays_visible(self):
        dpg.create_context()
        try:
            shell = _Shell()
            dpg.create_viewport(title="Safe Import picker regression", width=900, height=760)
            dpg.setup_dearpygui()
            dpg.show_viewport()

            def frames(count=8):
                for _ in range(count):
                    shell._drain_deferred_ui_calls()
                    dpg.render_dearpygui_frame()

            def visible(tag):
                return bool(dpg.get_item_state(tag).get("visible"))

            safe_import.open_file_select(shell)
            frames()
            self.assertTrue(visible(safe_import.FILE_MODAL))
            dpg.set_value(safe_import.PATH_INPUT, "original.json")
            real_file_dialog = dpg.file_dialog
            captured = {}

            @contextmanager
            def capture_dialog(**kwargs):
                # DPG's configuration getter omits cancel_callback. Preserve
                # the actual constructor and capture its callback arguments.
                captured.clear()
                captured.update(kwargs)
                with real_file_dialog(**kwargs) as dialog:
                    yield dialog

            for action, expected in (
                ("cancel_callback", "original.json"),
                ("callback", "chosen.json"),
                ("cancel_callback", "chosen.json"),
            ):
                # Invoke the wired Browse button, so this also tests shell wiring.
                buttons = dpg.get_item_children(safe_import.FILE_MODAL, 1)
                group = buttons[-1]
                browse = dpg.get_item_children(group, 1)[0]
                with patch.object(dpg, "file_dialog", capture_dialog):
                    dpg.get_item_configuration(browse)["callback"]()
                frames()
                # File dialogs do not expose window-style visibility reliably;
                # the standing window's real state is the regression target.
                self.assertTrue(dpg.get_item_configuration(safe_import.FILE_DIALOG)["show"])
                self.assertFalse(visible(safe_import.FILE_MODAL))
                callback = captured.get(action)
                self.assertTrue(callable(callback), f"Missing picker {action}")
                callback(safe_import.FILE_DIALOG, {"file_path_name": "chosen.json"})
                frames()
                self.assertFalse(dpg.does_item_exist(safe_import.FILE_DIALOG))
                self.assertTrue(visible(safe_import.FILE_MODAL), f"Import form hidden after {action}")
                self.assertEqual(dpg.get_value(safe_import.PATH_INPUT), expected)

            # The window titlebar X hides its tag. Re-entry must still render.
            dpg.configure_item(safe_import.FILE_MODAL, show=False)
            frames()
            safe_import.open_file_select(shell)
            frames()
            self.assertTrue(visible(safe_import.FILE_MODAL))
            self.assertTrue(visible(safe_import.PATH_INPUT))
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
