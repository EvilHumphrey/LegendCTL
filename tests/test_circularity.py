"""Unit tests for the circularity-sweep metric (live Diagnostics panel).

The sweep is pure math — no DPG, no XInput — so these tests feed it plain
``(x, y)`` floats and assert the derived avg-error / coverage / envelope.
"""

from __future__ import annotations

import math
import unittest

from zd_app.services.circularity import (
    DEFAULT_BIN_COUNT,
    DEFAULT_MIN_RADIUS,
    CircularitySweep,
)


def _feed_circle(sweep: CircularitySweep, radius: float, *, samples: int = 720) -> None:
    """Feed ``samples`` evenly-spaced points on a circle of the given radius."""

    for i in range(samples):
        angle = (i / samples) * 2.0 * math.pi
        sweep.add_sample(radius * math.cos(angle), radius * math.sin(angle))


class PerfectCircleTests(unittest.TestCase):
    def test_unit_circle_reads_near_zero_error_and_full_coverage(self) -> None:
        sweep = CircularitySweep()
        _feed_circle(sweep, 1.0)
        # Every bin saw radius 1.0 -> deviation from the unit circle is ~0.
        self.assertAlmostEqual(sweep.avg_error_pct, 0.0, places=6)
        self.assertAlmostEqual(sweep.coverage_pct, 100.0, places=6)
        self.assertEqual(sweep.visited_bins, DEFAULT_BIN_COUNT)

    def test_shrunken_circle_reads_uniform_undershoot(self) -> None:
        # A 0.9-radius circle is 10% short of the unit reference everywhere.
        sweep = CircularitySweep()
        _feed_circle(sweep, 0.9)
        self.assertAlmostEqual(sweep.avg_error_pct, 10.0, places=4)
        self.assertAlmostEqual(sweep.coverage_pct, 100.0, places=6)

    def test_overshoot_circle_contributes_positive_error(self) -> None:
        # A 1.2-radius envelope (gate corners overshooting) is +20% error;
        # overshoot must count the same as undershoot, not cancel it.
        sweep = CircularitySweep()
        _feed_circle(sweep, 1.2)
        self.assertAlmostEqual(sweep.avg_error_pct, 20.0, places=4)


class KnownEnvelopeTests(unittest.TestCase):
    def test_one_sample_per_bin_center_gives_exact_mean_error(self) -> None:
        # Feed exactly one sample at each bin centre with a known radius so
        # max-per-bin == that radius and avg_error is exactly mean(|r-1|).
        sweep = CircularitySweep(bin_count=8)
        radii = [1.0, 0.8, 1.0, 0.6, 1.0, 0.8, 1.0, 0.6]
        step = 2.0 * math.pi / 8
        for idx, r in enumerate(radii):
            angle = (idx + 0.5) * step
            sweep.add_sample(r * math.cos(angle), r * math.sin(angle))
        expected = sum(abs(r - 1.0) for r in radii) / len(radii) * 100.0
        self.assertAlmostEqual(sweep.avg_error_pct, expected, places=6)
        self.assertAlmostEqual(sweep.coverage_pct, 100.0, places=6)

    def test_max_radius_per_bin_wins_over_later_smaller_sample(self) -> None:
        sweep = CircularitySweep(bin_count=4)
        # Two samples into the same (east) bin: the larger radius is the
        # envelope, even though it arrived first.
        sweep.add_sample(1.0, 0.0)
        sweep.add_sample(0.5, 0.0)
        radii = sweep.bin_radii()
        self.assertEqual(max(radii), 1.0)
        self.assertEqual(sweep.visited_bins, 1)

    def test_square_gate_envelope_reads_diagonal_overshoot(self) -> None:
        # A perfectly square gate: axes reach 1.0, diagonals reach sqrt(2).
        # Mean |r-1| over a dense square outline is a known positive value.
        sweep = CircularitySweep()
        samples = 720
        for i in range(samples):
            angle = (i / samples) * 2.0 * math.pi
            # Point on the unit square boundary at this angle.
            c, s = math.cos(angle), math.sin(angle)
            scale = 1.0 / max(abs(c), abs(s))
            sweep.add_sample(c * scale, s * scale)
        # Square corners sit at r=sqrt(2)~=1.414 -> error is clearly nonzero
        # and bounded by the corner overshoot.
        self.assertGreater(sweep.avg_error_pct, 5.0)
        self.assertLess(sweep.avg_error_pct, 41.5)
        self.assertAlmostEqual(sweep.coverage_pct, 100.0, places=6)


