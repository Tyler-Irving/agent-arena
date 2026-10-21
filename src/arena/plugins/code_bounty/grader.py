"""Grade a solution by running the problem's tests in a locked-down container.

Model-written code never runs on the host. Each grade gets a fresh
`arena-grader` container with no network, capped memory/CPU/processes, a
read-only filesystem, and the solution + tests mounted read-only.
"""

import asyncio
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from pydantic import BaseModel

from arena.core.plugin import PluginError

IMAGE = "arena-grader"
TIMEOUT_S = 30
MAX_OUTPUT_CHARS = 4000

# Docker Desktop (seen on 29.2.1 under WSL) sometimes misses a container's exit:
# pytest finishes and its process is gone, but docker keeps reporting the
# container as running and `docker run` never returns. Healthy grades finish in
# 1-2 s, so after STALL_CHECK_S we start checking whether the container still
# has any processes. If not, it is stalled: kill it and grade again.
STALL_CHECK_S = 5.0
STALL_POLL_S = 2.0
STALL_RETRIES = 2
# Upper bound on the short docker commands the grader runs alongside a grade.
DOCKER_CMD_TIMEOUT_S = 10.0

# pytest's -q summary line, e.g. "1 failed, 5 passed in 0.04s"
_SUMMARY_COUNT = re.compile(r"(\d+) (passed|failed|errors?)\b")


class GraderError(PluginError):
    """The sandbox itself failed (Docker missing, image not built, ...), not the solution."""

    what = "grader"


class _Stalled(Exception):
    """Docker still reports the container running, but nothing is running in it."""


class GradeResult(BaseModel):
    passed: bool
    tests_passed: int
    tests_total: int  # passed + failed + errors; a collection error counts as 1
    timed_out: bool = False
    exit_code: int | None  # pytest's exit code; 137 means killed (likely out of memory)
    stdout: str
    stderr: str
    duration_ms: int


_image_ready = False


async def check_image() -> None:
    """Fail fast if Docker is down or the image isn't built.

    Without this, a stopped Docker Desktop looks like a failing solution: the
    WSL docker stub exits 1, the same code pytest uses for failed tests.
    """
    global _image_ready
    if _image_ready:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", IMAGE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
    except FileNotFoundError as e:
        raise GraderError("docker CLI not found") from e
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise GraderError(
            f"{IMAGE} image unavailable (is Docker Desktop running? built with "
            f"`docker build -t {IMAGE} docker/{IMAGE}`?): {err.decode(errors='replace').strip()}"
        )
    _image_ready = True


