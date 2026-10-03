"""
models.py — the plain data objects everything else in the engine passes
around. Nothing in this file *does* anything; it only *describes* things.
Keeping the "nouns" of the system in one place makes the rest of the
code easy to read, because every other file can just import the shape
it needs instead of re-inventing it.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


# A single counter shared by every Submission, so each one gets a unique,
# ever-increasing ID just by asking for the next number.
_id_counter = itertools.count(1)


class Verdict(str, Enum):
    """The outcomes a submission (or a single test case) can end up with."""
    QUEUED = "QUEUED"                     # waiting in the scheduler
    JUDGING = "JUDGING"                   # a worker has picked it up
    ACCEPTED = "AC"
    WRONG_ANSWER = "WA"
    TIME_LIMIT_EXCEEDED = "TLE"
    MEMORY_LIMIT_EXCEEDED = "MLE"
    RUNTIME_ERROR = "RE"


@dataclass
class TestCase:
    """One hidden test: an input, the output it should produce, and limits."""
    stdin: str
    expected_stdout: str
    time_limit_s: float = 2.0
    memory_limit_mb: int = 64


@dataclass
class Problem:
    """A judge problem: a statement, a couple of visible samples, and the
    hidden test cases submissions are actually scored against."""
    problem_id: str
    title: str
    statement: str
    sample_input: str
    sample_output: str
    starter_code: str
    hidden_tests: List[TestCase]


@dataclass
class TestOutcome:
    """The result of running one submission against one TestCase."""
    verdict: Verdict
    runtime_ms: float
    memory_kb: int
    detail: str = ""


@dataclass
class Submission:
    """
    One user submission, from the moment it's created to the moment it's
    judged. This object is mutated in place as it moves through the
    pipeline (QUEUED -> JUDGING -> a final verdict) so the API layer can
    hand its `submission_id` to the browser and let the browser poll for
    updates on this same object.

    `priority_penalty_s` is the scheduler's estimate of how expensive this
    submission is to judge. It is what lets the scheduler push expensive
    work behind newer, cheaper work — see scheduler.py for the full
    explanation of how this becomes "aging" almost for free.
    """
    code: str
    problem_id: str
    submission_id: int = field(default_factory=lambda: next(_id_counter))
    submitted_at: float = field(default_factory=time.time)
    priority_penalty_s: float = 0.2

    status: Verdict = Verdict.QUEUED
    verdict: Optional[Verdict] = None
    test_outcomes: List[TestOutcome] = field(default_factory=list)
    started_judging_at: Optional[float] = None
    finished_at: Optional[float] = None

    def to_dict(self, tests_total: Optional[int] = None) -> dict:
        """Turns this object into plain JSON-friendly data for the API.
        `tests_total` is supplied by the caller (which knows the Problem)
        so the browser can show "3 of 6 tests run" while judging is
        still in progress."""
        return {
            "submission_id": self.submission_id,
            "problem_id": self.problem_id,
            "status": self.status.value,
            "verdict": self.verdict.value if self.verdict else None,
            "submitted_at": self.submitted_at,
            "wait_time_s": round(
                (self.started_judging_at or time.time()) - self.submitted_at, 3
            ),
            "total_time_s": round(
                (self.finished_at - self.submitted_at), 3
            ) if self.finished_at else None,
            "tests": [
                {
                    "verdict": t.verdict.value,
                    "runtime_ms": round(t.runtime_ms, 1),
                    "memory_kb": t.memory_kb,
                    "detail": t.detail,
                }
                for t in self.test_outcomes
            ],
            "tests_total": tests_total,
        }
