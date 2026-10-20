"""Inert temporary local-mirror fixtures; never create shortcuts or run payloads."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows local mirror boundary")
class LocalInstallBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        (self.repo / "tools").mkdir(parents=True)
        for name in ("install_local.ps1", "release_paths.ps1", "release_mirror.cs"):
            shutil.copyfile(ROOT / "tools" / name, self.repo / "tools" / name)
        # Keep the complete production mirror prefix and stop before any desktop
        # or COM operation. This is an inert disposable copy, never the real tool.
        installer = self.repo / "tools/install_local.ps1"
        prefix = installer.read_text(encoding="utf-8").split("# 3. (Re)create", 1)[0]
        installer.write_text(prefix + "\nthrow 'FIXTURE_MIRROR_COMPLETED'\n", encoding="utf-8")
        self.source = self.repo / "dist/ZDUltimateLegend-v1"
        self.source.mkdir(parents=True)
        (self.source / "ZD Ultimate Legend.exe").write_bytes(b"inert fixture")
        self.outside = self.root / "outside"
        self.outside.mkdir()
        (self.outside / "keep.txt").write_bytes(b"keep")

    def tearDown(self):
        self.temporary.cleanup()

    def _install(self):
        command = f"& '{self.repo / 'tools/install_local.ps1'}'"
        return subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )

    def _junction(self, link):
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(self.outside)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            self.skipTest(f"junction creation unavailable: {result.stderr}")

    def test_normal_mirror_preserves_behavior_without_touching_shortcuts(self):
        install = self.repo / "local-install"
        install.mkdir()
        (install / "stale.txt").write_bytes(b"old")
        (install / "stale-dir").mkdir()
        (install / "stale-dir/old.dll").write_bytes(b"old")
        (self.source / "nested").mkdir()
        (self.source / "nested/current.dll").write_bytes(b"current")
        result = self._install()
        self.assertIn("FIXTURE_MIRROR_COMPLETED", result.stderr)
        self.assertEqual(b"inert fixture", (install / "ZD Ultimate Legend.exe").read_bytes())
        self.assertFalse((install / "stale.txt").exists())
        self.assertFalse((install / "stale-dir").exists())
        self.assertEqual(b"current", (install / "nested/current.dll").read_bytes())
        self.assertEqual(b"keep", (self.outside / "keep.txt").read_bytes())

    def test_root_and_nested_destination_junctions_fail_before_mirror(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                install = self.repo / "local-install"
                if nested:
                    install.mkdir()
                link = install / "escape" if nested else install
                self._junction(link)
                try:
                    result = self._install()
                    self.assertIn("reparse point", result.stderr)
                    self.assertNotIn("FIXTURE_MIRROR_COMPLETED", result.stderr)
                    self.assertEqual(b"keep", (self.outside / "keep.txt").read_bytes())
                finally:
                    os.rmdir(link)
                    if nested:
                        install.rmdir()

    def test_source_subtree_junction_fails_before_mirror(self):
        link = self.source / "escape"
        self._junction(link)
        try:
            result = self._install()
            self.assertIn("reparse point", result.stderr)
            self.assertFalse((self.repo / "local-install").exists())
            self.assertEqual(b"keep", (self.outside / "keep.txt").read_bytes())
        finally:
            os.rmdir(link)

    def test_destination_hardlink_fails_before_overwriting_outside_bytes(self):
        install = self.repo / "local-install"
        install.mkdir()
        os.link(self.outside / "keep.txt", install / "ZD Ultimate Legend.exe")
        result = self._install()
        self.assertIn("hard-linked", result.stderr)
        self.assertNotIn("FIXTURE_MIRROR_COMPLETED", result.stderr)
        self.assertEqual(b"keep", (self.outside / "keep.txt").read_bytes())

    def test_directory_lease_blocks_rename_until_disposed(self):
        command = (
            f"Add-Type -Path '{ROOT / 'tools/release_mirror.cs'}'; "
            f"$mirror = New-Object LegendCtlReleaseMirror('{self.source}', '{self.repo / 'local-install'}'); "
            "$mirror.Prepare(); "
            "try { "
            f"try {{ [IO.Directory]::Move('{self.source}', '{self.source}-renamed'); "
            "throw 'RENAME_SUCCEEDED' } catch [IO.IOException] { 'RENAME_BLOCKED' } "
            "} finally { $mirror.Dispose() }; "
            f"[IO.Directory]::Move('{self.source}', '{self.source}-renamed'); 'RELEASED'"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("RENAME_BLOCKED", result.stdout)
        self.assertIn("RELEASED", result.stdout)
        self.assertNotIn("RENAME_SUCCEEDED", result.stderr)

    def test_destination_leaf_substitution_after_prepare_is_refused(self):
        install = self.repo / "local-install"
        install.mkdir()
        target = install / "ZD Ultimate Legend.exe"
        target.write_bytes(b"old")
        command = (
            "Add-Type 'using System; using System.Runtime.InteropServices; public class FixtureLink { "
            '[DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] '
            "public static extern bool CreateHardLinkW(string name, string target, IntPtr security); }'; "
            f"Add-Type -Path '{ROOT / 'tools/release_mirror.cs'}'; "
            f"$mirror = New-Object LegendCtlReleaseMirror('{self.source}', '{install}'); "
            "$mirror.Prepare(); try { "
            f"[IO.File]::Delete('{target}'); "
            f"$inserted = [FixtureLink]::CreateHardLinkW('{target}', '{self.outside / 'keep.txt'}', [IntPtr]::Zero); "
            "if ($inserted) { 'REPLACEMENT_INSERTED' } else { 'REPLACEMENT_BLOCKED' }; "
            "$mirror.Publish(); if ($inserted) { throw 'UNSAFE_PUBLICATION' } "
            "} finally { $mirror.Dispose() }"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        if "REPLACEMENT_INSERTED" in result.stdout:
            self.assertIn("hard-linked", result.stderr)
        else:
            self.assertIn("REPLACEMENT_BLOCKED", result.stdout)
        self.assertNotIn("UNSAFE_PUBLICATION", result.stderr)
        self.assertEqual(b"keep", (self.outside / "keep.txt").read_bytes())

    def test_publication_creates_a_fresh_file_object(self):
        install = self.repo / "local-install"
        install.mkdir()
        target = install / "ZD Ultimate Legend.exe"
        target.write_bytes(b"old")
        old_identity = target.stat().st_ino
        result = self._install()
        self.assertIn("FIXTURE_MIRROR_COMPLETED", result.stderr)
        self.assertNotEqual(old_identity, target.stat().st_ino)

    def test_destination_leaf_junction_after_prepare_never_reaches_target(self):
        install = self.repo / "local-install"
        install.mkdir()
        target = install / "ZD Ultimate Legend.exe"
        target.write_bytes(b"old")
        command = (
            f"Add-Type -Path '{ROOT / 'tools/release_mirror.cs'}'; "
            f"$mirror = New-Object LegendCtlReleaseMirror('{self.source}', '{install}'); "
            "$mirror.Prepare(); try { "
            f"[IO.File]::Delete('{target}'); "
            f"New-Item -ItemType Junction -Path '{target}' -Value '{self.outside}' -ErrorAction Stop | Out-Null; "
            "'JUNCTION_INSERTED'; $mirror.Publish(); throw 'UNSAFE_PUBLICATION' "
            "} finally { $mirror.Dispose() }"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            if "JUNCTION_INSERTED" in result.stdout:
                self.assertIn("reparse point", result.stderr)
            self.assertNotIn("UNSAFE_PUBLICATION", result.stderr)
            self.assertEqual(list(self.outside.iterdir()), [self.outside / "keep.txt"])
            self.assertEqual(b"keep", (self.outside / "keep.txt").read_bytes())
        finally:
            if target.exists() and target.is_dir():
                target.rmdir()

    def test_short_path_spelling_preserves_normal_mirror(self):
        get_short = ctypes.windll.kernel32.GetShortPathNameW
        get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        get_short.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_short(str(self.repo), buffer, len(buffer))
        self.assertGreater(length, 0)
        self.assertLess(length, len(buffer))
        short_repo = Path(buffer.value)
        if str(short_repo).casefold() == str(self.repo).casefold():
            self.skipTest("fixture filesystem does not provide a distinct 8.3 alias")
        command = (
            f"Add-Type -Path '{ROOT / 'tools/release_mirror.cs'}'; "
            f"$mirror = New-Object LegendCtlReleaseMirror('{short_repo / 'dist/ZDUltimateLegend-v1'}', "
            f"'{short_repo / 'local-install'}'); "
            "try { $mirror.Prepare(); $mirror.Publish(); 'MIRRORED' } finally { $mirror.Dispose() }"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MIRRORED", result.stdout)
        self.assertEqual((self.repo / "local-install/ZD Ultimate Legend.exe").read_bytes(), b"inert fixture")
