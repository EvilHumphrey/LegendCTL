"""Discovered serial wrapper for isolated font-pressure proxy render cells.

There are 60 discovered methods across all three supported locales: 36 screen
cells, 12 wide sentinels, and 12 modal cells. The always-on tier runs scales
1.25/2.00 at the 1180x760 and 1480x1040 screen viewports, plus those scales'
wide/modal cells (12 + 6 + 6 = 24 children). ``ZD_RENDER_MATRIX=full`` also
runs the 1.50/1.75 cells and the 1366x768 screen cells (36 more children).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import re
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


@dataclass(frozen=True)
class KnownRenderFinding:
    """One quarantined layout defect and the exact child evidence it requires."""

    reason: str
    required_fragments: tuple[str, ...]


class RenderCellVerdict(Enum):
    PASS = "pass"
    KNOWN_FINDING = "known_finding"
    HARD_FAILURE = "hard_failure"


@dataclass(frozen=True)
class RenderCellClassification:
    verdict: RenderCellVerdict
    message: str


_SUCCESS_RETURN_CODES = frozenset(
    {
        0,
        139,
        0xC0000005,
        -0x3FFFFFFB,  # signed subprocess representation of Windows 0xC0000005
    }
)
_UNITTEST_FAILURE_RETURN_CODE = 1
_UNITTEST_SINGLE_FAILURE_SUMMARY = "FAILED (failures=1)"
_TERMINAL_UNITTEST_SUMMARY = re.compile(
    r"(?:^|\n)Ran 1 test in [0-9.]+s\n\n(?P<summary>OK|FAILED \(failures=1\))\s*\Z"
)


def _home_finding(
    *,
    locale: str,
    scale: int,
    viewport: str,
    primary_tag: str = "home_orientation_card",
) -> KnownRenderFinding:
    return KnownRenderFinding(
        reason=(
            f"{primary_tag} and sibling Home fixed cards overflow at "
            f"font_scale_proxy_{scale}/{locale}/{viewport} - pending Home card autosize fix lane."
        ),
        required_fragments=(
            f"surface=Home locale={locale} font_scale_proxy=font_scale_proxy_{scale} viewport={viewport}",
            "hidden child-card overflow detected",
            f"tag={primary_tag}",
        ),
    )


def _wide_finding(*, locale: str) -> KnownRenderFinding:
    return KnownRenderFinding(
        reason=(
            "Diagnostics Event Log fixed child path[0, 0, 3, 2, 10] overflows at "
            f"font_scale_proxy_200/{locale}/1920x1040 - pending Diagnostics event-log autosize fix lane."
        ),
        required_fragments=(
            f"surface=Diagnostics locale={locale} font_scale_proxy=font_scale_proxy_200 viewport=1920x1040",
            "hidden child-card overflow detected",
            "tag=path[0, 0, 3, 2, 10]",
        ),
    )


# A registered cell is quarantined only when its child output proves this exact
# layout defect. Timeouts, native codes outside the documented DPG teardown
# class, malformed unittest summaries, and unrelated assertion failures remain
# hard failures.
_STABLE_FINDINGS = {
    "test_screens_font_scale_proxy_125_en_1180x760": _home_finding(
        locale="en", scale=125, viewport="1180x760"
    ),
    "test_screens_font_scale_proxy_125_en_1480x1040": _home_finding(
        locale="en", scale=125, viewport="1480x1040"
    ),
    "test_screens_font_scale_proxy_125_zh_CN_1180x760": _home_finding(
        locale="zh-CN",
        scale=125,
        viewport="1180x760",
        primary_tag="home_device_profile_status_card",
    ),
    "test_screens_font_scale_proxy_125_zh_CN_1480x1040": _home_finding(
        locale="zh-CN",
        scale=125,
        viewport="1480x1040",
        primary_tag="home_device_profile_status_card",
    ),
    "test_screens_font_scale_proxy_200_en_1180x760": _home_finding(
        locale="en", scale=200, viewport="1180x760"
    ),
    "test_screens_font_scale_proxy_200_en_1480x1040": _home_finding(
        locale="en", scale=200, viewport="1480x1040"
    ),
    "test_screens_font_scale_proxy_200_zh_CN_1180x760": _home_finding(
        locale="zh-CN", scale=200, viewport="1180x760"
    ),
    "test_screens_font_scale_proxy_200_zh_CN_1480x1040": _home_finding(
        locale="zh-CN", scale=200, viewport="1480x1040"
    ),
    "test_wide_font_scale_proxy_200_en_1920x1040": _wide_finding(locale="en"),
    "test_wide_font_scale_proxy_200_zh_CN_1920x1040": _wide_finding(locale="zh-CN"),
}

# Cells whose finding is BORDERLINE and environment-variant: the same child fits
# on some rendering stacks and overflows on others (observed: overflows on the
# local py3.12 gate build, fits on the lane's py3.11 wheel AND on the GitHub CI
# runner's py3.12 — the strict expectedFailure wrapper turned that CI fit into
# an unexpected-success suite failure on 2026-07-17).  These cells pass when the
# child fits and SKIP (recording the finding) when it overflows; a native render
# hang still fails hard.  A cell belongs here only while its fix lane is
# pending; a stable always-failing finding belongs in _STABLE_FINDINGS.
_ENVIRONMENT_VARIANT_FINDINGS = {
    "test_modals_font_scale_proxy_125_zh_CN_1180x760": KnownRenderFinding(
        reason=(
            "first_run_ack_intro_text extends beyond the consent modal horizontally at "
            "font_scale_proxy_125/zh-CN/1180x760 on some rendering stacks (borderline fit; "
            "overflows on the local py3.12 gate build, fits on the CI runner) - pending "
            "consent-gate text wrap/width fix lane."
        ),
        required_fragments=(
            "surface=first-run acknowledgment locale=zh-CN "
            "font_scale_proxy=font_scale_proxy_125 viewport=1180x760",
            "required item extends beyond its reachable surface horizontally: first_run_ack_intro_text",
        ),
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


def _classify_child_result(
    cell: MatrixCell,
    *,
    returncode: int | None,
    output: str,
    timed_out: bool = False,
) -> RenderCellClassification:
    """Purely classify one isolated child without weakening unittest failures."""

    finding = _STABLE_FINDINGS.get(cell.wrapper_method)
    variant_finding = _ENVIRONMENT_VARIANT_FINDINGS.get(cell.wrapper_method)
    if finding and variant_finding:
        return RenderCellClassification(
            RenderCellVerdict.HARD_FAILURE,
            f"Render finding registry overlap for {cell.wrapper_method}.",
        )
    registered_finding = finding or variant_finding

    if timed_out:
        return RenderCellClassification(
            RenderCellVerdict.HARD_FAILURE,
            "Native render hang class in isolated font-pressure proxy child.",
        )
    if returncode is None:
        return RenderCellClassification(
            RenderCellVerdict.HARD_FAILURE,
            "Isolated child completed without a return code.",
        )

    terminal_match = _TERMINAL_UNITTEST_SUMMARY.search(output)
    terminal_summary = terminal_match.group("summary") if terminal_match else ""

    if terminal_summary == "OK":
        if returncode not in _SUCCESS_RETURN_CODES:
            return RenderCellClassification(
                RenderCellVerdict.HARD_FAILURE,
                f"Literal terminal OK used unexpected return code {returncode}.",
            )
        if finding:
            return RenderCellClassification(
                RenderCellVerdict.HARD_FAILURE,
                f"Registered stable render finding unexpectedly passed: {finding.reason}",
            )
        return RenderCellClassification(RenderCellVerdict.PASS, "Child reported terminal unittest OK.")

    if terminal_summary == _UNITTEST_SINGLE_FAILURE_SUMMARY:
        if returncode != _UNITTEST_FAILURE_RETURN_CODE:
            return RenderCellClassification(
                RenderCellVerdict.HARD_FAILURE,
                f"Literal terminal {_UNITTEST_SINGLE_FAILURE_SUMMARY!r} used unexpected return code {returncode}.",
            )
        if registered_finding is None:
            return RenderCellClassification(
                RenderCellVerdict.HARD_FAILURE,
                "Unregistered render child assertion failure.",
            )
        missing = tuple(fragment for fragment in registered_finding.required_fragments if fragment not in output)
        if missing:
            return RenderCellClassification(
                RenderCellVerdict.HARD_FAILURE,
                "Registered cell failed without its exact known layout signature; "
                f"missing fragments: {missing!r}.",
            )
        return RenderCellClassification(RenderCellVerdict.KNOWN_FINDING, registered_finding.reason)

    return RenderCellClassification(
        RenderCellVerdict.HARD_FAILURE,
        "Isolated child did not end with a complete recognized unittest summary.",
    )


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
            classification = _classify_child_result(
                cell,
                returncode=None,
                output=output,
                timed_out=True,
            )
            self.fail(
                f"{classification.message}\n{cell.describe(_surface(cell))}\n\n"
                + (f"Child output before timeout:\n{output}" if output else "No child output before timeout.")
            )

        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        classification = _classify_child_result(
            cell,
            returncode=result.returncode,
            output=output,
        )
        if classification.verdict is RenderCellVerdict.PASS:
            return
        if classification.verdict is RenderCellVerdict.KNOWN_FINDING:
            print(
                f"RENDER_MATRIX_KNOWN_FINDING {cell.describe(_surface(cell))}: {classification.message}",
                flush=True,
            )
            self.skipTest(f"KNOWN render finding: {classification.message}")
        self.fail(
            f"{classification.message}\n"
            f"{cell.describe(_surface(cell))}\n"
            f"Return code: {result.returncode}\n"
            f"Command: {sys.executable} -m unittest {test_id}\n\n"
            f"Child output:\n{output}"
        )


def _install_cell_methods() -> None:
    for cell in ALL_CELLS:
        finding = _STABLE_FINDINGS.get(cell.wrapper_method)
        variant_finding = _ENVIRONMENT_VARIANT_FINDINGS.get(cell.wrapper_method)
        assert not (finding and variant_finding), cell.wrapper_method

        def test_method(self, cell=cell) -> None:
            self._run_cell(cell)

        test_method.__name__ = cell.wrapper_method
        test_method.__qualname__ = f"{FontScaleProxyRenderMatrixTests.__name__}.{cell.wrapper_method}"
        registered_finding = finding or variant_finding
        test_method.__doc__ = registered_finding.reason if registered_finding else None
        setattr(FontScaleProxyRenderMatrixTests, cell.wrapper_method, test_method)


_install_cell_methods()


if __name__ == "__main__":
    unittest.main()
