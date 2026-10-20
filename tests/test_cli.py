"""CLI: repeated runs, and stopping a run cleanly on Ctrl-C or SIGTERM."""

import asyncio
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from arena import cli
from arena.core import orchestrator
from arena.core.lineup import Lineup
from arena.core.runlog import RunLog
from arena.core.taskboard import SqliteTaskBoard
from arena.models.router import ModelResponse
from arena.plugins.code_bounty.grader import GraderError, GradeResult
from arena.plugins.code_bounty.problems import load_problems


def _reply() -> ModelResponse:
    return ModelResponse(
        text="```python\ngood\n```",
        input_tokens=10,
        output_tokens=5,
        latency_ms=1,
        model="fake/model",
        cost_usd=0.0,
    )


def _pass() -> GradeResult:
    return GradeResult(
        passed=True, tests_passed=1, tests_total=1, exit_code=0, stdout="", stderr="", duration_ms=1
    )


def test_repeat_makes_one_run_each(monkeypatch, tmp_path, capsys) -> None:
    async def fake_complete(model, system, messages, **kwargs):
        return _reply()

    async def fake_grade(code, problem_dir):
        return _pass()

    log = RunLog(tmp_path / "arena.db")
    monkeypatch.setattr(orchestrator, "complete", fake_complete)
    monkeypatch.setattr(orchestrator, "grade", fake_grade)
    monkeypatch.setattr(cli, "get_runlog", lambda: log)
    monkeypatch.setattr(cli, "SqliteTaskBoard", lambda: SqliteTaskBoard(tmp_path / "arena.db"))

    assert asyncio.run(cli.run("haiku-solo", "001", repeat=3)) == 0

    runs = log.db.execute("SELECT lineup, finished_at, problem, interrupted FROM runs ORDER BY id").fetchall()
    assert [(r["lineup"], r["finished_at"] is not None, r["problem"], r["interrupted"]) for r in runs] == [
        ("haiku-solo", True, "001", 0)
    ] * 3
    assert log.db.execute("SELECT COUNT(*) FROM results WHERE passed = 1").fetchone()[0] == 3
    out = capsys.readouterr().out
    assert "3 runs finished (1-3):" in out and "arena scoreboard --since 1" in out


def test_repeat_still_summarizes_finished_runs_when_grader_breaks(monkeypatch, tmp_path, capsys) -> None:
    grades = [_pass(), _pass(), GraderError("docker went away")]

    async def fake_complete(model, system, messages, **kwargs):
        return _reply()

    async def fake_grade(code, problem_dir):
        grade = grades.pop(0)
        if isinstance(grade, Exception):
            raise grade
        return grade

    log = RunLog(tmp_path / "arena.db")
    monkeypatch.setattr(orchestrator, "complete", fake_complete)
    monkeypatch.setattr(orchestrator, "grade", fake_grade)
    monkeypatch.setattr(cli, "get_runlog", lambda: log)
    monkeypatch.setattr(cli, "SqliteTaskBoard", lambda: SqliteTaskBoard(tmp_path / "arena.db"))

    assert asyncio.run(cli.run("haiku-solo", "001", repeat=5)) == 2
    out = capsys.readouterr().out
    assert "2 of 5 runs finished (1-2):" in out and "arena scoreboard --since 1" in out


def test_repeat_must_be_positive() -> None:
    with pytest.raises(Exception, match="at least 1"):
        cli.positive_int("0")


def test_cancelled_run_marks_task_interrupted(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        calling = asyncio.Event()

        async def hang(model, system, messages, **kwargs):
            calling.set()
            await asyncio.sleep(60)

        monkeypatch.setattr(orchestrator, "complete", hang)
        lineup = Lineup.model_validate({"name": "fake", "mode": "solo", "roles": {"solo": "fake/model"}})
        run = asyncio.create_task(orchestrator.run_lineup(lineup, load_problems()[:2], board, log))
        await calling.wait()
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

    log = RunLog(tmp_path / "arena.db")
    board = SqliteTaskBoard(tmp_path / "arena.db")
    asyncio.run(scenario())

    tasks = board.db.execute("SELECT problem_id, status, reason FROM tasks ORDER BY id").fetchall()
    # the problem in progress is failed, the one never started stays pending
    assert [tuple(t) for t in tasks] == [
        ("001-two-sum", "failed", "interrupted"),
        ("002-merge-intervals", "pending", None),
    ]
    assert tuple(log.db.execute("SELECT finished_at IS NOT NULL, interrupted FROM runs").fetchone()) == (1, 1)
    assert log.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0


# Runs `arena run` in a child process. In "model" mode the model call never
# returns; in "grade" mode the grade never returns and its cleanup (like killing
# the sandbox container) takes a second.
HANGING_RUN = """
import asyncio, sys
from pathlib import Path
from arena import cli
from arena.core import orchestrator
from arena.core.runlog import RunLog
from arena.core.taskboard import SqliteTaskBoard
from arena.models.router import ModelResponse

db, mode = Path(sys.argv[1]), sys.argv[2]

async def hang(*args, **kwargs):
    print("waiting", flush=True)
    await asyncio.sleep(60)

async def reply(*args, **kwargs):
    return ModelResponse(
        text="```python\\nx\\n```", input_tokens=1, output_tokens=1, latency_ms=1, model="m", cost_usd=0.0
    )

async def slow_cleanup_grade(code, problem_dir):
    try:
        await hang()
    finally:
        await asyncio.sleep(1)
        print("cleaned up", flush=True)

if mode == "model":
    orchestrator.complete = hang
else:
    orchestrator.complete = reply
    orchestrator.grade = slow_cleanup_grade
cli.get_runlog = lambda: RunLog(db)
cli.SqliteTaskBoard = lambda: SqliteTaskBoard(db)
sys.argv = ["arena", "run", "--lineup", "haiku-solo", "--problem", "001"]
cli.main()
"""


def _start_hanging_run(db: Path, mode: str) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(HANGING_RUN), str(db), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    while "waiting" not in proc.stdout.readline():
        assert proc.poll() is None, proc.stderr.read() if proc.stderr else ""
    return proc


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_stops_run_cleanly(tmp_path: Path, sig: signal.Signals) -> None:
    db = tmp_path / "arena.db"
    proc = _start_hanging_run(db, "model")
    try:
        proc.send_signal(sig)
        _, err = proc.communicate(timeout=10)
    finally:
        proc.kill()

    assert proc.returncode == 128 + sig, err  # 143 for SIGTERM, 130 for Ctrl-C
    assert "interrupted" in err
    log = RunLog(db)
    run = log.db.execute("SELECT finished_at, interrupted, problem FROM runs").fetchone()
    assert run["finished_at"] is not None and (run["interrupted"], run["problem"]) == (1, "001")
    assert tuple(log.db.execute("SELECT status, reason FROM tasks").fetchone()) == ("failed", "interrupted")


def test_second_signal_does_not_cut_cleanup_short(tmp_path: Path) -> None:
    proc = _start_hanging_run(tmp_path / "arena.db", "grade")
    try:
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.2)  # inside the one-second cleanup
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=10)
    finally:
        proc.kill()

    assert "cleaned up" in out, err
    assert "already stopping" in err
    assert proc.returncode == 128 + signal.SIGTERM
