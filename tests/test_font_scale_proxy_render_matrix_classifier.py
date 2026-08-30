"""Fast contract tests for the font-pressure render-child classifier."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from tests.isolated_font_scale_proxy_common import ALL_CELLS
from tests import test_font_scale_proxy_render_matrix as render_matrix


_CELL_BY_METHOD = {cell.wrapper_method: cell for cell in ALL_CELLS}
_STABLE_METHOD = "test_screens_font_scale_proxy_125_en_1180x760"
_VARIANT_METHOD = "test_modals_font_scale_proxy_125_zh_CN_1180x760"


def _failure_output(method: str) -> str:
    finding = (render_matrix._STABLE_FINDINGS | render_matrix._ENVIRONMENT_VARIANT_FINDINGS)[method]
    evidence = "\n".join(finding.required_fragments)
    return f"{evidence}\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n"


def _success_output() -> str:
    return "Ran 1 test in 0.001s\n\nOK\n"


class RenderChildClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        # Exercise quarantine handling without keeping a repaired UI defect in
        # the live registry just to supply a classifier fixture.
        finding = render_matrix.KnownRenderFinding(
            reason="Synthetic stable Home finding for classifier tests.",
            required_fragments=(
                _CELL_BY_METHOD[_STABLE_METHOD].describe("Home"),
                "hidden child-card overflow detected",
                "tag=home_orientation_card",
            ),
        )
        registry = patch.object(render_matrix, "_STABLE_FINDINGS", {_STABLE_METHOD: finding})
        registry.start()
        self.addCleanup(registry.stop)

    def test_timeout_is_hard_failure_even_with_known_signature(self) -> None:
        classification = render_matrix._classify_child_result(
            _CELL_BY_METHOD[_STABLE_METHOD],
            returncode=None,
            output=_failure_output(_STABLE_METHOD),
            timed_out=True,
        )

        self.assertIs(classification.verdict, render_matrix.RenderCellVerdict.HARD_FAILURE)
        self.assertIn("hang", classification.message)

    def test_unexpected_return_code_rejects_terminal_ok(self) -> None:
        method = "test_screens_font_scale_proxy_125_en_1366x768"
        classification = render_matrix._classify_child_result(
            _CELL_BY_METHOD[method],
            returncode=17,
            output=_success_output(),
        )

        self.assertIs(classification.verdict, render_matrix.RenderCellVerdict.HARD_FAILURE)
        self.assertIn("unexpected return code 17", classification.message)

    def test_early_or_fake_ok_is_not_a_complete_terminal_summary(self) -> None:
        method = "test_screens_font_scale_proxy_125_en_1366x768"
        for output in (
            "OK\nTraceback: later child failure\n",
            "fake child banner\nOK\n",
            "Ran 1 test in 0.001s\n\nFAILED (failures=1)\nOK\n",
        ):
            with self.subTest(output=output):
                classification = render_matrix._classify_child_result(
                    _CELL_BY_METHOD[method],
                    returncode=0,
                    output=output,
                )

                self.assertIs(classification.verdict, render_matrix.RenderCellVerdict.HARD_FAILURE)
                self.assertIn("complete recognized unittest summary", classification.message)

    def test_missing_terminal_summary_is_hard_failure(self) -> None:
        method = "test_screens_font_scale_proxy_125_en_1366x768"
        classification = render_matrix._classify_child_result(
            _CELL_BY_METHOD[method],
            returncode=0,
            output="RENDER_MATRIX_CHILD surface=Home\nRan 1 test in 0.001s\n",
        )

        self.assertIs(classification.verdict, render_matrix.RenderCellVerdict.HARD_FAILURE)

    def test_exact_stable_signature_is_known_finding(self) -> None:
        classification = render_matrix._classify_child_result(
            _CELL_BY_METHOD[_STABLE_METHOD],
            returncode=1,
            output=_failure_output(_STABLE_METHOD),
        )

        self.assertIs(classification.verdict, render_matrix.RenderCellVerdict.KNOWN_FINDING)
        self.assertEqual(classification.message, render_matrix._STABLE_FINDINGS[_STABLE_METHOD].reason)

    def test_exact_known_failure_signature_requires_unittest_failure_code(self) -> None:
        classification = render_matrix._classify_child_result(
            _CELL_BY_METHOD[_STABLE_METHOD],
            returncode=0,
            output=_failure_output(_STABLE_METHOD),
        )

        self.assertIs(classification.verdict, render_matrix.RenderCellVerdict.HARD_FAILURE)
        self.assertIn("unexpected return code 0", classification.message)

    def test_wrong_known_tag_is_hard_failure(self) -> None:
        output = _failure_output(_STABLE_METHOD).replace(
            "tag=home_orientation_card",
            "tag=unrelated_card",
        )
        classification = render_matrix._classify_child_result(
            _CELL_BY_METHOD[_STABLE_METHOD],
            returncode=1,
            output=output,
        )

        self.assertIs(classification.verdict, render_matrix.RenderCellVerdict.HARD_FAILURE)
        self.assertIn("exact known layout signature", classification.message)

    def test_unrelated_assertion_in_registered_cell_is_hard_failure(self) -> None:
        classification = render_matrix._classify_child_result(
            _CELL_BY_METHOD[_STABLE_METHOD],
            returncode=1,
            output="AssertionError: unrelated state invariant\nFAILED (failures=1)\n",
        )

        self.assertIs(classification.verdict, render_matrix.RenderCellVerdict.HARD_FAILURE)

    def test_stable_finding_terminal_ok_is_unexpected_success(self) -> None:
        classification = render_matrix._classify_child_result(
            _CELL_BY_METHOD[_STABLE_METHOD],
            returncode=0,
            output=_success_output(),
        )

        self.assertIs(classification.verdict, render_matrix.RenderCellVerdict.HARD_FAILURE)
        self.assertIn("unexpectedly passed", classification.message)

    def test_environment_variant_terminal_ok_passes(self) -> None:
        classification = render_matrix._classify_child_result(
            _CELL_BY_METHOD[_VARIANT_METHOD],
            returncode=0,
            output=_success_output(),
        )

        self.assertIs(classification.verdict, render_matrix.RenderCellVerdict.PASS)

    def test_environment_variant_exact_signature_is_known_finding(self) -> None:
        classification = render_matrix._classify_child_result(
            _CELL_BY_METHOD[_VARIANT_METHOD],
            returncode=1,
            output=_failure_output(_VARIANT_METHOD),
        )

        self.assertIs(classification.verdict, render_matrix.RenderCellVerdict.KNOWN_FINDING)

    def test_only_documented_success_and_dpg_teardown_codes_pass(self) -> None:
        method = "test_screens_font_scale_proxy_125_en_1366x768"
        for returncode in render_matrix._SUCCESS_RETURN_CODES:
            with self.subTest(returncode=returncode):
                classification = render_matrix._classify_child_result(
                    _CELL_BY_METHOD[method],
                    returncode=returncode,
                    output=_success_output(),
                )
                self.assertIs(classification.verdict, render_matrix.RenderCellVerdict.PASS)


class RenderFindingRegistryIntegrityTests(unittest.TestCase):
    def test_repaired_screen_and_wide_cells_have_no_exemptions(self) -> None:
        registered = set(render_matrix._STABLE_FINDINGS) | set(render_matrix._ENVIRONMENT_VARIANT_FINDINGS)
        self.assertFalse({cell.wrapper_method for cell in ALL_CELLS if cell.group != "modals"} & registered)

    def test_every_registered_key_is_a_real_matrix_cell(self) -> None:
        registered = set(render_matrix._STABLE_FINDINGS) | set(render_matrix._ENVIRONMENT_VARIANT_FINDINGS)

        self.assertTrue(registered)
        self.assertEqual(registered & set(_CELL_BY_METHOD), registered)
        self.assertFalse(
            set(render_matrix._STABLE_FINDINGS) & set(render_matrix._ENVIRONMENT_VARIANT_FINDINGS)
        )

    def test_every_finding_has_nonempty_unique_signature_fragments(self) -> None:
        findings = render_matrix._STABLE_FINDINGS | render_matrix._ENVIRONMENT_VARIANT_FINDINGS
        for method, finding in findings.items():
            with self.subTest(method=method):
                self.assertTrue(finding.reason.strip())
                self.assertTrue(finding.required_fragments)
                self.assertEqual(len(finding.required_fragments), len(set(finding.required_fragments)))
                self.assertTrue(all(fragment.strip() for fragment in finding.required_fragments))

    def test_every_finding_signature_names_its_exact_cell_descriptor(self) -> None:
        findings = render_matrix._STABLE_FINDINGS | render_matrix._ENVIRONMENT_VARIANT_FINDINGS
        surfaces = {
            "screens": "Home",
            "wide": "Diagnostics",
            "modals": "first-run acknowledgment",
        }
        for method, finding in findings.items():
            with self.subTest(method=method):
                cell = _CELL_BY_METHOD[method]
                self.assertIn(cell.describe(surfaces[cell.group]), finding.required_fragments)

    def test_success_code_allowlist_is_exact(self) -> None:
        self.assertEqual(
            render_matrix._SUCCESS_RETURN_CODES,
            {0, 139, 0xC0000005, -1073741819},
        )

    def test_installed_matrix_methods_do_not_retain_broad_expected_failure_flag(self) -> None:
        for cell in ALL_CELLS:
            with self.subTest(method=cell.wrapper_method):
                method = getattr(render_matrix.FontScaleProxyRenderMatrixTests, cell.wrapper_method)
                self.assertFalse(getattr(method, "__unittest_expecting_failure__", False))


if __name__ == "__main__":
    unittest.main()
