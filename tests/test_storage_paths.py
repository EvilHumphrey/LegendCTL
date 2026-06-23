"""Tests for packaged and source-mode storage path selection."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zd_app.models import AppSettings
from zd_app.storage.profile_store import ProfileStore
from zd_app.storage.settings_store import (
    SettingsStore,
    _default_user_data_dir,
    initialize_user_data_dir,
)
from zd_app.storage.wrapper_profile_store import WrapperProfileStore


class StoragePathTests(unittest.TestCase):
    def test_default_user_data_dir_dev_returns_zd_data(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch.object(
            sys, "frozen", False, create=True
        ):
            self.assertEqual(_default_user_data_dir(), Path("zd_data"))

    def test_default_user_data_dir_frozen_uses_appdata(self) -> None:
        with patch.dict(
            os.environ,
            {"APPDATA": r"C:\Users\test\AppData\Roaming"},
            clear=True,
        ), patch.object(sys, "frozen", True, create=True):
            self.assertEqual(
                _default_user_data_dir(),
                Path(r"C:\Users\test\AppData\Roaming\ZDUltimateLegend"),
            )

    def test_default_user_data_dir_env_override(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ZDUL_DATA_DIR": r"D:\portable\zdul",
                "APPDATA": r"C:\Users\test\AppData\Roaming",
            },
            clear=True,
        ), patch.object(sys, "frozen", True, create=True):
            self.assertEqual(_default_user_data_dir(), Path(r"D:\portable\zdul"))

    def test_settings_store_default_path_uses_user_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"ZDUL_DATA_DIR": tmpdir},
            clear=True,
        ), patch.object(sys, "frozen", False, create=True):
            store = SettingsStore()

            self.assertEqual(store.path, Path(tmpdir) / "settings.json")
            self.assertEqual(
                store.load().diagnostics_bundle_dir,
                str(Path(tmpdir) / "diagnostics"),
            )

    def test_profile_stores_default_base_uses_user_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"ZDUL_DATA_DIR": tmpdir},
            clear=True,
        ), patch.object(sys, "frozen", False, create=True):
            wrapper_store = WrapperProfileStore()
            legacy_store = ProfileStore()

            self.assertEqual(wrapper_store.base_dir, Path(tmpdir) / "wrapper_profiles")
            self.assertEqual(legacy_store.base_dir, Path(tmpdir) / "profiles")

    def testinitialize_user_data_dir_migrates_legacy_zd_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            exe_dir = root / "release"
            legacy = exe_dir / "zd_data"
            appdata = root / "appdata"
            legacy.mkdir(parents=True)
            (legacy / "settings.json").write_text('{"language": "English"}', encoding="utf-8")
            (legacy / "wrapper_profiles").mkdir()
            (legacy / "wrapper_profiles" / "fps.json").write_text("{}", encoding="utf-8")

            with patch.dict(os.environ, {"APPDATA": str(appdata)}, clear=True), patch.object(
                sys, "frozen", True, create=True
            ), patch.object(sys, "executable", str(exe_dir / "ZD Ultimate Legend.exe")):
                target = initialize_user_data_dir()

            self.assertEqual(target, appdata / "ZDUltimateLegend")
            self.assertTrue((target / "settings.json").exists())
            self.assertTrue((target / "wrapper_profiles" / "fps.json").exists())

    def testinitialize_user_data_dir_skips_migration_if_target_nonempty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            exe_dir = root / "release"
            legacy = exe_dir / "zd_data"
            appdata = root / "appdata"
            target = appdata / "ZDUltimateLegend"
            legacy.mkdir(parents=True)
            target.mkdir(parents=True)
            (legacy / "settings.json").write_text('{"language": "English"}', encoding="utf-8")
            (target / "existing.txt").write_text("keep", encoding="utf-8")

            with patch.dict(os.environ, {"APPDATA": str(appdata)}, clear=True), patch.object(
                sys, "frozen", True, create=True
            ), patch.object(sys, "executable", str(exe_dir / "ZD Ultimate Legend.exe")):
                initialize_user_data_dir()

            self.assertEqual((target / "existing.txt").read_text(encoding="utf-8"), "keep")
            self.assertFalse((target / "settings.json").exists())

    def test_app_settings_persists_language_field(self) -> None:
        settings = AppSettings(language="zh-CN")

        loaded = AppSettings.from_dict(settings.to_dict())

        self.assertEqual(loaded.language, "zh-CN")

    def test_app_settings_legacy_load_defaults_language_to_en(self) -> None:
        self.assertEqual(AppSettings.from_dict({}).language, "en")
        self.assertEqual(AppSettings.from_dict({"language": "English"}).language, "en")


if __name__ == "__main__":
    unittest.main()