async def grade(solution: str, problem_dir: Path, timeout_s: float = TIMEOUT_S) -> GradeResult:
    """Run problem_dir/test_solution.py against `solution` source in the sandbox."""
    await check_image()
    workdir = Path(tempfile.mkdtemp(prefix="arena-grade-"))
    try:
        (workdir / "solution.py").write_text(solution)
        shutil.copy(problem_dir / "test_solution.py", workdir / "test_solution.py")
        # The container runs as uid 10001, so it needs world-read on the mount.
        workdir.chmod(0o755)
        for f in workdir.iterdir():
            f.chmod(0o644)
        for _ in range(STALL_RETRIES + 1):
            try:
                return await _run(workdir, timeout_s)
            except _Stalled:
                continue
        raise GraderError(f"docker lost track of the container's exit {STALL_RETRIES + 1} times in a row")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _run(workdir: Path, timeout_s: float) -> GradeResult:
    name = f"arena-grade-{uuid.uuid4().hex[:12]}"
    cmd = [
        "docker", "run", "--rm", "--name", name,
        "--pull", "never",
        "--network", "none",
        "--memory", "512m", "--memory-swap", "512m",
        "--cpus", "1",
        "--pids-limit", "128",
        "--read-only", "--tmpfs", "/tmp:size=64m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "-v", f"{workdir}:/work:ro",
        IMAGE,
        "python", "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider",
    ]  # fmt: skip

    start = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as e:
        raise GraderError("docker CLI not found") from e

    output = asyncio.ensure_future(proc.communicate())
    deadline = start + timeout_s
    check_at = start + STALL_CHECK_S
    timed_out = stalled = False
    try:
        while True:
            wait_s = min(deadline, check_at) - time.perf_counter()
            if wait_s > 0:
                await asyncio.wait({output}, timeout=wait_s)
            if output.done():
                break
            if time.perf_counter() >= deadline:
                timed_out = True
                break
            # Keep the check from pushing the grade past its deadline.
            check_timeout = min(DOCKER_CMD_TIMEOUT_S, max(1.0, deadline - time.perf_counter()))
            # The grade may finish while the check runs; then it isn't stalled.
            if await _is_stalled(name, check_timeout) and not output.done():
                stalled = True
                break
            check_at = time.perf_counter() + STALL_POLL_S
    finally:
        # Timed out, stalled, or cancelled: don't leave the container running.
        if not output.done():
            await _stop(name, proc, output)
    out, err = output.result()
    if stalled:
        raise _Stalled
    duration_ms = int((time.perf_counter() - start) * 1000)

    stdout, stderr = out.decode(errors="replace"), err.decode(errors="replace")
    # 125-127 come from docker itself (daemon error, missing image) when the
    # container never started. The solution can exit with those codes too, but
    # then pytest has already written to stdout and docker has not complained.
    if not timed_out and proc.returncode in (125, 126, 127) and not stdout and "docker" in stderr:
        raise GraderError(f"docker run failed ({proc.returncode}): {stderr.strip()}")

    tests_passed, tests_total = parse_summary(stdout)
    return GradeResult(
        # Exit 0 alone isn't enough: a solution that calls sys.exit(0) on import
        # ends pytest before any test runs.
        passed=not timed_out and proc.returncode == 0 and tests_total > 0 and tests_passed == tests_total,
        tests_passed=tests_passed,
        tests_total=tests_total,
        timed_out=timed_out,
        exit_code=None if timed_out else proc.returncode,
        stdout=truncate(stdout),
        stderr=truncate(stderr),
        duration_ms=duration_ms,
    )


async def _docker(*args: str, timeout: float) -> tuple[int | None, str]:
    """Run a short docker command. Returns (exit code, stdout); None on timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except FileNotFoundError as e:
        raise GraderError("docker CLI not found") from e
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None, ""
    return proc.returncode, out.decode(errors="replace")


async def _is_stalled(name: str, timeout: float) -> bool:
    """Whether docker has lost track of a container whose `docker run` hasn't returned.

    Stalled if the container is gone, has exited, or is "running" with no
    processes. A container still being created, or a slow docker, is not.
    (A grade ending at the exact moment of a check can look stalled; that
    only costs a re-run.)
    """
    code, status = await _docker("inspect", "--format", "{{.State.Status}}", name, timeout=timeout)
    if code is None:
        return False
    if code != 0:  # no such container
        return True
    status = status.strip()
    if status == "created":
        return False
    if status != "running":  # exited, dead, removing
        return True
    code, top = await _docker("top", name, timeout=timeout)
    return code == 0 and len(top.strip().splitlines()) <= 1  # only the header line


async def _stop(name: str, proc: asyncio.subprocess.Process, output: asyncio.Future) -> None:
    """Kill the container, and make sure the `docker run` attached to it exits too."""
    # Killing only the docker CLI would leave the container running.
    await _docker("kill", name, timeout=DOCKER_CMD_TIMEOUT_S)
    await asyncio.wait({output}, timeout=DOCKER_CMD_TIMEOUT_S)
    if not output.done():
        # The kill didn't unblock `docker run`, which a stall can cause.
        proc.kill()
        await output
        await _docker("rm", "-f", name, timeout=DOCKER_CMD_TIMEOUT_S)


def parse_summary(stdout: str) -> tuple[int, int]:
    """Return (passed, total) from pytest -q output; (0, 0) if there is no summary."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return 0, 0
    counts = {"passed": 0, "failed": 0, "error": 0}
    for n, kind in _SUMMARY_COUNT.findall(lines[-1]):
        counts["error" if kind.startswith("error") else kind] += int(n)
    return counts["passed"], sum(counts.values())


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the head and tail; pytest puts failure details first and the summary last."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} chars truncated] ...\n{text[-half:]}"
