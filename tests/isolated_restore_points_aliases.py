"""Native duplicate-ID build regression, kept out of shared-process discovery."""

import unittest

import dearpygui.dearpygui as dpg

from tests.test_restore_points_screen import _FakeService, _rp, _shell_with
from zd_app.ui.screens import restore_points as screen


class IsolatedRestorePointsAliasTests(unittest.TestCase):
    def test_copied_restore_point_ids_keep_unique_rendered_action_aliases(self) -> None:
        shell = _shell_with(_FakeService(valid=[_rp(id="copied-id"), _rp(id="copied-id")]))
        dpg.create_context()
        try:
            with dpg.window(tag="duplicate_restore_host"):
                pass
            screen.build(shell, "duplicate_restore_host")
            tags = [
                dpg.get_item_alias(item) for item in dpg.get_all_items()
                if dpg.get_item_alias(item).startswith("restore_points_row_action_")
            ]
            self.assertEqual(len(tags), 8)
            self.assertEqual(len(set(tags)), 8)
            for action in ("view", "restore", "export", "delete"):
                self.assertEqual(sum(tag.endswith("_" + action) for tag in tags), 2)
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
