"""
scheduler.py — the priority queue that decides which waiting submission
gets judged next.

The goal, stated as a priority score:

    P(submission) = wait_time - priority_penalty

...process whichever submission currently has the HIGHEST P. A cheap
submission (small penalty) should jump ahead of an expensive one, but
even an expensive submission should eventually be judged if it's been
waiting long enough — that second guarantee is called "aging", and it's
what stops any submission from waiting forever.

The neat implementation trick: "now" is the same number for every
submission at the moment you compare them, so ranking by
(now - submitted_at - penalty) DESCENDING is the same as ranking by
(submitted_at + penalty) ASCENDING. That second form doesn't depend on
"now" at all, so we can compute it once, when the submission is queued,
and use Python's plain min-heap (heapq) with no extra bookkeeping. The
oldest, cheapest work naturally floats to the front; nothing needs to be
re-sorted as time passes.
"""

from __future__ import annotations

import heapq
import itertools
import threading
from typing import List, Optional, Tuple

from .models import Submission


class PriorityScheduler:
    """A thread-safe priority queue of Submissions, backed by a binary heap."""

    def __init__(self) -> None:
        self._heap: List[Tuple[float, int, Submission]] = []
        self._tie_breaker = itertools.count()  # keeps heapq happy when keys tie
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)

    def push(self, submission: Submission) -> None:
        """Add a submission to the queue. O(log n)."""
        key = submission.submitted_at + submission.priority_penalty_s
        with self._not_empty:
            heapq.heappush(self._heap, (key, next(self._tie_breaker), submission))
            self._not_empty.notify()

    def pop(self, timeout: Optional[float] = None) -> Optional[Submission]:
        """
        Remove and return the highest-priority submission. O(log n).
        Blocks up to `timeout` seconds if the queue is currently empty,
        then gives up and returns None so the caller can check whether
        it should keep waiting.
        """
        with self._not_empty:
            if not self._heap:
                self._not_empty.wait(timeout=timeout)
            if not self._heap:
                return None
            _, _, submission = heapq.heappop(self._heap)
            return submission

    def waiting_submissions(self) -> List[Submission]:
        """A snapshot of everything currently queued, in priority order.
        Used by the dashboard — does not remove anything from the queue."""
        with self._lock:
            ordered = sorted(self._heap, key=lambda entry: entry[0])
            return [submission for _, _, submission in ordered]

    def depth(self) -> int:
        with self._lock:
            return len(self._heap)
