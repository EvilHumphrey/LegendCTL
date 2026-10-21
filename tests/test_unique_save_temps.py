"""Synthetic-file coverage for settings and wrapper-profile save temporaries."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from zd_app.models import AppSettings, WrapperProfile
from zd_app.storage import atomic_write
from zd_app.storage.settings_store import SettingsStore
from zd_app.storage.snapshot_codec import snapshot_from_dict
from zd_app.storage.wrapper_profile_store import WrapperProfileStore


class UniqueSaveTempTests(unittest.TestCase):
    def _fixture(self, directory: Path, kind: str):
        if kind == "settings":
            target = directory / "settings.json"
            store = SettingsStore(target)

            def make_record(value):
                record = AppSettings(diagnostics_bundle_dir=value)
                setattr(record, "setup_drawer_dismissed", False)
                return record

        else:
            target = directory / "test-profile.json"
            store = WrapperProfileStore(directory)

            def make_record(value):
                return WrapperProfile(
                    name="Test Profile",
                    description=value,
                    snapshot=snapshot_from_dict({}),
                )

        return store, target, make_record

    def test_preplanted_predictable_temp_and_hardlink_sentinel_stay_unchanged(self):
        for kind in ("settings", "profile"):
            for alias in (False, True):
                with self.subTest(kind=kind, hardlink=alias):
                    with tempfile.TemporaryDirectory() as temp_dir:
                        directory = Path(temp_dir)
                        store, target, make_record = self._fixture(directory, kind)
                        predictable = target.with_suffix(".json.tmp")
                        sentinel = directory / "sentinel.bin"
                        sentinel.write_bytes(b"synthetic sentinel must survive")
                        if alias:
                            os.link(sentinel, predictable)
                        else:
                            predictable.write_bytes(sentinel.read_bytes())
                        before = sentinel.read_bytes()

                        store.save(make_record("saved value"))

                        self.assertEqual(sentinel.read_bytes(), before)
                        self.assertEqual(predictable.read_bytes(), before)
                        self.assertEqual(list(directory.glob("*.tmp")), [predictable])
                        self.assertIsInstance(json.loads(target.read_bytes()), dict)

    def test_native_text_serialization_and_round_trip_are_preserved(self):
        for kind in ("settings", "profile"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                directory = Path(temp_dir)
                store, target, make_record = self._fixture(directory, kind)
                record = make_record("Unicode \u00e9 and escaped\nnewline")

                result = store.save(record)

                payload = record.to_dict()
                if kind == "settings":
                    payload["setup_drawer_dismissed"] = False
                    loaded = store.load()
                    self.assertEqual(loaded.diagnostics_bundle_dir, record.diagnostics_bundle_dir)
                    self.assertIsNone(result)
                else:
                    loaded = store.load(record.name)
                    self.assertEqual(loaded.to_dict(), payload)
                    self.assertEqual(result, target)
                # A real text writer supplies the pre-change encoding/newline
                # oracle on each host, including CRLF translation on Windows.
                expected = directory / "expected.bin"
                with expected.open("w", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, indent=2))
                self.assertEqual(target.read_bytes(), expected.read_bytes())
                self.assertEqual(list(directory.glob("*.tmp")), [])

    def test_fsync_failure_keeps_best_effort_store_logging_and_publication(self):
        for kind in ("settings", "profile"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                store, target, make_record = self._fixture(Path(temp_dir), kind)
                module = "settings_store" if kind == "settings" else "wrapper_profile_store"
                with self.assertLogs(f"zd_app.storage.{module}", level="DEBUG") as logs:
                    with mock.patch.object(atomic_write.os, "fsync", side_effect=OSError("unsupported")):
                        store.save(make_record("complete despite unavailable fsync"))

                self.assertIsInstance(json.loads(target.read_bytes()), dict)
                self.assertIn("fsync unavailable for", logs.output[0])
                self.assertIn("unsupported", logs.output[0])
                self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_fdopen_failure_closes_allocated_descriptor_and_removes_temp(self):
        for kind in ("settings", "profile"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                store, target, make_record = self._fixture(Path(temp_dir), kind)
                store.save(make_record("original"))
                before = target.read_bytes()
                descriptors = []

                def fail_fdopen(fd, mode):
                    descriptors.append(fd)
                    raise OSError("cannot wrap descriptor")

                with mock.patch.object(atomic_write.os, "fdopen", side_effect=fail_fdopen):
                    with self.assertRaisesRegex(OSError, "cannot wrap descriptor"):
                        store.save(make_record("replacement"))

                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(list(target.parent.glob("*.tmp")), [])
                self.assertEqual(len(descriptors), 1)
                with self.assertRaises(OSError):
                    os.fstat(descriptors[0])

    def test_concurrent_saves_use_distinct_complete_temps(self):
        for kind in ("settings", "profile"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                store, target, make_record = self._fixture(Path(temp_dir), kind)
                ready = threading.Barrier(2, timeout=10)
                captured = []
                lock = threading.Lock()
                original_replace = os.replace

                def synchronized_replace(source, destination):
                    source = Path(source)
                    data = source.read_bytes()
                    with lock:
                        captured.append((source, data))
                    ready.wait()
                    # Both complete temps coexist before either publication.
                    # Serialize only the final OS calls: Windows may reject
                    # simultaneous replaces with a native sharing/access error,
                    # independent of whether writers shared a temp file.
                    with lock:
                        return original_replace(source, destination)

                with mock.patch.object(atomic_write.os, "replace", side_effect=synchronized_replace):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        futures = [pool.submit(store.save, make_record(value)) for value in ("first", "second")]
                        for future in futures:
                            future.result(timeout=15)

                self.assertEqual(len(captured), 2)
                self.assertNotEqual(captured[0][0], captured[1][0])
                self.assertTrue(all(path.parent == target.parent for path, _ in captured))
                field = "diagnostics_bundle_dir" if kind == "settings" else "description"
                self.assertEqual({json.loads(data)[field] for _, data in captured}, {"first", "second"})
                self.assertIn(target.read_bytes(), [data for _, data in captured])
                self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_primitive_default_still_propagates_fsync_error_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "record.bin"
            target.write_bytes(b"previous")
            with mock.patch.object(atomic_write.os, "fsync", side_effect=OSError("fsync failed")):
                with self.assertRaisesRegex(OSError, "fsync failed"):
                    atomic_write.atomic_write_bytes(target, b"replacement")

            self.assertEqual(target.read_bytes(), b"previous")
            self.assertEqual(list(target.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
