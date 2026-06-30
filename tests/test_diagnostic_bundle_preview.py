"""Tests for the diagnostic-bundle preview modal."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import dearpygui.dearpygui as dpg

from zd_app import i18n
from zd_app.services.diagnostic_bundle import (
    DiagnosticBundlePreviewItem,
    DiagnosticBundlePreviewManifest,
)
from zd_app.ui import diagnostic_bundle_preview


def _sample_manifest() -> DiagnosticBundlePreviewManifest:
    return DiagnosticBundlePreviewManifest(
        items=(
            DiagnosticBundlePreviewItem(
                key="report",
                count=1,
                member_count=1,
                member_paths=("report.md",),
            ),
            DiagnosticBundlePreviewItem(
                key="module_passports",
                count=0,
                member_count=0,
                metadata={"active_count": 0, "archived_count": 0},
            ),
            DiagnosticBundlePreviewItem(
                key="health_reports",
                count=0,
                member_count=0,
                metadata={"markdown_count": 0, "json_count": 0},
            ),
            DiagnosticBundlePreviewItem(
                key="wear_ledger",
                count=1,
                member_count=1,
                member_paths=("wear_ledger_summary.json",),
                metadata={
                    "window_days": 90,
                    "event_count": 0,
                    "service_note_count": 0,
                },
            ),
        ),
        excluded_items=(
            DiagnosticBundlePreviewItem(
                key="raw_event_log",
                count=0,
                member_count=0,
            ),
            DiagnosticBundlePreviewItem(
                key="raw_app_logs",
                count=0,
                member_count=0,
            ),
        ),
        member_paths=("report.md", "wear_ledger_summary.json"),
    )


def _sample_shell() -> SimpleNamespace:
    return SimpleNamespace(
        COLORS={
            "accent": (10, 10, 10, 255),
            "muted": (20, 20, 20, 255),
            "text": (30, 30, 30, 255),
        }
    )


def _collect_text_values(root) -> list[str]:
    values: list[str] = []
    stack = [root]
    while stack:
        item = stack.pop()
        try:
            if dpg.get_item_type(item) == "mvAppItemType::mvText":
                values.append(str(dpg.get_value(item)))
            for slot in range(4):
                stack.extend(dpg.get_item_children(item, slot) or [])
        except SystemError:
            continue
    return values


def _parent_alias(item) -> str:
    return str(dpg.get_item_alias(dpg.get_item_parent(item)))


class DiagnosticBundlePreviewModalTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_modal_renders_honest_summary_and_privacy_posture(self) -> None:
        dpg.create_context()
        try:
            diagnostic_bundle_preview.open_preview_modal(
                _sample_shell(),
                _sample_manifest(),
                on_export=lambda: None,
            )
            self.assertTrue(
                dpg.does_item_exist(
                    diagnostic_bundle_preview.PREVIEW_MODAL_TAG
                )
            )
            text = "\n".join(
                _collect_text_values(
                    diagnostic_bundle_preview.PREVIEW_MODAL_TAG
                )
            )
            self.assertIn("Nothing is uploaded", text)
            self.assertIn("Usernames and full paths are scrubbed", text)
            self.assertNotIn("VID/PID", text)
            self.assertIn("Device & configuration summary (1)", text)
            self.assertIn("Module passports (0)", text)
            self.assertIn("Raw app/crash logs (0)", text)
            self.assertIn("Privacy:", text)
        finally:
            dpg.destroy_context()

    def test_modal_uses_centered_fixed_layout_with_pinned_actions(self) -> None:
        tag = diagnostic_bundle_preview.PREVIEW_MODAL_TAG
        child_tag = diagnostic_bundle_preview._modal_child_tag

        dpg.create_context()
        try:
            with (
                patch.object(
                    diagnostic_bundle_preview.dpg,
                    "get_viewport_client_width",
                    return_value=1200,
                ),
                patch.object(
                    diagnostic_bundle_preview.dpg,
                    "get_viewport_client_height",
                    return_value=800,
                ),
            ):
                diagnostic_bundle_preview.open_preview_modal(
                    _sample_shell(),
                    _sample_manifest(),
                    on_export=lambda: None,
                )

            modal_config = dpg.get_item_configuration(tag)
            self.assertEqual(
                modal_config["width"],
                diagnostic_bundle_preview.PREVIEW_MODAL_WIDTH,
            )
            self.assertEqual(
                modal_config["height"],
                diagnostic_bundle_preview.PREVIEW_MODAL_HEIGHT,
            )
            self.assertTrue(modal_config["no_scrollbar"])
            self.assertEqual(dpg.get_item_pos(tag), [170, 80])

            content_tag = child_tag(tag, "content_region")
            content_config = dpg.get_item_configuration(content_tag)
            self.assertEqual(
                content_config["height"],
                diagnostic_bundle_preview.PREVIEW_CONTENT_HEIGHT,
            )
            self.assertFalse(content_config["no_scrollbar"])

            self.assertEqual(_parent_alias(child_tag(tag, "summary")), tag)
            self.assertEqual(_parent_alias(content_tag), tag)
            self.assertEqual(_parent_alias(child_tag(tag, "actions")), tag)
            self.assertEqual(
                dpg.get_item_label(child_tag(tag, "export_button")),
                "Export",
            )
            self.assertEqual(
                dpg.get_item_label(child_tag(tag, "cancel_button")),
                "Cancel",
            )
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
