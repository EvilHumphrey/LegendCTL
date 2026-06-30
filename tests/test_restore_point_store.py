"""Tests for the file-per-snapshot restore-point store."""

from __future__ import annotations

import json
import logging
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from zd_app.services.settings_service import ControllerSnapshot
from zd_app.storage.restore_point_models import (
    KIND,
    SCHEMA_VERSION,
    CaptureSource,
    CoverageCategory,
    CoverageState,
    DeviceIdentity,
    FieldCoverage,
    IdentityConfidence,
    RestorePoint,
    RestorePointCoverage,
    RestorePointSchemaError,
    RestorePointTrigger,
    restore_point_to_dict,
)
from zd_app.storage.restore_point_store import (
    FILENAME_PATTERN,
    RestorePointStore,
    filename_for,
)


def _empty_snapshot() -> ControllerSnapshot:
    return ControllerSnapshot(
        polling_rate=None,
        vibration=None,
        deadzones=None,
        axis_inversion_left=None,
        axis_inversion_right=None,
        sensitivity_left=None,
        sensitivity_right=None,
        trigger_left=None,
        trigger_right=None,
        button_bindings={},
        lighting_zones={},
    )


def _make_rp(
    *,
    id: str = "rp_20260524_190355_9b42de",
    created_at: str = "2026-05-24T19:03:55Z",
    title: str = "Before Safe Import — 2026-05-24 19:03",
    trigger_type: str = "before_safe_import_apply",
    product_string: str | None = "ZD Ultimate Legend",
) -> RestorePoint:
    coverage = RestorePointCoverage(
        captured_supported_count=0,
        total_supported_count=13,
        capture_source=CaptureSource.FRESH_READ,
        fields={
            "polling_rate": FieldCoverage(
                state=CoverageState.NOT_CAPTURED,
                readable=False,
                writable=True,
                category=CoverageCategory.DEVICE,
            ),
        },
    )
    return RestorePoint(
        schema_version=SCHEMA_VERSION,
        kind=KIND,
        id=id,
        created_at=created_at,
        app_version="2.0.0",
        app_build_commit=None,
        title=title,
        trigger=RestorePointTrigger(
            type=trigger_type,
            source_label=trigger_type.replace("_", " ").title(),
            reason=f"Created automatically for trigger={trigger_type}",
        ),
        device_identity=DeviceIdentity(
            vid="413D",
            pid="2104",
            product_string=product_string,
            firmware_version="1.18",
            identity_confidence=IdentityConfidence.READABLE,
        ),
        snapshot=_empty_snapshot(),
        coverage=coverage,
        last_restore_attempt=None,
    )


class FilenameTests(unittest.TestCase):
    def test_filename_matches_regex(self) -> None:
        rp = _make_rp()
        name = filename_for(rp)
        self.assertRegex(name, FILENAME_PATTERN)

    def test_filename_components_match_restore_point(self) -> None:
        rp = _make_rp(
            id="rp_20260524_190355_abcdef",
            created_at="2026-05-24T19:03:55Z",
            trigger_type="before_safe_import_apply",
        )
        name = filename_for(rp)
        match = FILENAME_PATTERN.fullmatch(name)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group("ts"), "20260524-190355")
        self.assertEqual(match.group("trigger"), "before_safe_import_apply")
        self.assertEqual(match.group("suffix"), "abcdef")

    def test_filename_sanitizes_trigger_with_punctuation(self) -> None:
        # Hostile trigger token shouldn't escape the filename slug.
        rp = _make_rp(trigger_type="weird trigger!@#")
        name = filename_for(rp)
        self.assertRegex(name, FILENAME_PATTERN)


class RoundTripStoreTests(unittest.TestCase):
    def test_save_then_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            rp = _make_rp()
            path = store.save(rp)
            self.assertTrue(path.exists())
            loaded = store.load(rp.id)
            self.assertEqual(loaded, rp)

    def test_load_missing_id_raises_file_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            with self.assertRaises(FileNotFoundError):
                store.load("rp_does_not_exist")


