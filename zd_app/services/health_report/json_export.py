"""JSON exporter for the Controller Health Report.

Pure function: ``to_json(report) -> str`` (and :func:`to_json_dict` for tests
that want to assert structure without re-parsing). Schema shape follows
the Health Report schema's recommended top-level shape and suggested field names —
all field names use ``observed_*`` / ``configured_*`` / ``suggested_*``;
never ``true_*`` / ``latency_*`` / ``defect_*`` / ``calibrated_*`` /
``hardware_health_*``. The forbidden-field-name test scans the produced dict
recursively to enforce this.

``schema_version`` starts at 1; bump it whenever a field is renamed or
removed (additive changes don't require a bump). Old JSON files are not
re-read by the app, so backwards compatibility is for outside consumers.
Bumped to 2 when the ``observed_report_cadence`` test section was removed
(app-layer Hz can't see the device's true polling rate).
"""

from __future__ import annotations

import json
from typing import Any

from zd_app.services.health_report.boundary import (
    CLAIM_BOUNDARY_ID,
    CLAIM_BOUNDARY_PARAGRAPH,
    MEASUREMENT_BOUNDARY_SUMMARY,
)
from zd_app.services.health_report.models import (
    HealthReport,
    StickRange,
    StickRangeReport,
    StickRestNoise,
    StickRestNoiseReport,
    TriggerRange,
    TriggerRangeReport,
)
from zd_app.services.path_scrub import scrub_value


SCHEMA_VERSION = 2


def to_json(report: HealthReport, *, indent: int | None = 2) -> str:
    """Serialize the report to a JSON string."""

    return json.dumps(to_json_dict(report), indent=indent, ensure_ascii=False)


def to_json_dict(report: HealthReport) -> dict[str, Any]:
    """Return the dict shape used by :func:`to_json`. Useful for tests."""

    return {
        "schema_version": SCHEMA_VERSION,
        "app_version": report.app_version,
        "app_build_commit": report.app_build_commit,
        "generated_at_local": report.generated_at_local,
        "claim_boundary": CLAIM_BOUNDARY_ID,
        "claim_boundary_paragraph": CLAIM_BOUNDARY_PARAGRAPH,
        "measurement_boundary": MEASUREMENT_BOUNDARY_SUMMARY,
        "overall_status": report.overall_status.value,
        "device": {
            # profile_name is operator-freeform and can be an absolute path;
            # controller_name is device-supplied. Scrub both so a home path
            # never ships in the standalone JSON export. ``... or original``
            # preserves None / "" exactly (scrub_value coerces falsy to "").
            "controller_name": scrub_value(report.device.controller_name)
            or report.device.controller_name,
            "configured_polling_hz": report.device.configured_polling_hz,
            "profile_name": scrub_value(report.device.profile_name)
            or report.device.profile_name,
        },
        "tests": {
            "stick_rest_noise": _stick_rest_dict(report.stick_rest_noise),
            "stick_range": _stick_range_dict(report.stick_range),
            "trigger_range": _trigger_range_dict(report.trigger_range),
        },
        "caveats": list(report.caveats),
    }


def _stick_rest_dict(report: StickRestNoiseReport) -> dict[str, Any]:
    return {
        "left": _stick_rest_side_dict(report.left),
        "right": _stick_rest_side_dict(report.right),
    }


def _stick_rest_side_dict(rest: StickRestNoise) -> dict[str, Any]:
    return {
        "sample_count": rest.sample_count,
        "duration_ms": rest.duration_ms,
        "median_center_x": rest.median_center_x,
        "median_center_y": rest.median_center_y,
        "mean_center_x": rest.mean_center_x,
        "mean_center_y": rest.mean_center_y,
        "max_abs_x": rest.max_abs_x,
        "max_abs_y": rest.max_abs_y,
        "max_r": rest.max_r,
        "p95_r": rest.p95_r,
        "p99_r": rest.p99_r,
        "suggested_deadzone_from_sample": rest.suggested_deadzone_from_sample,
        "quality": rest.quality.value,
        "label": rest.label.value,
    }


def _stick_range_dict(report: StickRangeReport) -> dict[str, Any]:
    return {
        "left": _stick_range_side_dict(report.left),
        "right": _stick_range_side_dict(report.right),
    }


def _stick_range_side_dict(side: StickRange) -> dict[str, Any]:
    return {
        "sample_count": side.sample_count,
        "duration_ms": side.duration_ms,
        "center_used_x": side.center_used_x,
        "center_used_y": side.center_used_y,
        "min_x": side.min_x,
        "max_x": side.max_x,
        "min_y": side.min_y,
        "max_y": side.max_y,
        "cardinal_reach_up": side.cardinal_reach_up,
        "cardinal_reach_down": side.cardinal_reach_down,
        "cardinal_reach_left": side.cardinal_reach_left,
        "cardinal_reach_right": side.cardinal_reach_right,
        "sector_coverage_threshold_pct": side.sector_coverage_threshold_pct,
        "sector_coverage_pct": side.sector_coverage_pct,
        "weakest_sector_index": side.weakest_sector_index,
        "weakest_sector_pct_of_max": side.weakest_sector_pct_of_max,
        "sectors": [
            {
                "sector_index": sector.sector_index,
                "angle_start_rad": sector.angle_start_rad,
                "angle_end_rad": sector.angle_end_rad,
                "max_observed_radius": sector.max_observed_radius,
            }
            for sector in side.sectors
        ],
        "quality": side.quality.value,
        "label": side.label.value,
    }


def _trigger_range_dict(report: TriggerRangeReport) -> dict[str, Any]:
    return {
        "left": _trigger_side_dict(report.left),
        "right": _trigger_side_dict(report.right),
    }


def _trigger_side_dict(trig: TriggerRange) -> dict[str, Any]:
    return {
        "sample_count": trig.sample_count,
        "duration_ms": trig.duration_ms,
        "observed_min": trig.observed_min,
        "observed_max": trig.observed_max,
        "observed_travel": trig.observed_travel,
        "monotonicity_violations": trig.monotonicity_violations,
        "largest_adjacent_delta": trig.largest_adjacent_delta,
        "quality": trig.quality.value,
        "label": trig.label.value,
    }


__all__ = ["SCHEMA_VERSION", "to_json", "to_json_dict"]
