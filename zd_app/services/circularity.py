"""Pure-Python circularity-sweep accumulator for the live Diagnostics panel.

As a thumbstick is rotated at full deflection, the panel feeds each
normalized ``(x, y)`` sample to :class:`CircularitySweep`. The sweep bins
samples by angle, tracks the maximum radius seen per bin (the swept
envelope), and derives two neutral metrics:

* ``avg_error_pct`` — mean absolute deviation of the per-bin envelope radius
  from the unit circle (reference radius 1.0), expressed as a percentage.
* ``coverage_pct`` — fraction of angle bins that received at least one
  qualifying sample, so a half-spin can't masquerade as a low-error result.

The module is deliberately free of DearPyGui and XInput imports: it takes
plain floats and returns plain numbers / point lists, which makes the metric
fully unit-testable without a render context or a controller. The panel owns
all drawing; this owns all math.

Circularity error is a *preference*, not a defect: a stock controller
typically reads ~5-12% because of its deadzone and gate shape. Lower values
come from a smaller deadzone, not from a "better" controller. Callers must
frame the percentage neutrally and never imply lower is better.
"""

from __future__ import annotations

import math
from typing import List, Tuple


# 72 bins -> 5 degrees per bin, matching the health-report sector
# granularity (measurements sector approach).
DEFAULT_BIN_COUNT = 72

# Samples below this radius are ignored entirely: a stick resting near
# center (or transiting through it between edge pushes) would otherwise
# stamp arbitrary bins with a near-zero envelope radius and inflate the
# error. The threshold sits well above any realistic deadzone yet far
# below full deflection, so genuine edge sweeps are never dropped.
DEFAULT_MIN_RADIUS = 0.30

# Reference radius for a perfectly circular full-deflection envelope.
REFERENCE_RADIUS = 1.0

_TWO_PI = 2.0 * math.pi


class CircularitySweep:
    """Accumulate stick samples into a per-angle max-radius envelope.

    Construct one per stick. Feed ``add_sample(x, y)`` every tick while a
    stick test is active; read ``avg_error_pct`` / ``coverage_pct`` /
    ``envelope_points()`` to render results; call ``reset()`` when a fresh
    test starts.
    """

    def __init__(
        self,
        *,
        bin_count: int = DEFAULT_BIN_COUNT,
        min_radius: float = DEFAULT_MIN_RADIUS,
    ) -> None:
        if bin_count < 3:
            raise ValueError(f"bin_count must be >= 3, got {bin_count}")
        self._bin_count = int(bin_count)
        self._min_radius = float(min_radius)
        self._max_radius: List[float] = [0.0] * self._bin_count
        self._visited: List[bool] = [False] * self._bin_count
        self._sample_count = 0

    # -- accumulation --------------------------------------------------------

    def add_sample(self, x: float, y: float) -> None:
        """Fold one normalized ``(x, y)`` sample into the envelope.

        Samples whose magnitude is below ``min_radius`` are dropped so a
        centered / transiting stick never marks a bin visited. For a
        qualifying sample, the bin's envelope radius only ever grows (max
        seen), which is what makes a full sweep converge to the true edge.
        """

        r = math.hypot(x, y)
        if r < self._min_radius:
            return
        idx = self._bin_index(math.atan2(y, x))
        self._sample_count += 1
        self._visited[idx] = True
        if r > self._max_radius[idx]:
            self._max_radius[idx] = r

    def reset(self) -> None:
        """Clear the envelope and counters for a new stick test."""

        self._max_radius = [0.0] * self._bin_count
        self._visited = [False] * self._bin_count
        self._sample_count = 0

    # -- derived metrics -----------------------------------------------------

    @property
    def bin_count(self) -> int:
        return self._bin_count

    @property
    def sample_count(self) -> int:
        """Total qualifying samples folded since the last reset."""

        return self._sample_count

    @property
    def visited_bins(self) -> int:
        return sum(self._visited)

    @property
    def coverage_pct(self) -> float:
        """Percentage of angle bins that received a qualifying sample."""

        return (self.visited_bins / self._bin_count) * 100.0

    @property
    def avg_error_pct(self) -> float:
        """Mean ``|r_bin - 1.0|`` over visited bins, as a percentage.

        Reference radius is the unit circle (full deflection). Both
        undershoot (deadzone / short throw) and overshoot (square gate
        corners reading > 1.0 on the diagonals) contribute positively, so
        this captures the controller's true envelope shape. Zero visited
        bins returns ``0.0`` — callers gate the display on ``coverage_pct``.
        """

        visited = [
            r for r, seen in zip(self._max_radius, self._visited) if seen
        ]
        if not visited:
            return 0.0
        total = sum(abs(r - REFERENCE_RADIUS) for r in visited)
        return (total / len(visited)) * 100.0

    # -- geometry for the polar plot ----------------------------------------

    def envelope_points(self) -> List[Tuple[float, float]]:
        """Return one ``(x, y)`` envelope vertex per bin, in angle order.

        Vertices are in unit space (math convention, ``+y`` up); the panel
        scales them and negates ``y`` for screen coordinates. Unvisited bins
        contribute a zero-radius vertex (the polygon collapses to the centre
        there), so a partial sweep reads as visibly incomplete rather than
        inventing an envelope it never measured. The list length always
        equals ``bin_count`` so the panel can update the polygon in place.
        """

        points: List[Tuple[float, float]] = []
        step = _TWO_PI / self._bin_count
        for idx in range(self._bin_count):
            angle = (idx + 0.5) * step
            r = self._max_radius[idx]
            points.append((r * math.cos(angle), r * math.sin(angle)))
        return points

    def bin_radii(self) -> List[float]:
        """Return the per-bin max-radius array (0.0 for unvisited bins)."""

        return list(self._max_radius)

    # -- internals -----------------------------------------------------------

    def _bin_index(self, angle: float) -> int:
        normalized = angle % _TWO_PI
        idx = int(normalized / (_TWO_PI / self._bin_count))
        if idx >= self._bin_count:  # guard the theta == 2*pi boundary
            idx = self._bin_count - 1
        return idx
