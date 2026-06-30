"""Helpers for rendering user-controlled text into shareable Markdown."""

from __future__ import annotations


_MARKDOWN_ESCAPE_CHARS = ("|", "[", "]", "(", ")", "`")


def escape_markdown(value: object) -> str:
    """Escape lightweight Markdown structure in already-scrubbed freeform text."""

    text = "" if value is None else str(value)
    text = " ".join(text.replace("\r\n", "\n").replace("\r", "\n").splitlines())
    for char in _MARKDOWN_ESCAPE_CHARS:
        text = text.replace(char, f"\\{char}")
    return text


__all__ = ["escape_markdown"]