class ListTests(unittest.TestCase):
    def test_list_returns_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            store.save(_make_rp(
                id="rp_20260524_180000_aaaaaa",
                created_at="2026-05-24T18:00:00Z",
            ))
            store.save(_make_rp(
                id="rp_20260524_200000_cccccc",
                created_at="2026-05-24T20:00:00Z",
            ))
            store.save(_make_rp(
                id="rp_20260524_190000_bbbbbb",
                created_at="2026-05-24T19:00:00Z",
            ))
            valid, skipped = store.list()
            self.assertEqual(skipped, [])
            self.assertEqual(
                [rp.id for rp in valid],
                [
                    "rp_20260524_200000_cccccc",
                    "rp_20260524_190000_bbbbbb",
                    "rp_20260524_180000_aaaaaa",
                ],
            )

    def test_list_skips_unreadable_files_with_low_panic_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            store.save(_make_rp())

            # A bare non-JSON file with the .json extension.
            (Path(tmp) / "20260524-180000_manual_aaaaaa.json").write_text(
                "this is not JSON",
                encoding="utf-8",
            )
            # A JSON object with the wrong kind.
            (Path(tmp) / "20260524-181000_manual_bbbbbb.json").write_text(
                json.dumps({"kind": "something_else", "schema_version": 1}),
                encoding="utf-8",
            )

            valid, skipped = store.list()
            self.assertEqual(len(valid), 1)
            self.assertEqual(len(skipped), 2)
            for entry in skipped:
                self.assertTrue(entry.path.endswith(".json"))
                self.assertTrue(entry.error)

    def test_list_with_unknown_schema_version_lands_in_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            rp = _make_rp()
            payload = restore_point_to_dict(rp)
            payload["schema_version"] = 999
            (Path(tmp) / "20260524-200000_future_dddddd.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            valid, skipped = store.list()
            self.assertEqual(valid, [])
            self.assertEqual(len(skipped), 1)
            # The schema error should surface explicitly (not just "parse error").
            self.assertIn("schema_version", skipped[0].error.lower())

    def test_skipped_file_error_scrubs_full_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            bad_path = Path(tmp) / "20260524-200000_manual_dddddd.json"
            bad_path.write_text("[" * 70 + "]" * 70, encoding="utf-8")

            valid, skipped = store.list()

            self.assertEqual(valid, [])
            self.assertEqual(len(skipped), 1)
            self.assertNotIn(str(bad_path), skipped[0].error)
            self.assertNotIn(str(Path(tmp)), skipped[0].error)
            self.assertIn(bad_path.name, skipped[0].error)


class ListVanishedFileTests(unittest.TestCase):
    def test_file_deleted_between_glob_and_read_skips_silently(self) -> None:
        # Captures (and their prunes) run on worker threads
        # now, while the RP screen lists the vault on the render thread. A
        # file unlinked between the glob and read_text is a benign race —
        # it must NOT land in ``skipped`` (which renders a corrupt-file
        # disclosure card), just a debug log.
        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            keeper = _make_rp()
            vanishing = _make_rp(
                id="rp_20260524_190401_d0d0d0",
                created_at="2026-05-24T19:04:01Z",
            )
            store.save(keeper)
            vanishing_path = store.save(vanishing)

            original_read_text = Path.read_text

            def racy_read_text(self, *args, **kwargs):
                if self.name == vanishing_path.name:
                    raise FileNotFoundError(2, "vanished mid-list", str(self))
                return original_read_text(self, *args, **kwargs)

            with patch.object(Path, "read_text", racy_read_text), self.assertLogs(
                "zd_app.storage.restore_point_store", level="DEBUG"
            ) as logs:
                valid, skipped = store.list()

            self.assertEqual([rp.id for rp in valid], [keeper.id])
            self.assertEqual(skipped, [])  # no disclosure card
            self.assertFalse(
                [r for r in logs.records if r.levelno >= logging.WARNING],
                "vanished file must not warn",
            )
            self.assertTrue(
                any("vanished" in line for line in logs.output), logs.output
            )


class AtomicWriteTests(unittest.TestCase):
    def test_save_uses_temp_then_replace(self) -> None:
        """Per the "Atomic write" rule — verify the temp-file-then-replace dance.

        We spy on ``Path.replace`` to confirm the final swap, and confirm
        no stray ``.tmp`` is left on disk after a successful save.
        """

        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            rp = _make_rp()

            original_replace = Path.replace
            replace_calls: list[tuple[str, str]] = []

            def _spy(self: Path, target):
                replace_calls.append((self.name, Path(target).name))
                return original_replace(self, target)

            with patch.object(Path, "replace", _spy):
                store.save(rp)

            self.assertEqual(len(replace_calls), 1)
            temp_name, final_name = replace_calls[0]
            self.assertTrue(temp_name.endswith(".json.tmp"))
            self.assertTrue(final_name.endswith(".json"))
            self.assertFalse(final_name.endswith(".tmp"))

            leftover_tmps = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(leftover_tmps, [])

    def test_partial_temp_file_does_not_appear_in_list(self) -> None:
        """A ``.tmp`` straggler from a crashed write must not show up."""

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "20260524-180000_manual_aaaaaa.json.tmp").write_text(
                "half-written",
                encoding="utf-8",
            )
            store = RestorePointStore(tmp)
            valid, skipped = store.list()
            self.assertEqual(valid, [])
            self.assertEqual(skipped, [])


