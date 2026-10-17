"""A parseable-but-non-object JSON root must degrade, never crash a reader.

Every reader covered here type-checks its *fields* carefully and used to skip
type-checking the *container*: a hand-edited or foreign file whose root is
``[]`` / ``"x"`` / ``3`` / ``null`` parses cleanly, then hits ``.get`` and
raises ``AttributeError`` past a catch tuple that never named it.

Each test asserts the reader's own documented degrade path — skip the file,
return ``None``, return an empty list — and NOT that the bad value is somehow
accepted. The roots are exercised as a matrix because the four shapes fail in
slightly different places (``null`` and ``3`` have no ``.get`` and no
``.items``; ``[]`` and ``"x"`` are iterable, so a sloppier fix could still
walk them).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from zd_app.services.module_passport import ModulePassportService
from zd_app.services.official_app_summary_service import OfficialAppSummaryService
from zd_app.services.wear_ledger import WearLedgerService
from zd_app.storage.restore_point_models import (
    RestorePointParseError,
    restore_point_from_dict,
)
from zd_app.storage.restore_point_store import RestorePointStore


NON_OBJECT_ROOTS: tuple[object, ...] = ([], "a string", 3, None)


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class RestorePointStoreNonObjectRootTests(unittest.TestCase):
    """``load`` / ``delete`` / ``prune`` read ``payload["id"]`` off the root."""

    def test_load_skips_a_non_object_file_and_reports_not_found(self) -> None:
        for root in NON_OBJECT_ROOTS:
            with self.subTest(root=root), tempfile.TemporaryDirectory() as tmp:
                store = RestorePointStore(base_dir=Path(tmp))
                _write(Path(tmp) / "20260727-120000_manual_abc123.json", root)

                with self.assertRaises(FileNotFoundError):
                    store.load("rp_20260727_120000_abc123")

    def test_delete_skips_a_non_object_file_and_reports_no_removal(self) -> None:
        for root in NON_OBJECT_ROOTS:
            with self.subTest(root=root), tempfile.TemporaryDirectory() as tmp:
                store = RestorePointStore(base_dir=Path(tmp))
                planted = Path(tmp) / "20260727-120000_manual_abc123.json"
                _write(planted, root)

                self.assertFalse(store.delete("rp_20260727_120000_abc123"))
                # Degrade means skip, not delete-something-else.
                self.assertTrue(planted.exists())

    def test_prune_skips_a_non_object_file_and_prunes_nothing(self) -> None:
        for root in NON_OBJECT_ROOTS:
            with self.subTest(root=root), tempfile.TemporaryDirectory() as tmp:
                store = RestorePointStore(base_dir=Path(tmp))
                _write(Path(tmp) / "20260727-120000_manual_abc123.json", root)

                self.assertEqual(store.prune(max_count=0, max_disk_mb=0), [])

    def test_list_surfaces_a_non_object_file_as_skipped(self) -> None:
        # Regression anchor, not a fail-on-base gate: ``list`` was already safe
        # because the codec checks its root. It is the sibling the other three
        # were supposed to match.
        for root in NON_OBJECT_ROOTS:
            with self.subTest(root=root), tempfile.TemporaryDirectory() as tmp:
                store = RestorePointStore(base_dir=Path(tmp))
                _write(Path(tmp) / "20260727-120000_manual_abc123.json", root)

                valid, skipped = store.list()

                self.assertEqual(valid, [])
                self.assertEqual(len(skipped), 1)


class RestorePointCodecNonObjectMemberTests(unittest.TestCase):
    """The root is checked; its sub-objects were not, and they reach ``.get``."""

    @staticmethod
    def _payload() -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "zd_restore_point",
            "id": "rp_20260727_120000_abc123",
            "created_at": "2026-07-27T12:00:00Z",
            "app_version": "2.6.2",
            "title": "planted",
            "trigger": {"type": "manual", "source_label": "x", "reason": "y"},
            "device_identity": {},
            "coverage": {
                "captured_supported_count": 0,
                "total_supported_count": 0,
                "capture_source": "fresh_read",
                "fields": {},
            },
            "snapshot": {},
        }

    def test_a_non_object_device_identity_is_a_parse_error(self) -> None:
        for member in ("a string", [], 3):
            with self.subTest(member=member):
                payload = self._payload()
                payload["device_identity"] = member

                with self.assertRaises(RestorePointParseError):
                    restore_point_from_dict(payload)

    def test_a_non_object_coverage_fields_map_is_a_parse_error(self) -> None:
        payload = self._payload()
        # ``or {}`` rescues a falsy value, so the shape that actually reaches
        # ``.items()`` is a NON-empty non-mapping.
        payload["coverage"] = dict(payload["coverage"], fields=["not", "a", "map"])

        with self.assertRaises(RestorePointParseError):
            restore_point_from_dict(payload)


class ModulePassportNonObjectRootTests(unittest.TestCase):
    def _service(self, base: Path) -> ModulePassportService:
        return ModulePassportService(base_dir=base)

    def test_get_returns_none_for_a_non_object_passport(self) -> None:
        for root in NON_OBJECT_ROOTS:
            with self.subTest(root=root), tempfile.TemporaryDirectory() as tmp:
                service = self._service(Path(tmp))
                _write(service.path_for("left"), root)

                self.assertIsNone(service.get("left"))

    def test_archive_readers_skip_a_non_object_passport(self) -> None:
        for root in NON_OBJECT_ROOTS:
            with self.subTest(root=root), tempfile.TemporaryDirectory() as tmp:
                service = self._service(Path(tmp))
                _write(service.archive_dir / "left_20260727-120000.json", root)

                self.assertEqual(service.list_archive(), [])
                self.assertEqual(service.list_archive_entries(), [])

    def test_get_returns_none_for_a_non_object_fingerprint_entry(self) -> None:
        # Regression anchor, not a fail-on-base gate: the passport codec already
        # rejects a non-mapping fingerprint entry with ValueError. Pinned here so
        # the container check inside the list is not lost the way the container
        # check on the root was.
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            _write(
                service.path_for("left"),
                {
                    "schema_version": 1,
                    "side": "left",
                    "module_id": "mod-1",
                    "assigned_at_utc": "2026-07-27T12:00:00Z",
                    "notes": "",
                    "fingerprints": ["not an object"],
                },
            )

            self.assertIsNone(service.get("left"))


class WearLedgerNonObjectLineTests(unittest.TestCase):
    def test_a_non_object_jsonl_line_is_skipped_and_the_rest_still_read(self) -> None:
        good = {
            "ts": "2026-07-27T12:00:00Z",
            "event_type": "service_note",
            "summary": "kept",
            "details": {},
            "schema_version": 1,
        }
        for root in NON_OBJECT_ROOTS:
            with self.subTest(root=root), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                service = WearLedgerService(
                    base_dir=base,
                    utc_now=lambda: datetime(2026, 7, 27, 12, tzinfo=timezone.utc),
                )
                (base / "events.jsonl").write_text(
                    json.dumps(root) + "\n" + json.dumps(good) + "\n",
                    encoding="utf-8",
                )

                events = service.read_events()

                self.assertEqual([event.summary for event in events], ["kept"])


class OfficialAppSummaryNonObjectProbeTests(unittest.TestCase):
    """The one reader here whose source is probe stdout, not a file.

    The fix lands in ``_run_probe`` rather than in a catch tuple, because
    ``read_summary``'s ``payload.get`` sits under no ``try`` at all and only one
    of its three callers wraps it. So the stub is the subprocess, not the probe
    — patching ``_run_probe`` itself would step over the code under test.
    """

    @staticmethod
    def _probe(payload: object):
        completed = type(
            "Completed", (), {"stdout": json.dumps(payload), "returncode": 0}
        )()
        return patch(
            "zd_app.services.official_app_summary_service.silent_run",
            return_value=completed,
        )

    def test_a_non_object_probe_stdout_root_yields_no_summary(self) -> None:
        for root in NON_OBJECT_ROOTS:
            with self.subTest(root=root):
                service = OfficialAppSummaryService()
                with self._probe(root):
                    self.assertIsNone(service.read_summary(force_refresh=True))

    def test_a_non_list_names_member_yields_no_summary(self) -> None:
        # An object root is not enough. ``read_summary`` iterates
        # ``payload.get("names", [])`` under no ``try``: ``null`` and ``3``
        # raise TypeError there, and a string or a dict are quietly iterated
        # character- or key-wise into a summary built from nonsense -- the
        # mapping case really does mint a summary out of a dictionary KEY.
        for names in (None, 3, "x", {}, {"Battery: 80%": 1}):
            with self.subTest(names=names):
                service = OfficialAppSummaryService()
                with self._probe({"names": names, "window_title": "Controller"}):
                    self.assertIsNone(service.read_summary(force_refresh=True))

    def test_a_non_string_window_title_yields_no_summary(self) -> None:
        for title in (3, [], {}, True):
            with self.subTest(window_title=title):
                service = OfficialAppSummaryService()
                with self._probe({"names": ["Battery: 80%"], "window_title": title}):
                    self.assertIsNone(service.read_summary(force_refresh=True))

    def test_a_well_formed_probe_still_produces_a_summary(self) -> None:
        # Anti-vacuity: the guards above must reject shapes, not the reader.
        service = OfficialAppSummaryService()
        with self._probe(
            {"names": ["Battery: 80%", "Version: 1.24"], "window_title": "Controller"}
        ):
            summary = service.read_summary(force_refresh=True)

        assert summary is not None
        self.assertEqual(summary.battery_level, "80%")
        self.assertEqual(summary.firmware_version, "1.24")
        self.assertEqual(summary.window_title, "Controller")

    def test_a_missing_window_title_is_still_allowed(self) -> None:
        service = OfficialAppSummaryService()
        with self._probe({"names": ["Battery: 80%"], "window_title": None}):
            summary = service.read_summary(force_refresh=True)

        assert summary is not None
        self.assertIsNone(summary.window_title)


class LocaleLoaderNonObjectRootTests(unittest.TestCase):
    """The locale loader cached whatever parsed, then crashed on it.

    ``_load`` assigned any parseable JSON root straight into the module cache.
    For English, ``_rebuild_reverse_en`` calls ``.items()`` on it in the very
    next line, so an ``en.json`` whose root is ``[]`` / ``"x"`` / ``3`` /
    ``null`` raised instead of taking the malformed-locale fallback two lines
    above -- while ``_other_locale_maps_for_ambiguity`` had carried the missing
    ``isinstance(data, dict)`` check all along.

    No locale file on disk is touched by any of this; every case is a temporary
    directory the loader is pointed at.
    """

    def setUp(self) -> None:
        import zd_app.i18n as i18n

        self.i18n = i18n
        saved = (
            dict(i18n._loaded),
            dict(i18n._reverse_en),
            dict(i18n._ambiguous_en),
        )

        def restore() -> None:
            for live, original in zip(
                (i18n._loaded, i18n._reverse_en, i18n._ambiguous_en), saved
            ):
                live.clear()
                live.update(original)

        self.addCleanup(restore)

    def _load(self, payload: object, locale: str = "en") -> dict:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        tmp = Path(holder.name)
        _write(tmp / f"{locale}.json", payload)
        for cache in (self.i18n._loaded, self.i18n._reverse_en, self.i18n._ambiguous_en):
            cache.clear()
        with patch.object(self.i18n, "_locale_dir", return_value=tmp):
            return self.i18n._load(locale)

    def test_a_non_object_locale_root_degrades_to_an_empty_table(self) -> None:
        for root in NON_OBJECT_ROOTS:
            with self.subTest(root=root):
                self.assertEqual(self._load(root), {})

    def test_a_non_object_locale_root_leaves_lookups_working(self) -> None:
        # The degrade path must be the one the loader already had for a missing
        # or unparseable file: keys resolve to their bracketed placeholder.
        for root in NON_OBJECT_ROOTS:
            with self.subTest(root=root):
                self._load(root)
                self.assertEqual(self.i18n.t("nav.home"), "[nav.home]")

    def test_a_non_string_value_is_dropped_and_the_rest_survive(self) -> None:
        table = self._load({"nav.home": "Home", "nav.broken": ["not", "a", "string"]})

        self.assertEqual(table, {"nav.home": "Home"})
        self.assertEqual(self.i18n.t("nav.home"), "Home")
        self.assertEqual(self.i18n.t("nav.broken"), "[nav.broken]")

    def test_a_well_formed_locale_still_loads(self) -> None:
        # Anti-vacuity: the validation must reject shapes, not content.
        table = self._load({"nav.home": "Home", "nav.about": "About"})

        self.assertEqual(table, {"nav.home": "Home", "nav.about": "About"})
        self.assertEqual(self.i18n.t("nav.about"), "About")

    def test_a_malformed_sibling_locale_does_not_break_the_english_load(self) -> None:
        # The sibling reader had the root check the loader was missing, but not
        # the value check: the ambiguity guard builds a SET of sibling values,
        # so an unhashable one raises there. It only reaches that line for an
        # English literal shared by more than one key, which is what this
        # fixture arranges.
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        tmp = Path(holder.name)
        _write(tmp / "en.json", {"nav.home": "Home", "home.title": "Home"})
        _write(tmp / "zh-CN.json", ["not", "an", "object"])
        _write(tmp / "ko.json", {"nav.home": ["unhashable"], "home.title": "translated"})
        for cache in (self.i18n._loaded, self.i18n._reverse_en, self.i18n._ambiguous_en):
            cache.clear()

        with patch.object(self.i18n, "_locale_dir", return_value=tmp):
            table = self.i18n._load("en")

        self.assertEqual(table, {"nav.home": "Home", "home.title": "Home"})


class WrapperProfileNonObjectRootTests(unittest.TestCase):
    """The sixth reader of this shape, and the most reachable one.

    ``WrapperProfile.from_dict`` carried no ``try`` at all, while its sibling
    ``Profile.from_dict`` has caught ``(KeyError, TypeError, AttributeError)``
    and re-raised ``ValueError`` since it was written. Both
    ``WrapperProfileStore`` readers catch ``ValueError`` but not
    ``AttributeError``, so a non-object root escaped as a crash: ``load`` past
    its ``WrapperProfileError`` wrapper, and ``list_profiles`` past the
    ``skipped`` list whose entire purpose is to survive one bad file. The fix
    converts at the source so both sinks are covered by the conversion they
    already expect.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _store(self):
        from zd_app.storage.wrapper_profile_store import WrapperProfileStore

        return WrapperProfileStore(base_dir=self.base)

    def test_from_dict_converts_a_non_object_root_to_valueerror(self) -> None:
        from zd_app.models import WrapperProfile

        for root in NON_OBJECT_ROOTS:
            with self.subTest(root=root):
                with self.assertRaises(ValueError):
                    WrapperProfile.from_dict(root)

    def test_list_profiles_skips_the_bad_file_instead_of_crashing(self) -> None:
        # The degrade path that matters: one hand-edited file must not take
        # the whole profile list down with it.
        for root in NON_OBJECT_ROOTS:
            with self.subTest(root=root):
                _write(self.base / "broken.json", root)
                profiles, skipped = self._store().list_profiles()
                self.assertEqual(profiles, [])
                self.assertEqual([p.name for p in skipped], ["broken.json"])

    def test_a_good_profile_still_loads_alongside_a_broken_one(self) -> None:
        # Anti-vacuity: proves the reader still reads, rather than the whole
        # directory failing closed and trivially satisfying the test above.
        store = self._store()
        good = WrapperProfileFixture.build()
        store.save(good)
        _write(self.base / "broken.json", [])

        profiles, skipped = store.list_profiles()
        self.assertEqual([p.name for p in profiles], [good.name])
        self.assertEqual([p.name for p in skipped], ["broken.json"])

    def test_load_raises_the_readers_own_error_type(self) -> None:
        from zd_app.storage.wrapper_profile_store import WrapperProfileError

        store = self._store()
        good = WrapperProfileFixture.build()
        path = store.save(good)
        _write(path, [])

        with self.assertRaises(WrapperProfileError):
            store.load(good.name)


class WrapperProfileFixture:
    """Smallest valid profile, built from the suite's shared empty snapshot."""

    @staticmethod
    def build():
        from tests.r2_shell_test_helpers import empty_snapshot
        from zd_app.models import WrapperProfile

        return WrapperProfile(name="Keeper", snapshot=empty_snapshot())


if __name__ == "__main__":
    unittest.main()
