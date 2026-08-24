"""Regression tests for the card-clip diagnostic's subprocess matrix."""

from __future__ import annotations

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from tools import diag_dpg_card_clip as card_clip


class CardClipMatrixTests(unittest.TestCase):
    @staticmethod
    def _completed(returncode: int) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=[], returncode=returncode)

    def _run_with_codes(self, codes: list[int]) -> tuple[int, list]:
        with patch.object(
            card_clip.subprocess,
            "run",
            side_effect=[self._completed(code) for code in codes],
        ) as run:
            with redirect_stdout(io.StringIO()):
                result = card_clip._run_matrix()
        return result, run.call_args_list

    def test_all_green_returns_zero_and_covers_all_locales_and_sizes(self) -> None:
        result, calls = self._run_with_codes([0] * 9)

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 9)
        self.assertEqual(
            {call.args[0][call.args[0].index("--locale") + 1] for call in calls},
            {"en", "zh-CN", "ko"},
        )
        self.assertTrue(
            all(
                call.kwargs["timeout"] == card_clip.MATRIX_CHILD_TIMEOUT_SECONDS
                for call in calls
            )
        )

    def test_any_clip_returns_one_after_running_every_cell(self) -> None:
        result, calls = self._run_with_codes([0, 1, 0, 0, 0, 0, 0, 0, 0])

        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 9)

    def test_first_hard_failure_wins_deterministically(self) -> None:
        result, calls = self._run_with_codes([0, 7, 1, 9, 0, 0, 0, 0, 0])

        self.assertEqual(result, 7)
        self.assertEqual(len(calls), 9)

    def test_measurement_error_is_a_distinct_hard_failure(self) -> None:
        def fail_measurement() -> list[dict]:
            raise RuntimeError("render failed")

        with redirect_stdout(io.StringIO()):
            cards, measurement_error = card_clip._guard_measurement(
                "controller", fail_measurement
            )

        self.assertIsNone(cards)
        self.assertTrue(measurement_error)
        self.assertEqual(
            card_clip._diagnostic_exit_code(
                any_clip=True, measurement_error=measurement_error
            ),
            card_clip.DIAGNOSTIC_ERROR_EXIT_CODE,
        )

        result, calls = self._run_with_codes(
            [0, card_clip.DIAGNOSTIC_ERROR_EXIT_CODE, 1, 0, 0, 0, 0, 0, 0]
        )
        self.assertEqual(result, card_clip.DIAGNOSTIC_ERROR_EXIT_CODE)
        self.assertEqual(len(calls), 9)

    def test_unhandled_single_probe_error_is_not_reported_as_clipping(self) -> None:
        with (
            patch.object(sys, "argv", ["diag_dpg_card_clip.py", "--screen", "home"]),
            patch.object(card_clip, "_run", side_effect=RuntimeError("setup failed")),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            card_clip.main()

        self.assertEqual(raised.exception.code, card_clip.DIAGNOSTIC_ERROR_EXIT_CODE)

    def test_timeout_is_a_hard_failure_and_matrix_continues(self) -> None:
        effects = [
            self._completed(0),
            subprocess.TimeoutExpired(cmd=["python", "probe.py"], timeout=1),
            *[self._completed(0) for _ in range(7)],
        ]
        with patch.object(card_clip.subprocess, "run", side_effect=effects) as run:
            with redirect_stdout(io.StringIO()):
                result = card_clip._run_matrix()

        self.assertEqual(result, card_clip.MATRIX_TIMEOUT_EXIT_CODE)
        self.assertEqual(run.call_count, 9)

    def test_child_launch_error_is_hard_failure_and_matrix_continues(self) -> None:
        effects = [
            self._completed(0),
            OSError("child executable unavailable"),
            self._completed(1),
            *[self._completed(0) for _ in range(6)],
        ]
        with patch.object(card_clip.subprocess, "run", side_effect=effects) as run:
            with redirect_stdout(io.StringIO()):
                result = card_clip._run_matrix()

        self.assertEqual(result, card_clip.DIAGNOSTIC_ERROR_EXIT_CODE)
        self.assertEqual(run.call_count, 9)


if __name__ == "__main__":
    unittest.main()
