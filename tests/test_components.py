"""Tests for the reusable layout components (card / section / metric).

Components are exercised against a real Dear PyGui context (the package shim in
``tests/__init__.py`` shares one native context across the run), mirroring the
screen tests. The card-surface theme is a module global, so every test resets
it before and after so registration in one test can't leak into another.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import dearpygui.dearpygui as dpg

from zd_app.i18n import set_locale
from zd_app.ui import components
from zd_app.ui.themes import COLORS


def _text_values() -> list[str]:
    out: list[str] = []
    for item in dpg.get_all_items():
        if dpg.get_item_type(item) == "mvAppItemType::mvText":
            value = dpg.get_value(item)
            if value is not None:
                out.append(value)
    return out


def _child_windows() -> list[int]:
    return [
        item
        for item in dpg.get_all_items()
        if dpg.get_item_type(item) == "mvAppItemType::mvChildWindow"
    ]


def _tables() -> list[int]:
    return [
        item
        for item in dpg.get_all_items()
        if dpg.get_item_type(item) == "mvAppItemType::mvTable"
    ]


def _table_columns() -> list[int]:
    return [
        item
        for item in dpg.get_all_items()
        if dpg.get_item_type(item) == "mvAppItemType::mvTableColumn"
    ]


def _buttons() -> list[int]:
    return [
        item
        for item in dpg.get_all_items()
        if dpg.get_item_type(item) == "mvAppItemType::mvButton"
    ]


def _color_ints(item: int) -> tuple[int, ...]:
    """Item color as 0-255 ints (Dear PyGui reports it as 0-1 floats)."""
    raw = dpg.get_item_configuration(item)["color"]
    return tuple(round(channel * 255) for channel in raw)


class ComponentTestBase(unittest.TestCase):
    def setUp(self) -> None:
        # All three component themes are module globals; reset every one before
        # and after so a registration in one test can't leak a (possibly stale)
        # id into another test — or, worse, into a later screen test whose
        # headless guard then tries to bind it. Mirrors the card-theme reset.
        for reset in (
            components.reset_card_theme,
            components.reset_table_theme,
            components.reset_destructive_theme,
        ):
            reset()
            self.addCleanup(reset)
        self.addCleanup(set_locale, "en")
        set_locale("en")
        dpg.create_context()
        self.addCleanup(dpg.destroy_context)


class MetricTests(ComponentTestBase):
    def test_metric_renders_label_and_value(self) -> None:
        with dpg.window():
            value_item = components.metric("Firmware", "1.24")

        values = _text_values()
        self.assertIn("Firmware", values)
        self.assertIn("1.24", values)
        self.assertEqual(dpg.get_value(value_item), "1.24")

    def test_metric_coerces_non_string_value(self) -> None:
        with dpg.window():
            value_item = components.metric("Pending", 0)
        self.assertEqual(dpg.get_value(value_item), "0")

    def test_metric_applies_value_tag(self) -> None:
        with dpg.window():
            components.metric("Active", "Config 1", value_tag="my_metric_value")
        self.assertTrue(dpg.does_item_exist("my_metric_value"))
        self.assertEqual(dpg.get_value("my_metric_value"), "Config 1")

    def test_metric_value_and_label_colors(self) -> None:
        accent = COLORS["accent.primary"]
        with dpg.window():
            value_item = components.metric(
                "Battery", "87%", value_color=accent, label_color=COLORS["text.muted"]
            )
        self.assertEqual(_color_ints(value_item), accent)

    def test_metric_default_value_color_is_primary(self) -> None:
        with dpg.window():
            value_item = components.metric("Label", "Value")
        self.assertEqual(_color_ints(value_item), COLORS["text.primary"])


class CardTests(ComponentTestBase):
    def test_card_creates_bordered_child_window_and_yields_id(self) -> None:
        with dpg.window():
            with components.card() as card_id:
                self.assertTrue(dpg.does_item_exist(card_id))
        cfg = dpg.get_item_configuration(card_id)
        self.assertTrue(cfg["border"])

    def test_card_default_width_fills_parent(self) -> None:
        with dpg.window():
            with components.card() as card_id:
                pass
        self.assertEqual(dpg.get_item_configuration(card_id)["width"], -1)

    def test_card_explicit_height_is_fixed(self) -> None:
        with dpg.window():
            with components.card(height=150) as card_id:
                pass
        self.assertEqual(dpg.get_item_configuration(card_id)["height"], 150)

    def test_card_negative_height_is_forwarded_as_fill_reserve(self) -> None:
        # A NEGATIVE height is the footer-reserve mode: the card fills the
        # parent's remaining height MINUS abs(height) (ImGui negative-size), so a
        # list/table card can be the single scroll surface while reserving space
        # for a pinned footer below it. The explicit height must be forwarded
        # verbatim and govern sizing — the legacy autosize_y fill stays OFF.
        with dpg.window():
            with components.card(height=-components.FOOTER_RESERVE_PX) as card_id:
                pass
        cfg = dpg.get_item_configuration(card_id)
        self.assertEqual(cfg["height"], -components.FOOTER_RESERVE_PX)
        self.assertFalse(
            cfg["autosize_y"],
            "an explicit (negative) height must suppress the legacy fill flag",
        )

    def test_card_fit_uses_auto_resize_y_not_legacy_fill(self) -> None:
        # fit=True is the DPG-2.x content-fit: a card that shrinks to its
        # content and can't clip. It must set auto_resize_y AND suppress the
        # legacy autosize_y (which FILLS the parent on DPG 2.x). This is the
        # contract the card-clip lane relies on across screens.
        with dpg.window():
            with components.card(fit=True) as card_id:
                pass
        cfg = dpg.get_item_configuration(card_id)
        self.assertTrue(cfg["auto_resize_y"], "fit=True must set auto_resize_y")
        self.assertFalse(cfg["autosize_y"], "fit=True must suppress legacy fill")

    def test_card_default_uses_legacy_autosize_y_fill(self) -> None:
        # The default (no height, no fit) keeps the legacy autosize_y fill so the
        # deliberate bounded-scroll lists (restore-points table, wear-ledger
        # event log) that depend on it are unchanged.
        with dpg.window():
            with components.card() as card_id:
                pass
        cfg = dpg.get_item_configuration(card_id)
        self.assertTrue(cfg["autosize_y"], "default card must keep legacy fill")
        self.assertFalse(cfg["auto_resize_y"], "default card is not content-fit")

    def test_card_content_nests_inside_the_panel(self) -> None:
        with dpg.window():
            with components.card() as card_id:
                inner = dpg.add_text("inside")
        self.assertEqual(dpg.get_item_parent(inner), card_id)

    def test_card_skips_theme_bind_when_unregistered(self) -> None:
        # Headless guard: with no registered theme the card still renders and
        # never calls bind_item_theme (mirrors typography's font guard).
        self.assertIsNone(components._CARD_THEME)
        with patch.object(components.dpg, "bind_item_theme") as mock_bind:
            with dpg.window():
                with components.card():
                    pass
        mock_bind.assert_not_called()

    def test_card_binds_registered_theme(self) -> None:
        theme_id = components.register_card_theme()
        self.assertEqual(components._CARD_THEME, theme_id)
        self.assertTrue(dpg.does_item_exist(theme_id))
        with patch.object(components.dpg, "bind_item_theme") as mock_bind:
            with dpg.window():
                with components.card() as card_id:
                    pass
        mock_bind.assert_called_once()
        self.assertEqual(mock_bind.call_args.args, (card_id, theme_id))

    def test_card_skips_bind_when_theme_id_is_stale(self) -> None:
        # The context shim deletes themes between tests; a dangling global must
        # not be bound (does_item_exist gate). Point at a non-existent id.
        components._CARD_THEME = 999_999
        self.assertFalse(dpg.does_item_exist(999_999))
        with patch.object(components.dpg, "bind_item_theme") as mock_bind:
            with dpg.window():
                with components.card():
                    pass
        mock_bind.assert_not_called()


class SectionTests(ComponentTestBase):
    def test_section_emits_title_and_yields_none_without_card(self) -> None:
        with patch("zd_app.ui.components.section_title") as mock_title:
            with dpg.window():
                with components.section("Connection") as ret:
                    self.assertIsNone(ret)
        mock_title.assert_called_once()
        self.assertEqual(mock_title.call_args.args[0], "Connection")

    def test_section_card_true_wraps_content_in_a_card(self) -> None:
        before = set(_child_windows())
        with patch("zd_app.ui.components.section_title"):
            with dpg.window():
                with components.section("Profile", card=True, height=120) as ret:
                    self.assertIsNotNone(ret)
        new_children = set(_child_windows()) - before
        self.assertIn(ret, new_children)
        self.assertEqual(dpg.get_item_configuration(ret)["height"], 120)

    def test_section_forwards_title_tag_to_real_helper(self) -> None:
        with dpg.window():
            with components.section("Heading", title_tag="my_section_title"):
                pass
        self.assertTrue(dpg.does_item_exist("my_section_title"))
        self.assertEqual(dpg.get_value("my_section_title"), "Heading")

    def test_section_suppresses_gap_spacer_when_gap_zero(self) -> None:
        with dpg.window():
            before = sum(
                1
                for i in dpg.get_all_items()
                if dpg.get_item_type(i) == "mvAppItemType::mvSpacer"
            )
            with components.section("NoGap", gap=0):
                pass
            after = sum(
                1
                for i in dpg.get_all_items()
                if dpg.get_item_type(i) == "mvAppItemType::mvSpacer"
            )
        self.assertEqual(after, before)

    def test_section_warns_when_sized_without_card(self) -> None:
        # width/height (and forwarded card kwargs) are silently dropped when
        # card=False — there's no container to size. The builder logs a
        # warning so the caller mistake surfaces instead of failing quietly.
        with patch("zd_app.ui.components.section_title"):
            with dpg.window():
                with self.assertLogs("zd_app.ui.components", level="WARNING") as cm:
                    with components.section("Sized", width=300):
                        pass
        self.assertTrue(
            any("card=True" in line for line in cm.output),
            f"Expected a width/height-without-card warning; got {cm.output}",
        )

    def test_section_does_not_warn_for_plain_inline_section(self) -> None:
        # The common case — an inline section with no sizing — must stay quiet.
        with patch("zd_app.ui.components.section_title"):
            with dpg.window():
                with self.assertNoLogs("zd_app.ui.components", level="WARNING"):
                    with components.section("Plain"):
                        pass

    def test_section_card_true_does_not_warn_when_sized(self) -> None:
        # Sizing is legitimate with card=True (it forwards to the card), so no
        # warning there.
        with patch("zd_app.ui.components.section_title"):
            with dpg.window():
                with self.assertNoLogs("zd_app.ui.components", level="WARNING"):
                    with components.section("Sized", card=True, height=120):
                        pass


class CardThemeRegistryTests(ComponentTestBase):
    def test_register_returns_live_theme_and_sets_global(self) -> None:
        theme_id = components.register_card_theme()
        self.assertIsNotNone(theme_id)
        self.assertEqual(components._CARD_THEME, theme_id)
        self.assertEqual(dpg.get_item_type(theme_id), "mvAppItemType::mvTheme")

    def test_reset_clears_global(self) -> None:
        components.register_card_theme()
        components.reset_card_theme()
        self.assertIsNone(components._CARD_THEME)


class TableTests(ComponentTestBase):
    def _column_labels(self) -> list[str]:
        return [dpg.get_item_configuration(c)["label"] for c in _table_columns()]

    def test_table_creates_a_table_widget_and_yields_id(self) -> None:
        with dpg.window():
            with components.table([components.Column("A"), components.Column("B")]) as tid:
                self.assertTrue(dpg.does_item_exist(tid))
        self.assertEqual(dpg.get_item_type(tid), "mvAppItemType::mvTable")

    def test_table_applies_standard_chrome_defaults(self) -> None:
        with dpg.window():
            with components.table([components.Column("A")]) as tid:
                pass
        cfg = dpg.get_item_configuration(tid)
        self.assertTrue(cfg["header_row"], "header row is part of the standard chrome")
        self.assertTrue(cfg["row_background"], "zebra rows are part of the standard chrome")
        self.assertTrue(cfg["borders_innerH"])
        self.assertTrue(cfg["borders_outerH"])
        self.assertFalse(cfg["borders_innerV"], "inner vertical borders stay off by default")

    def test_table_emits_one_column_per_spec_in_order(self) -> None:
        with dpg.window():
            with components.table(
                [components.Column("Name"), components.Column("State"), components.Column("When")]
            ):
                pass
        self.assertEqual(self._column_labels(), ["Name", "State", "When"])

    def test_table_accepts_bare_string_columns_as_label_shorthand(self) -> None:
        with dpg.window():
            with components.table(["X", "Y", "Z"]):
                pass
        self.assertEqual(self._column_labels(), ["X", "Y", "Z"])

    def test_table_applies_tag(self) -> None:
        with dpg.window():
            with components.table([components.Column("A")], tag="my_table"):
                pass
        self.assertTrue(dpg.does_item_exist("my_table"))

    def test_table_rows_nest_inside_the_table(self) -> None:
        with dpg.window():
            with components.table([components.Column("A")]) as tid:
                with dpg.table_row() as row:
                    dpg.add_text("cell")
        self.assertEqual(dpg.get_item_parent(row), tid)

    def test_table_forwards_resizable(self) -> None:
        with dpg.window():
            with components.table([components.Column("A")], resizable=True) as tid:
                pass
        self.assertTrue(dpg.get_item_configuration(tid)["resizable"])

    def test_table_kwargs_override_chrome_defaults(self) -> None:
        # A site can opt out of a default (e.g. drop the outer-H border) without
        # losing the rest of the standard chrome.
        with dpg.window():
            with components.table([components.Column("A")], borders_outerH=False) as tid:
                pass
        cfg = dpg.get_item_configuration(tid)
        self.assertFalse(cfg["borders_outerH"])
        self.assertTrue(cfg["row_background"], "other defaults still apply")

    def test_table_skips_theme_bind_when_unregistered(self) -> None:
        # Headless guard: with no registered table theme the table still renders
        # and never calls bind_item_theme (mirrors card()'s guard).
        self.assertIsNone(components._TABLE_THEME)
        with patch.object(components.dpg, "bind_item_theme") as mock_bind:
            with dpg.window():
                with components.table([components.Column("A")]):
                    pass
        mock_bind.assert_not_called()

    def test_table_binds_registered_theme(self) -> None:
        theme_id = components.register_table_theme()
        self.assertEqual(components._TABLE_THEME, theme_id)
        with patch.object(components.dpg, "bind_item_theme") as mock_bind:
            with dpg.window():
                with components.table([components.Column("A")]) as tid:
                    pass
        mock_bind.assert_called_once()
        self.assertEqual(mock_bind.call_args.args, (tid, theme_id))

    def test_table_skips_bind_when_theme_id_is_stale(self) -> None:
        components._TABLE_THEME = 999_999
        self.assertFalse(dpg.does_item_exist(999_999))
        with patch.object(components.dpg, "bind_item_theme") as mock_bind:
            with dpg.window():
                with components.table([components.Column("A")]):
                    pass
        mock_bind.assert_not_called()


class TableColumnTests(ComponentTestBase):
    def test_fixed_width_column_sets_width_fixed_and_no_resize(self) -> None:
        with dpg.window():
            with components.table(
                [components.Column("Actions", width=280, no_resize=True)]
            ):
                pass
        cfg = dpg.get_item_configuration(_table_columns()[0])
        self.assertTrue(cfg["width_fixed"], "a width= column must be fixed-width")
        self.assertTrue(cfg["no_resize"])
        self.assertEqual(cfg["init_width_or_weight"], 280)

    def test_weight_column_is_stretch_not_fixed(self) -> None:
        with dpg.window():
            with components.table([components.Column("Metric", weight=2.0)]):
                pass
        cfg = dpg.get_item_configuration(_table_columns()[0])
        self.assertFalse(cfg["width_fixed"], "a weight= column must stay stretchable")
        self.assertEqual(cfg["init_width_or_weight"], 2.0)

    def test_plain_column_has_no_fixed_width(self) -> None:
        with dpg.window():
            with components.table([components.Column("Name")]):
                pass
        cfg = dpg.get_item_configuration(_table_columns()[0])
        self.assertFalse(cfg["width_fixed"])


class RightCellTests(ComponentTestBase):
    def test_right_cell_renders_the_value(self) -> None:
        with dpg.window():
            with components.table([components.Column("V", numeric=True)]):
                with dpg.table_row():
                    item = components.right_cell(254)
        self.assertEqual(dpg.get_value(item), "254")

    def test_right_cell_defaults_to_primary_text_color(self) -> None:
        with dpg.window():
            with components.table([components.Column("V", numeric=True)]):
                with dpg.table_row():
                    item = components.right_cell("97.0%")
        self.assertEqual(_color_ints(item), COLORS["text.primary"])


class ActionButtonTests(ComponentTestBase):
    def test_action_button_returns_a_button(self) -> None:
        with dpg.window():
            item = components.action_button("View")
        self.assertEqual(dpg.get_item_type(item), "mvAppItemType::mvButton")

    def test_action_button_forwards_callback_and_user_data(self) -> None:
        sentinel = object()
        cb = lambda *a: None  # noqa: E731
        with dpg.window():
            item = components.action_button("Apply", user_data="Apex", callback=cb)
        self.assertEqual(dpg.get_item_user_data(item), "Apex")
        self.assertIs(dpg.get_item_callback(item), cb)

    def test_neutral_action_button_does_not_bind_a_theme(self) -> None:
        with patch.object(components.dpg, "bind_item_theme") as mock_bind:
            with dpg.window():
                components.action_button("View")
        mock_bind.assert_not_called()

    def test_destructive_action_button_binds_registered_theme(self) -> None:
        theme_id = components.register_destructive_theme()
        self.assertEqual(components._DESTRUCTIVE_THEME, theme_id)
        with patch.object(components.dpg, "bind_item_theme") as mock_bind:
            with dpg.window():
                item = components.action_button("Delete", destructive=True)
        mock_bind.assert_called_once()
        self.assertEqual(mock_bind.call_args.args, (item, theme_id))

    def test_destructive_action_button_skips_bind_when_unregistered(self) -> None:
        # Headless guard: destructive style is a no-op when the theme was never
        # registered (e.g. unit tests that skip _setup_theme).
        self.assertIsNone(components._DESTRUCTIVE_THEME)
        with patch.object(components.dpg, "bind_item_theme") as mock_bind:
            with dpg.window():
                components.action_button("Delete", destructive=True)
        mock_bind.assert_not_called()


class TableEmptyStateTests(ComponentTestBase):
    def test_empty_state_renders_the_message(self) -> None:
        with dpg.window():
            item = components.table_empty_state("No rows yet")
        self.assertEqual(dpg.get_value(item), "No rows yet")

    def test_empty_state_uses_muted_text(self) -> None:
        with dpg.window():
            item = components.table_empty_state("Nothing here")
        self.assertEqual(_color_ints(item), COLORS["text.muted"])

    def test_empty_state_applies_tag(self) -> None:
        with dpg.window():
            components.table_empty_state("Empty", tag="empty_tag")
        self.assertTrue(dpg.does_item_exist("empty_tag"))


class TableThemeRegistryTests(ComponentTestBase):
    def test_register_table_theme_returns_live_theme_and_sets_global(self) -> None:
        theme_id = components.register_table_theme()
        self.assertIsNotNone(theme_id)
        self.assertEqual(components._TABLE_THEME, theme_id)
        self.assertEqual(dpg.get_item_type(theme_id), "mvAppItemType::mvTheme")

    def test_reset_table_theme_clears_global(self) -> None:
        components.register_table_theme()
        components.reset_table_theme()
        self.assertIsNone(components._TABLE_THEME)

    def test_register_destructive_theme_returns_live_theme_and_sets_global(self) -> None:
        theme_id = components.register_destructive_theme()
        self.assertIsNotNone(theme_id)
        self.assertEqual(components._DESTRUCTIVE_THEME, theme_id)
        self.assertEqual(dpg.get_item_type(theme_id), "mvAppItemType::mvTheme")

    def test_reset_destructive_theme_clears_global(self) -> None:
        components.register_destructive_theme()
        components.reset_destructive_theme()
        self.assertIsNone(components._DESTRUCTIVE_THEME)


if __name__ == "__main__":
    unittest.main()
