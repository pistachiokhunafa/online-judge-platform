import os
import sys
import time

import pytest

from engine.models import TestCase, Verdict
from engine.sandbox import OUTPUT_LIMIT_BYTES, docker_command, run_test_case

POSIX = os.name == "posix"
TEST = TestCase("2 3\n", "5", time_limit_s=1.0, memory_limit_mb=128)


def is_alive(pid):
    """True if `pid` is still running (a zombie waiting to be reaped counts as dead)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return sys.platform != "linux"


def run(code):
    return run_test_case(code, TEST)


def test_correct_answer_is_accepted():
    assert run("a, b = map(int, input().split())\nprint(a + b)").verdict == Verdict.ACCEPTED


def test_wrong_answer_does_not_leak_expected_output():
    outcome = run("print(4)")
    assert outcome.verdict == Verdict.WRONG_ANSWER
    assert "5" not in outcome.detail


def test_runtime_error_reports_traceback_without_judge_paths():
    outcome = run("raise ValueError('boom')")
    assert outcome.verdict == Verdict.RUNTIME_ERROR
    assert "ValueError: boom" in outcome.detail
    assert "oj-" not in outcome.detail  # temp dir name is hidden


def test_infinite_loop_is_time_limit_exceeded():
    started = time.perf_counter()
    assert run("while True:\n    pass").verdict == Verdict.TIME_LIMIT_EXCEEDED
    assert time.perf_counter() - started < 3


def test_sleeping_is_time_limit_exceeded():
    assert run("import time\ntime.sleep(10)").verdict == Verdict.TIME_LIMIT_EXCEEDED


@pytest.mark.skipif(not POSIX, reason="process groups are POSIX-only")
def test_background_processes_are_killed(tmp_path):
    """A submission must not be able to outlive its time limit by forking."""
    pid_file = tmp_path / "child.pid"
    code = (
        "import os, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(60)\n"
        "else:\n"
        f"    open({str(pid_file)!r}, 'w').write(str(pid))\n"
        "    time.sleep(60)\n"
    )
    assert run(code).verdict == Verdict.TIME_LIMIT_EXCEEDED
    child = int(pid_file.read_text())
    time.sleep(0.2)
    assert not is_alive(child)


@pytest.mark.skipif(not POSIX, reason="process groups are POSIX-only")
def test_background_processes_are_killed_even_after_normal_exit(tmp_path):
    pid_file = tmp_path / "child.pid"
    code = (
        "import os, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(60)\n"
        "    os._exit(0)\n"
        f"open({str(pid_file)!r}, 'w').write(str(pid))\n"
        "print(5)\n"
    )
    assert run(code).verdict == Verdict.ACCEPTED
    child = int(pid_file.read_text())
    time.sleep(0.2)
    assert not is_alive(child)


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is only enforced on Linux")
def test_huge_allocation_is_memory_limit_exceeded():
    assert run("x = bytearray(1024 * 1024 * 1024)").verdict == Verdict.MEMORY_LIMIT_EXCEEDED


@pytest.mark.skipif(not POSIX, reason="output cap uses RLIMIT_FSIZE")
def test_flooding_stdout_hits_output_limit():
    outcome = run("import sys\nwhile True:\n    sys.stdout.write('x' * 65536)")
    assert outcome.verdict == Verdict.RUNTIME_ERROR
    assert "output limit" in outcome.detail


def test_environment_variables_are_not_inherited(monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "hunter2")
    code = "import os\nprint('LEAK' if 'SUPER_SECRET_TOKEN' in os.environ else 5)"
    assert run(code).verdict == Verdict.ACCEPTED


def test_runs_in_a_private_temp_directory():
    code = "import os\nprint(5 if sorted(os.listdir('.')) == ['solution.py'] else os.getcwd())"
    assert run(code).verdict == Verdict.ACCEPTED


def test_temp_directory_is_deleted_afterwards():
    outcome = run("import os, sys\nopen('marker', 'w').write('x')\nsys.exit(os.getcwd())")
    workdir = outcome.detail.strip()
    assert os.path.isabs(workdir)
    assert not os.path.exists(workdir)


def test_docker_command_is_locked_down():
    cmd = docker_command("oj-test", "/tmp/work", TEST)
    joined = " ".join(cmd)
    for flag in ["--network none", "--read-only", "--cap-drop ALL",
                 "--security-opt no-new-privileges", "--pids-limit", "--user 65534:65534",
                 "--memory 128m", "--memory-swap 128m", "/tmp/work:/sandbox:ro",
                 f"fsize={OUTPUT_LIMIT_BYTES}"]:
        assert flag in joined
