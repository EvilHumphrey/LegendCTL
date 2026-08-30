"""Reachability must use the actual scrolling window, not a table alias."""

import unittest
from unittest.mock import patch

from tests import isolated_font_scale_proxy_common as render_common


class ScrollSurfaceTests(unittest.TestCase):
    def test_non_scrolling_table_does_not_own_host_window_scroll_range(self) -> None:
        with patch.object(render_common, "dpg") as dpg:
            dpg.get_item_parent.side_effect = {"button": "table", "table": "page"}.get
            dpg.get_item_type.side_effect = {
                "table": "mvAppItemType::mvTable",
                "page": "mvAppItemType::mvChildWindow",
            }.get
            dpg.get_item_configuration.return_value = {"scrollY": False}
            # DPG returns the host scroll range for both items, but the table
            # has no scroll viewport of its own.
            dpg.get_y_scroll_max.return_value = 180.0
            self.assertEqual(render_common._nearest_scroll_surface("button", "fallback"), "page")

    def test_explicitly_scrolling_table_keeps_its_own_scroll_range(self) -> None:
        with patch.object(render_common, "dpg") as dpg:
            dpg.get_item_parent.return_value = "table"
            dpg.get_item_type.return_value = "mvAppItemType::mvTable"
            dpg.get_item_configuration.return_value = {"scrollY": True}
            dpg.get_y_scroll_max.return_value = 180.0
            self.assertEqual(render_common._nearest_scroll_surface("button", "page"), "table")

    def test_clipped_control_in_real_scrolling_child_still_fails_reachability(self) -> None:
        with patch.object(render_common, "dpg") as dpg:
            dpg.does_item_exist.return_value = True
            dpg.get_item_parent.return_value = "card"
            dpg.get_item_type.return_value = "mvAppItemType::mvChildWindow"
            dpg.get_y_scroll_max.return_value = 180.0
            dpg.get_x_scroll.return_value = 0.0
            dpg.get_y_scroll.return_value = 0.0
            dpg.get_item_pos.return_value = (190, 20)
            dpg.get_item_rect_size.side_effect = {"button": (60, 30), "card": (200, 100)}.get
            case = render_common.MatrixCell("screens", 2.0, "en", 1180, 760)
            with self.assertRaisesRegex(AssertionError, "extends beyond.*horizontally"):
                render_common.assert_item_reachable(self, "button", "page", case=case, surface="control")


if __name__ == "__main__":
    unittest.main()
