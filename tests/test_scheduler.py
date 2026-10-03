import threading
import time

from engine.models import Submission
from engine.scheduler import PriorityScheduler


def make(penalty, submitted_at):
    s = Submission(code="print(1)", problem_id="sum-two-integers", priority_penalty_s=penalty)
    s.submitted_at = submitted_at
    return s


def test_cheaper_submission_jumps_ahead_of_expensive_one():
    sched = PriorityScheduler()
    expensive = make(penalty=2.0, submitted_at=100.0)
    cheap = make(penalty=0.1, submitted_at=100.5)   # arrived later but cheaper
    sched.push(expensive)
    sched.push(cheap)
    assert sched.pop() is cheap
    assert sched.pop() is expensive


def test_aging_old_expensive_submission_eventually_wins():
    sched = PriorityScheduler()
    old_expensive = make(penalty=2.0, submitted_at=100.0)  # key 102.0
    new_cheap = make(penalty=0.1, submitted_at=105.0)      # key 105.1
    sched.push(new_cheap)
    sched.push(old_expensive)
    assert sched.pop() is old_expensive


def test_ties_keep_insertion_order():
    sched = PriorityScheduler()
    first, second = make(0.5, 10.0), make(0.5, 10.0)
    sched.push(first)
    sched.push(second)
    assert [sched.pop(), sched.pop()] == [first, second]


def test_pop_on_empty_queue_times_out_with_none():
    started = time.perf_counter()
    assert PriorityScheduler().pop(timeout=0.1) is None
    assert time.perf_counter() - started >= 0.09


def test_pop_wakes_up_when_something_is_pushed():
    sched = PriorityScheduler()
    item = make(0.1, 1.0)
    threading.Timer(0.05, sched.push, args=(item,)).start()
    assert sched.pop(timeout=2) is item


def test_depth_and_snapshot_do_not_consume():
    sched = PriorityScheduler()
    a, b = make(1.0, 1.0), make(0.0, 1.0)
    sched.push(a)
    sched.push(b)
    assert sched.depth() == 2
    assert sched.waiting_submissions() == [b, a]
    assert sched.depth() == 2


def test_concurrent_pushes_and_pops_lose_nothing():
    sched = PriorityScheduler()
    items = [make(i % 3 * 0.1, float(i)) for i in range(200)]
    pushers = [threading.Thread(target=sched.push, args=(s,)) for s in items]
    for t in pushers:
        t.start()
    for t in pushers:
        t.join()
    popped = [sched.pop(timeout=1) for _ in items]
    assert sorted(id(s) for s in popped) == sorted(id(s) for s in items)
    assert sched.depth() == 0
