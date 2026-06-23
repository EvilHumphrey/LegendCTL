"""Unit tests for the first-run onboarding dialog.

All Tkinter primitives are dependency-injected so the tests run on
headless / non-display environments.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from zd_app.ui.onboarding import prompt_for_vendor_path


class FakeRoot:
    def __init__(self) -> None:
        self.withdrawn = False
        self.destroyed = False

    def withdraw(self) -> None:
        self.withdrawn = True

    def destroy(self) -> None:
        self.destroyed = True


class TestPromptForVendorPath(unittest.TestCase):
    def test_returns_chosen_path(self) -> None:
        root = FakeRoot()
        chosen = prompt_for_vendor_path(
            askopenfilename=lambda **_: r"C:\picked\ZD Game Zone 3.7.exe",
            showinfo=lambda **_: None,
            tk_factory=lambda: root,
        )
        self.assertEqual(chosen, r"C:\picked\ZD Game Zone 3.7.exe")
        self.assertTrue(root.withdrawn)
        self.assertTrue(root.destroyed)

    def test_returns_none_when_cancelled(self) -> None:
        chosen = prompt_for_vendor_path(
            askopenfilename=lambda **_: "",  # Tkinter returns "" on cancel
            showinfo=lambda **_: None,
            tk_factory=FakeRoot,
        )
        self.assertIsNone(chosen)

    def test_returns_none_when_dialog_returns_none(self) -> None:
        chosen = prompt_for_vendor_path(
            askopenfilename=lambda **_: None,
            showinfo=lambda **_: None,
            tk_factory=FakeRoot,
        )
        self.assertIsNone(chosen)

    def test_shows_info_dialog_before_file_picker(self) -> None:
        order: list[str] = []

        def fake_showinfo(**_kwargs):
            order.append("showinfo")

        def fake_askopen(**_kwargs):
            order.append("askopen")
            return r"C:\picked.exe"

        prompt_for_vendor_path(
            askopenfilename=fake_askopen,
            showinfo=fake_showinfo,
            tk_factory=FakeRoot,
        )
        self.assertEqual(order, ["showinfo", "askopen"])

    def test_root_destroyed_even_when_picker_raises(self) -> None:
        root = FakeRoot()

        def boom(**_kwargs):
            raise RuntimeError("unexpected")

        with self.assertRaises(RuntimeError):
            prompt_for_vendor_path(
                askopenfilename=boom,
                showinfo=lambda **_: None,
                tk_factory=lambda: root,
            )
        self.assertTrue(root.destroyed)


if __name__ == "__main__":
    unittest.main()