class CoverageTests(unittest.TestCase):
    def test_partial_spin_reads_low_coverage(self) -> None:
        # Sweep only the first quadrant (0..90 deg) -> ~25% coverage.
        sweep = CircularitySweep()
        for i in range(200):
            angle = (i / 200) * (math.pi / 2.0)
            sweep.add_sample(math.cos(angle), math.sin(angle))
        self.assertLess(sweep.coverage_pct, 30.0)
        self.assertGreater(sweep.coverage_pct, 20.0)

    def test_empty_sweep_is_zero_error_and_zero_coverage(self) -> None:
        sweep = CircularitySweep()
        self.assertEqual(sweep.avg_error_pct, 0.0)
        self.assertEqual(sweep.coverage_pct, 0.0)
        self.assertEqual(sweep.visited_bins, 0)
        self.assertEqual(sweep.sample_count, 0)


class ThresholdAndResetTests(unittest.TestCase):
    def test_center_dwell_below_threshold_is_ignored(self) -> None:
        sweep = CircularitySweep()
        # A stick resting near centre must not mark bins or inflate error.
        for _ in range(500):
            sweep.add_sample(0.05, -0.05)
        self.assertEqual(sweep.visited_bins, 0)
        self.assertEqual(sweep.sample_count, 0)
        self.assertEqual(sweep.avg_error_pct, 0.0)

    def test_sample_at_threshold_boundary_counts(self) -> None:
        sweep = CircularitySweep()
        # Exactly at the threshold radius along +x.
        sweep.add_sample(DEFAULT_MIN_RADIUS, 0.0)
        self.assertEqual(sweep.sample_count, 1)
        self.assertEqual(sweep.visited_bins, 1)

    def test_reset_clears_accumulator(self) -> None:
        sweep = CircularitySweep()
        _feed_circle(sweep, 1.0)
        self.assertGreater(sweep.visited_bins, 0)
        sweep.reset()
        self.assertEqual(sweep.visited_bins, 0)
        self.assertEqual(sweep.sample_count, 0)
        self.assertEqual(sweep.avg_error_pct, 0.0)
        self.assertEqual(sweep.coverage_pct, 0.0)


class EnvelopeGeometryTests(unittest.TestCase):
    def test_envelope_points_length_equals_bin_count(self) -> None:
        sweep = CircularitySweep()
        self.assertEqual(len(sweep.envelope_points()), DEFAULT_BIN_COUNT)
        _feed_circle(sweep, 1.0)
        self.assertEqual(len(sweep.envelope_points()), DEFAULT_BIN_COUNT)

    def test_unvisited_bins_are_zero_radius_vertices(self) -> None:
        sweep = CircularitySweep(bin_count=4)
        sweep.add_sample(1.0, 0.0)  # east bin only
        points = sweep.envelope_points()
        # Exactly one vertex is off-centre; the rest collapse to the origin.
        off_centre = [p for p in points if math.hypot(*p) > 1e-9]
        self.assertEqual(len(off_centre), 1)

    def test_envelope_vertex_sits_at_bin_center_angle(self) -> None:
        sweep = CircularitySweep(bin_count=4)
        # Feed a sample at the bin-0 centre (45 deg) so the envelope vertex
        # falls exactly on the sampled angle (binning places the vertex at
        # the bin centre, not the raw sample angle).
        angle = math.pi / 4
        sweep.add_sample(math.cos(angle), math.sin(angle))
        points = sweep.envelope_points()
        off_centre = [p for p in points if math.hypot(*p) > 1e-9]
        self.assertEqual(len(off_centre), 1)
        vx, vy = off_centre[0]
        self.assertAlmostEqual(vx, math.cos(angle), places=6)
        self.assertAlmostEqual(vy, math.sin(angle), places=6)


class ConstructionGuardTests(unittest.TestCase):
    def test_too_few_bins_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CircularitySweep(bin_count=2)


if __name__ == "__main__":
    unittest.main()
