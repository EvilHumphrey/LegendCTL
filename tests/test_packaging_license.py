"""Packaging & license consistency tests (security hardening).

Static checks that the project license is MIT consistently across every
surface that states it, and that the pinned dependency files carry no
floating version ranges. No app/dpg context required for the dependency
checks; the About-screen check imports the module for its license table.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Iterator


_REPO_ROOT = Path(__file__).resolve().parent.parent
_LICENSE = _REPO_ROOT / "LICENSE"
_PROJECT_TXT = _REPO_ROOT / "assets" / "licenses" / "project.txt"
_README = _REPO_ROOT / "README.md"
_NOTICE = _REPO_ROOT / "NOTICE"
_PYINSTALLER_SPEC = _REPO_ROOT / "pyinstaller_main_zd.spec"
_NOTO_SANS_KR_REGULAR = _REPO_ROOT / "assets" / "fonts" / "NotoSansKR-Regular.ttf"
_NOTO_SANS_KR_SEMIBOLD = _REPO_ROOT / "assets" / "fonts" / "NotoSansKR-SemiBold.ttf"
_NOTO_SANS_KR_LICENSE = _REPO_ROOT / "assets" / "licenses" / "noto-sans-kr.txt"
_REQ_FILES = (
    _REPO_ROOT / "requirements.txt",
    _REPO_ROOT / "requirements-build.txt",
)


class LicenseConsistencyTests(unittest.TestCase):
    """LICENSE, project.txt, README, and the About screen must all say MIT."""

    def test_root_license_is_mit(self) -> None:
        text = _LICENSE.read_text(encoding="utf-8")
        self.assertTrue(
            text.lstrip().startswith("MIT License"),
            "root LICENSE must be the MIT License",
        )

    def test_bundled_project_license_says_mit_not_tbd(self) -> None:
        text = _PROJECT_TXT.read_text(encoding="utf-8")
        self.assertIn("MIT", text)
        self.assertNotIn("TBD", text)

    def test_readme_license_section_says_mit_not_tbd(self) -> None:
        text = _README.read_text(encoding="utf-8")
        self.assertIn("Project license: MIT", text)
        # The old placeholder "TBD" license wording must be gone.
        self.assertNotIn("license: TBD", text)
        self.assertNotIn("TBD pending", text)

    def test_about_screen_project_spdx_is_mit(self) -> None:
        from zd_app.ui.screens import about

        spdx_by_slug = {slug: spdx for slug, spdx, _ in about.LICENSE_DEPS}
        self.assertEqual(spdx_by_slug.get("project"), "MIT")


class DependencyPinningTests(unittest.TestCase):
    """requirements*.txt must pin exact versions — no floating ranges."""

    @staticmethod
    def _requirement_lines(path: Path) -> Iterator[str]:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            # Skip blanks, comments, and include directives (e.g. -r requirements.txt).
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            yield line

    def test_no_floating_ranges_in_requirements(self) -> None:
        for path in _REQ_FILES:
            self.assertTrue(path.is_file(), f"missing {path}")
            for line in self._requirement_lines(path):
                with self.subTest(file=path.name, line=line):
                    self.assertNotIn(">", line, f"floating range in {path.name}: {line}")
                    self.assertNotIn("<", line, f"floating range in {path.name}: {line}")
                    self.assertIn("==", line, f"unpinned requirement in {path.name}: {line}")


class NotoSansKrPackagingTests(unittest.TestCase):
    def test_korean_font_assets_and_ofl_notice_are_present(self) -> None:
        for path in (_NOTO_SANS_KR_REGULAR, _NOTO_SANS_KR_SEMIBOLD):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)

        license_text = _NOTO_SANS_KR_LICENSE.read_text(encoding="utf-8")
        self.assertIn("Noto Sans KR", license_text)
        self.assertIn("Copyright 2014-2021 Adobe", license_text)
        self.assertIn("SIL OPEN FONT LICENSE Version 1.1", license_text)
        self.assertIn("Noto Sans KR (font)", _NOTICE.read_text(encoding="utf-8"))

    def test_pyinstaller_globs_korean_ttf_and_license_assets(self) -> None:
        spec = _PYINSTALLER_SPEC.read_text(encoding="utf-8")

        self.assertIn('("assets/fonts/*.ttf", "assets/fonts")', spec)
        self.assertIn('("assets/licenses/*.txt", "assets/licenses")', spec)


if __name__ == "__main__":
    unittest.main()
