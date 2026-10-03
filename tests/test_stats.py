from engine.models import TestOutcome, Verdict
from engine.stats import StatsTracker


def test_empty_snapshot():
    snap = StatsTracker().snapshot()
    assert snap["test_cases_judged"] == 0
    assert snap["verdicts"] == {}


def test_percentiles_and_verdict_counts():
    tracker = StatsTracker()
    for ms in range(1, 101):  # 1..100 ms
        tracker.record(TestOutcome(Verdict.ACCEPTED, float(ms), 0))
    tracker.record(TestOutcome(Verdict.WRONG_ANSWER, 50.0, 0))
    snap = tracker.snapshot()
    assert snap["test_cases_judged"] == 101
    assert snap["p50_ms"] == 50.0
    assert 95.0 <= snap["p95_ms"] <= 96.0
    assert snap["verdicts"] == {"AC": 100, "WA": 1}


def test_percentile_interpolates_between_values():
    assert StatsTracker._percentile([10.0, 20.0], 50) == 15.0
    assert StatsTracker._percentile([], 50) == 0.0
