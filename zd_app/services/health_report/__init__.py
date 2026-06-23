"""Controller Health Report — local, app-layer, honest measurement.

Local, app-layer controller health measurement: stick rest noise, range
coverage, and trigger travel derived from HID input reports during a
guided local test.

Public surface:

- :class:`Sample` — one observation of XInput state at a moment in time.
- :class:`HealthReport` + sub-dataclasses — the final report shape.
- :class:`HealthReportService` — orchestrator for the 3-step wizard.
- :func:`to_markdown` / :func:`to_json` — exporters.
- :data:`CLAIM_BOUNDARY_PARAGRAPH` — single source of truth for the honesty
  boundary paragraph that appears verbatim in every export.
"""

from __future__ import annotations

from zd_app.services.health_report.boundary import (
    CLAIM_BOUNDARY_ID,
    CLAIM_BOUNDARY_PARAGRAPH,
    MEASUREMENT_BOUNDARY_SUMMARY,
    SHORT_REVIEWER_QUOTE,
    UI_FOOTER_LINE,
)
from zd_app.services.health_report.json_export import (
    SCHEMA_VERSION,
    to_json,
    to_json_dict,
)
from zd_app.services.health_report.markdown_export import to_markdown
from zd_app.services.health_report.quick_check import (
    PHASE_DURATIONS_S,
    PHASE_ORDER,
    QUICK_TOTAL_BUDGET_REGRESSION_S,
    QUICK_TOTAL_BUDGET_S,
    QuickCheckPhase,
    QuickCheckProgress,
    QuickCheckService,
    QuickCheckState,
    RANGE_DURATION_S as QUICK_RANGE_DURATION_S,
    ReadinessStatus,
    ReadinessVerdict,
    REST_DURATION_S as QUICK_REST_DURATION_S,
    TRIGGER_DURATION_S as QUICK_TRIGGER_DURATION_S,
    classify_verdict,
)
from zd_app.services.health_report.sample_capture import (
    SampleCollector,
    SampleProvider,
    make_xinput_sample_provider,
)
from zd_app.services.health_report.service import (
    REST_DURATION_S,
    ROTATION_DURATION_S,
    TRIGGER_DURATION_S,
    HealthReportService,
    HealthReportState,
    StepProgress,
)
from zd_app.services.health_report.measurements import (
    compute_observed_cadence,
    compute_overall_status,
    compute_stick_range,
    compute_stick_rest_noise,
    compute_trigger_range,
    filter_samples_by_time_range,
)
from zd_app.services.health_report.models import (
    CadenceQualityLabel,
    DeviceContext,
    HealthReport,
    ObservedCadence,
    OverallStatus,
    RangeCoverageLabel,
    RestNoiseLabel,
    Sample,
    SampleQuality,
    StickRange,
    StickRangeReport,
    StickRestNoise,
    StickRestNoiseReport,
    StickSectorCoverage,
    TriggerRange,
    TriggerRangeReport,
    TriggerSmoothnessLabel,
)


__all__ = [
    "CLAIM_BOUNDARY_ID",
    "CLAIM_BOUNDARY_PARAGRAPH",
    "MEASUREMENT_BOUNDARY_SUMMARY",
    "PHASE_DURATIONS_S",
    "PHASE_ORDER",
    "QUICK_RANGE_DURATION_S",
    "QUICK_REST_DURATION_S",
    "QUICK_TOTAL_BUDGET_REGRESSION_S",
    "QUICK_TOTAL_BUDGET_S",
    "QUICK_TRIGGER_DURATION_S",
    "REST_DURATION_S",
    "ROTATION_DURATION_S",
    "SCHEMA_VERSION",
    "SHORT_REVIEWER_QUOTE",
    "TRIGGER_DURATION_S",
    "UI_FOOTER_LINE",
    "CadenceQualityLabel",
    "DeviceContext",
    "HealthReport",
    "HealthReportService",
    "HealthReportState",
    "ObservedCadence",
    "OverallStatus",
    "QuickCheckPhase",
    "QuickCheckProgress",
    "QuickCheckService",
    "QuickCheckState",
    "RangeCoverageLabel",
    "ReadinessStatus",
    "ReadinessVerdict",
    "RestNoiseLabel",
    "Sample",
    "SampleCollector",
    "SampleProvider",
    "SampleQuality",
    "StepProgress",
    "StickRange",
    "StickRangeReport",
    "StickRestNoise",
    "StickRestNoiseReport",
    "StickSectorCoverage",
    "TriggerRange",
    "TriggerRangeReport",
    "TriggerSmoothnessLabel",
    "classify_verdict",
    "compute_observed_cadence",
    "compute_overall_status",
    "compute_stick_range",
    "compute_stick_rest_noise",
    "compute_trigger_range",
    "filter_samples_by_time_range",
    "make_xinput_sample_provider",
    "to_json",
    "to_json_dict",
    "to_markdown",
]
