"""Tests for the Readiness Check (quick-mode) service.

Covers four buckets the spec calls out:

1. Verdict logic — synthetic samples for each green / yellow / red scenario,
   asserting the returned ReadinessVerdict status + observation keys.
2. Phase ordering — REST -> RANGE -> TRIGGER walks through state machine.
3. 20s budget — total_budget_s + the regression ceiling.
4. Threshold reuse — the quick-mode constants point at the same
   ``measurements.py`` thresholds as the full Health Report.
5. Forbidden-phrase scan — readiness_check i18n keys must follow the same
   honesty discipline as health_report.* keys.
"""

from __future__ import annotations

import json
import math
import time
import unittest
from itertools import count
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from zd_app.services.health_report import (
    DeviceContext,
    PHASE_DURATIONS_S,
    PHASE_ORDER,
    QUICK_RANGE_DURATION_S,
    QUICK_REST_DURATION_S,
    QUICK_TOTAL_BUDGET_REGRESSION_S,
    QUICK_TOTAL_BUDGET_S,
    QUICK_TRIGGER_DURATION_S,
    QuickCheckPhase,
    QuickCheckService,
    QuickCheckState,
    ReadinessStatus,
    Sample,
    classify_verdict,
)
from zd_app.services.health_report.measurements import (
    REST_NOISE_GOOD_P99_PCT,
    REST_NOISE_NOTICEABLE_P99_PCT,
    STICK_RANGE_GOOD_PCT,
    STICK_RANGE_LIMITED_PCT,
)
from zd_app.services.health_report.quick_check import (
    OBS_CADENCE_CONSISTENT,
    OBS_CADENCE_INCONSISTENT,
    OBS_INSUFFICIENT_SAMPLES,
    OBS_RANGE_GOOD,
    OBS_RANGE_LIMITED,
    OBS_RANGE_RETEST,
    OBS_REST_CLEAN,
    OBS_REST_HIGH,
    OBS_REST_NOTICEABLE,
    OBS_TRIGGER_LIMITED,
    OBS_TRIGGER_NOISY,
    OBS_TRIGGER_SMOOTH,
    OBS_TRIGGER_STEPPED,
)


# ---------------------------------------------------------------------------
# Helpers (mirror test_health_report_service.py)
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt_s: float) -> None:
        self._t += dt_s


class _ScriptedProvider:
    def __init__(self, samples: list[Sample]) -> None:
        self._samples = list(samples)
        self._idx = 0

    def __call__(self) -> Optional[Sample]:
        if self._idx >= len(self._samples):
            return None
        s = self._samples[self._idx]
        self._idx += 1
        return s


def _device_provider(*, hz: int = 1000) -> DeviceContext:
    return DeviceContext(
        controller_name="ZD Ultimate Legend",
        configured_polling_hz=hz,
        profile_name=None,
    )


# Sample factories — synthetic streams sized to the spec's MIN_SAMPLES_FOR_*
# thresholds so each phase has plenty of samples to score.


def _clean_rest_samples(*, n: int = 600, base_ts_ns: int = 0) -> list[Sample]:
    """Very low rest noise: tiny jitter around (0, 0) on both sticks."""

    samples: list[Sample] = []
    for i in range(n):
        samples.append(Sample(
            timestamp_ns=base_ts_ns + i * 1_000_000,
            packet_number=i,
            left_stick_x=(i % 3) - 1,    # -1, 0, 1 cycle
            left_stick_y=(i % 3) - 1,
            right_stick_x=(i % 3) - 1,
            right_stick_y=(i % 3) - 1,
            left_trigger=0, right_trigger=0,
        ))
    return samples


def _noticeable_rest_samples(*, n: int = 600, base_ts_ns: int = 0) -> list[Sample]:
    """Rest noise above GOOD but below HIGH band."""

    # NOTICEABLE_P99_PCT is 6, GOOD is 2 — aim for p99 around 4.
    samples: list[Sample] = []
    for i in range(n):
        # Roughly uniform within radius ~4 to push p99 above 2.
        offset = (i % 9) - 4    # range [-4, 4]
        samples.append(Sample(
            timestamp_ns=base_ts_ns + i * 1_000_000,
            packet_number=i,
            left_stick_x=offset, left_stick_y=offset,
            right_stick_x=offset, right_stick_y=offset,
            left_trigger=0, right_trigger=0,
        ))
    return samples


