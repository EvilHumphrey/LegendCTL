"""Tests for silent subprocess wrappers."""

from __future__ import annotations

import importlib
import unittest
from unittest.mock import MagicMock, patch

import zd_app.services._subprocess_helpers as helpers


class TestSubprocessHelpers(unittest.TestCase):
    def tearDown(self) -> None:
        importlib.reload(helpers)

    def test_silent_creationflags_on_windows(self) -> None:
        with patch("sys.platform", "win32"):
            importlib.reload(helpers)

        self.assertEqual(helpers.SILENT_CREATIONFLAGS, 0x08000000)

    def test_silent_creationflags_on_non_windows(self) -> None:
        with patch("sys.platform", "linux"):
            importlib.reload(helpers)

        self.assertEqual(helpers.SILENT_CREATIONFLAGS, 0)

    def test_silent_run_passes_creationflag(self) -> None:
        completed = MagicMock()
        with patch.object(helpers.subprocess, "run", return_value=completed) as run_mock:
            result = helpers.silent_run(["echo", "x"])

        self.assertIs(result, completed)
        self.assertEqual(run_mock.call_args.kwargs["creationflags"], helpers.SILENT_CREATIONFLAGS)

    def test_silent_run_preserves_caller_creationflags(self) -> None:
        with patch.object(helpers.subprocess, "run") as run_mock:
            helpers.silent_run(["x"], creationflags=0x200)

        self.assertEqual(
            run_mock.call_args.kwargs["creationflags"],
            helpers.SILENT_CREATIONFLAGS | 0x200,
        )

    def test_silent_popen_passes_creationflag(self) -> None:
        process = MagicMock()
        with patch.object(helpers.subprocess, "Popen", return_value=process) as popen_mock:
            result = helpers.silent_popen(["echo", "x"])

        self.assertIs(result, process)
        self.assertEqual(popen_mock.call_args.kwargs["creationflags"], helpers.SILENT_CREATIONFLAGS)

    def test_silent_run_forwards_other_kwargs(self) -> None:
        with patch.object(helpers.subprocess, "run") as run_mock:
            helpers.silent_run(["echo", "x"], capture_output=True, text=True)

        self.assertTrue(run_mock.call_args.kwargs["capture_output"])
        self.assertTrue(run_mock.call_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
