# Online Judge — Scalable Code Evaluation & Judging Platform

A small, real, runnable web version of the Online Code Evaluation &amp;
Judging Platform project. Submitted Python code actually executes, in
its own subprocess, against real hidden test cases — this isn't a mock.
Open it in a browser, submit code, and watch it move through the exact
pipeline the project write-up describes: **queue → schedule → judge in
parallel → verdict → stats**.

## Running it

Requires Python 3.9+ and nothing else beyond Flask.

```bash
pip install -r requirements.txt
python3 run.py
```

Then open **http://127.0.0.1:5050** in Chrome (or any browser). Pick a
problem, write or edit the solution, hit **Submit**, and watch the
verdict, the test-by-test grid, the priority queue lane, and the live
stats panel update in real time.

Everything runs locally — no data leaves your machine, and nothing is
installed system-wide beyond the one `flask` package.

## Project layout

```
online_judge_web/
├── run.py                     entry point — `python3 run.py`
├── requirements.txt
├── examples/online_judge_cli.py   standalone command-line version (no web UI)
├── backend/
│   ├── app.py                 Flask routes (thin — no judging logic here)
│   └── engine/
│       ├── models.py          Submission, TestCase, Verdict — the "nouns"
│       ├── scheduler.py       the priority-queue heap + aging
│       ├── sandbox.py         runs submitted code in an isolated subprocess
│       ├── judge.py           fans test cases out, reduces results in
│       ├── stats.py           mean / stdev / P50 / P95 / P99
│       ├── problems.py        the 3 built-in problems + hidden tests
│       └── service.py         wires it all together + background workers
└── frontend/
    ├── index.html
    ├── style.css               plain CSS, no framework
    └── app.js                  fetch() + polling, no framework
```

Every file has one job. If you're trying to understand the system,
read them in this order: `models.py` → `scheduler.py` → `sandbox.py` →
`judge.py` → `service.py` → `app.py` → `app.js`. That's the same order
a submission actually flows through the system.

## How a submission moves through the system

1. **Browser → `POST /api/submissions`** — `app.py` hands the code to
   `JudgeService.submit()`, which wraps it in a `Submission` and pushes
   it onto the `PriorityScheduler` heap. The response comes back
   immediately with a `submission_id` — the browser doesn't wait around.
2. **Scheduling** — a small pool of background "submission worker"
   threads (`service.py`) continuously pop the highest-priority
   submission off the heap. See the big comment block at the top of
   `scheduler.py` for the trick that makes "older submissions get
   pushed to the front over time" work with a plain heap, no re-sorting.
3. **Judging** — `judge.py` fans the problem's hidden test cases out
   across a shared thread pool (parallel execution), then reduces the
   results back into one final verdict.
4. **Sandboxing** — each test case actually runs the submitted code in
   its own subprocess, with a wall-clock timeout and a memory ceiling
   (`sandbox.py`). This is the only file that talks to the OS directly.
5. **Stats** — every test-case result is recorded into `StatsTracker`
   (`stats.py`), which is what powers the "Live Stats" panel.
6. **Browser polling** — the frontend polls `GET /api/submissions/<id>`
   every 500ms until the verdict is final, and polls `GET /api/dashboard`
   every second for the queue lane, stats, and activity feed.

## Design choices worth knowing about

- **All tests run, even after a failure.** The original command-line
  version of this project (see `examples/online_judge_cli.py`) cancels
  remaining test cases the moment one fails ("fail-fast") to save
  compute. This web version deliberately runs every test case instead,
  so the UI can show a real "5 of 6 passed" grid — a small compute cost
  for a much clearer interface. Same engine, different trade-off for a
  different context.
- **The priority penalty is a simple heuristic.** `service.py` estimates
  a submission's cost from its code length, just to make the queue's
  ordering visible and meaningful in a demo. A real judge would build
  this estimate from historical execution data per user or problem.
- **Sandboxing is subprocess + a memory `rlimit`, not a container.**
  It's real isolation, but real judging platforms go further with
  Docker or micro-VMs so a submission can't see the filesystem or
  network at all. The scaled-down version here is meant to be readable
  top to bottom, not production-hardened.
- **Two thread pools, matching the two math sections in the write-up:**
  `submission_workers` (the "c" in the stability condition
  ρ = λ/(cμ)) and the shared `test_case_pool` (the "n" in Amdahl's Law).
- **The Flask dev server is fine for this demo, not for production.**
  It says so in its own startup warning — for real traffic you'd put a
  proper WSGI server (gunicorn, uWSGI) in front of it.

## A note on the sandbox

`sandbox.py`'s memory limit (`resource.setrlimit(RLIMIT_AS, ...)`) is
POSIX-only, so it won't work as-is on Windows — it'll just skip the
memory cap there and grade everything else normally. Submissions are
plain Python read-from-stdin / write-to-stdout programs, the same
interface real competitive-programming judges use. Adding a second
language would mean adding a new branch in `sandbox.py` that shells out
to that language's compiler/interpreter instead of `sys.executable` —
nothing else in the engine would need to change.
