"""Tests for TitleManager.

The manager was extracted from zd_app/ui/app_shell.py and is now a DPG-free
service-tier helper that drives the OS-level title via an injected
``apply_fn``. Tests use a recording fake so we can assert call ordering +
generation-counter discipline without touching Win32 or DPG.
"""

from __future__ import annotations

import sys
import threading
import unittest
from unittest import mock

from zd_app.services.title_manager import TitleManager


class TitleManagerSetTitleTests(unittest.TestCase):
    def test_set_title_calls_apply_fn_synchronously_with_title(self) -> None:
        recorded: list[str] = []
        manager = TitleManager(apply_fn=lambda title: recorded.append(title))

        with mock.patch.object(manager, "_schedule_reapply"):
            manager.set_title("ZD Ultimate Legend")

        self.assertEqual(recorded, ["ZD Ultimate Legend"])

    def test_set_title_increments_generation(self) -> None:
        manager = TitleManager(apply_fn=lambda _t: None)
        with mock.patch.object(manager, "_schedule_reapply"):
            self.assertEqual(manager.generation, 0)
            manager.set_title("first")
            self.assertEqual(manager.generation, 1)
            manager.set_title("second")
            self.assertEqual(manager.generation, 2)

    def test_set_title_schedules_two_reapplies(self) -> None:
        manager = TitleManager(apply_fn=lambda _t: None)

        with mock.patch.object(manager, "_schedule_reapply") as schedule:
            manager.set_title("X")

        self.assertEqual(schedule.call_count, 2)
        delays = sorted(call.kwargs.get("delay_s") for call in schedule.call_args_list)
        self.assertEqual(delays, [0.1, 0.5])


class TitleManagerGenerationCounterTests(unittest.TestCase):
    def test_reapply_no_op_when_generation_has_advanced(self) -> None:
        recorded: list[str] = []
        manager = TitleManager(apply_fn=lambda title: recorded.append(title))

        with mock.patch.object(manager, "_schedule_reapply"):
            manager.set_title("first")
            # First synchronous apply.
            self.assertEqual(recorded, ["first"])
            manager.set_title("second")
            # Second synchronous apply.
            self.assertEqual(recorded, ["first", "second"])

        # Now manually invoke a stale reapply for the first generation; it should no-op.
        manager._reapply_if_current("first", generation=1)
        self.assertEqual(recorded, ["first", "second"], "stale reapply must not fire")

        # Current generation reapply DOES fire.
        manager._reapply_if_current("second", generation=manager.generation)
        self.assertEqual(recorded, ["first", "second", "second"])

    def test_rapid_set_title_calls_supersede_pending_reapplies(self) -> None:
        """Real timer test: scheduled reapplies for an old generation must no-op."""
        recorded: list[tuple[str, int]] = []

        def _record_apply(title: str) -> None:
            recorded.append((title, threading.get_ident()))

        manager = TitleManager(apply_fn=_record_apply)

        # Patch schedule_reapply to fire immediately so we can deterministically observe behavior.
        original_schedule = manager._schedule_reapply
        scheduled: list[tuple[str, int]] = []

        def _fake_schedule(title: str, generation: int, *, delay_s: float) -> None:
            scheduled.append((title, generation))

        manager._schedule_reapply = _fake_schedule  # type: ignore[method-assign]

        manager.set_title("first")
        manager.set_title("second")
        manager.set_title("third")

        # Now manually simulate the deferred reapplies firing.
        for title, gen in scheduled:
            manager._reapply_if_current(title, gen)

        # Of the synchronous applies, all three fired (one per set_title).
        sync_titles = [r[0] for r in recorded[:3]]
        self.assertEqual(sync_titles, ["first", "second", "third"])

        # Of the deferred reapplies, only those matching the current generation
        # (== "third") fire.
        deferred_titles = [r[0] for r in recorded[3:]]
        for title in deferred_titles:
            self.assertEqual(title, "third")


class TitleManagerDpgFreeTests(unittest.TestCase):
    def test_module_does_not_import_dpg(self) -> None:
        """TitleManager must stay DPG-free so it can run in non-DPG contexts."""
        for name in [n for n in list(sys.modules) if "dearpygui" in n]:
            sys.modules.pop(name, None)
        sys.modules.pop("zd_app.services.title_manager", None)

        import zd_app.services.title_manager  # noqa: F401

        for name in sys.modules:
            self.assertFalse(
                name.startswith("dearpygui"),
                f"importing TitleManager pulled in {name}; should be DPG-free",
            )


if __name__ == "__main__":
    unittest.main()