def _high_rest_samples(*, n: int = 600, base_ts_ns: int = 0) -> list[Sample]:
    """Rest noise well above NOTICEABLE band (simulated drift)."""

    samples: list[Sample] = []
    for i in range(n):
        # Cycle through values that produce p99 around 15.
        offset = (i % 25) - 12    # range [-12, 12]
        samples.append(Sample(
            timestamp_ns=base_ts_ns + i * 1_000_000,
            packet_number=i,
            left_stick_x=offset, left_stick_y=offset,
            right_stick_x=offset, right_stick_y=offset,
            left_trigger=0, right_trigger=0,
        ))
    return samples


def _full_rotation_range_samples(
    *, n: int = 640, radius_pct: int = 85, base_ts_ns: int = 700_000_000
) -> list[Sample]:
    """Full circular rotation of both sticks at the given radius."""

    samples: list[Sample] = []
    for i in range(n):
        theta = (i / n) * 4.0 * math.pi    # ~2 laps
        samples.append(Sample(
            timestamp_ns=base_ts_ns + i * 1_000_000,
            packet_number=600 + i,
            left_stick_x=int(radius_pct * math.cos(theta)),
            left_stick_y=int(radius_pct * math.sin(theta)),
            right_stick_x=int(radius_pct * math.cos(theta)),
            right_stick_y=int(radius_pct * math.sin(theta)),
            left_trigger=0, right_trigger=0,
        ))
    return samples


def _partial_rotation_range_samples(
    *, n: int = 640, base_ts_ns: int = 700_000_000
) -> list[Sample]:
    """Only one quadrant covered — produces uneven / retest range coverage."""

    samples: list[Sample] = []
    for i in range(n):
        # Only quadrant 0 (theta in [0, pi/2]).
        theta = (i / n) * (math.pi / 2.0)
        samples.append(Sample(
            timestamp_ns=base_ts_ns + i * 1_000_000,
            packet_number=600 + i,
            left_stick_x=int(85 * math.cos(theta)),
            left_stick_y=int(85 * math.sin(theta)),
            right_stick_x=int(85 * math.cos(theta)),
            right_stick_y=int(85 * math.sin(theta)),
            left_trigger=0, right_trigger=0,
        ))
    return samples


def _smooth_trigger_samples(
    *, n: int = 240, base_ts_ns: int = 1_400_000_000
) -> list[Sample]:
    """Smooth full-range pull/release on both triggers."""

    samples: list[Sample] = []
    half = n // 2
    for i in range(n):
        if i < half:
            v = int(255 * (i / half))
        else:
            v = int(255 * (1 - (i - half) / half))
        samples.append(Sample(
            timestamp_ns=base_ts_ns + i * 1_000_000,
            packet_number=1240 + i,
            left_stick_x=0, left_stick_y=0,
            right_stick_x=0, right_stick_y=0,
            left_trigger=v, right_trigger=v,
        ))
    return samples


def _stepped_trigger_samples(
    *, n: int = 240, base_ts_ns: int = 1_400_000_000
) -> list[Sample]:
    """Stepped trigger — large jumps trip the STEPPED band.

    Goes 0 -> 60 -> 120 -> 180 -> 255 -> 180 -> 120 -> 60 -> 0 so observed
    min/max satisfies the near-full check (<=5 and >=250) AND ``large_delta``
    transitions (gaps of 60-75 at each step boundary) exceed
    ``TRIGGER_STEPPED_DELTA_COUNT`` (=3).
    """

    samples: list[Sample] = []
    levels_up = [0, 60, 120, 180, 255]
    levels_down = list(reversed(levels_up))
    samples_per_step = n // (len(levels_up) + len(levels_down) - 1)
    seq: list[int] = []
    for lvl in levels_up + levels_down[1:]:
        seq.extend([lvl] * samples_per_step)
    while len(seq) < n:
        seq.append(seq[-1])
    seq = seq[:n]
    for i, v in enumerate(seq):
        samples.append(Sample(
            timestamp_ns=base_ts_ns + i * 1_000_000,
            packet_number=1240 + i,
            left_stick_x=0, left_stick_y=0,
            right_stick_x=0, right_stick_y=0,
            left_trigger=v, right_trigger=v,
        ))
    return samples


