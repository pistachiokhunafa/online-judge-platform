"""
sandbox.py — actually runs a submission's code and reports back exactly
what happened. This is the only file that touches the operating system
directly (processes, timeouts, resource limits).

Two backends, picked with the JUDGE_SANDBOX environment variable:

  "local"  (default) — a hardened child process on this machine:
      * runs in a fresh private temp directory (mode 0700) that is
        deleted afterwards, with an isolated interpreter (`python -I -S`)
        and an almost-empty environment (no inherited secrets/env vars);
      * gets its own process group, and the WHOLE group is killed when
        the test finishes or times out — so a submission can't escape
        the time limit by spawning background processes;
      * runs under rlimits: address space (memory), CPU seconds, max
        output-file size, open files, and no core dumps;
      * stdout/stderr go to capped files, not unbounded pipes, so a
        submission can't exhaust the judge's memory by printing forever.

      This limits resource abuse, but it is NOT full isolation: the code
      still runs as your user and can read your files and use the
      network. Only use "local" on your own machine.

  "docker" — each test runs in a throwaway container with no network,
      a read-only filesystem, a memory cap, a process (pids) cap, all
      Linux capabilities dropped, and an unprivileged user. Use this
      whenever you don't fully trust the people submitting code.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import List, Optional, Tuple

from .models import TestCase, TestOutcome, Verdict

try:
    import resource  # POSIX only
    HAVE_RESOURCE = True
except ImportError:  # e.g. Windows
    HAVE_RESOURCE = False

IS_POSIX = os.name == "posix"

# Hard caps that apply to every test case, regardless of the problem.
OUTPUT_LIMIT_BYTES = 1 * 1024 * 1024   # 1 MB of stdout (and of stderr)
MAX_OPEN_FILES = 64
STDERR_TAIL_CHARS = 300

DOCKER_IMAGE = os.environ.get("JUDGE_DOCKER_IMAGE", "python:3.12-slim")
DOCKER_PIDS_LIMIT = 64
# Container start-up time is not the submission's fault, so docker runs
# get this much extra wall-clock time before being declared TLE.
DOCKER_STARTUP_GRACE_S = 2.0


def sandbox_mode() -> str:
    mode = os.environ.get("JUDGE_SANDBOX", "local").strip().lower()
    if mode not in ("local", "docker"):
        raise ValueError(f"JUDGE_SANDBOX must be 'local' or 'docker', got {mode!r}")
    return mode


# ---------------------------------------------------------------- helpers --
def _minimal_env() -> dict:
    """The child gets no inherited environment (API keys, tokens, etc.)."""
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}
    if not IS_POSIX:  # Windows needs SYSTEMROOT for Python to start at all
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    return env


def _set_limit(which: int, value: int) -> None:
    """Lower a resource limit, never raising it above the existing hard cap
    (raising a hard limit isn't allowed for normal users)."""
    try:
        _, hard = resource.getrlimit(which)
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        resource.setrlimit(which, (value, value))
    except (ValueError, OSError):
        pass  # some platforms (e.g. macOS for RLIMIT_AS) refuse certain limits


def _local_limits(test: TestCase):
    """Runs INSIDE the child, after fork and before exec."""
    def apply() -> None:
        _set_limit(resource.RLIMIT_AS, test.memory_limit_mb * 1024 * 1024)
        _set_limit(resource.RLIMIT_CPU, int(test.time_limit_s) + 1)
        _set_limit(resource.RLIMIT_FSIZE, OUTPUT_LIMIT_BYTES)
        _set_limit(resource.RLIMIT_NOFILE, MAX_OPEN_FILES)
        _set_limit(resource.RLIMIT_CORE, 0)
    return apply


def _kill_process_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _read_capped(path: str) -> str:
    with open(path, "rb") as f:
        data = f.read(OUTPUT_LIMIT_BYTES + 1)
    return data.decode("utf-8", errors="replace")


def _max_rss_kb(rusage) -> int:
    # Linux reports ru_maxrss in KB, macOS in bytes.
    rss = rusage.ru_maxrss
    return int(rss // 1024) if sys.platform == "darwin" else int(rss)


# ------------------------------------------------------------- execution --
def _run_posix(cmd: List[str], workdir: str, stdin_path: str, stdout_path: str,
               stderr_path: str, timeout_s: float, preexec) -> Tuple[Optional[int], int, bool]:
    """
    Runs `cmd` in its own session/process group. Returns
    (exit_code_or_negative_signal, peak_memory_kb, timed_out).

    os.wait4() gives the resource usage of exactly THIS child (unlike
    RUSAGE_CHILDREN, which mixes together every test running in parallel).
    """
    with open(stdin_path, "rb") as fin, open(stdout_path, "wb") as fout, \
            open(stderr_path, "wb") as ferr:
        proc = subprocess.Popen(
            cmd, cwd=workdir, stdin=fin, stdout=fout, stderr=ferr,
            env=_minimal_env(), start_new_session=True, preexec_fn=preexec,
            close_fds=True,
        )
    timed_out = threading.Event()

    def on_timeout() -> None:
        timed_out.set()
        _kill_process_group(proc.pid)

    timer = threading.Timer(timeout_s, on_timeout)
    timer.daemon = True
    timer.start()
    try:
        _, status, rusage = os.wait4(proc.pid, 0)
    finally:
        timer.cancel()
        # Always clean up anything the submission left running in the background.
        _kill_process_group(proc.pid)
    proc.returncode = os.waitstatus_to_exitcode(status)  # tell Popen it's reaped
    return proc.returncode, _max_rss_kb(rusage), timed_out.is_set()


def _run_portable(cmd: List[str], workdir: str, stdin_path: str, stdout_path: str,
                  stderr_path: str, timeout_s: float) -> Tuple[Optional[int], int, bool]:
    """Fallback for Windows: no process groups or rlimits available."""
    with open(stdin_path, "rb") as fin, open(stdout_path, "wb") as fout, \
            open(stderr_path, "wb") as ferr:
        proc = subprocess.Popen(cmd, cwd=workdir, stdin=fin, stdout=fout,
                                stderr=ferr, env=_minimal_env())
        try:
            return proc.wait(timeout=timeout_s), 0, False
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return proc.returncode, 0, True


def docker_command(container_name: str, workdir: str, test: TestCase) -> List[str]:
    """The exact `docker run` used for one test (kept separate so it can be unit-tested)."""
    mem = f"{test.memory_limit_mb}m"
    return [
        "docker", "run", "--rm", "-i",
        "--name", container_name,
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=16m,noexec",
        "--memory", mem, "--memory-swap", mem,
        "--pids-limit", str(DOCKER_PIDS_LIMIT),
        "--cpus", "1",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", "65534:65534",
        "--ulimit", f"fsize={OUTPUT_LIMIT_BYTES}:{OUTPUT_LIMIT_BYTES}",
        "--ulimit", f"nofile={MAX_OPEN_FILES}:{MAX_OPEN_FILES}",
        "-v", f"{workdir}:/sandbox:ro",
        "-w", "/sandbox",
        DOCKER_IMAGE,
        "python", "-I", "-S", "solution.py",
    ]


def run_test_case(code: str, test: TestCase) -> TestOutcome:
    """Executes `code` against one TestCase and returns a graded TestOutcome."""
    mode = sandbox_mode()
    workdir = tempfile.mkdtemp(prefix="oj-")  # created with mode 0700
    try:
        solution = os.path.join(workdir, "solution.py")
        with open(solution, "w", encoding="utf-8") as f:
            f.write(code)
        # Outputs live in a sibling dir the submission can't see in docker mode.
        io_dir = tempfile.mkdtemp(prefix="oj-io-")
        try:
            stdin_path = os.path.join(io_dir, "stdin")
            stdout_path = os.path.join(io_dir, "stdout")
            stderr_path = os.path.join(io_dir, "stderr")
            with open(stdin_path, "w", encoding="utf-8") as f:
                f.write(test.stdin)

            start = time.perf_counter()
            if mode == "docker":
                os.chmod(workdir, 0o755)
                os.chmod(solution, 0o644)
                name = f"oj-{uuid.uuid4().hex[:12]}"
                cmd = docker_command(name, workdir, test)
                try:
                    exit_code, memory_kb, timed_out = _run_portable(
                        cmd, workdir, stdin_path, stdout_path, stderr_path,
                        test.time_limit_s + DOCKER_STARTUP_GRACE_S)
                finally:
                    subprocess.run(["docker", "kill", name], capture_output=True)
            elif IS_POSIX and HAVE_RESOURCE:
                cmd = [sys.executable, "-I", "-S", "solution.py"]
                exit_code, memory_kb, timed_out = _run_posix(
                    cmd, workdir, stdin_path, stdout_path, stderr_path,
                    test.time_limit_s, _local_limits(test))
            else:
                cmd = [sys.executable, "-I", "-S", "solution.py"]
                exit_code, memory_kb, timed_out = _run_portable(
                    cmd, workdir, stdin_path, stdout_path, stderr_path, test.time_limit_s)
            runtime_ms = (time.perf_counter() - start) * 1000

            stdout = _read_capped(stdout_path)
            # Show "solution.py", not the judge's real temp path, in tracebacks.
            stderr = _read_capped(stderr_path).replace(workdir + os.sep, "")
            return _grade(test, exit_code, memory_kb, timed_out, runtime_ms,
                          stdout, stderr, mode)
        finally:
            shutil.rmtree(io_dir, ignore_errors=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _grade(test: TestCase, exit_code: Optional[int], memory_kb: int, timed_out: bool,
           runtime_ms: float, stdout: str, stderr: str, mode: str) -> TestOutcome:
    xcpu = getattr(signal, "SIGXCPU", None)
    xfsz = getattr(signal, "SIGXFSZ", None)

    if timed_out or (xcpu is not None and exit_code == -xcpu):
        return TestOutcome(Verdict.TIME_LIMIT_EXCEEDED, test.time_limit_s * 1000, memory_kb,
                           detail=f"exceeded the {test.time_limit_s:g}s time limit")

    # Python ignores SIGXFSZ, so hitting the file-size cap usually shows
    # up as an "EFBIG / File too large" OSError instead of a signal.
    output_capped = (len(stdout.encode("utf-8", errors="replace")) >= OUTPUT_LIMIT_BYTES
                     or "File too large" in stderr)
    if (xfsz is not None and exit_code == -xfsz) or output_capped:
        return TestOutcome(Verdict.RUNTIME_ERROR, runtime_ms, memory_kb,
                           detail="output limit exceeded (max 1 MB)")

    if exit_code != 0:
        oom_killed = mode == "docker" and exit_code == 137
        if "MemoryError" in stderr or oom_killed:
            return TestOutcome(Verdict.MEMORY_LIMIT_EXCEEDED, runtime_ms, memory_kb,
                               detail="allocation exceeded the memory limit")
        return TestOutcome(Verdict.RUNTIME_ERROR, runtime_ms, memory_kb,
                           detail=stderr.strip()[-STDERR_TAIL_CHARS:] or f"exit code {exit_code}")

    if stdout.strip() == test.expected_stdout.strip():
        return TestOutcome(Verdict.ACCEPTED, runtime_ms, memory_kb)

    # Never echo the expected output back: that would leak the hidden tests.
    return TestOutcome(Verdict.WRONG_ANSWER, runtime_ms, memory_kb,
                       detail="output does not match the expected output")
