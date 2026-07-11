"""Tests for packaged and source-mode storage path selection."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from zd_app.models import AppSettings
from zd_app.storage import settings_store
from zd_app.storage.profile_store import ProfileStore
from zd_app.storage.settings_store import (
    MIGRATION_STATE_COMPLETE,
    MIGRATION_STATE_FILENAME,
    MIGRATION_STATE_STARTED,
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

    def test_frozen_fresh_migration_records_states_and_copies_all_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            exe_dir = root / "release"
            legacy = exe_dir / "zd_data"
            appdata = root / "appdata"
            legacy.mkdir(parents=True)
            (legacy / "settings.json").write_text('{"language": "English"}', encoding="utf-8")
            (legacy / "profiles").mkdir()
            (legacy / "profiles" / "fps.json").write_text("{}", encoding="utf-8")

            with patch.dict(os.environ, {"APPDATA": str(appdata)}, clear=True), patch.object(
                sys, "frozen", True, create=True
            ), patch.object(sys, "executable", str(exe_dir / "ZD Ultimate Legend.exe")), mock.patch(
                "zd_app.storage.settings_store._write_migration_state",
                wraps=settings_store._write_migration_state,
            ) as write_state:
                target = initialize_user_data_dir()

            self.assertEqual(
                [call.args[1] for call in write_state.call_args_list],
                [MIGRATION_STATE_STARTED, MIGRATION_STATE_COMPLETE],
            )
            self.assertEqual(
                (target / MIGRATION_STATE_FILENAME).read_text(encoding="utf-8"),
                '{"state": "complete"}',
            )
            self.assertEqual(
                (target / "settings.json").read_text(encoding="utf-8"),
                '{"language": "English"}',
            )
            self.assertTrue((target / "profiles" / "fps.json").exists())

    def test_frozen_started_marker_resumes_only_missing_legacy_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            exe_dir = root / "release"
            legacy = exe_dir / "zd_data"
            appdata = root / "appdata"
            target = appdata / "ZDUltimateLegend"
            (legacy / "profiles").mkdir(parents=True)
            (legacy / "settings.json").write_text("legacy-settings", encoding="utf-8")
            (legacy / "profiles" / "existing.json").write_text("legacy-profile", encoding="utf-8")
            (legacy / "profiles" / "missing.json").write_text("new-profile", encoding="utf-8")
            (target / "profiles").mkdir(parents=True)
            (target / MIGRATION_STATE_FILENAME).write_text('{"state": "started"}', encoding="utf-8")
            (target / "settings.json").write_text("current-settings", encoding="utf-8")
            (target / "profiles" / "existing.json").write_text("current-profile", encoding="utf-8")

            with patch.dict(os.environ, {"APPDATA": str(appdata)}, clear=True), patch.object(
                sys, "frozen", True, create=True
            ), patch.object(sys, "executable", str(exe_dir / "ZD Ultimate Legend.exe")):
                initialize_user_data_dir()

            self.assertEqual((target / "settings.json").read_text(encoding="utf-8"), "current-settings")
            self.assertEqual(
                (target / "profiles" / "existing.json").read_text(encoding="utf-8"),
                "current-profile",
            )
            self.assertEqual((target / "profiles" / "missing.json").read_text(encoding="utf-8"), "new-profile")
            self.assertEqual(
                (target / MIGRATION_STATE_FILENAME).read_text(encoding="utf-8"),
                '{"state": "complete"}',
            )

    def test_frozen_copy_interruption_never_publishes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            exe_dir = root / "release"
            legacy = exe_dir / "zd_data"
            appdata = root / "appdata"
            target = appdata / "ZDUltimateLegend"
            legacy.mkdir(parents=True)
            (legacy / "settings.json").write_text("complete legacy settings", encoding="utf-8")
            real_replace = settings_store.os.replace

            def interrupt_before_publish(source, destination) -> None:
                if Path(destination) == target / "settings.json":
                    raise OSError("simulated interruption before publish")
                real_replace(source, destination)

            with patch.dict(os.environ, {"APPDATA": str(appdata)}, clear=True), patch.object(
                sys, "frozen", True, create=True
            ), patch.object(sys, "executable", str(exe_dir / "ZD Ultimate Legend.exe")), patch.object(
                settings_store.os, "replace", side_effect=interrupt_before_publish
            ):
                with self.assertRaises(OSError):
                    initialize_user_data_dir()

            self.assertFalse((target / "settings.json").exists())
            self.assertTrue((target / "settings.json.zdmig.tmp").exists())
            self.assertEqual(
                (target / MIGRATION_STATE_FILENAME).read_text(encoding="utf-8"),
                '{"state": "started"}',
            )

            with patch.dict(os.environ, {"APPDATA": str(appdata)}, clear=True), patch.object(
                sys, "frozen", True, create=True
            ), patch.object(sys, "executable", str(exe_dir / "ZD Ultimate Legend.exe")):
                initialize_user_data_dir()

            self.assertEqual(
                (target / "settings.json").read_text(encoding="utf-8"),
                "complete legacy settings",
            )
            self.assertFalse((target / "settings.json.zdmig.tmp").exists())

    def test_frozen_started_marker_does_not_recopy_complete_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            exe_dir = root / "release"
            legacy = exe_dir / "zd_data"
            appdata = root / "appdata"
            target = appdata / "ZDUltimateLegend"
            legacy.mkdir(parents=True)
            target.mkdir(parents=True)
            (legacy / "settings.json").write_text("legacy", encoding="utf-8")
            (target / "settings.json").write_text("complete current", encoding="utf-8")
            (target / MIGRATION_STATE_FILENAME).write_text(
                '{"state": "started"}', encoding="utf-8"
            )

            with patch.dict(os.environ, {"APPDATA": str(appdata)}, clear=True), patch.object(
                sys, "frozen", True, create=True
            ), patch.object(sys, "executable", str(exe_dir / "ZD Ultimate Legend.exe")), patch.object(
                settings_store.shutil, "copy2", wraps=settings_store.shutil.copy2
            ) as copy2:
                initialize_user_data_dir()

            copy2.assert_not_called()
            self.assertEqual(
                (target / "settings.json").read_text(encoding="utf-8"), "complete current"
            )

    def test_frozen_migration_temp_is_not_user_data_and_is_cleaned_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            exe_dir = root / "release"
            legacy = exe_dir / "zd_data"
            appdata = root / "appdata"
            target = appdata / "ZDUltimateLegend"
            legacy.mkdir(parents=True)
            target.mkdir(parents=True)
            (legacy / "settings.json").write_text("legacy", encoding="utf-8")
            (target / "settings.json.zdmig.tmp").write_text("partial", encoding="utf-8")

            with patch.dict(os.environ, {"APPDATA": str(appdata)}, clear=True), patch.object(
                sys, "frozen", True, create=True
            ), patch.object(sys, "executable", str(exe_dir / "ZD Ultimate Legend.exe")):
                initialize_user_data_dir()

            self.assertEqual((target / "settings.json").read_text(encoding="utf-8"), "legacy")
            self.assertFalse((target / "settings.json.zdmig.tmp").exists())

    def test_frozen_unmarked_nonempty_target_is_grandfathered(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            exe_dir = root / "release"
            legacy = exe_dir / "zd_data"
            appdata = root / "appdata"
            target = appdata / "ZDUltimateLegend"
            legacy.mkdir(parents=True)
            target.mkdir(parents=True)
            (legacy / "settings.json").write_text("legacy", encoding="utf-8")
            (target / "existing.txt").write_text("keep", encoding="utf-8")

            with patch.dict(os.environ, {"APPDATA": str(appdata)}, clear=True), patch.object(
                sys, "frozen", True, create=True
            ), patch.object(sys, "executable", str(exe_dir / "ZD Ultimate Legend.exe")):
                initialize_user_data_dir()

            self.assertEqual((target / "existing.txt").read_text(encoding="utf-8"), "keep")
            self.assertFalse((target / "settings.json").exists())
            self.assertEqual(
                (target / MIGRATION_STATE_FILENAME).read_text(encoding="utf-8"),
                '{"state": "complete"}',
            )

    def test_frozen_complete_marker_never_resurrects_legacy_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            exe_dir = root / "release"
            legacy = exe_dir / "zd_data"
            appdata = root / "appdata"
            target = appdata / "ZDUltimateLegend"
            legacy.mkdir(parents=True)
            target.mkdir(parents=True)
            (legacy / "settings.json").write_text("legacy", encoding="utf-8")
            (target / MIGRATION_STATE_FILENAME).write_text('{"state": "complete"}', encoding="utf-8")

            with patch.dict(os.environ, {"APPDATA": str(appdata)}, clear=True), patch.object(
                sys, "frozen", True, create=True
            ), patch.object(sys, "executable", str(exe_dir / "ZD Ultimate Legend.exe")):
                initialize_user_data_dir()

            self.assertFalse((target / "settings.json").exists())

    def test_frozen_corrupt_complete_marker_does_not_resurrect_deleted_legacy_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            exe_dir = root / "release"
            legacy = exe_dir / "zd_data"
            appdata = root / "appdata"
            target = appdata / "ZDUltimateLegend"
            legacy.mkdir(parents=True)
            target.mkdir(parents=True)
            # A completed migration copied this file before the user deleted it
            # from the new location. The legacy source remains intact.
            (legacy / "deleted.json").write_text("legacy", encoding="utf-8")
            (target / MIGRATION_STATE_FILENAME).write_text("not-json", encoding="utf-8")

            with patch.dict(os.environ, {"APPDATA": str(appdata)}, clear=True), patch.object(
                sys, "frozen", True, create=True
            ), patch.object(sys, "executable", str(exe_dir / "ZD Ultimate Legend.exe")), self.assertLogs(
                settings_store.logger, level="WARNING"
            ) as logs:
                initialize_user_data_dir()

            self.assertFalse((target / "deleted.json").exists())
            self.assertEqual(
                (target / MIGRATION_STATE_FILENAME).read_text(encoding="utf-8"),
                '{"state": "complete"}',
            )
            self.assertTrue(any("treating it as complete" in line for line in logs.output))

    def test_app_settings_persists_language_field(self) -> None:
        settings = AppSettings(language="zh-CN")

        loaded = AppSettings.from_dict(settings.to_dict())

        self.assertEqual(loaded.language, "zh-CN")

    def test_app_settings_legacy_load_defaults_language_to_en(self) -> None:
        self.assertEqual(AppSettings.from_dict({}).language, "en")
        self.assertEqual(AppSettings.from_dict({"language": "English"}).language, "en")


if __name__ == "__main__":
    unittest.main()