def _limited_trigger_samples(
    *, n: int = 240, base_ts_ns: int = 1_400_000_000
) -> list[Sample]:
    """Trigger that never reaches full pull — falls under LIMITED."""

    samples: list[Sample] = []
    half = n // 2
    for i in range(n):
        if i < half:
            v = int(150 * (i / half))    # only reaches ~150, not 250+
        else:
            v = int(150 * (1 - (i - half) / half))
        samples.append(Sample(
            timestamp_ns=base_ts_ns + i * 1_000_000,
            packet_number=1240 + i,
            left_stick_x=0, left_stick_y=0,
            right_stick_x=0, right_stick_y=0,
            left_trigger=v, right_trigger=v,
        ))
    return samples


def _noisy_trigger_samples(
    *, n: int = 240, base_ts_ns: int = 1_400_000_000
) -> list[Sample]:
    """Noisy trigger — many small monotonicity violations, but no single
    delta large enough to trip STEPPED (which is checked first).

    The smoothness classifier checks STEPPED before NOISY, so we need:
    - adjacent deltas < ``TRIGGER_LARGE_DELTA_THRESHOLD`` (=25), AND
    - many press-phase reversals > ``TRIGGER_NOISE_TOLERANCE_RAW`` (=2) to
      satisfy ``monotonicity_violations > n // 5``, AND
    - observed_min <= 5 and observed_max >= 250 so the LIMITED early-out
      doesn't fire.
    """

    samples: list[Sample] = []
    half = n // 2
    for i in range(n):
        if i < half:
            base = int(255 * (i / half))
        else:
            base = int(255 * (1 - (i - half) / half))
        # Small ±5 oscillation: delta between samples stays under 25, but
        # every odd step on press is a -5 reversal that the smoothness
        # check (tolerance=2) counts as a monotonicity violation.
        wobble = -5 if i % 2 == 1 else 5
        v = max(0, min(255, base + wobble))
        samples.append(Sample(
            timestamp_ns=base_ts_ns + i * 1_000_000,
            packet_number=1240 + i,
            left_stick_x=0, left_stick_y=0,
            right_stick_x=0, right_stick_y=0,
            left_trigger=v, right_trigger=v,
        ))
    return samples


# ---------------------------------------------------------------------------
# 1. Verdict logic — green / yellow / red scenarios
# ---------------------------------------------------------------------------


class ReadinessVerdictGreenTests(unittest.TestCase):
    def test_all_clean_phases_yields_green(self) -> None:
        rest = _clean_rest_samples()
        rng = _full_rotation_range_samples()
        trig = _smooth_trigger_samples()

        verdict = classify_verdict(
            rest_samples=rest,
            range_samples=rng,
            trigger_samples=trig,
            configured_polling_hz=1000,
        )

        self.assertEqual(verdict.status, ReadinessStatus.GREEN)
        self.assertIn(OBS_REST_CLEAN, verdict.observations)
        self.assertIn(OBS_RANGE_GOOD, verdict.observations)
        self.assertIn(OBS_TRIGGER_SMOOTH, verdict.observations)

    def test_green_verdict_never_has_red_observations(self) -> None:
        rest = _clean_rest_samples()
        rng = _full_rotation_range_samples()
        trig = _smooth_trigger_samples()

        verdict = classify_verdict(
            rest_samples=rest,
            range_samples=rng,
            trigger_samples=trig,
            configured_polling_hz=1000,
        )

        red_obs = {OBS_REST_HIGH, OBS_RANGE_RETEST, OBS_TRIGGER_NOISY,
                   OBS_CADENCE_INCONSISTENT, OBS_INSUFFICIENT_SAMPLES}
        self.assertEqual(
            set(verdict.observations) & red_obs, set(),
            f"Green verdict has red observations: {verdict.observations}",
        )