class PruneTests(unittest.TestCase):
    def test_prune_max_count_removes_oldest_auto_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            for offset in range(5):
                store.save(_make_rp(
                    id=f"rp_20260524_1{offset}0000_{'a' * 5}{offset}",
                    created_at=f"2026-05-24T1{offset}:00:00Z",
                    trigger_type="before_safe_import_apply",
                    product_string=None,
                ))
            pruned = store.prune(max_count=3, max_disk_mb=999)
            self.assertEqual(len(pruned), 2)
            # The oldest two should be the ones pruned.
            self.assertIn("rp_20260524_100000_aaaaa0", pruned)
            self.assertIn("rp_20260524_110000_aaaaa1", pruned)
            valid, _ = store.list()
            self.assertEqual(len(valid), 3)

    def test_prune_preserves_manual_until_disk_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            store.save(_make_rp(
                id="rp_20260524_100000_aaaaaa",
                created_at="2026-05-24T10:00:00Z",
                trigger_type="manual",
                product_string=None,
            ))
            for offset in range(4):
                store.save(_make_rp(
                    id=f"rp_20260524_1{offset + 1}0000_{'b' * 5}{offset}",
                    created_at=f"2026-05-24T1{offset + 1}:00:00Z",
                    trigger_type="before_safe_import_apply",
                    product_string=None,
                ))

            pruned = store.prune(max_count=3, max_disk_mb=999)
            self.assertNotIn("rp_20260524_100000_aaaaaa", pruned)
            self.assertEqual(len(pruned), 2)
            valid, _ = store.list()
            ids = {rp.id for rp in valid}
            self.assertIn("rp_20260524_100000_aaaaaa", ids)

    def test_prune_preserves_protected_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            for offset in range(5):
                store.save(_make_rp(
                    id=f"rp_20260524_1{offset}0000_{'c' * 5}{offset}",
                    created_at=f"2026-05-24T1{offset}:00:00Z",
                    trigger_type="before_safe_import_apply",
                    product_string=None,
                ))

            protected_id = "rp_20260524_100000_ccccc0"
            pruned = store.prune(
                max_count=2,
                max_disk_mb=999,
                protect={protected_id},
            )
            self.assertNotIn(protected_id, pruned)
            valid, _ = store.list()
            self.assertIn(protected_id, {rp.id for rp in valid})

    def test_prune_preserves_newest_first_readable_connect_per_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            # Older + newer first_readable_connect for the same device.
            store.save(_make_rp(
                id="rp_20260524_100000_01dfcc",
                created_at="2026-05-24T10:00:00Z",
                trigger_type="first_readable_connect",
                product_string="ZD Ultimate Legend",
            ))
            store.save(_make_rp(
                id="rp_20260524_110000_c0ffee",
                created_at="2026-05-24T11:00:00Z",
                trigger_type="first_readable_connect",
                product_string="ZD Ultimate Legend",
            ))
            # Plus filler auto-created snapshots.
            for offset in range(3):
                store.save(_make_rp(
                    id=f"rp_20260524_1{offset + 2}0000_f111e{offset}",
                    created_at=f"2026-05-24T1{offset + 2}:00:00Z",
                    trigger_type="before_safe_import_apply",
                    product_string=None,
                ))

            pruned = store.prune(max_count=2, max_disk_mb=999)
            # The newest first_readable_connect must survive.
            self.assertNotIn("rp_20260524_110000_c0ffee", pruned)

    def test_prune_disk_cap_enforces_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            for offset in range(5):
                store.save(_make_rp(
                    id=f"rp_20260524_1{offset}0000_disk{offset}",
                    created_at=f"2026-05-24T1{offset}:00:00Z",
                    trigger_type="before_safe_import_apply",
                    product_string=None,
                ))
            valid_before, _ = store.list()
            total_bytes_before = sum(
                (Path(tmp) / filename_for(rp)).stat().st_size for rp in valid_before
            )

            tiny_budget_mb = max(1, (total_bytes_before // (1024 * 1024)) // 2)
            store.prune(max_count=999, max_disk_mb=tiny_budget_mb)

            valid_after, _ = store.list()
            total_bytes_after = sum(
                (Path(tmp) / filename_for(rp)).stat().st_size for rp in valid_after
            )
            self.assertLessEqual(total_bytes_after, tiny_budget_mb * 1024 * 1024)


class DeleteTests(unittest.TestCase):
    def test_delete_removes_matching_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            rp = _make_rp()
            store.save(rp)
            self.assertTrue(store.delete(rp.id))
            valid, _ = store.list()
            self.assertEqual(valid, [])

    def test_delete_returns_false_for_unknown_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            self.assertFalse(store.delete("rp_unknown"))


class GuardedJsonReadTests(unittest.TestCase):
    """P5 [G4]: reads go through read_guarded_json, so a deeply nested or
    oversize local/tampered file is skipped (low-panic) instead of escaping the
    except. On base a deep file made json.loads raise RecursionError, which was
    NOT caught and crashed the whole listing."""

    def test_list_skips_deep_and_oversize_files_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            rp = _make_rp()
            store.save(rp)
            # Pathologically deep nesting: json.loads -> RecursionError on base.
            (Path(tmp) / "20260524-180000_manual_aaaaaa.json").write_text(
                "[" * 20000 + "]" * 20000, encoding="utf-8"
            )
            # Oversize but well-formed: rejected by the byte cap before parsing.
            (Path(tmp) / "20260524-181000_manual_bbbbbb.json").write_text(
                "[" + "0," * 700000 + "0]", encoding="utf-8"
            )

            valid, skipped = store.list()  # must not raise

            self.assertEqual(len(valid), 1)
            self.assertEqual(valid[0].id, rp.id)
            self.assertEqual(len(skipped), 2)
            for entry in skipped:
                self.assertTrue(entry.error)

    def test_load_and_delete_skip_deep_file_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RestorePointStore(tmp)
            rp = _make_rp()
            store.save(rp)
            (Path(tmp) / "20260524-180000_manual_aaaaaa.json").write_text(
                "[" * 20000 + "]" * 20000, encoding="utf-8"
            )

            # load() must skip the deep file and still find the good rp by id.
            self.assertEqual(store.load(rp.id).id, rp.id)
            # delete() must skip the deep file and still remove the good rp.
            self.assertTrue(store.delete(rp.id))


if __name__ == "__main__":
    unittest.main()
