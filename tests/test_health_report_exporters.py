"""Tests for the Health Report Markdown + JSON exporters.

Round-trip JSON shape assertions, all-8-Markdown-sections presence, and the
single most-load-bearing invariant: the verbatim claim-boundary paragraph
appears in every Markdown export and every JSON ``claim_boundary_paragraph``
field. (The forbidden-phrase test in
``tests/test_health_report_forbidden_phrases.py`` enforces the negative
claim — that nothing the exporters produce contains forbidden vocabulary.)

The observed-cadence section was removed from the report on 2026-05-30, so
the exporters no longer emit it.
"""

from __future__ import annotations

import dataclasses
import json
import math
import unittest

from zd_app.services.health_report import (
    CLAIM_BOUNDARY_ID,
    CLAIM_BOUNDARY_PARAGRAPH,
    MEASUREMENT_BOUNDARY_SUMMARY,
    SCHEMA_VERSION,
    DeviceContext,
    HealthReport,
    OverallStatus,
    RangeCoverageLabel,
    RestNoiseLabel,
    SampleQuality,
    StickRange,
    StickRangeReport,
    StickRestNoise,
    StickRestNoiseReport,
    StickSectorCoverage,
    TriggerRange,
    TriggerRangeReport,
    TriggerSmoothnessLabel,
    to_json,
    to_json_dict,
    to_markdown,
)


def _fixture_rest_side(label: RestNoiseLabel = RestNoiseLabel.GOOD) -> StickRestNoise:
    # Stick fields are signed integer percent of full axis travel (same unit
    # as the wrapper's Sticks-tab deadzone slider).
    return StickRestNoise(
        sample_count=512,
        duration_ms=5012.5,
        median_center_x=0.0,
        median_center_y=0.0,
        mean_center_x=0.0,
        mean_center_y=0.0,
        max_abs_x=1.0,
        max_abs_y=1.0,
        max_r=2.0,
        p95_r=1.0,
        p99_r=1.0,
        suggested_deadzone_from_sample=2,
        quality=SampleQuality.GOOD,
        label=label,
    )


def _fixture_range_side(label: RangeCoverageLabel = RangeCoverageLabel.GOOD) -> StickRange:
    sector_count = 32
    sectors = tuple(
        StickSectorCoverage(
            sector_index=i,
            angle_start_rad=(i / sector_count) * 2.0 * math.pi,
            angle_end_rad=((i + 1) / sector_count) * 2.0 * math.pi,
            max_observed_radius=85.0 - (i % 4) * 0.5,
        )
        for i in range(sector_count)
    )
    return StickRange(
        sample_count=820,
        duration_ms=8000.0,
        center_used_x=0.0,
        center_used_y=0.0,
        min_x=-85,
        max_x=85,
        min_y=-84,
        max_y=85,
        cardinal_reach_up=85,
        cardinal_reach_down=84,
        cardinal_reach_left=85,
        cardinal_reach_right=85,
        sectors=sectors,
        sector_coverage_threshold_pct=0.90,
        sector_coverage_pct=0.96875,
        weakest_sector_index=3,
        weakest_sector_pct_of_max=0.91,
        quality=SampleQuality.GOOD,
        label=label,
    )


def _fixture_trigger_side(
    label: TriggerSmoothnessLabel = TriggerSmoothnessLabel.SMOOTH,
) -> TriggerRange:
    return TriggerRange(
        sample_count=160,
        duration_ms=6000.0,
        observed_min=1,
        observed_max=254,
        observed_travel=253,
        monotonicity_violations=0,
        largest_adjacent_delta=12,
        quality=SampleQuality.GOOD,
        label=label,
    )


def _fixture_report(
    *,
    overall: OverallStatus = OverallStatus.NORMAL,
    caveats: tuple[str, ...] = (),
) -> HealthReport:
    return HealthReport(
        app_version="2.0.0",
        app_build_commit="abc1234",
        generated_at_local="2026-05-22T19:55:30-07:00",
        device=DeviceContext(
            controller_name="ZD Ultimate Legend",
            configured_polling_hz=1000,
            profile_name="Apex",
        ),
        stick_rest_noise=StickRestNoiseReport(
            left=_fixture_rest_side(), right=_fixture_rest_side()
        ),
        stick_range=StickRangeReport(
            left=_fixture_range_side(), right=_fixture_range_side()
        ),
        trigger_range=TriggerRangeReport(
            left=_fixture_trigger_side(), right=_fixture_trigger_side()
        ),
        overall_status=overall,
        caveats=caveats,
    )


# ---------------------------------------------------------------------------
# Markdown exporter
# ---------------------------------------------------------------------------


