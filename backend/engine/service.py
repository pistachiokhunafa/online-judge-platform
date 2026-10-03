"""
service.py — the single object the web layer talks to. Everything else
in `engine/` is a building block; this file assembles them into one
running system and hides the wiring behind four simple methods:

    service.submit(code, problem_id)   -> Submission
    service.get_submission(id)         -> Submission | None
    service.dashboard()                -> dict for the live stats panel
    service.start() / service.stop()   -> background worker lifecycle

This uses two pools: a small number of
`submission_workers` (each is the "c" in the stability condition
rho = lambda / (c*mu)) share one larger `test_case_pool` (the "n" in
Amdahl's Law) for the actual parallel test execution.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from typing import Deque, List, Optional

from .judge import judge
from .models import Submission
from .problems import get_problem, list_problems
from .scheduler import PriorityScheduler
from .stats import StatsTracker

# Abuse limits. A submission bigger than this is rejected before it is
# parsed or run; when the queue is this deep, new submissions are refused
# (back-pressure) instead of letting the backlog grow without bound; and
# only the most recent MAX_STORED_SUBMISSIONS are kept in memory.
MAX_CODE_BYTES = 64 * 1024
MAX_QUEUE_DEPTH = 100
MAX_STORED_SUBMISSIONS = 1000


class SubmissionRejected(Exception):
    """Raised when a submission is refused before it is queued."""

    def __init__(self, message: str, http_status: int) -> None:
        super().__init__(message)
        self.http_status = http_status


def _estimate_penalty_seconds(code: str) -> float:
    """
    A stand-in for a real `runtime_penalty(s)` model: a rough,
    cheap-to-compute guess at how expensive a submission will be, used
    only to order the queue — never to decide pass/fail. Real judges
    build this estimate from a user's or problem's execution history;
    here we simply assume longer code takes a little longer to judge,
    which is enough to make the priority queue's ordering visible and
    meaningful in the demo.
    """
    return min(2.0, len(code) / 500)


class JudgeService:
    def __init__(self, submission_workers: int = 3, test_case_workers: int = 8) -> None:
        self.scheduler = PriorityScheduler()
        self.stats = StatsTracker()

        self._submissions: "OrderedDict[int, Submission]" = OrderedDict()
        self._submissions_lock = threading.Lock()
        self._recent_ids: Deque[int] = deque(maxlen=25)

        self._test_case_pool = ThreadPoolExecutor(
            max_workers=test_case_workers, thread_name_prefix="test-case-worker"
        )
        self._submission_worker_count = submission_workers
        self._worker_threads: List[threading.Thread] = []
        self._running = False

    # ------------------------------------------------------------------ API
    def submit(self, code: str, problem_id: str) -> Submission:
        get_problem(problem_id)  # raises KeyError if the problem doesn't exist
        if len(code.encode("utf-8")) > MAX_CODE_BYTES:
            raise SubmissionRejected(
                f"code is larger than {MAX_CODE_BYTES // 1024} KB", http_status=413)
        if self.scheduler.depth() >= MAX_QUEUE_DEPTH:
            raise SubmissionRejected("judge queue is full, try again shortly", http_status=503)

        submission = Submission(
            code=code,
            problem_id=problem_id,
            priority_penalty_s=_estimate_penalty_seconds(code),
        )
        with self._submissions_lock:
            self._submissions[submission.submission_id] = submission
            self._recent_ids.append(submission.submission_id)
            self._evict_old_submissions()

        self.scheduler.push(submission)
        return submission

    def _evict_old_submissions(self) -> None:
        """Forget the oldest *finished* submissions once over the cap, so
        memory use stays bounded no matter how long the server runs.
        Caller must hold _submissions_lock."""
        excess = len(self._submissions) - MAX_STORED_SUBMISSIONS
        if excess <= 0:
            return
        for sid in [sid for sid, s in self._submissions.items()
                    if s.finished_at is not None][:excess]:
            del self._submissions[sid]

    def get_submission(self, submission_id: int) -> Optional[Submission]:
        with self._submissions_lock:
            return self._submissions.get(submission_id)

    def dashboard(self) -> dict:
        """Everything the live dashboard panel needs, in one call."""
        with self._submissions_lock:
            recent = [self._submissions[i] for i in self._recent_ids if i in self._submissions]

        queue_snapshot = [
            {
                "submission_id": s.submission_id,
                "problem_id": s.problem_id,
                "waiting_s": round(time.time() - s.submitted_at, 1),
                "priority_penalty_s": s.priority_penalty_s,
            }
            for s in self.scheduler.waiting_submissions()
        ]

        return {
            "queue_depth": self.scheduler.depth(),
            "submission_workers": self._submission_worker_count,
            "test_case_workers": self._test_case_pool._max_workers,
            "queue": queue_snapshot,
            "stats": self.stats.snapshot(),
            "recent_submissions": [
                s.to_dict(tests_total=len(get_problem(s.problem_id).hidden_tests))
                for s in reversed(recent)
            ],
            "problems": [
                {"problem_id": p.problem_id, "title": p.title} for p in list_problems()
            ],
        }

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Boots the background workers that continuously drain the queue."""
        if self._running:
            return
        self._running = True
        for i in range(self._submission_worker_count):
            thread = threading.Thread(
                target=self._worker_loop, name=f"submission-worker-{i}", daemon=True
            )
            thread.start()
            self._worker_threads.append(thread)

    def stop(self) -> None:
        self._running = False

    def _worker_loop(self) -> None:
        """
        One "submission worker": forever, pull the next highest-priority
        submission off the heap and judge it. Several of these run at
        once (see `submission_workers`), which is what lets multiple
        submissions be *in judging* at the same time.
        """
        while self._running:
            submission = self.scheduler.pop(timeout=0.5)
            if submission is None:
                continue  # nothing waiting right now — loop and check again
            judge(submission, self._test_case_pool)
            for outcome in submission.test_outcomes:
                self.stats.record(outcome)
