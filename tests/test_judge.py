from concurrent.futures import ThreadPoolExecutor

import pytest

from engine.judge import _rank_final_verdict, judge
from engine.models import Submission, TestOutcome, Verdict
from engine.problems import get_problem


@pytest.fixture(scope="module")
def pool():
    with ThreadPoolExecutor(max_workers=4) as executor:
        yield executor


def test_rank_final_verdict():
    def o(v):
        return TestOutcome(v, 1.0, 0)
    assert _rank_final_verdict([o(Verdict.ACCEPTED)] * 3) == Verdict.ACCEPTED
    assert _rank_final_verdict([o(Verdict.ACCEPTED), o(Verdict.WRONG_ANSWER)]) == Verdict.WRONG_ANSWER
    assert _rank_final_verdict([o(Verdict.TIME_LIMIT_EXCEEDED), o(Verdict.RUNTIME_ERROR)]) \
        == Verdict.RUNTIME_ERROR


def test_starter_code_is_accepted_for_sum_problem(pool):
    problem = get_problem("sum-two-integers")
    sub = Submission(code=problem.starter_code, problem_id=problem.problem_id)
    judge(sub, pool)
    assert sub.verdict == Verdict.ACCEPTED
    assert len(sub.test_outcomes) == len(problem.hidden_tests)
    assert sub.finished_at is not None


def test_partially_correct_solution_is_wrong_answer(pool):
    sub = Submission(code='print("PRIME")', problem_id="is-prime")
    judge(sub, pool)
    assert sub.verdict == Verdict.WRONG_ANSWER
    verdicts = {o.verdict for o in sub.test_outcomes}
    assert verdicts == {Verdict.ACCEPTED, Verdict.WRONG_ANSWER}


@pytest.mark.parametrize("code", [
    "def broken(:\n    pass",
    "(" * 1000 + ")" * 1000,      # absurd nesting must not crash the judge
    "x = 1\x00",                  # null byte
])
def test_unparseable_code_is_runtime_error_without_running(pool, code):
    sub = Submission(code=code, problem_id="sum-two-integers")
    judge(sub, pool)
    assert sub.verdict == Verdict.RUNTIME_ERROR
    assert len(sub.test_outcomes) == 1
