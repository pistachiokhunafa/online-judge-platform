"""
Scalable Online Code Judging Platform — Reference Implementation
==================================================================

This is a small, REAL, runnable version of the engine described in the
project write-up. It isn't a mock — submitted code actually executes in
its own subprocess, output is actually compared, and runtime/memory are
actually measured. It demonstrates the four ideas from the write-up with
working code instead of theory:

  1. PRIORITY-QUEUE SCHEDULING (binary heap + "aging") instead of FIFO.
  2. A PARALLEL WORKER POOL that fans a submission's test cases out
     across threads and fans the results back in (fan-out / fan-in).
  3. FAIL-FAST VERDICT GENERATION: Accepted (AC), Wrong Answer (WA),
     Time Limit Exceeded (TLE), Memory Limit Exceeded (MLE), Runtime
     Error (RE).
  4. A STATISTICS LAYER tracking runtime & memory per test case, and
     reporting percentiles (P50 / P95 / P99), not just an average.

For safety and portability, submissions here are plain Python snippets
that read from stdin and write to stdout — the same interface real
competitive-programming judges use — executed in an isolated child
process with a wall-clock timeout and a memory ceiling. To support a
new language you would only need to add an entry to LANGUAGE_RUNNERS
near the bottom of the file; nothing else in the engine changes.

Run it directly to see a live demo:
    python3 online_judge.py
"""

from __future__ import annotations

import heapq
import itertools
import os
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

try:
    import resource  # POSIX only — used to cap a submission's memory use
    HAVE_RESOURCE = True
except ImportError:                      # e.g. running on Windows
    HAVE_RESOURCE = False


# ============================================================================
# 1. VERDICTS
# ============================================================================

class Verdict(Enum):
    """The five outcomes a judged submission can end up with."""
    ACCEPTED = "AC"
    WRONG_ANSWER = "WA"
    TIME_LIMIT_EXCEEDED = "TLE"
    MEMORY_LIMIT_EXCEEDED = "MLE"
    RUNTIME_ERROR = "RE"


# ============================================================================
# 2. DATA MODEL
# ============================================================================

@dataclass
class TestCase:
    """One hidden test: an input, the output it should produce, and limits."""
    stdin: str
    expected_stdout: str
    time_limit_s: float = 2.0
    memory_limit_mb: int = 64


@dataclass
class Submission:
    """
    One user submission waiting to be judged.

    `runtime_penalty_s` is the scheduler's estimate of how expensive this
    submission is to judge (e.g. derived from the problem's time limit,
    or the user's historical average). It is what lets the scheduler
    de-prioritise heavy submissions without needing to know exactly how
    long they will take.
    """
    submission_id: int
    code: str
    problem_id: str
    test_cases: List[TestCase]
    submitted_time: float = field(default_factory=time.time)
    runtime_penalty_s: float = 0.0


@dataclass
class TestResult:
    verdict: Verdict
    runtime_s: float
    memory_kb: int
    detail: str = ""


@dataclass
class JudgeResult:
    submission: Submission
    verdict: Verdict
    test_results: List[TestResult]
    total_wall_time_s: float


# ============================================================================
# 3. THE SCHEDULER — a binary heap with "aging" baked in for free
# ============================================================================
#
# The write-up defines a priority score:
#
#       P(s) = wait_time(s) - runtime_penalty(s)
#            = (now - submitted_time(s)) - runtime_penalty(s)
#
# and says: process the submission with the HIGHEST P(s) next.
#
# Here's the trick that makes this cheap to implement with a plain
# min-heap (heapq): "now" is the same for every submission at the instant
# you compare them, so ranking by P(s) descending is mathematically the
# same as ranking by (submitted_time(s) + runtime_penalty(s)) ASCENDING.
# That's just a number we can compute once, at push time — no need to
# re-sort the heap every tick to make "waiting longer" matter. This is
# the same idea behind Linux's CFS scheduler ("virtual runtime"): cheap,
# old work gets a small key and floats to the front; expensive work gets
# pushed behind newer arrivals by its penalty, but a large enough wait
# always drags it back to the front eventually — starvation-free.
#
class PriorityScheduler:
    """Thread-safe priority queue of Submissions, backed by a binary heap."""

    def __init__(self) -> None:
        self._heap: List[Tuple[float, int, Submission]] = []
        self._tie_breaker = itertools.count()   # keeps heapq happy on ties
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)

    def push(self, submission: Submission) -> None:
        # Smaller key = judged sooner. O(log n).
        key = submission.submitted_time + submission.runtime_penalty_s
        with self._not_empty:
            heapq.heappush(self._heap, (key, next(self._tie_breaker), submission))
            self._not_empty.notify()

    def pop(self, timeout: Optional[float] = None) -> Optional[Submission]:
        """Block until the highest-priority submission is available. O(log n)."""
        with self._not_empty:
            if not self._heap:
                self._not_empty.wait(timeout=timeout)
            if not self._heap:
                return None
            _, _, submission = heapq.heappop(self._heap)
            return submission

    def peek_size(self) -> int:
        with self._lock:
            return len(self._heap)


