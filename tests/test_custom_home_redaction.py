"""Synthetic account contexts exercise the shared artifact privacy boundary."""

from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
import zipfile
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from zd_app.services import path_scrub
from zd_app.services.diagnostic_bundle import DiagnosticBundleService
from zd_app.services.diagnostic_bundle.service import _BundleMember
from zd_app.services.module_passport import ModulePassportService


class CustomHomePathTests(unittest.TestCase):
    HOME = r"D:\Profiles\Jane Doe"

    def scrub(self, value: str, *homes: str) -> str:
        return path_scrub.scrub_paths(value, home_prefixes=homes or (self.HOME,))

    def test_windows_bare_home_and_encoded_namespace_spellings(self) -> None:
        paths = (
            self.HOME, "d:/profiles/JANE DOE", r"\\?\D:\Profiles\Jane Doe",
            r"\\.\D:\Profiles\Jane Doe", r"D:\Profiles\.\Jane Doe",
            r"D:\Profiles.\Jane Doe", self.HOME.replace("\\", "\\\\"),
        )
        for value in paths:
            with self.subTest(value=value):
                self.assertEqual(self.scrub(value), "<HOME>")
                self.assertNotIn("Jane Doe", self.scrub(repr(value)))
                self.assertNotIn("Jane Doe", self.scrub(json.dumps(value)))

    def test_known_home_in_prose_and_spaced_sibling_is_conservatively_masked(self) -> None:
        for tail in (" today", " Other", "'", "', 'next'", '")', "]", ", next", "`", ": unavailable"):
            with self.subTest(tail=tail):
                out = self.scrub("note " + self.HOME + tail)
                self.assertNotIn("Jane Doe", out)
                self.assertIn("<HOME>", out)
        self.assertEqual(self.scrub(self.HOME + " today"), "<HOME> today")

    def test_non_whitespace_sibling_and_plain_account_text_are_not_replaced(self) -> None:
        for suffix in ("Backup", "-Other", "_2", "2", "'s"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.scrub(self.HOME + suffix), "Jane Doe" + suffix)
        self.assertEqual(self.scrub("Jane Doe wrote the calibration note"),
                         "Jane Doe wrote the calibration note")
        self.assertEqual(self.scrub(r"D:\Other\Jane Doe"), "Jane Doe")
        self.assertEqual(self.scrub(r"\\srv\share\Dave Lee"), "Dave Lee")

    def test_unicode_windows_account_spelling_survives_cache_normalization(self) -> None:
        for account in ("Groß", "İpek", "ﬀ House"):
            home = "D:/Profiles/" + account
            with self.subTest(account=account):
                self.assertEqual(self.scrub(home, home), "<HOME>")
                self.assertEqual(self.scrub(home + "/report.txt", home), "report.txt")
                with patch.object(path_scrub.Path, "home", return_value=PureWindowsPath(home)):
                    self.assertEqual(path_scrub.scrub_paths(home), "<HOME>")

    def test_account_components_are_removed_before_source_and_app_markers(self) -> None:
        for name in ("zd_app", "_internal", "ZDUltimateLegend"):
            home = "D:/Profiles/" + name
            with self.subTest(name=name):
                self.assertEqual(self.scrub(home, home), "<HOME>")
                self.assertEqual(self.scrub(home + "/Documents/report.txt", home), "report.txt")
        self.assertEqual(self.scrub(self.HOME + r"\AppData\Roaming\ZDUltimateLegend\logs\app.log"),
                         "<APP_DATA>/logs/app.log")
        self.assertEqual(self.scrub(self.HOME + r"\src\zd_app\services\foo.py"),
                         "zd_app/services/foo.py")

    def test_custom_posix_home_is_a_case_sensitive_token_anchor(self) -> None:
        home = "/srv/profiles/Jane Doe"
        self.assertEqual(self.scrub(home, home), "<HOME>")
        self.assertEqual(self.scrub(home + "/report.txt", home), "report.txt")
        self.assertEqual(self.scrub(home + "/zd_app/services/foo.py", home),
                         "zd_app/services/foo.py")
        self.assertEqual(self.scrub("/srv/profiles/jane doe", home), "/srv/profiles/jane doe")
        self.assertEqual(self.scrub(home + "Backup", home), home + "Backup")
        self.assertEqual(self.scrub("/usr/lib/libfoo.so", home), "/usr/lib/libfoo.so")

    def test_unc_home_and_extended_unc_encodings(self) -> None:
        home = r"\\srv\profiles\Jane Doe"
        for value in (home, "//SRV/profiles/JANE DOE", r"\\?\UNC\srv\profiles\Jane Doe",
                      r"\\.\UNC\srv\profiles\Jane Doe", home.replace("\\", "\\\\")):
            with self.subTest(value=value):
                self.assertEqual(self.scrub(value, home), "<HOME>")
        self.assertEqual(self.scrub(r"\\srv\profiles\Jane DoeBackup", home), "Jane DoeBackup")
        self.assertEqual(self.scrub(r"\\other\profiles\Jane Doe", home), "Jane Doe")

    def test_lexically_equivalent_custom_homes_are_recognized_without_io(self) -> None:
        cases = (
            (self.HOME, "D:/Profiles/other/../Jane Doe"),
            (self.HOME, r"D:\Profiles\other\nested\..\..\Jane Doe"),
            (self.HOME, r"\\?\D:\Profiles\other\..\Jane Doe"),
            ("/srv/profiles/Jane Doe", "/srv/profiles/other/../Jane Doe"),
            ("/srv/profiles/Jane Doe", "/other/../srv/profiles/Jane Doe"),
            ("/srv/profiles/Jane Doe", "/./srv/profiles/Jane Doe"),
            ("/srv/profiles/Jane Doe", "/srv/./profiles/other/../Jane Doe"),
        )
        with patch.object(path_scrub.Path, "resolve", side_effect=AssertionError("must not resolve")), \
             patch.object(path_scrub.Path, "stat", side_effect=AssertionError("must not stat")):
            for home, alternate in cases:
                for text in (alternate, json.dumps(alternate), repr(alternate)):
                    with self.subTest(home=home, text=text):
                        out = self.scrub(text, home)
                        self.assertNotIn("Jane Doe", out)
                        self.assertIn("<HOME>", out)
                        self.assertEqual(self.scrub(out, home), out)
                self.assertEqual(self.scrub(alternate, home), "<HOME>")
                self.assertEqual(self.scrub(alternate + "/report.txt", home), "report.txt")
                self.assertEqual(self.scrub(alternate + "/zd_app/services/foo.py", home),
                                 "zd_app/services/foo.py")

    def test_unc_share_root_can_be_the_account_home(self) -> None:
        home = "//server/Jane Doe"
        for text in (home, r"\\server\Jane Doe", r"\\?\UNC\server\Jane Doe",
                     r"\\.\UNC\server\Jane Doe", json.dumps(r"\\server\Jane Doe")):
            with self.subTest(text=text):
                self.assertNotIn("Jane Doe", self.scrub(text, home))
                self.assertIn("<HOME>", self.scrub(text, home))
        self.assertEqual(self.scrub(home + "/report.txt", home), "report.txt")
        self.assertEqual(self.scrub("//other/Jane Doe", home), "Jane Doe")
        self.assertEqual(self.scrub("//server/Jane DoeBackup", home), "Jane DoeBackup")

    def test_posix_candidate_scanner_preserves_unrelated_paths_urls_and_siblings(self) -> None:
        home = "/srv/profiles/Jane Doe"
        for text in ("/usr/lib/libfoo.so", "/usr/other/../lib/libfoo.so",
                     "/tmp/srv/profiles/Jane Doe", "prose /ordinary/token stays useful",
                     "https://example.test/./srv/profiles/other/../JaneDoe",
                     "file:///other/../srv/profiles/JaneDoe",
                     "https://example.test/a/../srv/profiles/Jane%20Doe"):
            with self.subTest(text=text):
                self.assertEqual(self.scrub(text, home), text)
        alternate = "/srv/profiles/other/../Jane Doe"
        for prefix in ("/usr/lib ", "/usr/lib,", "/usr/lib;", "['/usr/lib', '",
                       "https://example.test/safe "):
            with self.subTest(prefix=prefix):
                out = self.scrub(prefix + alternate, home)
                self.assertNotIn("Jane Doe", out)
                self.assertIn("<HOME>", out)
                self.assertTrue(out.startswith(prefix))
        for url in ("https://example.test/./srv/profiles/other/../Owner",
                    "file:///other/../srv/profiles/Owner",
                    "smb://server/other/../srv/profiles/Owner"):
            self.assertEqual(self.scrub(url, "/srv/profiles/Owner"), url)

    def test_traversal_reroot_and_glued_paths_cannot_reemit_account(self) -> None:
        for home in (self.HOME, "/srv/profiles/Jane Doe"):
            for tail in ("/../Jane Doe", "/zd_app/../Jane Doe",
                         "/a.txt//srv/share/Jane Doe", "/a.txt/Users/Jane Doe",
                         "/a.txt//other/ZDUltimateLegend/Jane Doe",
                         " today/Users/Jane Doe", " text//srv/share/Jane Doe"):
                with self.subTest(home=home, tail=tail):
                    self.assertNotIn("Jane Doe", self.scrub(home + tail, home))
            for first in ("C:/data/zd_app/a.py", "C:/data/ZDUltimateLegend/a.json", home):
                for glue in ("", ",", ";", "', '", " ", " and "):
                    with self.subTest(first=first, glue=glue, home=home):
                        out = self.scrub(first + glue + home, home)
                        self.assertNotIn("Jane Doe", out)

    def test_injected_context_flows_through_every_public_text_and_json_api(self) -> None:
        homes = (self.HOME, "/srv/profiles/Jane Doe")
        self.assertEqual(path_scrub.scrub_value(self.HOME, home_prefixes=homes), "<HOME>")
        self.assertEqual(path_scrub.sanitize_shareable_text(self.HOME, home_prefixes=homes), "<HOME>")
        value = {self.HOME: [self.HOME, {"note": homes[1]}], "count": 3, "flag": False}
        self.assertEqual(path_scrub.sanitize_shareable_json(value, home_prefixes=homes),
                         {"<HOME>": ["<HOME>", {"note": "<HOME>"}], "count": 3, "flag": False})
        self.assertIn(self.HOME, value)
        with self.assertRaisesRegex(ValueError, "collision"):
            path_scrub.sanitize_shareable_json({self.HOME: 1, homes[1]: 2}, home_prefixes=homes)

    def test_default_provider_is_lazy_nonresolving_and_recovers_after_failure(self) -> None:
        with patch.object(path_scrub.Path, "home", side_effect=[RuntimeError("unavailable"),
                                                              PureWindowsPath(self.HOME)]), \
             patch.object(path_scrub.Path, "resolve", side_effect=AssertionError("must not resolve")), \
             patch.object(path_scrub.Path, "stat", side_effect=AssertionError("must not stat")):
            self.assertEqual(path_scrub.scrub_paths(r"C:\Users\ExampleUser"), "<HOME>")
            self.assertEqual(path_scrub.scrub_paths(self.HOME), "<HOME>")
        with patch.object(path_scrub.Path, "home", side_effect=AssertionError("injection bypasses provider")):
            self.assertEqual(self.scrub(self.HOME), "<HOME>")

    def test_default_provider_context_changes_and_formatter_uses_current_home(self) -> None:
        with patch.object(path_scrub.Path, "home", side_effect=[PureWindowsPath(self.HOME),
                                                              PureWindowsPath("E:/Profiles/Alex Smith")]):
            self.assertEqual(path_scrub.scrub_paths(self.HOME), "<HOME>")
            record = logging.LogRecord("test", logging.INFO, "fixture.py", 1,
                                       "E:/Profiles/Alex Smith", (), None)
            self.assertEqual(path_scrub.PathScrubbingFormatter().format(record), "<HOME>")

    def test_invalid_root_relative_and_traversal_candidates_are_ignored(self) -> None:
        homes = ("", "/", "D:/", "D:relative", "relative/name",
                 "D:/Profiles/../Jane Doe", "bad\npath")
        self.assertEqual(path_scrub.scrub_paths("report.txt", home_prefixes=homes), "report.txt")
        self.assertEqual(path_scrub.scrub_paths(self.HOME, home_prefixes=homes), "Jane Doe")

    def test_pattern_cache_is_bounded_and_normalized_context_is_reused(self) -> None:
        for index in range(12):
            path_scrub.scrub_paths("note", home_prefixes=(f"D:/Profiles/Synthetic{index}",))
        info = path_scrub._home_path_patterns.cache_info()
        self.assertLessEqual(info.currsize, 8)
        before = info.hits
        self.scrub(self.HOME)
        self.scrub("d:/profiles/jane doe", "d:/profiles/jane doe")
        self.assertGreater(path_scrub._home_path_patterns.cache_info().hits, before)


