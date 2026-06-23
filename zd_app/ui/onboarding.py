"""First-run onboarding dialog for the wrapper.

Uses Tkinter (Python stdlib) because the wrapper boots before AppShell
and DearPyGUI's render context isn't yet initialized — paying its
window-class-registration cost just for one dialog isn't worth it, and
running both UI toolkits in the same process pre-AppShell adds avoidable
risk.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional


logger = logging.getLogger(__name__)


def prompt_for_vendor_path(
    *,
    initial_dir: str = "",
    askopenfilename: Optional[Callable[..., str]] = None,
    showinfo: Optional[Callable[..., None]] = None,
    tk_factory: Optional[Callable[[], object]] = None,
) -> Optional[str]:
    """Show the operator a Tkinter file picker for vendor.exe.

    Returns the chosen path, or ``None`` if the user cancelled. The Tkinter
    primitives are dependency-injectable so unit tests don't need a display.
    """
    if askopenfilename is None or showinfo is None or tk_factory is None:
        # Lazy-import Tkinter so headless test environments don't fail at
        # module import time.
        import tkinter as tk
        from tkinter import filedialog, messagebox

        askopenfilename = askopenfilename or filedialog.askopenfilename
        showinfo = showinfo or messagebox.showinfo
        tk_factory = tk_factory or tk.Tk

    root = tk_factory()
    try:
        # Hide the empty root window — only the dialogs should appear.
        if hasattr(root, "withdraw"):
            root.withdraw()
        showinfo(
            title="ZD Ultimate Legend Wrapper - First-Run Setup",
            message=(
                "ZD Game Zone 3.7.exe was not found in any default install "
                "location.\n\n"
                "Click OK, then browse to your ZD Game Zone 3.7 installation."
            ),
        )
        chosen = askopenfilename(
            title="Select ZD Game Zone 3.7.exe",
            initialdir=initial_dir,
            filetypes=[
                ("ZD Game Zone (vendor)", "ZD Game Zone 3.7.exe"),
                ("Executables", "*.exe"),
                ("All files", "*.*"),
            ],
        )
        if not chosen:
            return None
        return str(chosen)
    finally:
        try:
            if hasattr(root, "destroy"):
                root.destroy()
        except Exception:  # pragma: no cover - cleanup best-effort
            pass