class ReadinessVerdictYellowTests(unittest.TestCase):
    def test_noticeable_rest_yields_yellow(self) -> None:
        verdict = classify_verdict(
            rest_samples=_noticeable_rest_samples(),
            range_samples=_full_rotation_range_samples(),
            trigger_samples=_smooth_trigger_samples(),
            configured_polling_hz=1000,
        )

        self.assertEqual(verdict.status, ReadinessStatus.YELLOW)
        self.assertIn(OBS_REST_NOTICEABLE, verdict.observations)

    def test_limited_trigger_yields_yellow(self) -> None:
        verdict = classify_verdict(
            rest_samples=_clean_rest_samples(),
            range_samples=_full_rotation_range_samples(),
            trigger_samples=_limited_trigger_samples(),
            configured_polling_hz=1000,
        )

        self.assertEqual(verdict.status, ReadinessStatus.YELLOW)
        self.assertIn(OBS_TRIGGER_LIMITED, verdict.observations)

    def test_stepped_trigger_yields_yellow(self) -> None:
        verdict = classify_verdict(
            rest_samples=_clean_rest_samples(),
            range_samples=_full_rotation_range_samples(),
            trigger_samples=_stepped_trigger_samples(),
            configured_polling_hz=1000,
        )

        self.assertEqual(verdict.status, ReadinessStatus.YELLOW)
        self.assertIn(OBS_TRIGGER_STEPPED, verdict.observations)


class ReadinessVerdictRedTests(unittest.TestCase):
    def test_high_rest_noise_yields_red(self) -> None:
        verdict = classify_verdict(
            rest_samples=_high_rest_samples(),
            range_samples=_full_rotation_range_samples(),
            trigger_samples=_smooth_trigger_samples(),
            configured_polling_hz=1000,
        )

        self.assertEqual(verdict.status, ReadinessStatus.RED)
        self.assertIn(OBS_REST_HIGH, verdict.observations)

    def test_partial_rotation_yields_red(self) -> None:
        verdict = classify_verdict(
            rest_samples=_clean_rest_samples(),
            range_samples=_partial_rotation_range_samples(),
            trigger_samples=_smooth_trigger_samples(),
            configured_polling_hz=1000,
        )

        # Partial coverage produces RETEST or UNEVEN -> RED.
        self.assertEqual(verdict.status, ReadinessStatus.RED)
        self.assertIn(OBS_RANGE_RETEST, verdict.observations)

    def test_noisy_trigger_yields_red(self) -> None:
        verdict = classify_verdict(
            rest_samples=_clean_rest_samples(),
            range_samples=_full_rotation_range_samples(),
            trigger_samples=_noisy_trigger_samples(),
            configured_polling_hz=1000,
        )

        self.assertEqual(verdict.status, ReadinessStatus.RED)
        self.assertIn(OBS_TRIGGER_NOISY, verdict.observations)

    def test_insufficient_samples_yields_red(self) -> None:
        verdict = classify_verdict(
            rest_samples=[],
            range_samples=[],
            trigger_samples=[],
            configured_polling_hz=1000,
        )

        self.assertEqual(verdict.status, ReadinessStatus.RED)
        self.assertIn(OBS_INSUFFICIENT_SAMPLES, verdict.observations)


