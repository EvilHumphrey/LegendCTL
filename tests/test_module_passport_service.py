"""Tests for :class:`ModulePassportService`.

Exercises:

- ``get`` returns ``None`` when no passport exists, parses a real file when one
  does, and tolerates corrupted JSON without raising.
- ``assign`` persists a fresh passport, archives any prior one, sanitises
  the module_id, refuses empty IDs, and emits a wear_ledger event.
- ``append_fingerprint`` updates the side's history, refuses cross-side
  fingerprints, refuses when no passport exists, emits wear_ledger event.
- ``update_notes`` rewrites the notes field, no-ops when text is unchanged,
  emits wear_ledger event only on real change.
- ``list_archive`` reads archived passports newest-first, skips corrupted
  files.
- Construction tolerates an unwritable base_dir.
- Bad ``side`` raises ValueError up front (programmer-error, not user input).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from unittest import mock

from zd_app.services.module_passport import (
    ARCHIVE_DIRNAME,
    ModulePassportService,
    TREND_STATUS_DRIFTING,
    TREND_STATUS_INSUFFICIENT,
    TREND_STATUS_INVESTIGATE,
    TREND_STATUS_STABLE,
)
from zd_app.services.wear_ledger import WearLedgerService
from zd_app.services.wear_ledger.models import (
    MODULE_ASSIGNED,
    MODULE_CHARACTERIZED,
    MODULE_NOTES_UPDATED,
    MODULE_TREND_FLAGGED,
)
from zd_app.services.module_passport.service import MAX_PASSPORT_BYTES
from zd_app.storage._import_guards import MAX_IMPORT_BYTES
from zd_app.storage.module_passport_models import (
    SCHEMA_VERSION,
    ModuleFingerprint,
    ModulePassport,
    SIDE_LEFT,
    SIDE_RIGHT,
    STATUS_GOOD,
    STATUS_WATCH,
)


def _fingerprint(side: str = SIDE_LEFT, **overrides) -> ModuleFingerprint:
    base = dict(
        timestamp_utc="2026-05-26T18:42:11Z",
        side=side,
        duration_ms=60_000,
        samples_count=12_000,
        noise_floor_percent=1.4,
        centering_offset_x=0.2,
        centering_offset_y=-0.1,
        circularity_coverage_percent=0.97,
        outer_deadzone_min_axis=96.0,
        outer_deadzone_max_axis=99.0,
        asymmetry_score=0.04,
        bitness_observed=128,
        tremor_metric=0.18,
        linearity_score=0.05,
        overall_status=STATUS_GOOD,
        notes=None,
    )
    base.update(overrides)
    return ModuleFingerprint(**base)


def _utc_clock(start: datetime) -> Callable[[], datetime]:
    state = {"now": start}

    def _now() -> datetime:
        current = state["now"]
        state["now"] = current + timedelta(seconds=1)
        return current

    return _now


class BaseServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        # Single advancing clock shared between the ledger and the passport
        # service so event timestamps separate cleanly across calls (the
        # ledger sorts by ts string; same-second events would tie).
        self.clock = _utc_clock(datetime(2026, 5, 26, 18, 0, 0, tzinfo=timezone.utc))
        self.ledger_base = self.base / "ledger"
        self.ledger = WearLedgerService(base_dir=self.ledger_base, utc_now=self.clock)
        self.service = ModulePassportService(
            base_dir=self.base / "passports",
            wear_ledger=self.ledger,
            utc_now=self.clock,
        )


class GetTests(BaseServiceTestCase):
    def test_get_returns_none_when_no_file(self) -> None:
        self.assertIsNone(self.service.get(SIDE_LEFT))
        self.assertIsNone(self.service.get(SIDE_RIGHT))

    def test_get_returns_persisted_passport(self) -> None:
        assigned = self.service.assign(SIDE_LEFT, "STOCK", notes="baseline")
        self.assertIsNotNone(assigned)
        again = self.service.get(SIDE_LEFT)
        self.assertEqual(again, assigned)

    def test_get_tolerates_corrupted_json(self) -> None:
        path = self.service.path_for(SIDE_LEFT)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json{", encoding="utf-8")
        self.assertIsNone(self.service.get(SIDE_LEFT))

    def test_get_rejects_invalid_side(self) -> None:
        with self.assertRaises(ValueError):
            self.service.get("middle")


class AssignTests(BaseServiceTestCase):
    def test_assign_writes_file(self) -> None:
        passport = self.service.assign(SIDE_LEFT, "STOCK_BASELINE_LEFT")
        self.assertIsNotNone(passport)
        assert passport is not None
        path = self.service.path_for(SIDE_LEFT)
        self.assertTrue(path.exists())
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["module_id"], "STOCK_BASELINE_LEFT")
        self.assertEqual(on_disk["side"], SIDE_LEFT)

    def test_assign_sanitises_module_id(self) -> None:
        passport = self.service.assign(SIDE_RIGHT, "  K   silver  ", notes="x")
        assert passport is not None
        self.assertEqual(passport.module_id, "K silver")

    def test_assign_rejects_empty_module_id(self) -> None:
        result = self.service.assign(SIDE_LEFT, "   \n  ", notes="")
        self.assertIsNone(result)
        self.assertFalse(self.service.path_for(SIDE_LEFT).exists())

    def test_assign_archives_prior_passport(self) -> None:
        first = self.service.assign(SIDE_LEFT, "STOCK_FIRST")
        assert first is not None
        second = self.service.assign(SIDE_LEFT, "K_SILVER")
        assert second is not None
        archive_dir = self.service.archive_dir
        archived = list(archive_dir.glob("left_*"))
        self.assertEqual(len(archived), 1)
        archived_payload = json.loads(archived[0].read_text(encoding="utf-8"))
        self.assertEqual(archived_payload["module_id"], "STOCK_FIRST")
        # Active passport is the new one.
        active = self.service.get(SIDE_LEFT)
        assert active is not None
        self.assertEqual(active.module_id, "K_SILVER")

    def test_assign_archives_history_intact(self) -> None:
        passport = self.service.assign(SIDE_LEFT, "STOCK")
        assert passport is not None
        self.service.append_fingerprint(SIDE_LEFT, _fingerprint())
        self.service.append_fingerprint(SIDE_LEFT, _fingerprint(overall_status=STATUS_WATCH))
        self.service.assign(SIDE_LEFT, "NEW")
        archived_paths = list(self.service.archive_dir.glob("left_STOCK_*"))
        self.assertEqual(len(archived_paths), 1)
        archived = json.loads(archived_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(len(archived["fingerprints"]), 2)

    def test_assign_emits_wear_ledger_event(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK_LEFT", notes="baseline")
        events = self.ledger.read_events(event_types=[MODULE_ASSIGNED])
        self.assertEqual(len(events), 1)
        details = dict(events[0].details)
        self.assertEqual(details["side"], SIDE_LEFT)
        self.assertEqual(details["module_id"], "STOCK_LEFT")
        self.assertIsNone(details["replaced_module_id"])

    def test_assign_records_replaced_module_id(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK_LEFT")
        self.service.assign(SIDE_LEFT, "K_SILVER")
        events = self.ledger.read_events(event_types=[MODULE_ASSIGNED])
        latest = events[0]  # read_events sorts newest first
        self.assertEqual(latest.details["module_id"], "K_SILVER")
        self.assertEqual(latest.details["replaced_module_id"], "STOCK_LEFT")


class AppendFingerprintTests(BaseServiceTestCase):
    def test_append_returns_none_when_no_active_passport(self) -> None:
        result = self.service.append_fingerprint(SIDE_LEFT, _fingerprint())
        self.assertIsNone(result)

    def test_append_records_fingerprint(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK")
        fp = _fingerprint()
        updated = self.service.append_fingerprint(SIDE_LEFT, fp)
        assert updated is not None
        self.assertEqual(updated.fingerprints, (fp,))
        reread = self.service.get(SIDE_LEFT)
        assert reread is not None
        self.assertEqual(reread.fingerprints, (fp,))

    def test_append_refuses_cross_side(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK_LEFT")
        wrong = _fingerprint(side=SIDE_RIGHT)
        self.assertIsNone(self.service.append_fingerprint(SIDE_LEFT, wrong))

    def test_append_emits_wear_ledger_event(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK")
        self.service.append_fingerprint(
            SIDE_LEFT, _fingerprint(overall_status=STATUS_WATCH)
        )
        events = self.ledger.read_events(event_types=[MODULE_CHARACTERIZED])
        self.assertEqual(len(events), 1)
        details = dict(events[0].details)
        self.assertEqual(details["side"], SIDE_LEFT)
        self.assertEqual(details["module_id"], "STOCK")
        self.assertEqual(details["overall_status"], STATUS_WATCH)
        self.assertEqual(details["duration_ms"], 60_000)
        self.assertEqual(details["samples_count"], 12_000)
        self.assertIn("noise_floor_percent", details)
        self.assertIn("bitness_observed", details)


class UpdateNotesTests(BaseServiceTestCase):
    def test_update_notes_returns_none_when_no_passport(self) -> None:
        self.assertIsNone(self.service.update_notes(SIDE_LEFT, "x"))

    def test_update_notes_writes_and_emits_event(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK")
        result = self.service.update_notes(SIDE_LEFT, "New notes about feel.")
        assert result is not None
        self.assertEqual(result.notes, "New notes about feel.")
        events = self.ledger.read_events(event_types=[MODULE_NOTES_UPDATED])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].details["module_id"], "STOCK")
        self.assertEqual(events[0].details["notes_length"], len("New notes about feel."))

    def test_update_notes_noop_when_unchanged(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK", notes="same text")
        before = self.ledger.read_events(event_types=[MODULE_NOTES_UPDATED])
        result = self.service.update_notes(SIDE_LEFT, "same text")
        self.assertIsNotNone(result)
        after = self.ledger.read_events(event_types=[MODULE_NOTES_UPDATED])
        self.assertEqual(len(before), len(after))

    def test_update_notes_sanitises_input(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK")
        self.service.update_notes(SIDE_LEFT, "line1\r\nline2\x00")
        passport = self.service.get(SIDE_LEFT)
        assert passport is not None
        self.assertEqual(passport.notes, "line1\nline2")


class ListArchiveTests(BaseServiceTestCase):
    def test_list_archive_empty(self) -> None:
        self.assertEqual(self.service.list_archive(), [])

    def test_list_archive_returns_newest_first(self) -> None:
        self.service.assign(SIDE_LEFT, "FIRST")
        self.service.assign(SIDE_LEFT, "SECOND")
        self.service.assign(SIDE_LEFT, "THIRD")
        archived = self.service.list_archive()
        # Two archives written (FIRST -> archive when SECOND lands, SECOND ->
        # archive when THIRD lands).
        self.assertEqual(len(archived), 2)
        self.assertEqual(archived[0].module_id, "SECOND")
        self.assertEqual(archived[1].module_id, "FIRST")

    def test_list_archive_filters_by_side(self) -> None:
        self.service.assign(SIDE_LEFT, "L_FIRST")
        self.service.assign(SIDE_LEFT, "L_SECOND")
        self.service.assign(SIDE_RIGHT, "R_FIRST")
        self.service.assign(SIDE_RIGHT, "R_SECOND")
        left = self.service.list_archive(SIDE_LEFT)
        right = self.service.list_archive(SIDE_RIGHT)
        self.assertEqual([p.module_id for p in left], ["L_FIRST"])
        self.assertEqual([p.module_id for p in right], ["R_FIRST"])

    def test_list_archive_skips_corrupted(self) -> None:
        self.service.assign(SIDE_LEFT, "FIRST")
        self.service.assign(SIDE_LEFT, "SECOND")
        # Drop a corrupted file in the archive — list_archive must skip it.
        corrupted = self.service.archive_dir / "left_BROKEN_20260526T000000Z.json"
        corrupted.write_text("not-json{", encoding="utf-8")
        archived = self.service.list_archive(SIDE_LEFT)
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].module_id, "FIRST")


class ListArchiveEntriesTests(BaseServiceTestCase):
    """``list_archive_entries`` augments ``list_archive`` with archived_at_utc.

    The archived_at_utc is parsed back out of the archive filename stem,
    which is the only place the timestamp lives (the passport JSON itself
    doesn't carry it).
    """

    def test_empty(self) -> None:
        self.assertEqual(self.service.list_archive_entries(), [])

    def test_returns_archived_at_from_filename(self) -> None:
        self.service.assign(SIDE_LEFT, "FIRST")
        # Advance the clock so SECOND's archive stamp differs from FIRST's.
        self.service.assign(SIDE_LEFT, "SECOND")
        entries = self.service.list_archive_entries(SIDE_LEFT)
        self.assertEqual(len(entries), 1)
        passport, archived_at = entries[0]
        self.assertEqual(passport.module_id, "FIRST")
        # Parsed UTC matches the assigned_at_utc shape: 'YYYY-MM-DDTHH:MM:SSZ'.
        self.assertRegex(archived_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_sort_newest_archived_first(self) -> None:
        self.service.assign(SIDE_LEFT, "FIRST")
        self.service.assign(SIDE_LEFT, "SECOND")
        self.service.assign(SIDE_LEFT, "THIRD")
        entries = self.service.list_archive_entries(SIDE_LEFT)
        # Two archive files — SECOND was archived later than FIRST.
        ids = [p.module_id for p, _ in entries]
        self.assertEqual(ids, ["SECOND", "FIRST"])

    def test_filters_by_side(self) -> None:
        self.service.assign(SIDE_LEFT, "L_FIRST")
        self.service.assign(SIDE_LEFT, "L_SECOND")
        self.service.assign(SIDE_RIGHT, "R_FIRST")
        self.service.assign(SIDE_RIGHT, "R_SECOND")
        left = self.service.list_archive_entries(SIDE_LEFT)
        right = self.service.list_archive_entries(SIDE_RIGHT)
        self.assertEqual([p.module_id for p, _ in left], ["L_FIRST"])
        self.assertEqual([p.module_id for p, _ in right], ["R_FIRST"])

    def test_skips_corrupted(self) -> None:
        self.service.assign(SIDE_LEFT, "FIRST")
        self.service.assign(SIDE_LEFT, "SECOND")
        corrupted = self.service.archive_dir / "left_BROKEN_20260526T000000Z.json"
        corrupted.write_text("not-json{", encoding="utf-8")
        entries = self.service.list_archive_entries(SIDE_LEFT)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][0].module_id, "FIRST")

    def test_handles_unparseable_filename_stamp(self) -> None:
        # Manually drop a passport file with a stem that doesn't match the
        # _ARCHIVE_STAMP_RE pattern. The entry should still be returned
        # with an empty archived_at, sinking to the bottom of the sort.
        self.service.assign(SIDE_LEFT, "STAMPED")
        # Drop a stamp-less file alongside.
        odd = self.service.archive_dir / "left_LEGACY.json"
        odd_payload = {
            "schema_version": 1,
            "side": SIDE_LEFT,
            "module_id": "LEGACY",
            "assigned_at_utc": "2025-01-01T00:00:00Z",
            "notes": "",
            "fingerprints": [],
        }
        import json as _json

        odd.write_text(_json.dumps(odd_payload), encoding="utf-8")
        # Now reassign once more so STAMPED gets archived too — that one has
        # a real stamp; LEGACY has empty stamp and sinks below.
        self.service.assign(SIDE_LEFT, "NEW_HEAD")
        entries = self.service.list_archive_entries(SIDE_LEFT)
        ids_and_stamps = [(p.module_id, ts) for p, ts in entries]
        # STAMPED's archived_at is populated (non-empty), LEGACY's is empty.
        legacy_entry = next(item for item in ids_and_stamps if item[0] == "LEGACY")
        stamped_entry = next(item for item in ids_and_stamps if item[0] == "STAMPED")
        self.assertEqual(legacy_entry[1], "")
        self.assertNotEqual(stamped_entry[1], "")
        # Sort puts stamped first (non-empty timestamps), legacy last.
        self.assertEqual(ids_and_stamps[-1][0], "LEGACY")


class ConstructionTests(unittest.TestCase):
    def test_construction_creates_archive_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "passports"
            ModulePassportService(base_dir=base)
            self.assertTrue((base / ARCHIVE_DIRNAME).exists())

    def test_construction_tolerates_mkdir_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "passports"
            with mock.patch(
                "zd_app.services.module_passport.service.Path.mkdir",
                side_effect=OSError("simulated"),
            ):
                # Should not raise.
                service = ModulePassportService(base_dir=base)
            # Subsequent get returns None (no file, swallowed errors).
            self.assertIsNone(service.get(SIDE_LEFT))

    def test_assign_with_no_wear_ledger_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ModulePassportService(base_dir=Path(tmp))
            self.assertIsNotNone(service.assign(SIDE_LEFT, "ID"))


class SideValidationTests(BaseServiceTestCase):
    def test_get_rejects_unknown_side(self) -> None:
        with self.assertRaises(ValueError):
            self.service.get("middle")

    def test_assign_rejects_unknown_side(self) -> None:
        with self.assertRaises(ValueError):
            self.service.assign("middle", "ID")

    def test_append_rejects_unknown_side(self) -> None:
        with self.assertRaises(ValueError):
            self.service.append_fingerprint("middle", _fingerprint())

    def test_update_notes_rejects_unknown_side(self) -> None:
        with self.assertRaises(ValueError):
            self.service.update_notes("middle", "x")

    def test_archive_existing_refuses_out_of_range_side(self) -> None:
        # Defense-in-depth: a directly-constructed passport with a tampered side
        # (the load boundary normally rejects it) must not archive to a path
        # outside the archive dir — _archive_existing validates side first.
        bad = ModulePassport(
            side="../../evil",
            module_id="STOCK",
            assigned_at_utc="2026-05-26T18:00:00Z",
            notes="",
        )
        with self.assertRaises(ValueError):
            self.service._archive_existing(bad)
        # Nothing was written anywhere under (or above) the base dir.
        self.assertEqual(list(self.base.rglob("*evil*")), [])
        self.assertEqual(list(self.base.parent.glob("*evil*")), [])


class IsolationTests(BaseServiceTestCase):
    """Per-side semantics: writes to one side never touch the other."""

    def test_left_and_right_are_independent(self) -> None:
        self.service.assign(SIDE_LEFT, "L_ID")
        self.service.assign(SIDE_RIGHT, "R_ID")
        l_pass = self.service.get(SIDE_LEFT)
        r_pass = self.service.get(SIDE_RIGHT)
        assert l_pass is not None and r_pass is not None
        self.assertEqual(l_pass.module_id, "L_ID")
        self.assertEqual(r_pass.module_id, "R_ID")

    def test_reassigning_left_does_not_touch_right(self) -> None:
        self.service.assign(SIDE_LEFT, "L_ID")
        self.service.assign(SIDE_RIGHT, "R_ID")
        self.service.assign(SIDE_LEFT, "L_NEW")
        r_pass = self.service.get(SIDE_RIGHT)
        assert r_pass is not None
        self.assertEqual(r_pass.module_id, "R_ID")


# ---------------------------------------------------------------------------
# Trend integration — compute_passport_trend + module_trend_flagged event
# ---------------------------------------------------------------------------


def _trend_fingerprint(
    *,
    day_offset: float,
    base_ts: datetime = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    side: str = SIDE_LEFT,
    noise_floor_percent: float = 1.0,
    samples_count: int = 12_000,
) -> ModuleFingerprint:
    """Build a fingerprint at ``base_ts + day_offset`` days, otherwise standard."""

    ts = base_ts + timedelta(days=day_offset)
    return ModuleFingerprint(
        timestamp_utc=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        side=side,
        duration_ms=60_000,
        samples_count=samples_count,
        noise_floor_percent=noise_floor_percent,
        centering_offset_x=0.2,
        centering_offset_y=-0.1,
        circularity_coverage_percent=0.97,
        outer_deadzone_min_axis=96.0,
        outer_deadzone_max_axis=99.0,
        asymmetry_score=0.04,
        bitness_observed=128,
        tremor_metric=0.18,
        linearity_score=0.05,
        overall_status=STATUS_GOOD,
        notes=None,
    )


class ComputePassportTrendTests(BaseServiceTestCase):
    def test_returns_none_when_no_passport(self) -> None:
        self.assertIsNone(self.service.compute_passport_trend(SIDE_LEFT))

    def test_returns_insufficient_for_empty_passport(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK")
        summary = self.service.compute_passport_trend(SIDE_LEFT)
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.status, TREND_STATUS_INSUFFICIENT)

    def test_returns_insufficient_for_single_fingerprint(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK")
        self.service.append_fingerprint(
            SIDE_LEFT, _trend_fingerprint(day_offset=0.0)
        )
        summary = self.service.compute_passport_trend(SIDE_LEFT)
        assert summary is not None
        self.assertEqual(summary.status, TREND_STATUS_INSUFFICIENT)

    def test_drifting_detected_across_span(self) -> None:
        # 8 fingerprints, 1-day spaced, noise_floor up 0.01 per day —
        # long runway → drifting (not investigate).
        self.service.assign(SIDE_LEFT, "STOCK")
        for i in range(8):
            self.service.append_fingerprint(
                SIDE_LEFT,
                _trend_fingerprint(
                    day_offset=float(i), noise_floor_percent=1.0 + 0.01 * i
                ),
            )
        summary = self.service.compute_passport_trend(SIDE_LEFT)
        assert summary is not None
        self.assertEqual(summary.status, TREND_STATUS_DRIFTING)

    def test_rejects_invalid_side(self) -> None:
        with self.assertRaises(ValueError):
            self.service.compute_passport_trend("middle")


class TrendFlaggedEventTests(BaseServiceTestCase):
    def test_no_event_on_first_fingerprint(self) -> None:
        # Pre + post both land at insufficient_data → no flip → no event.
        self.service.assign(SIDE_LEFT, "STOCK")
        self.service.append_fingerprint(
            SIDE_LEFT, _trend_fingerprint(day_offset=0.0)
        )
        events = self.ledger.read_events(event_types=[MODULE_TREND_FLAGGED])
        self.assertEqual(events, [])

    def test_event_fires_on_status_flip_to_drifting(self) -> None:
        # Append 8 fingerprints across 7+ days with a mild upward trend.
        # The first 3 stay insufficient; somewhere along the way the status
        # flips to drifting and the event fires exactly once.
        self.service.assign(SIDE_LEFT, "STOCK")
        for i in range(8):
            self.service.append_fingerprint(
                SIDE_LEFT,
                _trend_fingerprint(
                    day_offset=float(i), noise_floor_percent=1.0 + 0.01 * i
                ),
            )
        events = self.ledger.read_events(event_types=[MODULE_TREND_FLAGGED])
        # Exactly one flip event (insufficient → drifting). Subsequent
        # appends keep status at drifting so no re-fire.
        self.assertEqual(len(events), 1)
        details = dict(events[0].details)
        self.assertEqual(details["side"], SIDE_LEFT)
        self.assertEqual(details["module_id"], "STOCK")
        self.assertEqual(details["new_status"], TREND_STATUS_DRIFTING)
        self.assertIn("attention_metrics", details)
        self.assertIn("noise_floor", details["attention_metrics"])

    def test_no_event_on_status_improvement(self) -> None:
        # Push to drifting, then add a final fingerprint that keeps things
        # in the drifting band. The "stable → drifting" flip is what emits,
        # NOT improvement back the other direction.
        self.service.assign(SIDE_LEFT, "STOCK")
        for i in range(8):
            self.service.append_fingerprint(
                SIDE_LEFT,
                _trend_fingerprint(
                    day_offset=float(i), noise_floor_percent=1.0 + 0.01 * i
                ),
            )
        flip_events = self.ledger.read_events(event_types=[MODULE_TREND_FLAGGED])
        self.assertEqual(len(flip_events), 1)
        # Append more fingerprints keeping the same trend slope — status
        # remains drifting, no additional events emitted.
        for i in range(8, 12):
            self.service.append_fingerprint(
                SIDE_LEFT,
                _trend_fingerprint(
                    day_offset=float(i), noise_floor_percent=1.0 + 0.01 * i
                ),
            )
        after_events = self.ledger.read_events(event_types=[MODULE_TREND_FLAGGED])
        self.assertEqual(len(after_events), 1)

    def test_event_details_include_previous_and_new_status(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK")
        for i in range(8):
            self.service.append_fingerprint(
                SIDE_LEFT,
                _trend_fingerprint(
                    day_offset=float(i), noise_floor_percent=1.0 + 0.01 * i
                ),
            )
        events = self.ledger.read_events(event_types=[MODULE_TREND_FLAGGED])
        self.assertEqual(len(events), 1)
        details = dict(events[0].details)
        # First flip is insufficient_data → drifting since N had to reach
        # the confidence floor before the trend could fire.
        self.assertIn(
            details["previous_status"],
            (TREND_STATUS_INSUFFICIENT, TREND_STATUS_STABLE),
        )
        self.assertEqual(details["new_status"], TREND_STATUS_DRIFTING)


class GuardedJsonReadTests(BaseServiceTestCase):
    """P5 [G4]: passport reads go through read_guarded_json, so a deeply nested
    or oversize local/tampered file is skipped instead of raising RecursionError
    (which escaped the old except — get() catches ValueError but not
    RecursionError — and crashed the read).

    The byte ceiling is now the passport-specific ``MAX_PASSPORT_BYTES`` (16 MiB,
    not the 1 MiB untrusted-import default; see PassportReadCapTests), so the
    ~1.4 MiB ``_OVERSIZE`` fixture sits UNDER the real cap. These tests patch the
    cap below the fixture to keep exercising the over-ceiling skip path; the
    depth guard is unchanged, so the deep-file cases need no patch."""

    _DEEP = "[" * 20000 + "]" * 20000
    _OVERSIZE = "[" + "0," * 700000 + "0]"
    _PATCHED_CAP = 100_000  # below _OVERSIZE (~1.4 MiB), above a real passport

    def test_get_tolerates_deep_nested_file(self) -> None:
        path = self.service.path_for(SIDE_LEFT)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._DEEP, encoding="utf-8")
        self.assertIsNone(self.service.get(SIDE_LEFT))  # must not raise

    def test_get_tolerates_oversize_file(self) -> None:
        path = self.service.path_for(SIDE_LEFT)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._OVERSIZE, encoding="utf-8")
        with mock.patch(
            "zd_app.services.module_passport.service.MAX_PASSPORT_BYTES",
            self._PATCHED_CAP,
        ):
            self.assertIsNone(self.service.get(SIDE_LEFT))  # must not raise

    def test_list_archive_skips_deep_and_oversize_files(self) -> None:
        self.service.assign(SIDE_LEFT, "FIRST")
        self.service.assign(SIDE_LEFT, "SECOND")  # archives FIRST
        archive = self.service.archive_dir
        (archive / "left_DEEP_20260526T000000Z.json").write_text(
            self._DEEP, encoding="utf-8"
        )
        (archive / "left_BIG_20260526T000001Z.json").write_text(
            self._OVERSIZE, encoding="utf-8"
        )

        with mock.patch(
            "zd_app.services.module_passport.service.MAX_PASSPORT_BYTES",
            self._PATCHED_CAP,
        ):
            archived = self.service.list_archive(SIDE_LEFT)  # must not raise
            entries = self.service.list_archive_entries(SIDE_LEFT)  # must not raise

        self.assertEqual([p.module_id for p in archived], ["FIRST"])
        self.assertEqual([p.module_id for p, _ in entries], ["FIRST"])


class PassportReadCapTests(BaseServiceTestCase):
    """P3 (round-3): passport reads use the passport-specific
    ``MAX_PASSPORT_BYTES`` ceiling, not the 1 MiB untrusted-import default.

    A passport is app-owned and append-only — one fingerprint per ~60s
    characterization run — so a heavy user's file grows past 1 MiB (~1,900
    fingerprints) over its lifetime. Under the import cap, ``get()`` returned
    ``None`` for that user and the side silently dropped out of the diagnostic
    bundle ("Not assigned", no ``module_passports/{side}.json`` member).
    Fail-on-base: ``get()`` returns ``None`` for a valid >1 MiB passport."""

    def _persist_passport(self, side: str, n_fingerprints: int) -> ModulePassport:
        passport = ModulePassport(
            side=side,
            module_id="STOCK_BIG",
            assigned_at_utc="2026-05-26T18:00:00Z",
            notes="",
            fingerprints=tuple(
                _fingerprint(side=side) for _ in range(n_fingerprints)
            ),
            schema_version=SCHEMA_VERSION,
        )
        self.assertTrue(self.service._persist(passport))
        return passport

    def test_get_returns_valid_passport_above_import_cap(self) -> None:
        passport = self._persist_passport(SIDE_LEFT, 2500)
        size = self.service.path_for(SIDE_LEFT).stat().st_size
        # Premise: the file sits between the old import cap and the new one.
        self.assertGreater(size, MAX_IMPORT_BYTES)
        self.assertLess(size, MAX_PASSPORT_BYTES)

        loaded = self.service.get(SIDE_LEFT)  # base: None (file > 1 MiB)
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded.fingerprints), 2500)
        self.assertEqual(loaded, passport)

    def test_passport_above_passport_cap_still_skipped(self) -> None:
        # Sub-cap control: a file ABOVE the passport ceiling is still skipped
        # (returns None, never raises) — the guard is raised, not removed.
        self._persist_passport(SIDE_RIGHT, 50)  # ~27 KB
        with mock.patch(
            "zd_app.services.module_passport.service.MAX_PASSPORT_BYTES", 4096
        ):
            self.assertIsNone(self.service.get(SIDE_RIGHT))

    def test_large_passport_renders_in_diagnostic_bundle(self) -> None:
        # The P3 payoff: a >1 MiB passport no longer drops out of the shareable
        # diagnostic bundle. Base: get() -> None -> "Not assigned" + the
        # module_passports/left.json member is omitted.
        import zipfile

        from zd_app.services.diagnostic_bundle import DiagnosticBundleService

        self._persist_passport(SIDE_LEFT, 2500)
        self.service.assign(SIDE_RIGHT, "STOCK_RIGHT")  # small; so neither side
        #                                                  is "Not assigned"
        bundle = DiagnosticBundleService(
            base_dir=self.base / "bundles",
            module_passport_service=self.service,
            health_report_dir=self.base / "health_reports",
            wear_ledger=self.ledger,
            app_data_dir=self.base,
            utc_now=self.clock,
        )

        md = bundle.generate_markdown()
        self.assertNotIn("Not assigned", md)
        self.assertIn("STOCK_BIG", md)  # the >1 MiB left passport rendered

        target = self.base / "bundle.zip"
        self.assertIsNotNone(bundle.generate_bundle_zip(target))
        with zipfile.ZipFile(target, "r") as zf:
            names = set(zf.namelist())
        self.assertIn(f"module_passports/{SIDE_LEFT}.json", names)


if __name__ == "__main__":
    unittest.main()
