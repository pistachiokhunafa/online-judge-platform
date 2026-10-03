"""
stats.py — turns raw per-test-case measurements into the numbers a
dashboard actually wants to show: how many of each verdict, the average
runtime, and — more importantly — the tail of the runtime distribution
(P50 / P95 / P99). An average can hide a bad outlier; percentiles can't.
"""

from __future__ import annotations

import statistics
import threading
from collections import Counter
from typing import List

from .models import TestOutcome, Verdict


class StatsTracker:
    """Thread-safe running statistics over every test case ever judged."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runtimes_ms: List[float] = []
        self._verdict_counts: Counter = Counter()

    def record(self, outcome: TestOutcome) -> None:
        with self._lock:
            self._runtimes_ms.append(outcome.runtime_ms)
            self._verdict_counts[outcome.verdict] += 1

    @staticmethod
    def _percentile(sorted_values: List[float], p: float) -> float:
        """Linear-interpolation percentile — the same method spreadsheet
        software uses, so the numbers match what people expect to see."""
        if not sorted_values:
            return 0.0
        k = (len(sorted_values) - 1) * (p / 100)
        lower, upper = int(k), min(int(k) + 1, len(sorted_values) - 1)
        if lower == upper:
            return sorted_values[lower]
        return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (k - lower)

    def snapshot(self) -> dict:
        """A plain-data summary, ready to serialize straight to JSON."""
        with self._lock:
            runtimes = sorted(self._runtimes_ms)
            n = len(runtimes)
            verdicts = dict(self._verdict_counts)

        if n == 0:
            return {
                "test_cases_judged": 0, "mean_ms": 0, "stdev_ms": 0,
                "p50_ms": 0, "p95_ms": 0, "p99_ms": 0, "verdicts": {},
            }

        return {
            "test_cases_judged": n,
            "mean_ms": round(statistics.fmean(runtimes), 1),
            "stdev_ms": round(statistics.pstdev(runtimes), 1) if n > 1 else 0.0,
            "p50_ms": round(self._percentile(runtimes, 50), 1),
            "p95_ms": round(self._percentile(runtimes, 95), 1),
            "p99_ms": round(self._percentile(runtimes, 99), 1),
            "verdicts": {v.value: c for v, c in verdicts.items()},
        }
