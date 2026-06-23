"""Public-vocab hygiene guard for the shipped surface.

Asserts that the user-facing surface — ``zd_app/**/*.py``, the locale JSON
(``zd_app/i18n/locales/*.json``), ``tools/**/*.py``, ``docs/**/*.md``,
``.github/`` templates, and the top-level ``CHANGELOG.md`` / ``README.md`` /
``SECURITY.md`` / ``SUPPORT.md`` — carries no internal-process or
reverse-engineering vocabulary: review-finding tags ("Bug 1", "item N3",
"polish 2 N1"), parked-feature / release-arc shorthand, dated review/timeline
references, RE-provenance phrasing ("vendor-app trace", "research replay"),
internal panel-grouping vocab ("dev-research"), vendor-protocol jargon
("vendor protocol" / "厂商协议"), or dangling commit SHAs in comments. ``tests/`` is deliberately excluded — its scaffolding vocabulary
doesn't ship. This is a forward guard: it fails the moment such vocab reappears
in a shipped file.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# Internal-process phrases that must never appear in shipped text.
_DENYLIST: tuple[re.Pattern[str], ...] = (
    re.compile(r"polish \d+ N\d"),            # layout-iteration tags ("polish 2 N1")
    re.compile(r"\bBug [12]\b"),              # review-finding tags ("Bug 1")
    re.compile(r"\bitem N\d\b"),              # review-finding tags ("item N3")
    re.compile(r"must-fix before unpark"),    # parked-feature process note
    re.compile(r"the v2 quality arc"),        # internal release-arc phrase
    re.compile(r"\d{4}-\d{2}-\d{2} review"),  # dated-finding refs ("2026-06-09 review")
    re.compile(r"vendor-app trace", re.I),    # RE-provenance phrasing
    re.compile(r"research replay", re.I),     # RE-provenance phrasing
    re.compile(r"dev-research", re.I),        # internal panel-grouping vocab
    re.compile(r"vendor protocol", re.I),     # vendor-protocol jargon (use plain product copy)
    re.compile("厂商协议"),                     # vendor-protocol jargon (zh-CN)
)

# A bare "...on YYYY-MM-DD..." note in code is an internal timeline reference
# (e.g. a schema-version bump log: "Bumped to 2 on 2026-05-30 when ..."). Scoped
# to ``.py`` and to this specific phrasing on purpose: prose docs legitimately
# narrate dates, and a bare technical-provenance date ("confirmed 2026-05-30",
# "(2026-05-30)", an ISO timestamp example) is engineering documentation, not
# process vocab — only the "on DATE" timeline phrasing is flagged.
_DATED_PROCESS_NOTE = re.compile(r"\bon \d{4}-\d{2}-\d{2}\b")

# A 7-40 char lowercase-hex word, used to spot a dangling commit SHA in a
# comment. Filtered by :func:`_looks_like_sha` to carry BOTH a digit and an
# a-f letter, so English words ("effaced") and pure-decimal numbers don't trip
# it. ``\b`` keeps it from matching inside a longer hex run or a 0x-literal.
_HEX_WORD = re.compile(r"\b[0-9a-f]{7,40}\b")


def _looks_like_sha(word: str) -> bool:
    return any(c.isdigit() for c in word) and any(c in "abcdef" for c in word)


def _rglob(rel: str, *patterns: str) -> list[Path]:
    base = _ROOT / rel
    if not base.exists():
        return []
    out: list[Path] = []
    for pattern in patterns:
        out += [p for p in base.rglob(pattern) if "__pycache__" not in p.parts]
    return sorted(out)


def _shipped_files() -> list[Path]:
    files: list[Path] = []
    # Application code + the user-facing locale strings.
    files += _rglob("zd_app", "*.py")
    files += _rglob("zd_app/i18n/locales", "*.json")
    # Build / dev tooling that ships in the repo.
    files += _rglob("tools", "*.py")
    # Public-facing docs + the GitHub issue/PR templates.
    files += _rglob("docs", "*.md")
    files += _rglob(".github", "*.md", "*.yml", "*.yaml")
    # Named top-level docs.
    for name in ("CHANGELOG.md", "README.md", "SECURITY.md", "SUPPORT.md"):
        path = _ROOT / name
        if path.exists():
            files.append(path)
    return files


class PublicVocabHygieneTests(unittest.TestCase):
    def test_shipped_surface_has_no_internal_vocab(self) -> None:
        offenders: list[str] = []
        for path in _shipped_files():
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(_ROOT).as_posix()
            for pattern in _DENYLIST:
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    offenders.append(f"{rel}:{line}: {match.group(0)!r}")
        self.assertEqual(
            offenders,
            [],
            "internal-process vocab in shipped surface:\n" + "\n".join(offenders),
        )

    def test_shipped_py_has_no_dated_process_note(self) -> None:
        offenders: list[str] = []
        for path in _shipped_files():
            if path.suffix != ".py":
                continue  # prose docs legitimately narrate dates
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(_ROOT).as_posix()
            for match in _DATED_PROCESS_NOTE.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{rel}:{line}: {match.group(0)!r}")
        self.assertEqual(
            offenders,
            [],
            "dated process note in shipped code:\n" + "\n".join(offenders),
        )

    def test_shipped_comments_have_no_dangling_sha(self) -> None:
        offenders: list[str] = []
        for path in _shipped_files():
            if path.suffix != ".py":
                continue  # SHA check is scoped to code comments
            for lineno, raw in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                comment = raw.partition("#")[2]
                if not comment:
                    continue
                for word in _HEX_WORD.findall(comment):
                    if _looks_like_sha(word):
                        rel = path.relative_to(_ROOT).as_posix()
                        offenders.append(f"{rel}:{lineno}: {word}")
        self.assertEqual(
            offenders,
            [],
            "dangling commit SHA in shipped comments:\n" + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