class MarkdownExporterTests(unittest.TestCase):
    def test_claim_boundary_paragraph_appears_verbatim(self) -> None:
        md = to_markdown(_fixture_report())

        self.assertIn(CLAIM_BOUNDARY_PARAGRAPH, md)

    def test_all_eight_sections_present(self) -> None:
        md = to_markdown(_fixture_report(caveats=("Test caveat",)))

        expected_headers = (
            "# Controller Health Report",
            "## What this report measures (and what it does not)",
            "## Device and configuration",
            "## Stick rest noise",
            "## Stick range coverage",
            "## Trigger range",
            "## Retest notes and caveats",
            "## App build identity",
        )
        for header in expected_headers:
            with self.subTest(header=header):
                self.assertIn(header, md)

    def test_observed_cadence_section_absent(self) -> None:
        # The observed-cadence section was removed (2026-05-30); make sure it
        # doesn't creep back into the Markdown export.
        md = to_markdown(_fixture_report())

        self.assertNotIn("## Observed input report cadence", md)

    def test_overall_status_renders_using_qualitative_label(self) -> None:
        md = to_markdown(_fixture_report(overall=OverallStatus.POSSIBLE_ISSUE))

        self.assertIn("Possible issue observed", md)
        # No 0-100 score per the "avoid 0-100 score" design rule.
        self.assertNotIn("/100", md)

    def test_caveats_default_message_when_none(self) -> None:
        md = to_markdown(_fixture_report(caveats=()))

        self.assertIn("No additional notes for this run.", md)

    def test_caveats_render_as_bullets_when_present(self) -> None:
        md = to_markdown(_fixture_report(
            caveats=("Movement detected during rest, retest suggested.",
                     "Triggers not pulled fully on first attempt."),
        ))

        self.assertIn("- Movement detected during rest, retest suggested.", md)
        self.assertIn("- Triggers not pulled fully on first attempt.", md)

    def test_app_build_identity_includes_version(self) -> None:
        md = to_markdown(_fixture_report())

        self.assertIn("App version: 2.0.0", md)
        self.assertIn("Build commit: abc1234", md)


# ---------------------------------------------------------------------------
# JSON exporter
# ---------------------------------------------------------------------------


class JsonExporterTests(unittest.TestCase):
    def test_top_level_shape_matches_spec(self) -> None:
        data = to_json_dict(_fixture_report())

        expected_keys = {
            "schema_version",
            "app_version",
            "app_build_commit",
            "generated_at_local",
            "claim_boundary",
            "claim_boundary_paragraph",
            "measurement_boundary",
            "overall_status",
            "device",
            "tests",
            "caveats",
        }
        self.assertEqual(set(data), expected_keys)
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        self.assertEqual(data["claim_boundary"], CLAIM_BOUNDARY_ID)
        self.assertEqual(data["claim_boundary_paragraph"], CLAIM_BOUNDARY_PARAGRAPH)
        self.assertEqual(data["measurement_boundary"], MEASUREMENT_BOUNDARY_SUMMARY)

    def test_tests_subtree_has_three_sections(self) -> None:
        data = to_json_dict(_fixture_report())

        self.assertEqual(
            set(data["tests"]),
            {"stick_rest_noise", "stick_range", "trigger_range"},
        )

    def test_device_subtree_has_three_fields(self) -> None:
        data = to_json_dict(_fixture_report())

        self.assertEqual(
            set(data["device"]),
            {"controller_name", "configured_polling_hz", "profile_name"},
        )

    def test_stick_rest_carries_suggested_deadzone(self) -> None:
        data = to_json_dict(_fixture_report())

        left = data["tests"]["stick_rest_noise"]["left"]
        # Integer percent — same unit as the Sticks-tab deadzone slider.
        self.assertEqual(left["suggested_deadzone_from_sample"], 2)
        self.assertIn("p99_r", left)
        self.assertIn("p95_r", left)

    def test_stick_range_carries_sector_array(self) -> None:
        data = to_json_dict(_fixture_report())

        sectors = data["tests"]["stick_range"]["left"]["sectors"]
        self.assertEqual(len(sectors), 32)
        for s in sectors:
            with self.subTest(index=s["sector_index"]):
                self.assertEqual(
                    set(s),
                    {"sector_index", "angle_start_rad", "angle_end_rad",
                     "max_observed_radius"},
                )

    def test_json_string_round_trips_to_dict(self) -> None:
        s = to_json(_fixture_report())
        reparsed = json.loads(s)

        self.assertEqual(reparsed["schema_version"], SCHEMA_VERSION)
        self.assertEqual(reparsed["overall_status"], "normal")

    def test_caveats_serializes_as_list(self) -> None:
        data = to_json_dict(_fixture_report(caveats=("First caveat", "Second caveat")))

        self.assertEqual(data["caveats"], ["First caveat", "Second caveat"])

    def test_overall_status_serializes_enum_value(self) -> None:
        data = to_json_dict(_fixture_report(overall=OverallStatus.RETEST_RECOMMENDED))

        self.assertEqual(data["overall_status"], "retest_recommended")


class ExportPathScrubbingTests(unittest.TestCase):
    """Standalone Health Report exports must not ship the operator's home path.

    ``profile_name`` is freeform operator text and can be an absolute path. The
    rich diagnostic bundle re-scrubs embedded copies, but the standalone saved
    ``.md`` / ``.json`` must be path-clean on their own.
    """

    def _report_with_profile(self, profile_name: str) -> HealthReport:
        base = _fixture_report()
        return dataclasses.replace(
            base,
            device=dataclasses.replace(base.device, profile_name=profile_name),
        )

    def test_markdown_scrubs_profile_name_path(self) -> None:
        md = to_markdown(self._report_with_profile(r"C:\Users\Jane Doe\evil"))
        self.assertNotIn("Jane", md)
        self.assertNotIn("Doe", md)
        self.assertNotIn(r"C:\Users", md)

    def test_json_scrubs_profile_name_path(self) -> None:
        report = self._report_with_profile(r"C:\Users\Jane Doe\evil")
        raw = to_json(report)
        self.assertNotIn("Jane", raw)
        self.assertNotIn("Doe", raw)
        self.assertNotIn("Jane", str(to_json_dict(report)["device"]["profile_name"]))

    def test_normal_profile_name_preserved(self) -> None:
        # A non-path profile name (even with a space) passes through untouched.
        md = to_markdown(self._report_with_profile("Apex Layout"))
        self.assertIn("Apex Layout", md)
        data = to_json_dict(self._report_with_profile("Apex Layout"))
        self.assertEqual(data["device"]["profile_name"], "Apex Layout")


if __name__ == "__main__":
    unittest.main()
