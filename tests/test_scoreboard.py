from pathlib import Path

import pytest

from arena.core.runlog import RunLog
from arena.core.scoreboard import format_scoreboard, scoreboard


def _trial(
    log: RunLog, run_id: int, problem: str, *, passed: bool, attempts: int, cost: float | None
) -> None:
    for _ in range(attempts):
        log.log_call(
            run_id=run_id,
            problem_id=problem,
            role="solo",
            model="m",
            think=None,
            system="",
            messages=[],
            input_tokens=100,
            output_tokens=40,
            reasoning_tokens=10,
        )
    log.log_result(
        run_id=run_id,
        problem_id=problem,
        passed=passed,
        attempts=attempts,
        total_calls=attempts,
        total_cost_usd=cost,
        wall_time_ms=1000 * attempts,
    )


@pytest.fixture
def log(tmp_path: Path) -> RunLog:
    log = RunLog(tmp_path / "arena.db")
    # cheap lineup, two runs: 3 of 4 trials pass
    for _ in range(2):
        run = log.start_run("cheap", "code_bounty")
        _trial(log, run, "001", passed=True, attempts=1, cost=0.01)
        _trial(log, run, "002", passed=False, attempts=3, cost=0.03)
        log.finish_run(run)
    log.db.execute("UPDATE results SET passed = 1 WHERE id = 4")  # second run solved 002
    # local lineup: everything passes, free
    run = log.start_run("local", "code_bounty")
    _trial(log, run, "001", passed=True, attempts=1, cost=0.0)
    _trial(log, run, "002", passed=True, attempts=1, cost=0.0)
    log.finish_run(run)
    # unpriced lineup
    run = log.start_run("unpriced", "code_bounty")
    _trial(log, run, "001", passed=True, attempts=1, cost=None)
    _trial(log, run, "002", passed=True, attempts=1, cost=0.02)
    log.finish_run(run)
    # smoke runs are not part of the scoreboard
    log.start_run("smoke", "smoke")
    log.db.commit()
    return log


def test_aggregates_per_lineup(log: RunLog) -> None:
    rows = {r.lineup: r for r in scoreboard(log)}
    assert set(rows) == {"cheap", "local", "unpriced"}

    cheap = rows["cheap"]
    assert (cheap.runs, cheap.trials, cheap.passed) == (2, 4, 3)
    assert cheap.pass_rate == 0.75
    assert cheap.avg_attempts == 2.0
    # tokens summed over each trial's calls, then averaged per trial
    assert (
        cheap.avg_input_tokens == 200 and cheap.avg_output_tokens == 80 and cheap.avg_reasoning_tokens == 20
    )
    assert cheap.total_cost_usd == pytest.approx(0.08)
    assert cheap.avg_cost_usd == pytest.approx(0.02)
    assert cheap.avg_wall_time_ms == 2000


def test_unknown_cost_stays_unknown(log: RunLog) -> None:
    rows = {r.lineup: r for r in scoreboard(log)}
    assert rows["unpriced"].total_cost_usd is None
    assert rows["local"].total_cost_usd == 0.0


def test_order_pass_rate_then_cost(log: RunLog) -> None:
    # local and unpriced both pass 100%; known-free beats unknown cost
    assert [r.lineup for r in scoreboard(log)] == ["local", "unpriced", "cheap"]


def test_format(log: RunLog) -> None:
    text = format_scoreboard(scoreboard(log))
    lines = text.splitlines()
    assert lines[0].split()[:4] == ["lineup", "runs", "passed", "pass"]
    assert "3/4" in text and "75%" in text and "$0.0200" in text
    unpriced = next(line for line in lines if line.startswith("unpriced"))
    assert unpriced.split()[-3:-1] == ["?", "?"]


def test_per_run_range(log: RunLog) -> None:
    rows = {r.lineup: r for r in scoreboard(log)}
    assert (rows["cheap"].run_low, rows["cheap"].run_high) == (0.5, 1.0)
    text = format_scoreboard(scoreboard(log))
    cheap = next(line for line in text.splitlines() if line.startswith("cheap"))
    local = next(line for line in text.splitlines() if line.startswith("local"))
    assert "50%-100%" in cheap.split()
    assert local.split()[3:5] == ["100%", "100%"]  # one run: no range


def test_since_leaves_out_earlier_runs(log: RunLog) -> None:
    # runs 1-2 are cheap, 3 is local, 4 is unpriced
    rows = {r.lineup: r for r in scoreboard(log, since=2)}
    assert set(rows) == {"cheap", "local", "unpriced"}
    cheap = rows["cheap"]
    assert (cheap.runs, cheap.passed, cheap.run_low, cheap.run_high) == (1, 2, 1.0, 1.0)
    assert [r.lineup for r in scoreboard(log, since=4)] == ["unpriced"]
    assert scoreboard(log, since=99) == []


def test_range_leaves_out_partial_runs(log: RunLog) -> None:
    # a single-problem run, an interrupted run and one still going: each still
    # counts as trials, but none of them is a full run to compare
    single = log.start_run("local", "code_bounty", problem="002")
    _trial(log, single, "002", passed=False, attempts=3, cost=0.0)
    log.finish_run(single)
    stopped = log.start_run("local", "code_bounty")
    _trial(log, stopped, "001", passed=False, attempts=3, cost=0.0)
    log.finish_run(stopped, interrupted=True)
    going = log.start_run("local", "code_bounty")
    _trial(log, going, "001", passed=False, attempts=3, cost=0.0)

    local = {r.lineup: r for r in scoreboard(log)}["local"]
    assert (local.runs, local.trials, local.passed) == (4, 5, 2)
    assert (local.run_low, local.run_high) == (1.0, 1.0)
    # a lineup with no full run shows no range
    assert {r.lineup: r for r in scoreboard(log, since=single)}["local"].run_low is None
    assert "  -  " in format_scoreboard(scoreboard(log, since=single))