class ReadinessVerdictObservationCountTests(unittest.TestCase):
    """The screen has limited room for bullets; verdict logic should never
    emit a runaway list, even when every phase has issues."""

    def test_observation_count_stays_within_screen_budget(self) -> None:
        verdict = classify_verdict(
            rest_samples=_high_rest_samples(),
            range_samples=_partial_rotation_range_samples(),
            trigger_samples=_noisy_trigger_samples(),
            configured_polling_hz=1000,
        )

        # 1 per axis (rest/range/trigger) + optional cadence ≤ 4 (or 5 if
        # insufficient samples dedupe doesn't kick in). Hard ceiling at 5.
        self.assertLessEqual(len(verdict.observations), 5)

    def test_green_run_emits_no_more_than_4_observations(self) -> None:
        # All clean — should be rest_clean + range_good + trigger_smooth +
        # at most one cadence observation.
        verdict = classify_verdict(
            rest_samples=_clean_rest_samples(),
            range_samples=_full_rotation_range_samples(),
            trigger_samples=_smooth_trigger_samples(),
            configured_polling_hz=1000,
        )

        self.assertLessEqual(len(verdict.observations), 4)


# ---------------------------------------------------------------------------
# 2. Phase ordering
# ---------------------------------------------------------------------------


class PhaseOrderingTests(unittest.TestCase):
    def test_phase_order_is_rest_range_trigger(self) -> None:
        self.assertEqual(
            PHASE_ORDER,
            (QuickCheckPhase.REST, QuickCheckPhase.RANGE, QuickCheckPhase.TRIGGER),
        )

    def test_state_machine_walks_phases_in_order(self) -> None:
        clk = _FakeClock()
        service = QuickCheckService(
            sample_provider=_ScriptedProvider([]),
            device_context_provider=_device_provider,
            clock=clk,
            phase_durations_s={
                QuickCheckPhase.REST: 0.01,
                QuickCheckPhase.RANGE: 0.01,
                QuickCheckPhase.TRIGGER: 0.01,
            },
        )

        try:
            service.start()
            self.assertEqual(service.state, QuickCheckState.REST)

            clk.advance(0.02)
            service.tick()
            self.assertEqual(service.state, QuickCheckState.RANGE)

            clk.advance(0.02)
            service.tick()
            self.assertEqual(service.state, QuickCheckState.TRIGGER)

            clk.advance(0.02)
            service.tick()
            self.assertEqual(service.state, QuickCheckState.COMPLETE)
            self.assertIsNotNone(service.verdict)
        finally:
            service.cancel()

    def test_tick_during_rest_without_elapsed_time_is_noop(self) -> None:
        clk = _FakeClock()
        service = QuickCheckService(
            sample_provider=_ScriptedProvider([]),
            device_context_provider=_device_provider,
            clock=clk,
            phase_durations_s={
                QuickCheckPhase.REST: 1.0,
                QuickCheckPhase.RANGE: 1.0,
                QuickCheckPhase.TRIGGER: 1.0,
            },
        )

        try:
            service.start()
            self.assertEqual(service.state, QuickCheckState.REST)

            # No time advance.
            service.tick()
            self.assertEqual(service.state, QuickCheckState.REST)
        finally:
            service.cancel()

    def test_cancel_mid_run_resets_to_cancelled(self) -> None:
        clk = _FakeClock()
        service = QuickCheckService(
            sample_provider=_ScriptedProvider([]),
            device_context_provider=_device_provider,
            clock=clk,
            phase_durations_s={
                QuickCheckPhase.REST: 1.0,
                QuickCheckPhase.RANGE: 1.0,
                QuickCheckPhase.TRIGGER: 1.0,
            },
        )

        try:
            service.start()
            service.cancel()

            self.assertEqual(service.state, QuickCheckState.CANCELLED)
            self.assertIsNone(service.verdict)
        finally:
            # cancel() is idempotent.
            service.cancel()

    def test_reset_returns_to_idle_and_clears_verdict(self) -> None:
        clk = _FakeClock()
        service = QuickCheckService(
            sample_provider=_ScriptedProvider([]),
            device_context_provider=_device_provider,
            clock=clk,
            phase_durations_s={
                QuickCheckPhase.REST: 0.01,
                QuickCheckPhase.RANGE: 0.01,
                QuickCheckPhase.TRIGGER: 0.01,
            },
        )

        try:
            service.start()
            clk.advance(0.05)
            service.tick()
            clk.advance(0.05)
            service.tick()
            clk.advance(0.05)
            service.tick()
            self.assertEqual(service.state, QuickCheckState.COMPLETE)

            service.reset()
            self.assertEqual(service.state, QuickCheckState.IDLE)
            self.assertIsNone(service.verdict)
        finally:
            service.cancel()

    def test_start_from_non_idle_raises(self) -> None:
        clk = _FakeClock()
        service = QuickCheckService(
            sample_provider=_ScriptedProvider([]),
            device_context_provider=_device_provider,
            clock=clk,
            phase_durations_s={
                QuickCheckPhase.REST: 1.0,
                QuickCheckPhase.RANGE: 1.0,
                QuickCheckPhase.TRIGGER: 1.0,
            },
        )

        try:
            service.start()
            with self.assertRaises(RuntimeError):
                service.start()
        finally:
            service.cancel()


