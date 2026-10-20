"""Opt-in artifact alias checks preserve ordinary import compatibility."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zd_app.storage import _import_guards as guards


class SingleLinkImportGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "source.json"
        self.source.write_text('{"ok": true}', encoding="utf-8")

    def test_regular_single_link_reads_remain_available_without_path_stat(self) -> None:
        with patch.object(Path, "stat", side_effect=AssertionError("pathname metadata check")):
            self.assertEqual(
                guards.read_guarded_json(self.source, require_single_link=True),
                {"ok": True},
            )

    def test_hardlinked_files_require_opt_in_rejection(self) -> None:
        alias = self.root / "alias.json"
        alias.hardlink_to(self.source)
        self.assertEqual(alias.stat().st_nlink, 2)
        self.assertEqual(guards.read_guarded_json(alias, local_only=True), {"ok": True})
        with self.assertRaises(guards.UnsafeImportPathError):
            guards.read_guarded_json(alias, require_single_link=True)

    @unittest.skipUnless(os.name == "nt", "Windows read-sharing contract")
    def test_opened_name_removal_and_writer_opens_are_blocked(self) -> None:
        real_fstat = os.fstat
        checked = False

        def check_locked_handle(descriptor: int):
            nonlocal checked
            info = real_fstat(descriptor)
            with self.assertRaises(PermissionError):
                self.source.unlink()
            with self.assertRaises(PermissionError):
                with self.source.open("wb"):
                    pass
            checked = True
            return info

        with patch.object(guards.os, "fstat", side_effect=check_locked_handle):
            self.assertEqual(
                guards.read_guarded_json(self.source, require_single_link=True),
                {"ok": True},
            )
        self.assertTrue(checked)
        # A successful unlink after close proves fixture rights were sufficient.
        self.source.unlink()

    @unittest.skipUnless(os.name == "nt", "Windows read-sharing contract")
    def test_link_added_during_read_is_rejected_by_post_read_metadata(self) -> None:
        real_fstat = os.fstat
        checks = 0

        def add_alias_after_first_check(descriptor: int):
            nonlocal checks
            info = real_fstat(descriptor)
            checks += 1
            if checks == 1:
                # Windows permits creating links even without write/delete
                # sharing. The second same-handle check must catch this alias.
                alias = self.root / "during_read.json"
                os.link(self.source, alias)
            return info

        with patch.object(guards.os, "fstat", side_effect=add_alias_after_first_check):
            with self.assertRaises(guards.UnsafeImportPathError):
                guards.read_guarded_json(self.source, require_single_link=True)
        self.assertEqual(checks, 2)


if __name__ == "__main__":
    unittest.main()
