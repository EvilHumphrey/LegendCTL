"""Discovered serial wrapper for isolated font-pressure proxy render cells.

There are 40 discovered methods: 24 screen-matrix cells, 8 wide sentinels,
and 8 modal cells.  The always-on tier runs scales 1.25/2.00 at the 1180x760
and 1480x1040 screen viewports, plus every wide/modal cell (8 + 4 + 4 = 16
children).  ``ZD_RENDER_MATRIX=full`` additionally runs the 1.50/1.75 cells
and the 1366x768 screen cells (the other 24 children).
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest

from tests.isolated_font_scale_proxy_common import ALL_CELLS, MatrixCell


_CHILD_CLASSES = {
    "screens": (
        "tests.isolated_font_scale_proxy_screens",
        "IsolatedFontScaleProxyScreenTest",
        "Home + Diagnostics + Live Verify + Controller + Restore Points",
    ),
    "wide": (
        "tests.isolated_font_scale_proxy_wide",
        "IsolatedFontScaleProxyWideTest",
        "Diagnostics + Live Verify Inspector",
    ),
    "modals": (
        "tests.isolated_font_scale_proxy_modals",
        "IsolatedFontScaleProxyModalTest",
        "first-run acknowledgment + crash-review modal + profile-delete modal swap",
    ),
}


# These annotations intentionally live on the discovered wrapper, not the
# isolated child.  The child retains its full-strength assertion and literal
# ``OK``-only subprocess verdict contract; a product-finding cell becomes an
# unexpected success as soon as the follow-up app fix lands.
_EXPECTED_FAILURE_REASONS = {
    "test_screens_font_scale_proxy_125_en_1180x760": (
        "home_orientation_card and sibling Home fixed cards overflow at font_scale_proxy_125/en/1180x760 - "
        "pending Home card autosize fix lane."
    ),
    "test_screens_font_scale_proxy_125_en_1480x1040": (
        "home_orientation_card and sibling Home fixed cards overflow at font_scale_proxy_125/en/1480x1040 - "
        "pending Home card autosize fix lane."
    ),
    "test_screens_font_scale_proxy_125_zh_CN_1180x760": (
        "home_orientation_card and sibling Home fixed cards overflow at font_scale_proxy_125/zh-CN/1180x760 - "
        "pending Home card autosize fix lane."
    ),
    "test_screens_font_scale_proxy_125_zh_CN_1480x1040": (
        "home_orientation_card and sibling Home fixed cards overflow at font_scale_proxy_125/zh-CN/1480x1040 - "
        "pending Home card autosize fix lane."
    ),
    "test_screens_font_scale_proxy_200_en_1180x760": (
        "home_orientation_card and sibling Home fixed cards overflow at font_scale_proxy_200/en/1180x760 - "
        "pending Home card autosize fix lane."
    ),
    "test_screens_font_scale_proxy_200_en_1480x1040": (
        "home_orientation_card and sibling Home fixed cards overflow at font_scale_proxy_200/en/1480x1040 - "
        "pending Home card autosize fix lane."
    ),
    "test_screens_font_scale_proxy_200_zh_CN_1180x760": (
        "home_orientation_card and sibling Home fixed cards overflow at font_scale_proxy_200/zh-CN/1180x760 - "
        "pending Home card autosize fix lane."
    ),
    "test_screens_font_scale_proxy_200_zh_CN_1480x1040": (
        "home_orientation_card and sibling Home fixed cards overflow at font_scale_proxy_200/zh-CN/1480x1040 - "
        "pending Home card autosize fix lane."
    ),
    "test_wide_font_scale_proxy_200_en_1920x1040": (
        "Diagnostics Event Log fixed child path[0, 0, 3, 2, 10] overflows at font_scale_proxy_200/en/1920x1040 - "
        "pending Diagnostics event-log autosize fix lane."
    ),
    "test_wide_font_scale_proxy_200_zh_CN_1920x1040": (
        "Diagnostics Event Log fixed child path[0, 0, 3, 2, 10] overflows at font_scale_proxy_200/zh-CN/1920x1040 - "
        "pending Diagnostics event-log autosize fix lane."
    ),
    "test_modals_font_scale_proxy_125_zh_CN_1180x760": (
        "first_run_ack_intro_text extends beyond the consent modal horizontally at "
        "font_scale_proxy_125/zh-CN/1180x760 on the py3.12 gate build (passed on the lane's "
        "py3.11 wheel - borderline fit) - pending consent-gate text wrap/width fix lane."
    ),
}


def _child_id(cell: MatrixCell) -> str:
    module, class_name, _surface = _CHILD_CLASSES[cell.group]
    return f"{module}.{class_name}.{cell.child_method}"


def _surface(cell: MatrixCell) -> str:
    return _CHILD_CLASSES[cell.group][2]


def _timeout_output(exc: subprocess.TimeoutExpired) -> str:
    parts: list[str] = []
    for part in (exc.stdout, exc.stderr):
        if isinstance(part, bytes):
            parts.append(part.decode(errors="replace"))
        elif part:
            parts.append(part)
    return "\n".join(parts)


class FontScaleProxyRenderMatrixTests(unittest.TestCase):
    """One fresh interpreter/viewport per cell; never launch children in parallel."""

    def _run_cell(self, cell: MatrixCell) -> None:
        if cell.tier == 2 and os.environ.get("ZD_RENDER_MATRIX") != "full":
            message = (
                "ZD_RENDER_MATRIX=full is not set; skipped Tier 2 "
                "font_scale_proxy_150/175 (all cells) and viewport 1366x768 "
                "(screen cells)."
            )
            print(f"RENDER_MATRIX_SKIP {cell.describe(_surface(cell))}: {message}", flush=True)
            self.skipTest(message)

        repo_root = Path(__file__).resolve().parents[1]
        test_id = _child_id(cell)
        print(f"RENDER_MATRIX_WRAPPER {cell.describe(_surface(cell))}", flush=True)
        reason = _EXPECTED_FAILURE_REASONS.get(cell.wrapper_method)
        if reason:
            print(
                f"RENDER_MATRIX_EXPECTED_FAILURE {cell.describe(_surface(cell))}: {reason}",
                flush=True,
            )
        try:
            result = subprocess.run(
                [sys.executable, "-m", "unittest", test_id],
                cwd=repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            output = _timeout_output(exc)
            self.fail(
                "Native render hang class in isolated font-pressure proxy child.\n"
                f"{cell.describe(_surface(cell))}\n\n"
                + (f"Child output before timeout:\n{output}" if output else "No child output before timeout.")
            )

        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if any(line.strip() == "OK" for line in output.splitlines()):
            return
        self.fail(
            "Isolated font-pressure proxy child did not report literal unittest OK.\n"
            f"{cell.describe(_surface(cell))}\n"
            f"Return code: {result.returncode}\n"
            f"Command: {sys.executable} -m unittest {test_id}\n\n"
            f"Child output:\n{output}"
        )


def _install_cell_methods() -> None:
    for cell in ALL_CELLS:
        reason = _EXPECTED_FAILURE_REASONS.get(cell.wrapper_method)

        def test_method(self, cell=cell) -> None:
            self._run_cell(cell)

        test_method.__name__ = cell.wrapper_method
        test_method.__qualname__ = f"{FontScaleProxyRenderMatrixTests.__name__}.{cell.wrapper_method}"
        test_method.__doc__ = reason
        if reason:
            test_method = unittest.expectedFailure(test_method)
        setattr(FontScaleProxyRenderMatrixTests, cell.wrapper_method, test_method)


_install_cell_methods()


if __name__ == "__main__":
    unittest.main()
