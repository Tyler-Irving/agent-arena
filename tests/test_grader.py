import asyncio
import subprocess
from pathlib import Path

import pytest

from arena.plugins.code_bounty.grader import grade, parse_summary, truncate

TWO_SUM = Path(__file__).parent.parent / "problems" / "001-two-sum"

GOOD = """
def two_sum(nums, target):
    seen = {}
    for j, n in enumerate(nums):
        if target - n in seen:
            return (seen[target - n], j)
        seen[n] = j
"""

# Builds the index map up front, so it can pair an element with itself:
# two_sum([3, 2, 4], 6) returns (0, 0). Fails 1 of the 6 tests.
BAD = """
def two_sum(nums, target):
    index = {n: i for i, n in enumerate(nums)}
    for i, n in enumerate(nums):
        if target - n in index:
            return (i, index[target - n])
"""


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("....\n6 passed in 0.02s\n", (6, 6)),
        ("F.....\n1 failed, 5 passed in 0.03s\n", (5, 6)),
        ("E\n1 error in 0.10s\n", (0, 1)),
        ("2 failed, 1 passed, 3 errors in 1.2s", (1, 6)),
        ("", (0, 0)),
    ],
)
def test_parse_summary(stdout: str, expected: tuple[int, int]) -> None:
    assert parse_summary(stdout) == expected


def test_truncate_keeps_head_and_tail() -> None:
    text = "HEAD" + "x" * 10_000 + "TAIL"
    out = truncate(text, limit=100)
    assert out.startswith("HEAD") and out.endswith("TAIL") and "truncated" in out
    assert truncate("short", limit=100) == "short"


@pytest.mark.docker
def test_good_solution_passes() -> None:
    r = asyncio.run(grade(GOOD, TWO_SUM))
    assert r.passed, r.model_dump()
    assert (r.tests_passed, r.tests_total) == (6, 6)


@pytest.mark.docker
def test_bad_solution_fails() -> None:
    r = asyncio.run(grade(BAD, TWO_SUM))
    assert not r.passed
    assert (r.tests_passed, r.tests_total) == (5, 6)
    assert "test_does_not_reuse_element" in r.stdout


@pytest.mark.docker
def test_syntax_error_fails() -> None:
    r = asyncio.run(grade("def two_sum(nums, target)\n    return (0, 1)\n", TWO_SUM))
    assert not r.passed
    assert r.tests_passed == 0
    assert "SyntaxError" in r.stdout


@pytest.mark.docker
@pytest.mark.parametrize("code", [0, 126])
def test_solution_exiting_early_fails(code: int) -> None:
    # Ends pytest during collection, before any test runs.
    r = asyncio.run(grade(f"import os\nos._exit({code})\n", TWO_SUM))
    assert not r.passed
    assert r.tests_passed == 0


@pytest.mark.docker
def test_infinite_loop_times_out_and_container_is_removed() -> None:
    r = asyncio.run(grade("while True:\n    pass\n", TWO_SUM, timeout_s=3))
    assert r.timed_out and not r.passed
    leftover = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "name=arena-grade-"], capture_output=True, text=True
    )
    assert leftover.stdout.strip() == ""


SANDBOX_TESTS = """
import os
import socket

import pytest


def test_no_network():
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 53), timeout=2)


def test_mount_is_read_only():
    with pytest.raises(OSError):
        open("/work/escape.txt", "w")


def test_root_fs_is_read_only():
    with pytest.raises(OSError):
        open("/etc/escape.txt", "w")


def test_not_root():
    assert os.getuid() != 0
"""


@pytest.mark.docker
def test_sandbox_restrictions(tmp_path: Path) -> None:
    (tmp_path / "test_solution.py").write_text(SANDBOX_TESTS)
    r = asyncio.run(grade("", tmp_path))
    assert r.passed, r.stdout
    assert (r.tests_passed, r.tests_total) == (4, 4)
