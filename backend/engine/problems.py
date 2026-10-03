"""
problems.py — the small, fixed catalogue of problems this demo judge can
grade. Real platforms load these from a database; a plain Python dict is
enough here and keeps the whole project runnable with zero setup.

Test cases are listed explicitly (not randomly generated) on purpose:
every submission to the same problem should be measured against the
exact same tests, so results are fair and reproducible.
"""

from __future__ import annotations

from typing import Dict, List

from .models import Problem, TestCase


def _sum_two_integers() -> Problem:
    tests = [
        TestCase("2 3\n", "5"),
        TestCase("10 20\n", "30"),
        TestCase("-5 5\n", "0"),
        TestCase("0 0\n", "0"),
        TestCase("999999 1\n", "1000000"),
        TestCase("-100 -250\n", "-350"),
    ]
    return Problem(
        problem_id="sum-two-integers",
        title="Sum of Two Integers",
        statement=(
            "Read two space-separated integers A and B from standard input "
            "and print A + B."
        ),
        sample_input="2 3",
        sample_output="5",
        starter_code=(
            "a, b = map(int, input().split())\n"
            "print(a + b)\n"
        ),
        hidden_tests=tests,
    )


def _reverse_string() -> Problem:
    tests = [
        TestCase("hello\n", "olleh"),
        TestCase("racecar\n", "racecar"),
        TestCase("Online Judge\n", "egduJ enilnO"),
        TestCase("a\n", "a"),
        TestCase("Codeforces\n", "secrofedoC"),
    ]
    return Problem(
        problem_id="reverse-string",
        title="Reverse a String",
        statement="Read one line of text and print it reversed.",
        sample_input="hello",
        sample_output="olleh",
        starter_code=(
            "s = input()\n"
            "print(s[::-1])\n"
        ),
        hidden_tests=tests,
    )


def _is_prime() -> Problem:
    tests = [
        TestCase("2\n", "PRIME"),
        TestCase("17\n", "PRIME"),
        TestCase("1\n", "NOT PRIME"),
        TestCase("100\n", "NOT PRIME"),
        TestCase("97\n", "PRIME"),
        TestCase("1000003\n", "PRIME"),
    ]
    return Problem(
        problem_id="is-prime",
        title="Is It Prime?",
        statement=(
            "Read one integer N and print \"PRIME\" if it is a prime number, "
            "or \"NOT PRIME\" otherwise. (1 is not prime.)"
        ),
        sample_input="17",
        sample_output="PRIME",
        starter_code=(
            "n = int(input())\n"
            "# TODO: decide whether n is prime\n"
            "print(\"PRIME\")\n"
        ),
        hidden_tests=tests,
    )


_PROBLEMS: Dict[str, Problem] = {
    p.problem_id: p for p in (_sum_two_integers(), _reverse_string(), _is_prime())
}


def list_problems() -> List[Problem]:
    return list(_PROBLEMS.values())


def get_problem(problem_id: str) -> Problem:
    if problem_id not in _PROBLEMS:
        raise KeyError(f"Unknown problem_id: {problem_id!r}")
    return _PROBLEMS[problem_id]
