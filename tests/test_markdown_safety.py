"""Tests for share-safe Markdown escaping (markdown_safety.escape_markdown)."""

from __future__ import annotations

import unittest

from zd_app.services.markdown_safety import escape_markdown


class EscapeMarkdownTests(unittest.TestCase):
    def test_angle_brackets_are_escaped(self) -> None:
        # Fix C: a crafted USB iProduct like "<img src=x>" must not pass raw
        # HTML / Markdown structure into a share-safe export.
        self.assertEqual(escape_markdown("<img src=x>"), r"\<img src=x\>")

    def test_backslash_is_escaped_first_and_not_doubled_by_later_chars(self) -> None:
        # Backslash must be escaped BEFORE the others; escaping it afterward would
        # double the backslashes added for | [ ] ( ) ` < >.
        self.assertEqual(escape_markdown(r"a\b"), r"a\\b")
        self.assertEqual(escape_markdown(r"\<"), r"\\\<")
        self.assertEqual(escape_markdown(r"[x]"), r"\[x\]")

    def test_existing_structure_chars_still_escaped(self) -> None:
        self.assertEqual(escape_markdown("|[]()"), r"\|\[\]\(\)")
        self.assertEqual(escape_markdown("`"), "\\`")

    def test_none_and_newlines_collapse_without_error(self) -> None:
        self.assertEqual(escape_markdown(None), "")
        self.assertEqual(escape_markdown("a\r\nb"), "a b")


if __name__ == "__main__":
    unittest.main()
