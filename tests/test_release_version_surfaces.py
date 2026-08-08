"""Keep runtime and Windows release metadata on one version."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from zd_app.version import __version__, __version_tuple__


REPO_ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionSurfaceTests(unittest.TestCase):
    def test_runtime_version_string_matches_runtime_tuple(self) -> None:
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        expected = tuple(int(part) for part in __version__.split(".")) + (0,)
        self.assertEqual(__version_tuple__, expected)

    def test_windows_metadata_matches_runtime_version(self) -> None:
        text = (REPO_ROOT / "version_info.txt").read_text(encoding="utf-8")
        expected_tuple = tuple(int(part) for part in __version__.split(".")) + (0,)
        expected_quad = ".".join(str(part) for part in expected_tuple)

        for field in ("filevers", "prodvers"):
            with self.subTest(field=field):
                match = re.search(
                    rf"(?m)^\s*{field}\s*=\s*\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)",
                    text,
                )
                self.assertIsNotNone(match)
                self.assertEqual(tuple(int(part) for part in match.groups()), expected_tuple)

        for field in ("FileVersion", "ProductVersion"):
            with self.subTest(field=field):
                match = re.search(
                    rf"StringStruct\(u'{field}',\s*u'([^']+)'\)", text
                )
                self.assertIsNotNone(match)
                self.assertEqual(match.group(1), expected_quad)


if __name__ == "__main__":
    unittest.main()