# ============================================================================
# 4. THE SANDBOX — actually runs a submission's code against one test case
# ============================================================================

def _set_memory_limit(memory_limit_mb: int):
    """Runs in the CHILD process, right before exec, to cap its address space."""
    def limiter():
        limit_bytes = memory_limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    return limiter if HAVE_RESOURCE else None


def run_test_case(code: str, test: TestCase) -> TestResult:
    """
    Executes `code` (a Python program) in its own subprocess against one
    TestCase, and returns exactly what the write-up says the platform
    tracks: a verdict, a runtime, and a memory reading.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        source_path = f.name

    start = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, source_path],
            input=test.stdin,
            capture_output=True,
            text=True,
            timeout=test.time_limit_s,
            preexec_fn=_set_memory_limit(test.memory_limit_mb) if HAVE_RESOURCE else None,
        )
        runtime_s = time.perf_counter() - start

        # ru_maxrss is the peak resident memory of the finished child, in KB
        # on Linux. This is a coarse, POSIX-only measurement — real judges
        # use cgroups for precise, per-process accounting.
        memory_kb = (
            resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            if HAVE_RESOURCE else 0
        )

        if proc.returncode != 0:
            if "MemoryError" in proc.stderr:
                return TestResult(Verdict.MEMORY_LIMIT_EXCEEDED, runtime_s, memory_kb, proc.stderr[-200:])
            return TestResult(Verdict.RUNTIME_ERROR, runtime_s, memory_kb, proc.stderr[-200:])

        actual = proc.stdout.strip()
        expected = test.expected_stdout.strip()
        if actual == expected:
            return TestResult(Verdict.ACCEPTED, runtime_s, memory_kb)
        return TestResult(Verdict.WRONG_ANSWER, runtime_s, memory_kb,
                           detail=f"expected {expected!r}, got {actual!r}")

    except subprocess.TimeoutExpired:
        return TestResult(Verdict.TIME_LIMIT_EXCEEDED, test.time_limit_s, 0)
    finally:
        os.unlink(source_path)


# ============================================================================
# 5. THE JUDGE — fan-out the test cases, fan-in the verdict (fail-fast)
# ============================================================================

def judge_submission(
    submission: Submission,
    test_case_pool: ThreadPoolExecutor,
    fail_fast: bool = True,
) -> JudgeResult:
    """
    "Compiles" (here: a syntax check — the serial, non-parallel part of
    the work, i.e. the (1-p) term in Amdahl's Law) then fans every test
    case for this submission out across `test_case_pool` in parallel,
    and reduces the individual results into one final verdict.
    """
    wall_start = time.perf_counter()

    # --- serial step: a stand-in for "compile once" ------------------------
    try:
        compile(submission.code, "<submission>", "exec")
    except SyntaxError as e:
        return JudgeResult(submission, Verdict.RUNTIME_ERROR, [], 0.0)

    # --- parallel step: fan-out -------------------------------------------
    futures = {
        test_case_pool.submit(run_test_case, submission.code, tc): tc
        for tc in submission.test_cases
    }

    results: List[TestResult] = []
    final_verdict = Verdict.ACCEPTED
    for future in as_completed(futures):
        result = future.result()
        results.append(result)
        if result.verdict != Verdict.ACCEPTED and final_verdict == Verdict.ACCEPTED:
            final_verdict = result.verdict          # first failure reported
            if fail_fast:
                # Best-effort: cancels any test that hasn't started yet.
                # (Tests already running in a subprocess finish naturally —
                # we don't kill live child processes in this reference build.)
                for f in futures:
                    f.cancel()

    wall_time = time.perf_counter() - wall_start
    return JudgeResult(submission, final_verdict, results, wall_time)


# ============================================================================
# 6. STATISTICS LAYER — mean, standard deviation, and tail percentiles
# ============================================================================

class StatsTracker:
    """
    Collects every test-case measurement and turns it into the numbers an
    operator actually cares about. Percentiles matter more than the mean:
    a system that is fast for 99 requests and terrible for 1 still LOOKS
    fine on average — P95/P99 is what catches that.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runtimes_ms: List[float] = []
        self._memory_kb: List[float] = []
        self._verdicts: Counter = Counter()

    def record(self, result: TestResult) -> None:
        with self._lock:
            self._runtimes_ms.append(result.runtime_s * 1000)
            self._memory_kb.append(result.memory_kb)
            self._verdicts[result.verdict] += 1

    @staticmethod
    def _percentile(values: List[float], p: float) -> float:
        if not values:
            return 0.0
        values = sorted(values)
        k = (len(values) - 1) * (p / 100)
        f, c = int(k), min(int(k) + 1, len(values) - 1)
        if f == c:
            return values[f]
        return values[f] + (values[c] - values[f]) * (k - f)

    def report(self) -> str:
        with self._lock:
            n = len(self._runtimes_ms)
            if n == 0:
                return "No test cases judged yet."
            mean = statistics.fmean(self._runtimes_ms)
            sigma = statistics.pstdev(self._runtimes_ms) if n > 1 else 0.0
            p50 = self._percentile(self._runtimes_ms, 50)
            p95 = self._percentile(self._runtimes_ms, 95)
            p99 = self._percentile(self._runtimes_ms, 99)
            verdict_line = ", ".join(
                f"{v.value}={c}" for v, c in sorted(self._verdicts.items(), key=lambda kv: kv[0].value)
            )
        return (
            f"  test cases judged : {n}\n"
            f"  verdict breakdown : {verdict_line}\n"
            f"  mean runtime      : {mean:7.1f} ms\n"
            f"  std dev (sigma)   : {sigma:7.1f} ms\n"
            f"  P50 / P95 / P99   : {p50:7.1f} / {p95:7.1f} / {p99:7.1f} ms"
        )


# ============================================================================
# 7. THE SYSTEM — ties scheduler + worker pools + stats together
# ============================================================================

class OnlineJudgeSystem:
    """
    Two independent pools, matching the two levels of parallelism in the
    write-up:

      * `submission_workers` pulls the next submission off the priority
        heap and judges it — this is the "c" in the stability condition
        rho = lambda / (c * mu).
      * `test_case_pool` is shared by all of them and is where individual
        test cases actually run — this is the "n" in Amdahl's Law.
    """

    def __init__(self, submission_workers: int = 4, test_case_workers: int = 16):
        self.scheduler = PriorityScheduler()
        self.stats = StatsTracker()
        self._test_case_pool = ThreadPoolExecutor(max_workers=test_case_workers)
        self._submission_workers = submission_workers
        self._stop = threading.Event()
        self._results: List[JudgeResult] = []
        self._results_lock = threading.Lock()

    def submit(self, submission: Submission) -> None:
        self.scheduler.push(submission)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            submission = self.scheduler.pop(timeout=0.5)
            if submission is None:
                continue
            result = judge_submission(submission, self._test_case_pool)
            for tr in result.test_results:
                self.stats.record(tr)
            with self._results_lock:
                self._results.append(result)

    def run_for(self, seconds: float) -> None:
        """Starts the submission-level worker pool and lets it drain the queue."""
        threads = [
            threading.Thread(target=self._worker_loop, daemon=True)
            for _ in range(self._submission_workers)
        ]
        for t in threads:
            t.start()
        time.sleep(seconds)
        # keep going until the backlog is actually empty, then stop
        while self.scheduler.peek_size() > 0:
            time.sleep(0.1)
        time.sleep(0.3)  # let in-flight submissions finish
        self._stop.set()
        for t in threads:
            t.join(timeout=2)
        self._test_case_pool.shutdown(wait=True)

    def results(self) -> List[JudgeResult]:
        with self._results_lock:
            return list(self._results)


# ============================================================================
# 8. DEMO — synthetic submissions, Poisson arrivals, a live run
# ============================================================================

# Five sample "solutions" to the same toy problem — read two ints, print
# their sum — deliberately covering every verdict the judge can produce.
SAMPLE_SOLUTIONS = {
    "correct": "a, b = map(int, input().split())\nprint(a + b)",
    "wrong_answer": "a, b = map(int, input().split())\nprint(a - b)",       # WA
    "slow": "import time\na, b = map(int, input().split())\ntime.sleep(3)\nprint(a + b)",  # TLE
    "crashes": "a, b = map(int, input().split())\nprint(a + undefined_name)",  # RE
    "memory_hog": "a, b = map(int, input().split())\nx = [0] * (10**9)\nprint(a + b)",  # MLE
}

def make_test_cases(n: int = 6) -> List[TestCase]:
    cases = []
    for _ in range(n):
        a, b = random.randint(1, 1000), random.randint(1, 1000)
        cases.append(TestCase(stdin=f"{a} {b}\n", expected_stdout=str(a + b),
                               time_limit_s=1.0, memory_limit_mb=64))
    return cases


def generate_submissions(count: int) -> List[Submission]:
    weighted_pool = (
        ["correct"] * 6 + ["wrong_answer"] * 2 +
        ["slow"] * 1 + ["crashes"] * 1 + ["memory_hog"] * 1
    )
    subs = []
    for i in range(count):
        kind = random.choice(weighted_pool)
        code = SAMPLE_SOLUTIONS[kind]
        # runtime_penalty is the scheduler's *guess* at cost — a known
        # "slow" category is estimated as expensive without running it.
        penalty = 2.5 if kind == "slow" else 0.05
        subs.append(Submission(
            submission_id=i,
            code=code,
            problem_id="A+B",
            test_cases=make_test_cases(),
            runtime_penalty_s=penalty,
        ))
    return subs


def demo(num_submissions: int = 24, arrival_rate_per_s: float = 3.0,
         submission_workers: int = 4, test_case_workers: int = 16) -> None:
    """
    Simulates submissions arriving as a Poisson process at
    `arrival_rate_per_s` (this is lambda from Little's Law) and judges
    them through the full pipeline described above.
    """
    print("=" * 72)
    print("ONLINE JUDGE — LIVE DEMO")
    print(f"  submissions to send : {num_submissions}")
    print(f"  arrival rate (lambda): {arrival_rate_per_s:.2f} submissions/sec")
    print(f"  submission workers (c): {submission_workers}")
    print(f"  test-case workers      : {test_case_workers}")
    print("=" * 72)

    system = OnlineJudgeSystem(submission_workers=submission_workers,
                                test_case_workers=test_case_workers)
    submissions = generate_submissions(num_submissions)

    def producer():
        for s in submissions:
            s.submitted_time = time.time()
            system.submit(s)
            # Poisson arrivals: inter-arrival time ~ Exponential(lambda)
            time.sleep(random.expovariate(arrival_rate_per_s))

    start = time.time()
    prod_thread = threading.Thread(target=producer, daemon=True)
    prod_thread.start()
    prod_thread.join()

    system.run_for(seconds=0.5)
    elapsed = time.time() - start

    verdicts = Counter(r.verdict for r in system.results())
    print("\nFINAL VERDICTS (per submission):")
    for v in Verdict:
        print(f"  {v.value:>4}: {verdicts.get(v, 0)}")

    print("\nTEST-CASE STATISTICS:")
    print(system.stats.report())

    achieved_throughput = num_submissions / elapsed
    print("\nTHROUGHPUT:")
    print(f"  wall-clock time     : {elapsed:.2f} s")
    print(f"  achieved throughput : {achieved_throughput:.2f} submissions/sec "
          f"({achieved_throughput * 60:.0f}/min)")
    print("=" * 72)


if __name__ == "__main__":
    demo()
