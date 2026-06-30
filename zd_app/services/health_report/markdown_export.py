"""Markdown exporter for the Controller Health Report.

Pure function: ``to_markdown(report) -> str``. The Health Report schema's export-format priority
specified the report sections; the observed-cadence section was removed
(2026-05-30) because app-layer Hz can't see the device's true polling rate.
Section 2 is the verbatim claim-boundary paragraph from ``boundary.py``, and
the forbidden-phrase test asserts that paragraph appears unchanged in every
Markdown export.

Layout choices:

- ASCII-only table dividers (``|`` and ``-``). Markdown viewers (Notepad,
  Discord, GitHub, Reddit) all render this consistently.
- Numeric formatting is compact (no excessive zeroes); millisecond values
  show 3 decimal places so 8 kHz expected intervals (0.125 ms) read cleanly.
- Section 9 (app/version/build) goes last so the file's intro is the report
  content, not metadata.
"""

from __future__ import annotations

from zd_app.services.health_report.boundary import CLAIM_BOUNDARY_PARAGRAPH
from zd_app.services.markdown_safety import escape_markdown
from zd_app.services.path_scrub import scrub_value
from zd_app.services.health_report.models import (
    HealthReport,
    OverallStatus,
    RangeCoverageLabel,
    RestNoiseLabel,
    StickRange,
    StickRestNoise,
    StickRestNoiseReport,
    StickRangeReport,
    TriggerRange,
    TriggerRangeReport,
    TriggerSmoothnessLabel,
)


# Honest, non-judgmental human labels. Per the "Overall report labels" rule:
# avoid 0-100 score; use qualitative bands.
_OVERALL_STATUS_LABEL = {
    OverallStatus.NORMAL: "No obvious issues observed",
    OverallStatus.TUNING_SUGGESTED: "Tuning suggested",
    OverallStatus.RETEST_RECOMMENDED: "Retest recommended",
    OverallStatus.POSSIBLE_ISSUE: "Possible issue observed",
}

_REST_NOISE_LABEL = {
    RestNoiseLabel.GOOD: "Good",
    RestNoiseLabel.NOTICEABLE: "Noticeable",
    RestNoiseLabel.HIGH: "High - retest recommended",
    RestNoiseLabel.INSUFFICIENT: "Insufficient sample",
}

_RANGE_LABEL = {
    RangeCoverageLabel.GOOD: "Good",
    RangeCoverageLabel.LIMITED: "Limited",
    RangeCoverageLabel.UNEVEN: "Uneven",
    RangeCoverageLabel.RETEST: "Retest recommended",
    RangeCoverageLabel.INSUFFICIENT: "Insufficient sample",
}

_TRIGGER_LABEL = {
    TriggerSmoothnessLabel.SMOOTH: "Smooth",
    TriggerSmoothnessLabel.STEPPED: "Stepped",
    TriggerSmoothnessLabel.NOISY: "Noisy",
    TriggerSmoothnessLabel.LIMITED: "Limited range - retest recommended",
    TriggerSmoothnessLabel.INSUFFICIENT: "Insufficient sample",
}


def to_markdown(report: HealthReport) -> str:
    """Render a Health Report as Markdown.

    The output begins with the report title and overall status, then the
    verbatim claim-boundary paragraph from ``boundary.py``. Every measurement table
    includes the sample count and duration so the reader can see how much
    data backs the numbers.
    """

    sections: list[str] = []
    sections.append(_section_summary(report))
    sections.append(_section_claim_boundary())
    sections.append(_section_device(report))
    sections.append(_section_stick_rest(report.stick_rest_noise))
    sections.append(_section_stick_range(report.stick_range))
    sections.append(_section_trigger_range(report.trigger_range))
    sections.append(_section_retest_notes(report.caveats))
    sections.append(_section_build_identity(report))
    return "\n\n".join(sections).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _section_summary(report: HealthReport) -> str:
    lines = [
        "# Controller Health Report",
        "",
        f"**Overall status:** {_OVERALL_STATUS_LABEL[report.overall_status]}",
        f"**Generated at:** {report.generated_at_local}",
    ]
    return "\n".join(lines)


def _section_claim_boundary() -> str:
    # Section header + the verbatim paragraph. Tests assert the paragraph
    # appears unchanged in the output.
    return "## What this report measures (and what it does not)\n\n" + CLAIM_BOUNDARY_PARAGRAPH


