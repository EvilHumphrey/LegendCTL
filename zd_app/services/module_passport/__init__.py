"""Module Passport — per-side stick-module characterization + lifecycle log.

A controller has two passports (left + right). Each
identifies the installed module by an operator-assigned freeform
``module_id`` and carries an append-only history of 60-second
:class:`zd_app.storage.module_passport_models.ModuleFingerprint`
characterization runs. Swap a side and the previous passport is archived
verbatim. Events flow through the existing wear ledger so the lifecycle
shows up in the chronological log alongside profile applies and restore
points.
"""

from __future__ import annotations

from zd_app.services.module_passport.characterize import (
    CHARACTERIZATION_TOTAL_S,
    CharacterizationOrchestrator,
    CharacterizationPhase,
    CharacterizationProgress,
    CharacterizationState,
    PHASE_DURATIONS_S,
    classify_fingerprint,
    compute_asymmetry_score,
    compute_bitness_observed,
    compute_linearity_score,
    compute_outer_deadzone_band,
    compute_tremor_metric,
)
from zd_app.services.module_passport.service import (
    ARCHIVE_DIRNAME,
    ModulePassportService,
)
from zd_app.services.module_passport.trend_analysis import (
    ATTENTION_AGE_DAYS,
    CONFIDENCE_HIGH,
    CONFIDENCE_INSUFFICIENT,
    CONFIDENCE_LOW,
    CONFIDENCE_MODERATE,
    INVESTIGATE_DAYS_TO_THRESHOLD,
    METRIC_ORDER,
    MetricTrend,
    PassportTrendSummary,
    TREND_STATUS_DRIFTING,
    TREND_STATUS_INSUFFICIENT,
    TREND_STATUS_INVESTIGATE,
    TREND_STATUS_STABLE,
    compute_metric_trend,
    summarize_passport_trends,
)


__all__ = [
    "ARCHIVE_DIRNAME",
    "ATTENTION_AGE_DAYS",
    "CHARACTERIZATION_TOTAL_S",
    "CONFIDENCE_HIGH",
    "CONFIDENCE_INSUFFICIENT",
    "CONFIDENCE_LOW",
    "CONFIDENCE_MODERATE",
    "CharacterizationOrchestrator",
    "CharacterizationPhase",
    "CharacterizationProgress",
    "CharacterizationState",
    "INVESTIGATE_DAYS_TO_THRESHOLD",
    "METRIC_ORDER",
    "MetricTrend",
    "ModulePassportService",
    "PHASE_DURATIONS_S",
    "PassportTrendSummary",
    "TREND_STATUS_DRIFTING",
    "TREND_STATUS_INSUFFICIENT",
    "TREND_STATUS_INVESTIGATE",
    "TREND_STATUS_STABLE",
    "classify_fingerprint",
    "compute_asymmetry_score",
    "compute_bitness_observed",
    "compute_linearity_score",
    "compute_metric_trend",
    "compute_outer_deadzone_band",
    "compute_tremor_metric",
    "summarize_passport_trends",
]
