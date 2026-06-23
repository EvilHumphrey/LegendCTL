"""Import-boundary guard — gives teeth to the SECURITY.md / README / ARCHITECTURE
constraint claims (no network, no input injection, no third-party beyond DearPyGui,
imports nothing outside the app's own packages).

This walks every shipped ``zd_app/`` module + ``main_zd.py``, collects the root
package of every ``import`` / ``from ... import`` (at any nesting depth), and asserts:

  * the ALLOW set — every non-stdlib root is one of: the app's own packages
    (``zd_app``, ``main_zd``), the sole UI runtime dependency (``dearpygui``), or
    the optional HID backend (``hid``). Nothing else (no ``requests``, no
    ``vgamepad``, no random third-party) can sneak in.
  * the DENY set — NOTHING imports a network stack (socket / urllib / requests /
    http / ftplib / smtplib / asyncio / aiohttp / httpx) or an input-injection /
    virtual-device / hook library (vgamepad / vJoy / ViGEm / pynput / keyboard /
    pyautogui / SetWindowsHookEx-style modules).

Together these make the public-facing "no network calls", "no input injection /
virtual devices", "imports nothing outside zd_app/ + main_zd.py + build tools"
promises *test-enforced*, not merely true-by-inspection. A regression that adds a
forbidden import fails here immediately.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

# Repo root = two levels up from this file (tests/ -> repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SHIPPED_ROOTS = (_REPO_ROOT / "zd_app", _REPO_ROOT / "main_zd.py")

# The app's own packages + the only permitted third-party runtime deps.
#   dearpygui — the UI toolkit (the sole hard runtime dependency).
#   hid       — optional HID backend (hidapi), imported guarded; talks to the
#               controller over USB HID, not a network/injection surface.
_ALLOWED_THIRD_PARTY = frozenset({"zd_app", "main_zd", "dearpygui", "hid"})

# Imports that would contradict the published constraints. Roots only.
_DENIED_ROOTS = frozenset(
    {
        # network stacks — contradict "no network calls / no telemetry / no auto-update"
        "socket", "ssl", "urllib", "urllib2", "urllib3", "requests", "httpx",
        "http", "ftplib", "smtplib", "poplib", "imaplib", "telnetlib",
        "asyncio", "aiohttp", "websockets", "xmlrpc",
        # input-injection / virtual-device / global-hook libraries —
        # contradict "no input injection / virtual devices / hooking"
        "vgamepad", "vjoy", "vJoy", "ViGEm", "pyvjoy",
        "pynput", "keyboard", "mouse", "pyautogui", "pydirectinput", "autoit",
    }
)


def _iter_shipped_py_files():
    for root in _SHIPPED_ROOTS:
        if root.is_file() and root.suffix == ".py":
            yield root
        elif root.is_dir():
            yield from root.rglob("*.py")


def _app_internal_names() -> frozenset[str]:
    """Module/package names defined inside the app itself.

    The app imports some sibling modules by bare name (e.g. ``from hid_transport
    import ...`` inside ``zd_app/protocol/``), which parse as level-0 imports but
    resolve to the app's OWN code, not a third party. Treat every ``.py`` stem and
    package directory under ``zd_app/`` as app-internal so the boundary check only
    flags genuine outside dependencies.
    """
    names = {"zd_app", "main_zd"}
    pkg = _REPO_ROOT / "zd_app"
    if pkg.is_dir():
        for p in pkg.rglob("*.py"):
            names.add(p.stem)
        for d in pkg.rglob("*"):
            if d.is_dir():
                names.add(d.name)
    return frozenset(names)


def _import_roots(tree: ast.AST):
    """Yield the root package name of every import in the module (any depth)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            # level>0 is a relative import (within the app) — not a third party.
            if node.level == 0 and node.module:
                yield node.module.split(".")[0]


class ImportBoundaryTests(unittest.TestCase):
    def test_shipped_code_imports_stay_within_the_allowed_boundary(self) -> None:
        stdlib = set(sys.stdlib_module_names)
        app_internal = _app_internal_names()
        offenders: list[str] = []
        for py in _iter_shipped_py_files():
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for root in _import_roots(tree):
                if root in stdlib or root in _ALLOWED_THIRD_PARTY or root in app_internal:
                    continue
                rel = py.relative_to(_REPO_ROOT).as_posix()
                offenders.append(f"{rel}: imports '{root}' (not stdlib / not in the allow set)")
        self.assertEqual(
            offenders,
            [],
            "Shipped code may only import stdlib + "
            f"{sorted(_ALLOWED_THIRD_PARTY)}. Out-of-boundary imports:\n  "
            + "\n  ".join(offenders),
        )

    def test_no_network_or_input_injection_imports(self) -> None:
        hits: list[str] = []
        for py in _iter_shipped_py_files():
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for root in _import_roots(tree):
                if root in _DENIED_ROOTS:
                    rel = py.relative_to(_REPO_ROOT).as_posix()
                    hits.append(f"{rel}: forbidden import '{root}'")
        self.assertEqual(
            hits,
            [],
            "Shipped code imports a network/injection library, contradicting the "
            "no-network / no-input-injection constraints:\n  " + "\n  ".join(hits),
        )


if __name__ == "__main__":
    unittest.main()
