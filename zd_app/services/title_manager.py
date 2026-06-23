"""Win32 OS-level window-title management with generation-counter coordination.

The wrapper's OS title needs occasional re-application after DPG / glfw
internals briefly overwrite ``SetWindowTextW`` with a stale ASCII form
(observed in R2.3 testing roughly 100ms and 500ms after the create_viewport
call and after locale rebuilds). This manager schedules two delayed
re-applies; each fires only if the generation counter is still current,
so a rapid title change supersedes pending re-applies.

DPG-free at import time. The OS-level Win32 work is injected as an
``apply_fn`` callback so the ``zd_app/ui/app_shell.py`` ``set_window_title_unicode``
helper (which has its own dedicated test surface) can stay where it is and
this manager can be unit-tested with a recording fake.

R2.3 Fix C — locale-conditional ASCII fallback for zh-CN — is handled at the
i18n layer (``zh-CN.json``'s ``app.window_title`` key resolves to a pure-ASCII
string), so callers always pass the already-localized title here without
locale awareness inside the manager itself.

Extracted from ``zd_app/ui/app_shell.py`` so title management lives outside
the UI tier.
"""

from __future__ import annotations

import threading
from typing import Callable


class TitleManager:
    """Coordinates ``SetWindowTextW`` calls + generation-counter reapplies."""

    def __init__(self, apply_fn: Callable[[str], object]) -> None:
        """Construct a manager that calls ``apply_fn(title)`` on each apply.

        ``apply_fn`` is expected to perform the actual OS-level title update
        (``SetWindowTextW`` on Windows). Its return value is ignored.
        """
        self._apply_fn = apply_fn
        self._generation = 0
        self._lock = threading.Lock()

    @property
    def generation(self) -> int:
        """Current generation counter; increments on every ``set_title`` call."""
        with self._lock:
            return self._generation

    def set_title(self, title: str) -> None:
        """Apply ``title`` immediately and schedule two delayed re-applies."""
        with self._lock:
            self._generation += 1
            generation = self._generation
        self._apply_fn(title)
        self._schedule_reapply(title, generation, delay_s=0.1)
        self._schedule_reapply(title, generation, delay_s=0.5)

    def _schedule_reapply(self, title: str, generation: int, *, delay_s: float) -> None:
        timer = threading.Timer(
            delay_s,
            lambda: self._reapply_if_current(title, generation),
        )
        timer.daemon = True
        timer.start()

    def _reapply_if_current(self, title: str, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
        self._apply_fn(title)
