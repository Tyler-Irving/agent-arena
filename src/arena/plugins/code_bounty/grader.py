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

IMAGE = "arena-grader"
TIMEOUT_S = 30
MAX_OUTPUT_CHARS = 4000

# pytest's -q summary line, e.g. "1 failed, 5 passed in 0.04s"
_SUMMARY_COUNT = re.compile(r"(\d+) (passed|failed|errors?)\b")


class GraderError(RuntimeError):
    """The sandbox itself failed (Docker missing, image not built, ...), not the solution."""


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
        return await _run(workdir, timeout_s)
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

    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        timed_out = True
        # Killing the docker CLI would leave the container running; kill the container.
        killer = await asyncio.create_subprocess_exec(
            "docker", "kill", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        await killer.wait()
        out, err = await proc.communicate()
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
