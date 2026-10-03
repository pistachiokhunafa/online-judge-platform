"""
judge.py — turns one Submission into a final Verdict.

Two steps, matching the two halves of Amdahl's Law from the write-up:

  1. A quick SERIAL check (a syntax check stands in for "compile once")
     — this is the part of the work that can't be parallelized.
  2. A PARALLEL fan-out of every hidden test case across a thread pool,
     followed by a fan-in that reduces all those individual results into
     one final verdict.

Design choice worth calling out: this web version runs every test case
to completion, even after the first failure, so the UI can show a full
"5 of 6 passed" style breakdown — the command-line version in this same
project instead cancels remaining tests the moment one fails ("fail
fast") to save compute. Same engine, two reasonable trade-offs; pick
whichever your interface needs.
"""

from __future__ import annotations

import time
from concurrent.futures import Executor, as_completed
from typing import List

from .models import Submission, TestOutcome, Verdict
from .sandbox import run_test_case


def _rank_final_verdict(outcomes: List[TestOutcome]) -> Verdict:
    """
    AC only if every test passed. Otherwise, report the first *kind* of
    failure found, in the order a competitive programmer would expect
    to see it explained: a crash matters more than a wrong answer, which
    matters more than "you ran out of time or memory" — but any ordering
    is defensible; this one just keeps failures from feeling random.
    """
    order = [Verdict.RUNTIME_ERROR, Verdict.WRONG_ANSWER,
             Verdict.TIME_LIMIT_EXCEEDED, Verdict.MEMORY_LIMIT_EXCEEDED]
    present = {o.verdict for o in outcomes}
    for verdict in order:
        if verdict in present:
            return verdict
    return Verdict.ACCEPTED


def judge(submission: Submission, test_case_pool: Executor) -> None:
    """
    Judges `submission` in place: runs every hidden test case for its
    problem, then fills in submission.test_outcomes, submission.verdict,
    submission.status, and submission.finished_at.

    Mutating the Submission object directly (rather than returning a new
    one) is what lets the web API keep handing the browser the same
    submission_id and have every poll see the latest progress.
    """
    from .problems import get_problem  # local import avoids a circular import

    submission.status = Verdict.JUDGING
    submission.started_judging_at = time.time()

    problem = get_problem(submission.problem_id)

    # --- serial step -------------------------------------------------------
    try:
        compile(submission.code, "<submission>", "exec")
    except SyntaxError as error:
        submission.test_outcomes = [
            TestOutcome(Verdict.RUNTIME_ERROR, 0.0, 0, detail=f"SyntaxError: {error}")
        ]
        submission.verdict = Verdict.RUNTIME_ERROR
        submission.status = submission.verdict
        submission.finished_at = time.time()
        return

    # --- parallel step: fan-out ---------------------------------------------
    futures = [
        test_case_pool.submit(run_test_case, submission.code, test)
        for test in problem.hidden_tests
    ]

    # --- fan-in --------------------------------------------------------------
    # as_completed() yields futures in the order they FINISH, which is great
    # for a live "test 3 just finished" feel, but we want the tests reported
    # back in the same order the problem defines them — so collect first,
    # then sort by that original position.
    index_by_future = {future: i for i, future in enumerate(futures)}
    outcomes: List[TestOutcome] = [None] * len(futures)  # type: ignore[list-item]
    for future in as_completed(futures):
        outcomes[index_by_future[future]] = future.result()

    submission.test_outcomes = outcomes
    submission.verdict = _rank_final_verdict(outcomes)
    submission.status = submission.verdict
    submission.finished_at = time.time()
