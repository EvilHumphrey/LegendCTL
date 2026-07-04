"""Helpers for rendering user-controlled text into shareable Markdown."""

from __future__ import annotations


# Backslash MUST be first: escaping it after the others would double the
# backslashes we add for |, [, ], etc. Angle brackets are escaped so a device
# descriptor string such as an iProduct of "<img src=x>" cannot pass raw HTML/
# Markdown structure into the share-safe compat-report / trust-self-check exports.
_MARKDOWN_ESCAPE_CHARS = ("\\", "|", "[", "]", "(", ")", "`", "<", ">")


def escape_markdown(value: object) -> str:
    """Escape lightweight Markdown structure in already-scrubbed freeform text."""

    text = "" if value is None else str(value)
    text = " ".join(text.replace("\r\n", "\n").replace("\r", "\n").splitlines())
    for char in _MARKDOWN_ESCAPE_CHARS:
        text = text.replace(char, f"\\{char}")
    return text


__all__ = ["escape_markdown"]