# ---------------------------------------------------------------------------
# 3. 20s budget
# ---------------------------------------------------------------------------


class BudgetTests(unittest.TestCase):
    def test_total_budget_is_20_seconds(self) -> None:
        self.assertEqual(QUICK_TOTAL_BUDGET_S, 20.0)

    def test_phase_durations_sum_to_total_budget(self) -> None:
        self.assertEqual(
            sum(PHASE_DURATIONS_S.values()),
            QUICK_TOTAL_BUDGET_S,
        )

    def test_phase_durations_individually(self) -> None:
        # Spec: 5 + 8 + 7 = 20.
        self.assertEqual(QUICK_REST_DURATION_S, 5.0)
        self.assertEqual(QUICK_RANGE_DURATION_S, 8.0)
        self.assertEqual(QUICK_TRIGGER_DURATION_S, 7.0)

    def test_regression_ceiling_is_30_seconds_or_less(self) -> None:
        # The spec calls a regression if implementation pushes past 30s.
        self.assertLessEqual(QUICK_TOTAL_BUDGET_REGRESSION_S, 30.0)
        self.assertGreater(QUICK_TOTAL_BUDGET_REGRESSION_S, QUICK_TOTAL_BUDGET_S)

    def test_service_reports_20s_total_budget(self) -> None:
        service = QuickCheckService(
            sample_provider=_ScriptedProvider([]),
            device_context_provider=_device_provider,
        )

        self.assertEqual(service.total_budget_s, 20.0)


# ---------------------------------------------------------------------------
# 4. Threshold reuse
# ---------------------------------------------------------------------------


