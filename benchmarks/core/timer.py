"""Precision timer with warmup, iterations, and percentile calculation."""

from __future__ import annotations

import statistics
import time
from typing import Callable

from .base import TimingStats


class PrecisionTimer:
    """High-precision timer for benchmark measurements.

    Runs warmup iterations (discarded), then measured iterations.
    Computes min/max/median/mean/p95/p99/stddev from measured timings.
    """

    def __init__(self, warmup: int = 2, iterations: int = 5):
        self.warmup = warmup
        self.iterations = iterations

    def measure(self, fn: Callable[[], object]) -> TimingStats:
        """Run fn with warmup + measured iterations, return stats."""
        # Warmup — discard results
        for _ in range(self.warmup):
            fn()

        # Measured runs
        timings_ms: list[float] = []
        for _ in range(self.iterations):
            start = time.perf_counter()
            fn()
            elapsed = (time.perf_counter() - start) * 1000
            timings_ms.append(elapsed)

        return self._compute_stats(timings_ms)

    def measure_once(self, fn: Callable[[], object]) -> TimingStats:
        """Single measured run (for expensive operations like bulk inserts)."""
        # One warmup
        for _ in range(self.warmup):
            fn()

        start = time.perf_counter()
        fn()
        elapsed = (time.perf_counter() - start) * 1000

        return TimingStats(
            iterations=1,
            min_ms=elapsed,
            max_ms=elapsed,
            mean_ms=elapsed,
            median_ms=elapsed,
            p95_ms=elapsed,
            p99_ms=elapsed,
            stddev_ms=0.0,
            total_ms=elapsed,
        )

    def _compute_stats(self, timings_ms: list[float]) -> TimingStats:
        n = len(timings_ms)
        if n == 0:
            return TimingStats(
                iterations=0,
                min_ms=0, max_ms=0, mean_ms=0, median_ms=0,
                p95_ms=0, p99_ms=0, stddev_ms=0, total_ms=0,
            )

        sorted_t = sorted(timings_ms)
        mean = statistics.mean(sorted_t)
        median = statistics.median(sorted_t)
        stddev = statistics.stdev(sorted_t) if n > 1 else 0.0

        # Percentiles via sorted position
        p95 = self._percentile(sorted_t, 0.95)
        p99 = self._percentile(sorted_t, 0.99)

        return TimingStats(
            iterations=n,
            min_ms=sorted_t[0],
            max_ms=sorted_t[-1],
            mean_ms=round(mean, 4),
            median_ms=round(median, 4),
            p95_ms=round(p95, 4),
            p99_ms=round(p99, 4),
            stddev_ms=round(stddev, 4),
            total_ms=round(sum(sorted_t), 4),
        )

    @staticmethod
    def _percentile(sorted_data: list[float], p: float) -> float:
        """Linear interpolation percentile on sorted data."""
        n = len(sorted_data)
        if n == 1:
            return sorted_data[0]
        idx = p * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return sorted_data[lo] + frac * (sorted_data[hi] - sorted_data[lo])
