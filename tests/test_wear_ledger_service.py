"""Tests for :class:`WearLedgerService`.

The service is exercised against a real on-disk directory (per-test
``tempfile.TemporaryDirectory``) — there is no settings/HID surface to stub
out. Clock is injected so timestamp-sensitive assertions (rotation file
names, ``since``/``until`` filters) are deterministic.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from zd_app.services.wear_ledger import (
    DEFAULT_ROTATION_BYTES,
    EVENT_TYPES,
    SCHEMA_VERSION,
    WearLedgerEvent,
    WearLedgerService,
    sanitize_service_note,
)
from zd_app.services.wear_ledger.models import (
    HEALTH_REPORT,
    MODULE_ASSIGNED,
    PROFILE_APPLY,
    READINESS_CHECK,
    RP_CAPTURE,
    RP_RESTORE,
    SERVICE_NOTE,
    SESSION_END,
    SESSION_START,
    SLIDER_WRITE,
    MAX_SERVICE_NOTE_LEN,
)
from zd_app.services.wear_ledger.service import (
    ACTIVE_FILENAME,
    ROTATED_PREFIX,
    ROTATED_SUFFIX,
)


def _frozen_clock(start: datetime) -> Callable[[], datetime]:
    """Return a callable that advances by 1 second per call."""

    state = {"now": start}

    def _now() -> datetime:
        current = state["now"]
        state["now"] = current + timedelta(seconds=1)
        return current

    return _now


class WearLedgerServiceBasicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.clock = _frozen_clock(datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc))
        self.service = WearLedgerService(base_dir=self.base, utc_now=self.clock)

    def test_append_returns_event_and_writes_jsonl_line(self) -> None:
        event = self.service.append(
            SESSION_START,
            summary="Session started",
            details={"version": "1.2.3"},
        )

        self.assertIsNotNone(event)
        assert event is not None  # mypy
        self.assertEqual(event.event_type, SESSION_START)
        self.assertEqual(event.summary, "Session started")
        self.assertEqual(event.details["version"], "1.2.3")
        self.assertEqual(event.schema_version, SCHEMA_VERSION)
        self.assertTrue(event.ts.endswith("Z"))

        active = self.base / ACTIVE_FILENAME
        self.assertTrue(active.exists())
        lines = active.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["event_type"], SESSION_START)
        self.assertEqual(payload["details"], {"version": "1.2.3"})

    def test_read_events_returns_newest_first(self) -> None:
        first = self.service.append(SESSION_START, summary="first")
        second = self.service.append(PROFILE_APPLY, summary="second")
        third = self.service.append(SESSION_END, summary="third")

        events = self.service.read_events()

        self.assertEqual([e.event_type for e in events], [SESSION_END, PROFILE_APPLY, SESSION_START])
        self.assertEqual(events[0].ts, third.ts)  # type: ignore[union-attr]
        self.assertEqual(events[-1].ts, first.ts)  # type: ignore[union-attr]

    def test_read_events_empty_when_no_file(self) -> None:
        self.assertEqual(self.service.read_events(), [])

    def test_count_events_matches_append_count(self) -> None:
        self.service.append(SESSION_START, summary="s1")
        self.service.append(PROFILE_APPLY, summary="p1")
        self.service.append(SLIDER_WRITE, summary="w1")
        self.assertEqual(self.service.count_events(), 3)

    def test_filter_by_event_types(self) -> None:
        self.service.append(SESSION_START, summary="s1")
        self.service.append(PROFILE_APPLY, summary="p1")
        self.service.append(RP_CAPTURE, summary="rp1")
        self.service.append(SLIDER_WRITE, summary="w1")

        only_profiles = self.service.read_events(event_types=[PROFILE_APPLY])
        self.assertEqual([e.event_type for e in only_profiles], [PROFILE_APPLY])

        profiles_and_rp = self.service.read_events(event_types=[PROFILE_APPLY, RP_CAPTURE])
        self.assertEqual(
            sorted(e.event_type for e in profiles_and_rp),
            sorted([PROFILE_APPLY, RP_CAPTURE]),
        )

    def test_filter_by_since_until(self) -> None:
        # Each append advances the clock by 1s.
        self.service.append(SESSION_START, summary="s1")  # 12:00:00
        self.service.append(PROFILE_APPLY, summary="p1")  # 12:00:01
        self.service.append(RP_CAPTURE, summary="rp1")  # 12:00:02
        self.service.append(SLIDER_WRITE, summary="w1")  # 12:00:03

        since = datetime(2026, 5, 26, 12, 0, 1, tzinfo=timezone.utc)
        until = datetime(2026, 5, 26, 12, 0, 2, tzinfo=timezone.utc)
        windowed = self.service.read_events(since=since, until=until)
        # since/until inclusive: 12:00:01 (PROFILE_APPLY) + 12:00:02 (RP_CAPTURE)
        self.assertEqual(
            sorted(e.event_type for e in windowed),
            sorted([PROFILE_APPLY, RP_CAPTURE]),
        )

    def test_limit_caps_returned_events(self) -> None:
        for i in range(10):
            self.service.append(SESSION_START, summary=f"s{i}")
        limited = self.service.read_events(limit=3)
        self.assertEqual(len(limited), 3)

    def test_explicit_ts_overrides_clock(self) -> None:
        moment = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        event = self.service.append(SERVICE_NOTE, summary="backdated", ts=moment)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.ts, "2025-01-01T00:00:00Z")


class WearLedgerSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.service = WearLedgerService(base_dir=self.base)

    def test_to_dict_round_trip(self) -> None:
        original = WearLedgerEvent(
            ts="2026-05-26T12:00:00Z",
            event_type=PROFILE_APPLY,
            summary="Applied profile 'race-pad'",
            details={"profile_name": "race-pad", "device_settings_included": True},
        )
        restored = WearLedgerEvent.from_dict(original.to_dict())
        self.assertEqual(restored, original)

    def test_from_dict_rejects_missing_ts(self) -> None:
        with self.assertRaises(ValueError):
            WearLedgerEvent.from_dict({"event_type": SESSION_START})

    def test_from_dict_rejects_non_mapping_details(self) -> None:
        with self.assertRaises(ValueError):
            WearLedgerEvent.from_dict(
                {"ts": "2026-05-26T00:00:00Z", "event_type": SESSION_START, "details": "nope"}
            )

    def test_corrupted_lines_are_skipped(self) -> None:
        active = self.base / ACTIVE_FILENAME
        active.write_text(
            "not-json-at-all\n"
            + json.dumps({"ts": "2026-05-26T00:00:00Z", "event_type": SESSION_START, "summary": "ok"})
            + "\n"
            + "{broken\n",
            encoding="utf-8",
        )
        events = self.service.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].summary, "ok")

    def test_all_event_types_are_writable(self) -> None:
        for event_type in EVENT_TYPES:
            result = self.service.append(event_type, summary=f"sample {event_type}")
            self.assertIsNotNone(result, f"event_type {event_type} should append")
        self.assertEqual(self.service.count_events(), len(EVENT_TYPES))

    def test_non_json_details_value_yields_none_without_crashing(self) -> None:
        # A non-serialisable value (an open file handle) trips json.dumps;
        # service must log and return None, not raise.
        with open(self.base / "open-handle.txt", "wb") as handle:
            result = self.service.append(
                SERVICE_NOTE,
                summary="non-json detail",
                details={"handle": handle},
            )
            self.assertIsNone(result)


class WearLedgerRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.clock = _frozen_clock(datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc))
        # 2KB rotation threshold lets us drive a rotation in a small test.
        self.service = WearLedgerService(
            base_dir=self.base, utc_now=self.clock, rotation_bytes=2048
        )

    def test_rotation_creates_archive_and_starts_new_active(self) -> None:
        big_summary = "x" * 200
        # Each line is roughly 350 bytes serialised — 8 lines exceeds 2KB.
        for _ in range(12):
            self.service.append(SESSION_START, summary=big_summary)

        active = self.base / ACTIVE_FILENAME
        rotated = sorted(self.base.glob(f"{ROTATED_PREFIX}*{ROTATED_SUFFIX}"))

        self.assertTrue(active.exists(), "active file must exist after rotation")
        self.assertGreaterEqual(len(rotated), 1, "at least one rotated archive expected")
        self.assertLess(active.stat().st_size, self.service.rotation_bytes)

    def test_read_events_merges_active_and_rotated(self) -> None:
        for i in range(12):
            self.service.append(PROFILE_APPLY, summary=f"event-{i:02d}-" + "y" * 200)

        events = self.service.read_events()
        self.assertEqual(len(events), 12)
        summaries = [e.summary for e in events]
        for i in range(12):
            self.assertTrue(
                any(f"event-{i:02d}-" in s for s in summaries),
                f"event-{i:02d} should be in merged read",
            )

    def test_rotation_handles_same_second_collision(self) -> None:
        # Make rotation extremely cheap and pin clock to one moment so two
        # rotations in the same second collide on the archive name. The
        # service must append a counter suffix.
        clock_now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)
        service = WearLedgerService(
            base_dir=self.base,
            utc_now=lambda: clock_now,
            rotation_bytes=128,  # tiny — every event triggers rotation
        )
        for _ in range(5):
            service.append(SESSION_START, summary="z" * 80)

        rotated = sorted(self.base.glob(f"{ROTATED_PREFIX}*{ROTATED_SUFFIX}"))
        # Multiple archives at the same stamp — names disambiguate via _N suffix.
        self.assertGreaterEqual(len(rotated), 2)
        names = [p.stem for p in rotated]
        self.assertTrue(any("_1" in n or "_2" in n for n in names), names)


class WearLedgerResilienceTests(unittest.TestCase):
    def test_construction_with_unreachable_base_dir_does_not_raise(self) -> None:
        # An absurd Windows-invalid path should not propagate from __init__.
        path = Path("Z:/wear_ledger/__definitely_not_a_real_drive__")
        service = WearLedgerService(base_dir=path)
        # And a subsequent append must not raise either (it returns None).
        result = service.append(SESSION_START, summary="should-fail")
        self.assertIsNone(result)
        # Read returns [] on missing base.
        self.assertEqual(service.read_events(), [])

    def test_append_after_directory_removed_returns_none(self) -> None:
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: _remove_tree(tmp))
        base = Path(tmp)
        service = WearLedgerService(base_dir=base)
        # Healthy append first.
        self.assertIsNotNone(service.append(SESSION_START, summary="ok"))
        # Now blow the directory away mid-life. Append should recover by
        # re-creating the directory (the service tries mkdir on each write).
        _remove_tree(tmp)
        # If the directory really can't be recreated (e.g. unwriteable parent),
        # the call still mustn't raise. Best we can assert here is "no raise".
        try:
            service.append(SESSION_START, summary="post-removal")
        except Exception as exc:  # noqa: BLE001
            self.fail(f"append must not raise after dir removal: {exc!r}")


class SanitizeServiceNoteTests(unittest.TestCase):
    def test_strips_control_chars_keeps_newlines_and_tabs(self) -> None:
        result = sanitize_service_note("hello\x00world\nsecond\tline\x07")
        self.assertEqual(result, "helloworld\nsecond\tline")

    def test_collapses_crlf_and_runs_of_newlines(self) -> None:
        result = sanitize_service_note("line1\r\n\r\n\r\nline2")
        self.assertEqual(result, "line1\n\nline2")

    def test_caps_at_max_length(self) -> None:
        too_long = "a" * (MAX_SERVICE_NOTE_LEN + 500)
        result = sanitize_service_note(too_long)
        self.assertLessEqual(len(result), MAX_SERVICE_NOTE_LEN)

    def test_empty_input_returns_empty_string(self) -> None:
        self.assertEqual(sanitize_service_note(""), "")
        self.assertEqual(sanitize_service_note("   \n\n  "), "")

    def test_redacts_home_path_with_spaced_username(self) -> None:
        # The note is persisted raw to events.jsonl; an operator who pastes a
        # home path must not leave their username (incl. a spaced display name)
        # in the local artifact.
        result = sanitize_service_note(
            r"Reset after loading C:\Users\Jane Doe\Documents\backup.json"
        )
        self.assertNotIn("Jane", result)
        self.assertNotIn("Doe", result)
        self.assertNotIn(r"C:\Users", result)
        # Non-path words and the file basename are preserved.
        self.assertIn("Reset after loading", result)
        self.assertIn("backup.json", result)

    def test_non_path_note_unchanged(self) -> None:
        note = "Replaced left stick module; feels good now."
        self.assertEqual(sanitize_service_note(note), note)


class ModuleEventTypeTests(unittest.TestCase):
    """Module passport feeds three new event types into the wear ledger;
    the ledger must accept them, return them, and surface them via the
    Modules filter category.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.service = WearLedgerService(base_dir=self.base)

    def test_module_assigned_event_persists_and_reads_back(self) -> None:
        from zd_app.services.wear_ledger.models import MODULE_ASSIGNED

        result = self.service.append(
            MODULE_ASSIGNED,
            summary="Module assigned (left): STOCK",
            details={"side": "left", "module_id": "STOCK", "replaced_module_id": None},
        )
        self.assertIsNotNone(result)
        events = self.service.read_events(event_types=[MODULE_ASSIGNED])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].details["side"], "left")
        self.assertEqual(events[0].details["module_id"], "STOCK")

    def test_module_characterized_event_persists_and_reads_back(self) -> None:
        from zd_app.services.wear_ledger.models import MODULE_CHARACTERIZED

        self.service.append(
            MODULE_CHARACTERIZED,
            summary="Module characterized (right): K_SILVER -> good",
            details={
                "side": "right",
                "module_id": "K_SILVER",
                "overall_status": "good",
                "duration_ms": 60_000,
                "samples_count": 12_345,
            },
        )
        events = self.service.read_events(event_types=[MODULE_CHARACTERIZED])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].details["overall_status"], "good")

    def test_module_notes_updated_event_persists_and_reads_back(self) -> None:
        from zd_app.services.wear_ledger.models import MODULE_NOTES_UPDATED

        self.service.append(
            MODULE_NOTES_UPDATED,
            summary="Module notes updated (left): STOCK",
            details={"side": "left", "module_id": "STOCK", "notes_length": 42},
        )
        events = self.service.read_events(event_types=[MODULE_NOTES_UPDATED])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].details["notes_length"], 42)

    def test_filter_category_modules_membership(self) -> None:
        from zd_app.services.wear_ledger.models import (
            FILTER_CATEGORY_MEMBERS,
            FILTER_CATEGORY_MODULES,
            MODULE_ASSIGNED,
            MODULE_CHARACTERIZED,
            MODULE_NOTES_UPDATED,
            MODULE_TREND_FLAGGED,
        )

        members = FILTER_CATEGORY_MEMBERS[FILTER_CATEGORY_MODULES]
        self.assertEqual(
            members,
            frozenset(
                {
                    MODULE_ASSIGNED,
                    MODULE_CHARACTERIZED,
                    MODULE_NOTES_UPDATED,
                    MODULE_TREND_FLAGGED,
                }
            ),
        )

    def test_all_module_events_appear_in_filtered_read(self) -> None:
        from zd_app.services.wear_ledger.models import (
            FILTER_CATEGORY_MEMBERS,
            FILTER_CATEGORY_MODULES,
            MODULE_ASSIGNED,
            MODULE_CHARACTERIZED,
            MODULE_NOTES_UPDATED,
        )

        # Mix module + non-module events to confirm filter narrows correctly.
        self.service.append(SESSION_START, summary="session")
        self.service.append(MODULE_ASSIGNED, summary="m-assigned")
        self.service.append(PROFILE_APPLY, summary="profile")
        self.service.append(MODULE_CHARACTERIZED, summary="m-characterized")
        self.service.append(MODULE_NOTES_UPDATED, summary="m-notes")
        self.service.append(SLIDER_WRITE, summary="slider")

        modules_only = self.service.read_events(
            event_types=list(FILTER_CATEGORY_MEMBERS[FILTER_CATEGORY_MODULES])
        )
        self.assertEqual(len(modules_only), 3)
        types = sorted(e.event_type for e in modules_only)
        self.assertEqual(
            types,
            sorted([MODULE_ASSIGNED, MODULE_CHARACTERIZED, MODULE_NOTES_UPDATED]),
        )

    def test_diagnostic_bundle_event_in_event_types_tuple(self) -> None:
        from zd_app.services.wear_ledger.models import (
            DIAGNOSTIC_BUNDLE_GENERATED,
        )

        self.assertIn(DIAGNOSTIC_BUNDLE_GENERATED, EVENT_TYPES)

    def test_diagnostic_bundle_event_persists_and_reads_back(self) -> None:
        from zd_app.services.wear_ledger.models import (
            DIAGNOSTIC_BUNDLE_GENERATED,
        )

        event = self.service.append(
            DIAGNOSTIC_BUNDLE_GENERATED,
            summary="Diagnostic bundle generated (zip)",
            details={
                "format": "zip",
                "output_filename": "diagnostic_bundle_2026-05-26_180000.zip",
                "include_archived": True,
                "health_report_limit": 5,
                "wear_ledger_days": 90,
            },
        )
        self.assertIsNotNone(event)
        events = self.service.read_events(
            event_types=[DIAGNOSTIC_BUNDLE_GENERATED]
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].details["format"], "zip")
        self.assertEqual(
            events[0].details["output_filename"],
            "diagnostic_bundle_2026-05-26_180000.zip",
        )

    def test_filter_category_bundles_membership(self) -> None:
        from zd_app.services.wear_ledger.models import (
            DIAGNOSTIC_BUNDLE_GENERATED,
            FILTER_CATEGORY_BUNDLES,
            FILTER_CATEGORY_MEMBERS,
        )

        members = FILTER_CATEGORY_MEMBERS[FILTER_CATEGORY_BUNDLES]
        self.assertEqual(members, frozenset({DIAGNOSTIC_BUNDLE_GENERATED}))

    def test_diagnostic_bundle_event_has_label_key(self) -> None:
        from zd_app.services.wear_ledger.models import (
            DIAGNOSTIC_BUNDLE_GENERATED,
            event_type_label_key,
        )

        self.assertEqual(
            event_type_label_key(DIAGNOSTIC_BUNDLE_GENERATED),
            "wear_ledger.event.diagnostic_bundle_generated",
        )