def _section_device(report: HealthReport) -> str:
    device = report.device
    # controller_name / profile_name are freeform — profile_name in particular
    # is operator text that can be an absolute path. Scrub, then escape Markdown
    # table syntax before the standalone export (also copied to clipboard and
    # embedded in the diagnostic bundle). scrub_value returns "" for None, so the
    # fallbacks hold.
    controller = escape_markdown(scrub_value(device.controller_name)) or "(unknown)"
    profile = escape_markdown(scrub_value(device.profile_name)) or "(none)"
    polling = (
        f"{device.configured_polling_hz} Hz" if device.configured_polling_hz else "(unknown)"
    )
    lines = [
        "## Device and configuration",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Controller | {controller} |",
        f"| Configured polling rate | {polling} |",
        f"| Active profile | {profile} |",
    ]
    return "\n".join(lines)


def _section_stick_rest(report: StickRestNoiseReport) -> str:
    lines = [
        "## Stick rest noise",
        "",
        "Observed while the sticks were untouched. The suggested deadzone is a "
        "practical starting point based on this sample, not a precise hardware "
        "specification.",
        "",
        "| Stick | Label | Samples | Duration (ms) | p95 r | p99 r | Max r | "
        "Suggested deadzone | Median center (X, Y) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        _stick_rest_row("Left", report.left),
        _stick_rest_row("Right", report.right),
    ]
    return "\n".join(lines)


def _stick_rest_row(label: str, rest: StickRestNoise) -> str:
    return (
        f"| {label} | {_REST_NOISE_LABEL[rest.label]} | {rest.sample_count} | "
        f"{rest.duration_ms:.1f} | {rest.p95_r:.1f} | {rest.p99_r:.1f} | "
        f"{rest.max_r:.1f} | {rest.suggested_deadzone_from_sample} | "
        f"({rest.median_center_x:.1f}, {rest.median_center_y:.1f}) |"
    )


def _section_stick_range(report: StickRangeReport) -> str:
    lines = [
        "## Stick range coverage",
        "",
        "Observed during the manual rotation step. Sector coverage depends on "
        "how fully the stick was rotated; this is not a precision mechanical "
        "measurement.",
        "",
        "| Stick | Label | Samples | Sector coverage | Weakest sector "
        "(% of max) | Min X | Max X | Min Y | Max Y |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        _stick_range_row("Left", report.left),
        _stick_range_row("Right", report.right),
    ]
    return "\n".join(lines)


def _stick_range_row(label: str, side: StickRange) -> str:
    coverage_pct_str = f"{side.sector_coverage_pct * 100:.1f}%"
    if side.weakest_sector_index is not None and side.weakest_sector_pct_of_max is not None:
        weakest_str = (
            f"#{side.weakest_sector_index} ({side.weakest_sector_pct_of_max * 100:.0f}%)"
        )
    else:
        weakest_str = "-"
    return (
        f"| {label} | {_RANGE_LABEL[side.label]} | {side.sample_count} | "
        f"{coverage_pct_str} | {weakest_str} | "
        f"{side.min_x} | {side.max_x} | {side.min_y} | {side.max_y} |"
    )


def _section_trigger_range(report: TriggerRangeReport) -> str:
    lines = [
        "## Trigger range",
        "",
        "Observed during the manual pull/release step. A limited range or "
        "jumpy trace can indicate configuration, mechanical behavior, "
        "firmware filtering, or simply an incomplete pull. Retest before "
        "treating a single result as a hardware fault.",
        "",
        "| Trigger | Label | Samples | Observed min | Observed max | "
        "Travel | Largest adjacent delta | Monotonicity violations |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        _trigger_row("Left", report.left),
        _trigger_row("Right", report.right),
    ]
    return "\n".join(lines)


def _trigger_row(label: str, trig: TriggerRange) -> str:
    return (
        f"| {label} | {_TRIGGER_LABEL[trig.label]} | {trig.sample_count} | "
        f"{trig.observed_min} | {trig.observed_max} | {trig.observed_travel} | "
        f"{trig.largest_adjacent_delta} | {trig.monotonicity_violations} |"
    )


def _section_retest_notes(caveats: tuple[str, ...]) -> str:
    if caveats:
        body = "\n".join(f"- {note}" for note in caveats)
    else:
        body = "- No additional notes for this run."
    return "## Retest notes and caveats\n\n" + body


def _section_build_identity(report: HealthReport) -> str:
    commit = report.app_build_commit or "(dev)"
    lines = [
        "## App build identity",
        "",
        f"- App version: {report.app_version}",
        f"- Build commit: {commit}",
    ]
    return "\n".join(lines)


__all__ = ["to_markdown"]
