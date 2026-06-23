"""Shared guards for importing untrusted profile / config JSON.

Imported files are untrusted: the v1 "Import Config" button and the v2 Safe
Import flow both read a user-chosen path. Reject implausibly large or deeply
nested files *before* parsing so a hostile file can neither exhaust memory nor
blow the JSON recursion limit. A real profile is a few KB and ~4 levels deep;
these are generous ceilings, not tuning knobs.

Single source of truth: both ``profile_store`` (v1) and
``wrapper_profile_store`` (v2 Safe Import) import these.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MAX_IMPORT_BYTES = 1 * 1024 * 1024
MAX_IMPORT_JSON_DEPTH = 64


def _max_json_depth(text: str) -> int:
    """Largest structural bracket-nesting depth in ``text``.

    Brackets inside JSON strings are ignored. Used to reject pathologically
    nested input *before* ``json.loads`` can recurse deep enough to raise
    ``RecursionError`` (which the import callers do not catch).
    """

    depth = 0
    max_depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{" or ch == "[":
            depth += 1
            if depth > max_depth:
                max_depth = depth
        elif ch == "}" or ch == "]":
            depth -= 1
    return max_depth


def read_guarded_json(
    path: str | Path, *, max_bytes: int = MAX_IMPORT_BYTES
) -> Any:
    """Read and parse a JSON file behind the size and depth guards.

    Raises ``ValueError`` if the file exceeds ``max_bytes`` or nests deeper than
    ``MAX_IMPORT_JSON_DEPTH``. The caller validates the parsed value's shape
    (e.g. rejecting a non-object root).

    ``max_bytes`` defaults to ``MAX_IMPORT_BYTES`` — the right ceiling for an
    *untrusted* imported file. A trusted, app-owned, append-only store (e.g. the
    module passport, which grows one fingerprint per characterization run) can
    legitimately exceed 1 MiB over its lifetime, so those readers pass a higher
    ceiling. The depth guard is not relaxed: deep nesting is pathological for
    every JSON the wrapper reads, trusted or not, and still protects
    ``json.loads`` from ``RecursionError``.
    """

    source = Path(path)
    if source.stat().st_size > max_bytes:
        raise ValueError(
            f"Import file is too large (limit {max_bytes} bytes): {path}"
        )
    raw = source.read_text(encoding="utf-8")
    if _max_json_depth(raw) > MAX_IMPORT_JSON_DEPTH:
        raise ValueError(f"Import file JSON nesting is too deep: {path}")
    return json.loads(raw)