class CustomHomeArtifactTests(unittest.TestCase):
    def test_alternate_and_share_root_homes_reach_final_zip_boundary_safely(self) -> None:
        for home, note in (
            ("D:/Profiles/Jane Doe", "D:/Profiles/other/../Jane Doe"),
            ("/srv/profiles/Jane Doe", "/other/../srv/profiles/other/../Jane Doe"),
            ("//server/Jane Doe", "//server/Jane Doe"),
        ):
            with self.subTest(home=home), tempfile.TemporaryDirectory() as directory, \
                 patch.object(path_scrub, "_current_home_prefixes", return_value=(home,)):
                service = DiagnosticBundleService(base_dir=Path(directory))
                raw = [
                    _BundleMember("report.md", "note " + note, "report"),
                    _BundleMember("module_passports/left.json", json.dumps({"notes": note}), "module_passport"),
                    _BundleMember("module_passports/archive/left_fixture.json",
                                  json.dumps({"notes": note}), "module_passport"),
                ]
                once = service._validated_bundle_members(raw)
                self.assertEqual(service._validated_bundle_members(once), once)
                payload = io.BytesIO()
                zip_class = zipfile.ZipFile
                with patch.object(service, "_bundle_members", return_value=raw), \
                     patch("zd_app.services.diagnostic_bundle.service.zipfile.ZipFile",
                           side_effect=lambda target, **kwargs: zip_class(payload, **kwargs)):
                    self.assertIsNotNone(service.generate_bundle_zip(Path(directory) / "export.zip"))
                with zip_class(payload) as archive:
                    self.assertEqual(len(archive.namelist()), 3)
                    for name in archive.namelist():
                        self.assertNotIn(b"Jane Doe", archive.read(name))
                        self.assertIn(b"<HOME>", archive.read(name))
                self.assertIn(note, raw[0].text)

    def test_local_passports_survive_standalone_and_active_archive_zip_export(self) -> None:
        home = r"D:\Profiles\Jane Doe"
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(path_scrub, "_current_home_prefixes", return_value=(home,)):
            root = Path(directory)
            passports = ModulePassportService(base_dir=root / "passports")
            passports.assign("left", "retired")
            passports.update_notes("left", home)
            passports.assign("left", "active")
            passports.update_notes("left", home)
            service = DiagnosticBundleService(base_dir=root / "bundles",
                                              module_passport_service=passports)
            self.assertEqual(passports.get("left").notes, home)
            self.assertEqual(passports.list_archive("left")[0].notes, home)
            self.assertNotIn("Jane Doe", service.generate_markdown())
            payload = io.BytesIO()
            zip_class = zipfile.ZipFile
            with patch("zd_app.services.diagnostic_bundle.service.zipfile.ZipFile",
                       side_effect=lambda target, **kwargs: zip_class(payload, **kwargs)):
                self.assertEqual(service.generate_bundle_zip(root / "export.zip"), root / "export.zip")
            with zip_class(payload) as archive:
                names = archive.namelist()
                self.assertIn("report.md", names)
                self.assertIn("module_passports/left.json", names)
                archived = [n for n in names if n.startswith("module_passports/archive/")]
                self.assertEqual(len(archived), 1)
                for name in names:
                    self.assertNotIn("Jane Doe", name)
                    self.assertNotIn(b"Jane Doe", archive.read(name))
                for name in ("module_passports/left.json", archived[0]):
                    self.assertEqual(json.loads(archive.read(name))["notes"], "<HOME>")
            self.assertEqual(passports.get("left").notes, home)
            self.assertEqual(passports.list_archive("left")[0].notes, home)

    def test_final_zip_boundary_sanitizes_raw_members_without_source_help(self) -> None:
        home = "/srv/profiles/Jane Doe"
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(path_scrub, "_current_home_prefixes", return_value=(home,)):
            service = DiagnosticBundleService(base_dir=Path(directory))
            raw = [
                _BundleMember("report.md", "note " + home, "report"),
                _BundleMember("module_passports/left.json", json.dumps({"notes": home}), "module_passport"),
                _BundleMember("module_passports/archive/left_fixture.json",
                              json.dumps({"notes": home}), "module_passport"),
            ]
            payload = io.BytesIO()
            zip_class = zipfile.ZipFile
            with patch.object(service, "_bundle_members", return_value=raw), \
                 patch("zd_app.services.diagnostic_bundle.service.zipfile.ZipFile",
                       side_effect=lambda target, **kwargs: zip_class(payload, **kwargs)):
                self.assertIsNotNone(service.generate_bundle_zip(Path(directory) / "export.zip"))
            with zip_class(payload) as archive:
                self.assertEqual(len(archive.namelist()), 3)
                for name in archive.namelist():
                    self.assertNotIn(b"Jane Doe", archive.read(name))
            self.assertIn(home, raw[0].text)


if __name__ == "__main__":
    unittest.main()
