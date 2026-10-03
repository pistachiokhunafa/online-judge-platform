"""
sandbox.py — actually runs a submission's code, in its own process, and
reports back exactly what happened. This is the only file that touches
the operating system directly (subprocess, timeouts, memory limits), so
it's the file to read if you're curious how "run untrusted code safely"
really works under the hood.

The approach:
  1. Write the submitted code to a temporary .py file.
  2. Launch it as a brand-new child process, piping in the test's stdin.
  3. Give it a wall-clock deadline (`timeout=`) and a memory ceiling
     (`RLIMIT_AS`, POSIX only) *before* it starts running.
  4. Read back its stdout, its exit code, and how long it took.

Real production judges take this further with containers or micro-VMs
(Docker, gVisor, Firecracker) so a submission can't see the filesystem,
network, or other submissions at all. The subprocess + resource-limit
approach here is the same *idea*, scaled down to something you can read
top to bottom in one sitting.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

from .models import TestCase, TestOutcome, Verdict

try:
    import resource  # POSIX only
    HAVE_RESOURCE = True
except ImportError:                       # e.g. running on Windows
    HAVE_RESOURCE = False


def _memory_limiter(memory_limit_mb: int):
    """
    Returns a function that, when it runs INSIDE the new child process
    (via subprocess's `preexec_fn`), caps how much address space that
    process is allowed to request. Any allocation past this limit makes
    Python raise MemoryError in the child — which we detect afterwards
    by checking whether "MemoryError" showed up in its stderr.
    """
    def limiter() -> None:
        limit_bytes = memory_limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    return limiter


def run_test_case(code: str, test: TestCase) -> TestOutcome:
    """Executes `code` against one TestCase and returns a graded TestOutcome."""

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        source_path = f.name

    start = time.perf_counter()
    try:
        process = subprocess.run(
            [sys.executable, source_path],
            input=test.stdin,
            capture_output=True,
            text=True,
            timeout=test.time_limit_s,
            preexec_fn=_memory_limiter(test.memory_limit_mb) if HAVE_RESOURCE else None,
        )
        runtime_ms = (time.perf_counter() - start) * 1000

        # Peak memory of the just-finished child process, in KB (Linux only).
        memory_kb = (
            resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            if HAVE_RESOURCE else 0
        )

        if process.returncode != 0:
            if "MemoryError" in process.stderr:
                return TestOutcome(Verdict.MEMORY_LIMIT_EXCEEDED, runtime_ms, memory_kb,
                                    detail="allocation exceeded the memory limit")
            return TestOutcome(Verdict.RUNTIME_ERROR, runtime_ms, memory_kb,
                                detail=process.stderr.strip()[-300:])

        actual_output = process.stdout.strip()
        expected_output = test.expected_stdout.strip()
        if actual_output == expected_output:
            return TestOutcome(Verdict.ACCEPTED, runtime_ms, memory_kb)

        return TestOutcome(
            Verdict.WRONG_ANSWER, runtime_ms, memory_kb,
            detail=f"expected {expected_output!r}, got {actual_output!r}",
        )

    except subprocess.TimeoutExpired:
        return TestOutcome(Verdict.TIME_LIMIT_EXCEEDED, test.time_limit_s * 1000, 0,
                            detail=f"exceeded the {test.time_limit_s:g}s time limit")
    finally:
        os.unlink(source_path)
