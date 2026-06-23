"""Cross-platform subprocess helpers for silent child-process startup."""

from __future__ import annotations

import subprocess
import sys
from typing import Any


SILENT_CREATIONFLAGS: int = 0x08000000 if sys.platform == "win32" else 0


def silent_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    """Run a subprocess without flashing a console window on Windows."""

    extra = kwargs.pop("creationflags", 0)
    kwargs["creationflags"] = SILENT_CREATIONFLAGS | extra
    return subprocess.run(*args, **kwargs)


def silent_popen(*args: Any, **kwargs: Any) -> subprocess.Popen:
    """Start a subprocess without flashing a console window on Windows."""

    extra = kwargs.pop("creationflags", 0)
    kwargs["creationflags"] = SILENT_CREATIONFLAGS | extra
    return subprocess.Popen(*args, **kwargs)
