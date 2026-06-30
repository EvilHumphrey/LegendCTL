"""Tests for AppSettings model + SettingsStore JSON round-trip."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zd_app.models import AppSettings
from zd_app.storage.settings_store import SettingsStore


_DEEP_JSON = "[" * 20000 + "]" * 20000


class AppSettingsModelTests(unittest.TestCase):
    def test_developer_panels_visible_default_false(self) -> None:
        settings = AppSettings()
        self.assertFalse(settings.developer_panels_visible)

    def test_to_dict_includes_developer_panels_visible(self) -> None:
        settings = AppSettings(developer_panels_visible=True)
        payload = settings.to_dict()
        self.assertIn("developer_panels_visible", payload)
        self.assertTrue(payload["developer_panels_visible"])

    def test_from_dict_handles_missing_developer_field(self) -> None:
        # Forward-compat: settings.json from before the UX-cleanup pass has
        # no developer_panels_visible field. Loader must default to False
        # rather than raising KeyError.
        legacy_payload = {
            "language": "en",
            "logging_verbosity": "Normal",
            "auto_read_on_connect": True,
            "diagnostics_bundle_dir": "zd_data/diagnostics",
            "show_legacy_screens": False,
        }
        settings = AppSettings.from_dict(legacy_payload)
        self.assertFalse(settings.developer_panels_visible)

    def test_from_dict_preserves_developer_field_when_present(self) -> None:
        settings = AppSettings.from_dict({
            "language": "en",
            "developer_panels_visible": True,
        })
        self.assertTrue(settings.developer_panels_visible)

    def test_from_dict_coerces_developer_field_to_bool(self) -> None:
        # Tolerate stray truthy values from hand-edited settings.json
        settings = AppSettings.from_dict({"developer_panels_visible": 1})
        self.assertTrue(settings.developer_panels_visible)
        settings = AppSettings.from_dict({"developer_panels_visible": 0})
        self.assertFalse(settings.developer_panels_visible)

    def test_first_run_acknowledged_default_false(self) -> None:
        self.assertFalse(AppSettings().first_run_acknowledged)

    def test_to_dict_includes_first_run_acknowledged(self) -> None:
        payload = AppSettings(first_run_acknowledged=True).to_dict()
        self.assertIn("first_run_acknowledged", payload)
        self.assertTrue(payload["first_run_acknowledged"])

    def test_from_dict_defaults_first_run_acknowledged_false_when_missing(self) -> None:
        # Forward-compat: settings.json written before the first-run gate has
        # no first_run_acknowledged field. An existing install must default to
        # False so the loader never raises — the gate then shows once on the
        # next launch (correct: a one-time legal accept is owed by prior users
        # too, not only brand-new installs).
        settings = AppSettings.from_dict({"language": "en"})
        self.assertFalse(settings.first_run_acknowledged)

    def test_from_dict_coerces_first_run_acknowledged_to_bool(self) -> None:
        self.assertTrue(
            AppSettings.from_dict({"first_run_acknowledged": 1}).first_run_acknowledged
        )
        self.assertFalse(
            AppSettings.from_dict({"first_run_acknowledged": 0}).first_run_acknowledged
        )


class SettingsStoreRoundTripTests(unittest.TestCase):
    def _store(self, tmp: Path) -> SettingsStore:
        return SettingsStore(path=tmp / "settings.json")

    def test_developer_field_round_trips_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            store = self._store(tmp)
            settings = AppSettings(developer_panels_visible=True)
            store.save(settings)

            disk = json.loads((tmp / "settings.json").read_text(encoding="utf-8"))
            self.assertTrue(disk["developer_panels_visible"])

            reloaded = store.load()
            self.assertTrue(reloaded.developer_panels_visible)

    def test_load_handles_legacy_settings_without_developer_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            (tmp / "settings.json").write_text(
                json.dumps({
                    "language": "en",
                    "logging_verbosity": "Normal",
                    "auto_read_on_connect": True,
                    "show_legacy_screens": False,
                }),
                encoding="utf-8",
            )
            store = self._store(tmp)
            settings = store.load()
            self.assertFalse(settings.developer_panels_visible)

    def test_first_run_acknowledged_round_trips_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            store = self._store(tmp)
            store.save(AppSettings(first_run_acknowledged=True))

            disk = json.loads((tmp / "settings.json").read_text(encoding="utf-8"))
            self.assertTrue(disk["first_run_acknowledged"])

            self.assertTrue(store.load().first_run_acknowledged)


class SettingsStoreCorruptionTests(unittest.TestCase):
    """A2/A3: a corrupt/tampered settings.json degrades to defaults instead of
    bricking launch every relaunch, and ``save`` is an atomic temp-then-replace.
    """

    def _store(self, tmp: Path) -> SettingsStore:
        return SettingsStore(path=tmp / "settings.json")

    def test_load_non_dict_root_returns_defaults(self) -> None:
        # A2: a parseable-but-non-object root would AttributeError on
        # payload.get(...); the loader must degrade to defaults, not raise.
        for root in ("null", "[]", "42", '"a string"'):
            with self.subTest(root=root):
                with tempfile.TemporaryDirectory() as tmp_str:
                    tmp = Path(tmp_str)
                    (tmp / "settings.json").write_text(root, encoding="utf-8")
                    store = self._store(tmp)
                    settings = store.load()  # must not raise
                    self.assertIsInstance(settings, AppSettings)
                    self.assertEqual(settings.language, AppSettings().language)

    def test_load_unhashable_language_returns_defaults(self) -> None:
        # A2: an unhashable language value trips a TypeError inside
        # _normalize_language_code; the broadened guard returns defaults.
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            (tmp / "settings.json").write_text(
                json.dumps({"language": [1, 2]}), encoding="utf-8"
            )
            store = self._store(tmp)
            settings = store.load()  # must not raise
            self.assertIsInstance(settings, AppSettings)
            self.assertEqual(settings.language, AppSettings().language)

    def test_deeply_nested_settings_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            (tmp / "settings.json").write_text(_DEEP_JSON, encoding="utf-8")
            store = self._store(tmp)

            settings = store.load()  # must not raise RecursionError

            self.assertIsInstance(settings, AppSettings)
            self.assertEqual(settings.language, AppSettings().language)

    def test_save_is_atomic_original_survives_failed_replace(self) -> None:
        # A3: a failure at the replace step must leave the prior settings.json
        # intact (atomic temp-then-replace). Pre-fix, save() wrote straight to
        # the final path, so the file was already clobbered with no replace
        # call to fail on.
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            store = self._store(tmp)
            store.save(AppSettings(language="en"))
            original = (tmp / "settings.json").read_text(encoding="utf-8")

            with mock.patch.object(
                Path, "replace", side_effect=OSError("simulated crash mid-write")
            ):
                with self.assertRaises(OSError):
                    store.save(AppSettings(language="zh-CN"))

            # The original file is untouched — the atomic write never exposed a
            # partial/clobbered final file.
            self.assertEqual(
                (tmp / "settings.json").read_text(encoding="utf-8"), original
            )


if __name__ == "__main__":
    unittest.main()
