"""Guard: shipped source must not leak the maintainer's Windows username.

A hardcoded ``C:\\Users\\humphrey\\...`` path once shipped inside the bundled
probe ``.ps1``. This test pins that ``zd_app/`` (the packaged code, which now
includes the vendored ``services/xinput.py``) never regrows a ``humphrey``
literal.

The maintainer's public alias ``EvilHumphrey`` (the MIT-copyright holder and the
GitHub org) is allowed — it appears in the repo/issue URLs in ``about.py`` and in
the root ``LICENSE``. The scan ignores that alias so only a bare ``humphrey``
(e.g. a ``C:\\Users\\humphrey\\`` path) is flagged.
"""

from __future__ import annotations

import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHIPPED_CODE_DIRS = ("zd_app",)
_SCANNED_SUFFIXES = ("*.py", "*.ps1")
_FORBIDDEN = "humphrey"
# The public maintainer alias (GitHub org + MIT-copyright holder) legitimately
# contains the forbidden substring; strip it before scanning so only the bare
# Windows username leaks.
_ALLOWED_ALIAS = "evilhumphrey"


class ShippedSourceHygieneTests(unittest.TestCase):
    def test_no_maintainer_username_in_shipped_code(self) -> None:
        offenders: list[str] = []
        for rel_dir in _SHIPPED_CODE_DIRS:
            base = _REPO_ROOT / rel_dir
            if not base.exists():
                continue
            for pattern in _SCANNED_SUFFIXES:
                for path in base.rglob(pattern):
                    text = path.read_text(encoding="utf-8", errors="ignore")
                    for lineno, line in enumerate(text.splitlines(), start=1):
                        if _FORBIDDEN in line.lower().replace(_ALLOWED_ALIAS, ""):
                            rel = path.relative_to(_REPO_ROOT)
                            offenders.append(f"{rel}:{lineno}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "Maintainer username leaked into shipped source:\n" + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
