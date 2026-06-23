"""Tests for the About screen."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n
from zd_app.ui.screens import about


def _build_in_fresh_context(shell) -> None:
    with dpg.window():
        with dpg.child_window(tag="content_region"):
            pass
    about.build(shell, "content_region")


class AboutScreenRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_about_renders_without_dpg_errors(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        # Force URLs ON for this render test so we can assert all three
        # action-row buttons exist. URL-hiding behavior is exercised in
        # AboutUrlButtonTests below.
        with patch.object(about, "REPO_URL", "https://example.com/repo"), \
             patch.object(about, "ISSUE_URL", "https://example.com/issues"):
            dpg.create_context()
            try:
                _build_in_fresh_context(shell)

                self.assertTrue(dpg.does_item_exist("about_app_name"))
                self.assertTrue(dpg.does_item_exist("about_tagline"))
                self.assertTrue(dpg.does_item_exist("about_disclaimer"))
                self.assertTrue(dpg.does_item_exist("about_footer_copyright"))
                self.assertTrue(dpg.does_item_exist("about_btn_repo"))
                self.assertTrue(dpg.does_item_exist("about_btn_licenses"))
                self.assertTrue(dpg.does_item_exist("about_btn_issue"))
            finally:
                dpg.destroy_context()

    def test_version_line_includes_build_date_when_set(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        with patch.object(about, "__build_date__", "2026-05-22"):
            dpg.create_context()
            try:
                _build_in_fresh_context(shell)
                line = dpg.get_value("about_version_line")
                self.assertIn("2026-05-22", line)
                self.assertIn(i18n.t("about.build_date_label"), line)
            finally:
                dpg.destroy_context()

    def test_version_line_omits_build_date_when_empty(self) -> None:
        # Source checkouts leave __build_date__ == "" — no trailing date label.
        shell = make_shell(settings_service=MagicMock())
        with patch.object(about, "__build_date__", ""):
            dpg.create_context()
            try:
                _build_in_fresh_context(shell)
                line = dpg.get_value("about_version_line")
                self.assertNotIn(i18n.t("about.build_date_label"), line)
            finally:
                dpg.destroy_context()


class AboutLicenseModalTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_license_modal_opens_on_button_click(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertFalse(dpg.does_item_exist("about_license_modal"))

            licenses_callback = dpg.get_item_callback("about_btn_licenses")
            licenses_callback()

            self.assertTrue(dpg.does_item_exist("about_license_modal"))
        finally:
            dpg.destroy_context()

    def test_license_modal_closes_via_close_button(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            about._open_license_view(shell)
            self.assertTrue(dpg.does_item_exist("about_license_modal"))

            close_callback = dpg.get_item_callback("about_license_close_btn")
            close_callback()

            self.assertFalse(dpg.does_item_exist("about_license_modal"))
        finally:
            dpg.destroy_context()

    def test_license_modal_lists_all_bundled_deps(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            about._open_license_view(shell)

            for slug, _spdx, _header_key in about.LICENSE_DEPS:
                section_tag = f"about_license_section_{slug.replace('-', '_')}"
                self.assertTrue(
                    dpg.does_item_exist(section_tag),
                    f"Expected modal section tag {section_tag} to exist",
                )
        finally:
            dpg.destroy_context()

    def test_license_files_exist_for_every_bundled_dep(self) -> None:
        licenses_dir = Path("assets/licenses")
        for slug, _spdx, _header_key in about.LICENSE_DEPS:
            path = licenses_dir / f"{slug}.txt"
            self.assertTrue(path.exists(), f"Missing license file: {path}")
            self.assertGreater(path.stat().st_size, 100, f"License file too small: {path}")


class AboutUrlButtonTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_repo_button_hidden_when_url_none(self) -> None:
        # Force REPO_URL=None: the Repository button should not render at all
        # when no URL is configured.
        shell = make_shell(settings_service=MagicMock())
        with patch.object(about, "REPO_URL", None):
            dpg.create_context()
            try:
                with dpg.window():
                    with dpg.child_window(tag="content_region"):
                        pass
                about.build(shell, "content_region")
                self.assertFalse(dpg.does_item_exist("about_btn_repo"))
            finally:
                dpg.destroy_context()

    def test_issue_button_hidden_when_url_none(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        with patch.object(about, "ISSUE_URL", None):
            dpg.create_context()
            try:
                with dpg.window():
                    with dpg.child_window(tag="content_region"):
                        pass
                about.build(shell, "content_region")
                self.assertFalse(dpg.does_item_exist("about_btn_issue"))
            finally:
                dpg.destroy_context()

    def test_licenses_button_always_visible(self) -> None:
        # The Licenses button opens the in-app modal, not a URL — it must
        # remain visible regardless of REPO_URL / ISSUE_URL state.
        shell = make_shell(settings_service=MagicMock())
        with patch.object(about, "REPO_URL", None), patch.object(about, "ISSUE_URL", None):
            dpg.create_context()
            try:
                with dpg.window():
                    with dpg.child_window(tag="content_region"):
                        pass
                about.build(shell, "content_region")
                self.assertTrue(dpg.does_item_exist("about_btn_licenses"))
            finally:
                dpg.destroy_context()

    def test_repo_button_visible_when_url_set(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        with patch.object(about, "REPO_URL", "https://example.com/repo"):
            dpg.create_context()
            try:
                with dpg.window():
                    with dpg.child_window(tag="content_region"):
                        pass
                about.build(shell, "content_region")
                self.assertTrue(dpg.does_item_exist("about_btn_repo"))
            finally:
                dpg.destroy_context()

    def test_open_repo_url_no_ops_when_url_none(self) -> None:
        # _open_repo_url must guard against the placeholder None state so a
        # stale callback (e.g. from cached widget state) doesn't open about:blank.
        with patch.object(about, "REPO_URL", None), patch.object(about, "webbrowser") as mock_webbrowser:
            about._open_repo_url()
            mock_webbrowser.open.assert_not_called()

    def test_open_issue_url_no_ops_when_url_none(self) -> None:
        with patch.object(about, "ISSUE_URL", None), patch.object(about, "webbrowser") as mock_webbrowser:
            about._open_issue_url()
            mock_webbrowser.open.assert_not_called()

    def test_open_url_invokes_webbrowser_when_url_set(self) -> None:
        with patch.object(about, "REPO_URL", "https://example.com/repo"), \
             patch.object(about, "ISSUE_URL", "https://example.com/issues"), \
             patch.object(about, "webbrowser") as mock_webbrowser:
            about._open_repo_url()
            about._open_issue_url()
            self.assertEqual(mock_webbrowser.open.call_count, 2)


class AboutI18nTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_about_i18n_parity_for_about_namespace(self) -> None:
        locale_dir = Path("zd_app/i18n/locales")
        en = json.loads((locale_dir / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((locale_dir / "zh-CN.json").read_text(encoding="utf-8"))

        en_about = {k for k in en if k.startswith("about.")}
        zh_about = {k for k in zh if k.startswith("about.")}

        self.assertEqual(en_about, zh_about)
        self.assertGreater(len(en_about), 10, "Expected at least the new about.* keys to be present")

    def test_about_zhcn_no_mojibake(self) -> None:
        locale_dir = Path("zd_app/i18n/locales")
        zh = json.loads((locale_dir / "zh-CN.json").read_text(encoding="utf-8"))

        for key, value in zh.items():
            if not key.startswith("about."):
                continue
            with self.subTest(key=key):
                self.assertNotIn("????", value, f"about.* tombstone in zh-CN: {key} = {value!r}")
                self.assertNotIn("?????", value, f"about.* tombstone in zh-CN: {key} = {value!r}")

    def test_about_disclaimer_renders_in_both_locales(self) -> None:
        shell = make_shell(settings_service=MagicMock())

        i18n.set_locale("en")
        expected_en = i18n.t("about.disclaimer")
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertEqual(dpg.get_value("about_disclaimer"), expected_en)
        finally:
            dpg.destroy_context()

        i18n.set_locale("zh-CN")
        expected_zh = i18n.t("about.disclaimer")
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertEqual(dpg.get_value("about_disclaimer"), expected_zh)
        finally:
            dpg.destroy_context()

        self.assertNotEqual(expected_en, expected_zh)
        self.assertNotIn("????", expected_zh)


# The ZD-required unaffiliated / warranty disclaimer, verbatim. ZD approved the
# public release on condition this exact wording appears in the app UI; it is a
# legal denial (same category as the whitelisted boundary.py claim-boundary
# paragraphs) and must render word-for-word. Pin it here so a future paraphrase
# of the i18n value fails the build.
ZD_DISCLAIMER_VERBATIM = (
    "This software is not developed or endorsed by ZD Gaming. Use at your own "
    "risk, and any controller issue caused by using this tool is not covered "
    "under the official warranty."
)


class AboutZdDisclaimerTests(unittest.TestCase):
    """The ZD-required disclaimer renders verbatim on the About surface."""

    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def tearDown(self) -> None:
        i18n.set_locale("en")

    def test_en_disclaimer_value_is_verbatim(self) -> None:
        self.assertEqual(i18n.t("about.zd_disclaimer"), ZD_DISCLAIMER_VERBATIM)

    def test_disclaimer_renders_verbatim_on_about_surface(self) -> None:
        shell = make_shell(settings_service=MagicMock())
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertTrue(dpg.does_item_exist("about_zd_disclaimer"))
            self.assertEqual(
                dpg.get_value("about_zd_disclaimer"), ZD_DISCLAIMER_VERBATIM
            )
        finally:
            dpg.destroy_context()

    def test_zh_cn_disclaimer_translated_but_keeps_zd_gaming(self) -> None:
        # zh-CN prose is translated, but the "ZD Gaming" brand name stays as-is
        # (per spec), and the disclaimer still renders on the screen.
        # NOTE: build the shell BEFORE switching locale — AppShell.__init__
        # reads AppSettings() (default locale "en") and would reset zh-CN.
        shell = make_shell(settings_service=MagicMock())

        i18n.set_locale("zh-CN")
        zh = i18n.t("about.zd_disclaimer")
        self.assertIn("ZD Gaming", zh)
        self.assertNotEqual(zh, ZD_DISCLAIMER_VERBATIM)
        self.assertNotIn("????", zh)

        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertEqual(dpg.get_value("about_zd_disclaimer"), zh)
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