class ThresholdReuseTests(unittest.TestCase):
    """quick_check should NOT introduce new tunables — verdicts must stay
    consistent with the full Health Report wizard."""

    def test_quick_check_module_does_not_redefine_rest_noise_thresholds(self) -> None:
        # Read the source file; the strings REST_NOISE_GOOD_P99_PCT = and
        # REST_NOISE_NOTICEABLE_P99_PCT = must NOT appear in quick_check.py
        # (they live in measurements.py — quick_check.py only imports them).
        import zd_app.services.health_report.quick_check as qc_module

        src = Path(qc_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn(
            "REST_NOISE_GOOD_P99_PCT =", src,
            "quick_check.py must not redefine rest-noise threshold",
        )
        self.assertNotIn(
            "REST_NOISE_NOTICEABLE_P99_PCT =", src,
            "quick_check.py must not redefine rest-noise threshold",
        )

    def test_quick_check_module_does_not_redefine_range_thresholds(self) -> None:
        import zd_app.services.health_report.quick_check as qc_module

        src = Path(qc_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn(
            "STICK_RANGE_GOOD_PCT =", src,
            "quick_check.py must not redefine range threshold",
        )
        self.assertNotIn(
            "STICK_RANGE_LIMITED_PCT =", src,
            "quick_check.py must not redefine range threshold",
        )

    def test_quick_check_threshold_imports_resolve_to_measurements_values(self) -> None:
        # Imports must produce the same numeric values as the full report.
        from zd_app.services.health_report.quick_check import (
            REST_NOISE_GOOD_P99_PCT as qc_good,
            REST_NOISE_NOTICEABLE_P99_PCT as qc_noticeable,
        )

        self.assertEqual(qc_good, REST_NOISE_GOOD_P99_PCT)
        self.assertEqual(qc_noticeable, REST_NOISE_NOTICEABLE_P99_PCT)


# ---------------------------------------------------------------------------
# 5. Forbidden-phrase scan on readiness_check i18n keys
# ---------------------------------------------------------------------------


_FORBIDDEN_PHRASES = (
    "input latency",
    "input lag",
    "latency",
    "lag",
    "usb polling verified",
    "true polling rate",
    "true 8000 hz",
    "bus-level polling",
    "ban-safe",
    "ban safe",
    "tournament-approved",
    "tournament approved",
    "anti-cheat safe",
    "calibrated",
    "factory calibration",
    "lab-grade",
    "health score",
    "hardware health",
    "firmware bug",
    "defect detected",
)


class ForbiddenPhrasesTests(unittest.TestCase):
    def _load_locale(self, name: str) -> dict[str, str]:
        return json.loads(Path("zd_app/i18n/locales", f"{name}.json").read_text(encoding="utf-8"))

    def _assert_no_forbidden(self, locale_name: str) -> None:
        data = self._load_locale(locale_name)
        readiness_keys = {
            k: v for k, v in data.items()
            if k.startswith("readiness_check.") or k == "nav.readiness_check"
        }
        self.assertGreater(
            len(readiness_keys), 20,
            f"Locale {locale_name} should have >20 readiness_check.* keys, "
            f"got {len(readiness_keys)}",
        )
        for key, value in readiness_keys.items():
            lower = value.lower()
            for phrase in _FORBIDDEN_PHRASES:
                with self.subTest(locale=locale_name, key=key, phrase=phrase):
                    self.assertNotIn(
                        phrase, lower,
                        msg=(
                            f"Locale {locale_name} key {key!r} value {value!r} "
                            f"contains forbidden phrase {phrase!r}"
                        ),
                    )

    def test_en_readiness_check_strings_have_no_forbidden_phrases(self) -> None:
        self._assert_no_forbidden("en")

    def test_zh_cn_readiness_check_strings_have_no_forbidden_phrases(self) -> None:
        self._assert_no_forbidden("zh-CN")

    def test_en_and_zh_cn_have_parity_on_readiness_keys(self) -> None:
        en = set(k for k in self._load_locale("en") if k.startswith("readiness_check.") or k == "nav.readiness_check")
        zh = set(k for k in self._load_locale("zh-CN") if k.startswith("readiness_check.") or k == "nav.readiness_check")

        self.assertEqual(
            en, zh,
            f"i18n parity missing: en-only {en - zh}, zh-only {zh - en}",
        )


# ---------------------------------------------------------------------------
# 6. Wear-ledger integration — readiness_check event on finalize
# ---------------------------------------------------------------------------


class WearLedgerIntegrationTests(unittest.TestCase):
    """QuickCheckService emits a wear-ledger event on finalize.

    Mirrors the pattern in test_health_report_service.WearLedgerIntegrationTests:
    bypass the live state machine by setting internal state directly + driving
    ``_finalize()`` with a fake collector that returns the synthetic samples
    for the desired verdict.
    """

    def _stage_finalize_state(
        self,
        service: "QuickCheckService",
        *,
        run_started_at: float = 1000.0,
        finalized_at: float = 1020.0,
    ) -> None:
        """Set the phase windows + run timestamps that ``_finalize()`` reads.

        Phase windows match the ``base_ts_ns`` values used by the synthetic
        sample helpers (rest=0, range=700_000_000, trigger=1_400_000_000) so
        ``filter_samples_by_time_range`` keeps each phase's samples in its own
        bucket. ``run_started_at`` lets the duration_ms computation produce a
        deterministic value (default 20s -> 20000ms).
        """

        service._device_at_start = _device_provider()
        service._t_zero_ns = 0
        service._t_zero_clock = run_started_at
        service._run_started_at = run_started_at
        service._phase_starts_at_ns = {
            QuickCheckPhase.REST: 0,
            QuickCheckPhase.RANGE: 700_000_000,
            QuickCheckPhase.TRIGGER: 1_400_000_000,
        }
        service._phase_ends_at_ns = {
            QuickCheckPhase.REST: 699_999_999,
            QuickCheckPhase.RANGE: 1_399_999_999,
            QuickCheckPhase.TRIGGER: 2_100_000_000,
        }
        # Drop the service's clock onto a fake that reports ``finalized_at``
        # so duration_ms = (finalized_at - run_started_at) * 1000 is stable.
        service._clock = lambda: finalized_at

    def test_run_emits_readiness_check_event_on_success(self) -> None:
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from zd_app.services.wear_ledger import WearLedgerService

        with tempfile.TemporaryDirectory() as tmp:
            ledger = WearLedgerService(base_dir=Path(tmp) / "wear_ledger")
            service = QuickCheckService(
                sample_provider=_ScriptedProvider([]),
                device_context_provider=_device_provider,
                wear_ledger=ledger,
            )
            all_samples = (
                _clean_rest_samples()
                + _full_rotation_range_samples()
                + _smooth_trigger_samples()
            )
            self._stage_finalize_state(service)
            service._collector = SimpleNamespace(
                stop=lambda: all_samples,
                running=False,
            )

            service._finalize()

            self.assertEqual(service.state, QuickCheckState.COMPLETE)
            self.assertIsNotNone(service.verdict)
            assert service.verdict is not None
            self.assertEqual(service.verdict.status, ReadinessStatus.GREEN)

            events = ledger.read_events()
            readiness_events = [e for e in events if e.event_type == "readiness_check"]
            self.assertEqual(len(readiness_events), 1)
            evt = readiness_events[0]
            self.assertEqual(evt.details["status"], "green")
            self.assertIn("observations", evt.details)
            self.assertIsInstance(evt.details["observations"], list)
            self.assertGreater(len(evt.details["observations"]), 0)
            self.assertEqual(evt.details["duration_ms"], 20_000)
            self.assertIn("green", evt.summary.lower())

    def test_run_emits_event_on_yellow_red_too(self) -> None:
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from zd_app.services.wear_ledger import WearLedgerService

        scenarios = (
            # (label, rest, range, trigger, expected_status)
            (
                "yellow",
                _noticeable_rest_samples(),
                _full_rotation_range_samples(),
                _smooth_trigger_samples(),
                ReadinessStatus.YELLOW,
            ),
            (
                "red",
                _high_rest_samples(),
                _full_rotation_range_samples(),
                _smooth_trigger_samples(),
                ReadinessStatus.RED,
            ),
        )

        for label, rest, rng, trig, expected in scenarios:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as tmp:
                    ledger = WearLedgerService(base_dir=Path(tmp) / "wear_ledger")
                    service = QuickCheckService(
                        sample_provider=_ScriptedProvider([]),
                        device_context_provider=_device_provider,
                        wear_ledger=ledger,
                    )
                    all_samples = rest + rng + trig
                    self._stage_finalize_state(service)
                    service._collector = SimpleNamespace(
                        stop=lambda samples=all_samples: samples,
                        running=False,
                    )

                    service._finalize()

                    assert service.verdict is not None
                    self.assertEqual(service.verdict.status, expected)

                    events = ledger.read_events()
                    readiness_events = [
                        e for e in events if e.event_type == "readiness_check"
                    ]
                    self.assertEqual(len(readiness_events), 1)
                    self.assertEqual(
                        readiness_events[0].details["status"], expected.value
                    )

    def test_run_without_wear_ledger_does_not_crash(self) -> None:
        from types import SimpleNamespace

        service = QuickCheckService(
            sample_provider=_ScriptedProvider([]),
            device_context_provider=_device_provider,
            wear_ledger=None,
        )
        self._stage_finalize_state(service)
        service._collector = SimpleNamespace(
            stop=lambda: [],
            running=False,
        )

        # Must not raise even though wear_ledger is None.
        service._finalize()

        self.assertEqual(service.state, QuickCheckState.COMPLETE)
        self.assertIsNotNone(service.verdict)


if __name__ == "__main__":
    unittest.main()