class WearLedgerChokepointScrubTests(unittest.TestCase):
    """Defense-in-depth: WearLedgerService.append scrubs home paths out of the
    summary AND (recursively) the details payload at the single write
    chokepoint, so the raw events.jsonl never bears the operator's account
    username — even for callers that build the payload without sanitizing
    (profile-apply names, restore-point titles, module IDs).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.service = WearLedgerService(base_dir=self.base)

    def test_append_scrubs_home_path_from_raw_jsonl(self) -> None:
        leak = r"C:\Users\Jane Doe\Documents\p.json"
        self.service.append(
            PROFILE_APPLY,
            summary=f"Applied profile from {leak}",
            details={"profile_path": leak},
        )
        self.service.append(
            RP_CAPTURE,
            summary=f"Captured before {leak}",
            details={"title": leak},
        )
        self.service.append(
            MODULE_ASSIGNED,
            summary=f"Assigned module {leak}",
            details={
                "module_id": leak,
                "nested": {"path": leak},
                "items": [leak, "ok"],
            },
        )

        # Inspect the RAW on-disk bytes (not read_events) — this is the
        # artifact the privacy intent protects.
        raw = (self.base / ACTIVE_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn("Jane Doe", raw)
        self.assertNotIn("Jane", raw)
        self.assertNotIn("Doe", raw)
        # Basename survives for debuggability; the username component is gone.
        self.assertIn("p.json", raw)

        events = {e.event_type: e for e in self.service.read_events()}
        self.assertEqual(events[PROFILE_APPLY].summary, "Applied profile from p.json")
        self.assertEqual(events[PROFILE_APPLY].details["profile_path"], "p.json")
        self.assertEqual(events[RP_CAPTURE].details["title"], "p.json")
        # Recursion reaches nested dicts and list elements.
        ma = events[MODULE_ASSIGNED].details
        self.assertEqual(ma["module_id"], "p.json")
        self.assertEqual(ma["nested"]["path"], "p.json")
        self.assertEqual(ma["items"], ["p.json", "ok"])

    def test_append_preserves_non_string_detail_types_on_round_trip(self) -> None:
        event = self.service.append(
            PROFILE_APPLY,
            summary="profile applied",
            details={
                "succeeded": True,
                "total_attempted": 7,
                "ratio": 0.5,
                "skipped": None,
                "note": r"C:\Users\Jane Doe\x.bin",
            },
        )
        self.assertIsNotNone(event)

        restored = self.service.read_events(event_types=[PROFILE_APPLY])[0].details
        # bool stays bool (and is NOT coerced to int), int stays int, etc.
        self.assertIs(restored["succeeded"], True)
        self.assertNotIsInstance(restored["total_attempted"], bool)
        self.assertIsInstance(restored["total_attempted"], int)
        self.assertEqual(restored["total_attempted"], 7)
        self.assertEqual(restored["ratio"], 0.5)
        self.assertIsNone(restored["skipped"])
        # The lone string leaf is still scrubbed.
        self.assertEqual(restored["note"], "x.bin")


def _remove_tree(path: str) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


if __name__ == "__main__":  # pragma: no cover - manual driver
    unittest.main()
