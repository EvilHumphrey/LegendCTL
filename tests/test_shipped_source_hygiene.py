"""Guard: public source must not leak the maintainer's identity.

A hardcoded ``C:\\Users\\<account>\\...`` path once shipped inside the bundled
probe ``.ps1``. This test pins both public code and tests so a fixture cannot
reintroduce the same leak.

Two scans are intentional. Banning one known literal catches the leak already
seen; the shape scan also flags any future ``<drive>:\\Users\\<account>`` whose
account is not a declared fictional placeholder.

The maintainer's public alias ``EvilHumphrey`` (the MIT-copyright holder and the
GitHub org) is allowed. Test fixtures also legitimately contain fictional home
paths, so those identities are allowlisted explicitly below.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCANNED_ROOTS = ("zd_app", "tests")
_SCANNED_SUFFIXES = (".py", ".ps1", ".json")
_FORBIDDEN = "humphrey"
# The public maintainer alias (GitHub org + MIT-copyright holder) legitimately
# contains the forbidden substring; strip it before scanning so only the bare
# Windows username leaks.
_ALLOWED_ALIAS = "evilhumphrey"

# This test necessarily names the literal it bans. It is excluded from its own
# scan, while every other tracked Python, PowerShell, and JSON file in the two
# public roots remains covered.
_SELF = "tests/test_shipped_source_hygiene.py"

# All fictional fixture identities. Extending this set is a deliberate claim
# that the new account is not a real person's local account.
_PLACEHOLDER_ACCOUNTS = frozenset(
    {
        ".",
        "..",
        "a",
        "alice",
        "alice doe",
        "avery",
        "avery stone",
        "bob",
        "carol",
        "dave",
        "default",
        "eve",
        "exampleuser",
        "jane",
        "jane doe",
        "jane.doe",
        "john",
        "john doe",
        "john.doe",
        "public",
        "real_user",
        "secretuser",
        "test",
        "tester",
        "testuser",
        "user",
        "username",
        "x",
        "y",
    }
)

# The scrubber tests intentionally exercise hostile and generated account
# shapes. The literal scan still covers that file and caught the original leak.
_SHAPE_SCAN_EXEMPT = frozenset({"tests/test_path_scrub.py"})


def _normalise_account(raw: str) -> str:
    """Reduce a captured account component to a comparable fixture name.

    A backtick ends a markdown code span, so anything after one is prose, not
    part of the account name -- truncate there. Deliberately NOT truncating at
    whitespace: that would let a real two-word account like
    ``Jane RealAccount`` pass as the allowlisted ``Jane``.
    """

    text = raw.strip()
    backtick = text.find("`")
    if backtick != -1:
        text = text[:backtick]
    return text.strip().strip("`*_'\",;:()[]{}<>").rstrip("\\/.").lower()


def _is_placeholder_account(raw: str) -> bool:
    account = _normalise_account(raw)
    if not account:
        return True
    # Environment-variable and angle-bracket stand-ins are placeholders by
    # construction: ``%USERNAME%``, ``%APPDATA%``, ``<user>``, ``<maintainer>``.
    # Matched on the PREFIX, not the whole capture, because the terminator
    # accepts end-of-line: a comment reading ``C:\Users\<name> in the report``
    # captures the placeholder plus the rest of the sentence.
    stripped = raw.strip()
    if stripped.startswith("%") or stripped.startswith("<"):
        return True
    return account in _PLACEHOLDER_ACCOUNTS


# Capture the complete account component, spaces included. Terminates on a
# separator, a quote, or end of line. Requiring a *separator* alone missed a bare
# or quoted home directory -- ``C:\Users\<real-account>`` at end of line, or
# immediately before a closing quote, was simply not detected. Prose that merely
# mentions ``C:\Users\<name> in the report`` is handled by the placeholder test
# rather than by the terminator, since a prose sentence also ends at a line break.
_HOME_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}([^\\/\"'\r\n]+?)(?=[\\/\"'\r\n]|$)",
    re.IGNORECASE,
)


class ShippedSourceHygieneTests(unittest.TestCase):
    def _public_files(self) -> list[str]:
        out = subprocess.run(
            ["git", "ls-files", "--", *_SCANNED_ROOTS],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return [
            line
            for line in out.splitlines()
            if line.endswith(_SCANNED_SUFFIXES) and line != _SELF
        ]

    def test_no_maintainer_username_in_public_source(self) -> None:
        offenders: list[str] = []
        for rel in self._public_files():
            path = _REPO_ROOT / rel
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _FORBIDDEN in line.lower().replace(_ALLOWED_ALIAS, ""):
                    offenders.append(f"{rel}:{lineno}: {line.strip()[:120]}")
        self.assertEqual(
            offenders,
            [],
            "Maintainer username leaked into public source:\n"
            + "\n".join(offenders),
        )

    def test_no_real_home_path_in_public_source(self) -> None:
        offenders: list[str] = []
        for rel in self._public_files():
            if rel.replace("\\", "/") in _SHAPE_SCAN_EXEMPT:
                continue
            path = _REPO_ROOT / rel
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for lineno, line in enumerate(text.splitlines(), start=1):
                for match in _HOME_PATH_RE.finditer(line):
                    if _is_placeholder_account(match.group(1)):
                        continue
                    offenders.append(
                        f"{rel}:{lineno}: account {match.group(1)!r} "
                        f"in {line.strip()[:100]}"
                    )
        self.assertEqual(
            offenders,
            [],
            "A home path with a non-placeholder account name reached public "
            "source. If the name is fictional, add it to "
            "_PLACEHOLDER_ACCOUNTS; otherwise scrub it:\n"
            + "\n".join(offenders),
        )


class HomePathShapeTests(unittest.TestCase):
    """Pin the shapes the scan must flag, and the prose it must not."""

    def _flagged(self, line: str) -> list[str]:
        return [
            match.group(1)
            for match in _HOME_PATH_RE.finditer(line)
            if not _is_placeholder_account(match.group(1))
        ]

    def test_bare_quoted_and_two_word_home_paths_are_flagged(self) -> None:
        for line, account in (
            (r"C:\Users\RealPerson", "RealPerson"),
            (r'home = "C:\Users\RealPerson"', "RealPerson"),
            (r"home = 'C:\Users\RealPerson'", "RealPerson"),
            (r"C:\Users\Jane RealAccount\AppData", "Jane RealAccount"),
        ):
            with self.subTest(line=line):
                self.assertEqual(self._flagged(line), [account])

    def test_placeholders_and_prose_are_not_flagged(self) -> None:
        for line in (
            r"see C:\Users\<name> in the report",
            r"C:\Users\%USERNAME%\AppData",
            r"``C:\Users\Bob``",
            r"C:\Users\Jane Doe\AppData",
        ):
            with self.subTest(line=line):
                self.assertEqual(self._flagged(line), [])


if __name__ == "__main__":
    unittest.main()
