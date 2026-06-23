"""Tests for the crash-review modal.

Exercises ``AppShell._show_crash_review_modal_if_any`` against a real DPG
context. Crash files are seeded into a temp ``user_data_dir/crashes`` and
the modal's button callbacks are invoked directly.
"""

from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n
from zd_app.i18n import t
from zd_app.models import AppSettings
from zd_app.services import crash_reporter


class CrashReviewModalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.user_data_dir = Path(self._tmp.name)
        (self.user_data_dir / "crashes").mkdir(parents=True, exist_ok=True)
        crash_reporter._reset_for_tests()
        i18n.set_locale("en")

    def tearDown(self) -> None:
        crash_reporter._reset_for_tests()
        self._tmp.cleanup()
        i18n.set_locale("en")

    def _make_shell_with_user_data_dir(self, settings: AppSettings | None = None):
        shell = make_shell(settings_service=mock.MagicMock(), settings=settings)
        # Replace settings_store.path so the modal uses our temp crashes dir.
        shell.settings_store = mock.MagicMock()
        shell.settings_store.path = self.user_data_dir / "settings.json"
        shell.settings_store.save = mock.MagicMock()
        shell.settings = settings or AppSettings()
        shell._dpg_context_ready = True
        return shell

    def _seed_crash(self, name: str, body: str = "test crash report") -> Path:
        path = self.user_data_dir / "crashes" / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_modal_not_built_when_no_unread_crashes(self) -> None:
        dpg.create_context()
        try:
            shell = self._make_shell_with_user_data_dir()
            shell._show_crash_review_modal_if_any()
            self.assertFalse(dpg.does_item_exist("crash_review_modal"))
        finally:
            dpg.destroy_context()

    def test_modal_built_when_unread_crashes_exist(self) -> None:
        self._seed_crash("20260509T120000Z.txt", body="boom traceback")
        dpg.create_context()
        try:
            shell = self._make_shell_with_user_data_dir()
            shell._show_crash_review_modal_if_any()
            self.assertTrue(dpg.does_item_exist("crash_review_modal"))
            preview = dpg.get_value("crash_review_preview")
            self.assertIn("boom traceback", preview)
        finally:
            dpg.destroy_context()

    def test_dismiss_button_marks_all_reviewed_and_saves_timestamp(self) -> None:
        report = self._seed_crash("20260509T130000Z.txt")
        dpg.create_context()
        try:
            shell = self._make_shell_with_user_data_dir()
            shell._show_crash_review_modal_if_any()
            self.assertTrue(dpg.does_item_exist("crash_review_modal"))

            # Invoke the dismiss callback directly.
            dpg.get_item_configuration("crash_review_dismiss_button")["callback"]()

            self.assertFalse(dpg.does_item_exist("crash_review_modal"))
            self.assertTrue((report.parent / "20260509T130000Z.txt.reviewed").exists())
            self.assertIsNotNone(shell.settings.last_reviewed_crash_timestamp)
            shell.settings_store.save.assert_called_once_with(shell.settings)
        finally:
            dpg.destroy_context()

    def test_save_button_invokes_startfile_then_marks_reviewed(self) -> None:
        report = self._seed_crash("20260509T140000Z.txt")
        dpg.create_context()
        try:
            shell = self._make_shell_with_user_data_dir()
            with mock.patch("zd_app.ui.app_shell.os.startfile", create=True) as startfile:
                shell._show_crash_review_modal_if_any()
                dpg.get_item_configuration("crash_review_save_button")["callback"]()

            startfile.assert_called_once()
            opened_path = Path(startfile.call_args.args[0])
            self.assertEqual(opened_path, self.user_data_dir / "crashes")
            self.assertTrue((report.parent / "20260509T140000Z.txt.reviewed").exists())
        finally:
            dpg.destroy_context()

    def test_github_and_send_buttons_disabled(self) -> None:
        self._seed_crash("20260509T150000Z.txt")
        dpg.create_context()
        try:
            shell = self._make_shell_with_user_data_dir()
            shell._show_crash_review_modal_if_any()
            self.assertFalse(
                dpg.get_item_configuration("crash_review_github_button")["enabled"]
            )
            self.assertFalse(
                dpg.get_item_configuration("crash_review_send_button")["enabled"]
            )
        finally:
            dpg.destroy_context()

    def test_modal_shows_more_count_for_multiple_unread(self) -> None:
        self._seed_crash("20260509T160000Z.txt")
        self._seed_crash("20260509T160500Z.txt")
        self._seed_crash("20260509T160930Z.txt")
        dpg.create_context()
        try:
            shell = self._make_shell_with_user_data_dir()
            shell._show_crash_review_modal_if_any()
            self.assertTrue(dpg.does_item_exist("crash_review_modal"))
            # Walk the modal's descendants and collect text widget values.
            text_values: list[str] = []

            def _walk(item_id) -> None:
                if dpg.get_item_type(item_id) == "mvAppItemType::mvText":
                    value = dpg.get_value(item_id)
                    if value:
                        text_values.append(str(value))
                slot_children = dpg.get_item_children(item_id) or {}
                for slot, ids in (slot_children.items() if isinstance(slot_children, dict) else []):
                    for nested in ids or []:
                        _walk(nested)

            _walk("crash_review_modal")
            expected_label = t("crash.review.preview_more").format(n=2)
            self.assertTrue(
                any(expected_label in v for v in text_values),
                f"expected {expected_label!r} in modal text widgets: {text_values}",
            )
        finally:
            dpg.destroy_context()

    def test_modal_body_includes_full_date_not_just_time(self) -> None:
        """Body shows YYYY-MM-DD HH:MM, not just HH:MM, so a
        crash from yesterday isn't ambiguous with one from this morning."""
        self._seed_crash("20260509T170000Z.txt", body="boom")
        dpg.create_context()
        try:
            shell = self._make_shell_with_user_data_dir()
            shell._show_crash_review_modal_if_any()
            self.assertTrue(dpg.does_item_exist("crash_review_modal"))
            # Walk descendants and find the body text — it's the first text
            # widget directly under the window (before the input_text preview).
            children = dpg.get_item_children("crash_review_modal", 1) or []
            body_text: str | None = None
            for child in children:
                if dpg.get_item_type(child) == "mvAppItemType::mvText":
                    body_text = dpg.get_value(child)
                    break
            self.assertIsNotNone(body_text, "body text widget not found")
            # The mtime of the seeded file ≈ now, so today's YYYY-MM-DD should
            # appear in the formatted body. Match the YYYY-MM-DD HH:MM pattern.
            import re
            self.assertRegex(
                body_text or "",
                r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}",
                "expected YYYY-MM-DD HH:MM date format in modal body",
            )
        finally:
            dpg.destroy_context()

    def test_modal_x_close_invokes_mark_all_reviewed(self) -> None:
        """Title-bar X close should behave identically to
        Dismiss — write .reviewed markers + persist last_reviewed_crash_timestamp."""
        report = self._seed_crash("20260509T180000Z.txt")
        dpg.create_context()
        try:
            shell = self._make_shell_with_user_data_dir()
            shell._show_crash_review_modal_if_any()
            self.assertTrue(dpg.does_item_exist("crash_review_modal"))

            on_close = dpg.get_item_configuration("crash_review_modal")["on_close"]
            self.assertTrue(callable(on_close), "on_close handler must be set on the modal window")
            on_close()

            self.assertTrue(
                (report.parent / "20260509T180000Z.txt.reviewed").exists(),
                ".reviewed marker should exist after X-close",
            )
            self.assertIsNotNone(shell.settings.last_reviewed_crash_timestamp)
            shell.settings_store.save.assert_called_once_with(shell.settings)
        finally:
            dpg.destroy_context()

    def test_since_filter_excludes_old_crashes(self) -> None:
        old = self._seed_crash("20260101T000000Z.txt")
        # Backdate so its mtime predates the threshold.
        import os
        old_mtime = (datetime.datetime.now() - datetime.timedelta(days=30)).timestamp()
        os.utime(old, (old_mtime, old_mtime))

        threshold = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        settings = AppSettings(
            last_reviewed_crash_timestamp=threshold.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        dpg.create_context()
        try:
            shell = self._make_shell_with_user_data_dir(settings=settings)
            shell._show_crash_review_modal_if_any()
            # Old crash predates the threshold — modal should not appear.
            self.assertFalse(dpg.does_item_exist("crash_review_modal"))
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
