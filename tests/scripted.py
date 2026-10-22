"""Scripted runs (fake models, fake grader) shared by the events and watch tests."""

import asyncio
import re
from pathlib import Path

import pytest

from arena.core import orchestrator
from arena.core import plugin as core_plugin
from arena.core.events import Event, read_events
from arena.core.lineup import Lineup
from arena.core.runlog import RunLog
from arena.core.taskboard import SqliteTaskBoard
from arena.models.router import ModelResponse
from arena.plugins import get_plugin
from arena.plugins.code_bounty import loop
from arena.plugins.code_bounty.grader import GradeResult
from arena.plugins.code_bounty.problems import load_problems

CODE_BOUNTY = get_plugin("code_bounty")
MAFIA = get_plugin("mafia")
TEAM = {"orchestrator": "fake/orch", "planner": "fake/plan", "coder": "fake/code", "reviewer": "fake/review"}
MAFIA_ROLES = {
    "narrator": "fake/narrator",
    "mafia": "fake/mafia",
    "detective": "fake/det",
    "villager": "fake/vil",
}

# game-01 seats: Finley (villager), Blake (mafia), Casey (villager), Gray (villager),
# Drew (villager), Avery (detective), Emery (mafia)
TOWN_WINS = {
    "kills": {1: "Finley", 2: "Casey"},
    "votes": {1: "Blake", 2: "Emery"},
    "checks": {1: "Blake", 2: "Emery"},
}


def reply(text: str, cost: float | None = 0.01) -> ModelResponse:
    return ModelResponse(
        text=text, input_tokens=10, output_tokens=20, latency_ms=5, cost_usd=cost, model="fake/model"
    )


def grade(passed: bool, tests_passed: int | None = None) -> GradeResult:
    return GradeResult(
        passed=passed,
        tests_passed=6 if passed else 5 if tests_passed is None else tests_passed,
        tests_total=6,
        exit_code=0 if passed else 1,
        stdout="ok" if passed else "FAILED test_x",
        stderr="",
        duration_ms=1,
    )


def events(log: RunLog, run_id: int) -> list[Event]:
    return read_events(log.db, run_id)


def kinds(evs: list[Event], only: set[str] | None = None) -> list[str]:
    return [e.kind for e in evs if only is None or e.kind in only]


def run_bounty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    replies: dict[str, list] | list,
    grades: list,
    max_attempts: int = 3,
) -> tuple[RunLog, int]:
    """Problem 001 through a solo or team lineup. A reply or grade that is an exception is raised."""

    async def fake_complete(model, system, messages, *, role, **kwargs):
        r = replies.pop(0) if isinstance(replies, list) else replies[role].pop(0)
        if isinstance(r, BaseException):
            raise r
        return r() if callable(r) else r  # a callable runs mid-call, then gives the reply

    async def fake_grade(code, problem_dir):
        g = grades.pop(0)
        if isinstance(g, BaseException):
            raise g
        return grade(g) if isinstance(g, bool) else g

    monkeypatch.setattr(core_plugin, "complete", fake_complete)
    monkeypatch.setattr(loop, "grade", fake_grade)
    roles = {"solo": "fake/model"} if mode == "solo" else TEAM
    lineup = Lineup.model_validate(
        {
            "name": f"fake-{mode}",
            "plugin": "code_bounty",
            "mode": mode,
            "roles": roles,
            "max_attempts": max_attempts,
        }
    )
    log = RunLog(tmp_path / "arena.db")
    board = SqliteTaskBoard(tmp_path / "arena.db")
    asyncio.run(orchestrator.run_lineup(CODE_BOUNTY, lineup, load_problems("001"), board, log, "001"))
    assert log.last_run_id is not None
    return log, log.last_run_id


def mafia_complete(
    script: dict | None, seen: list[tuple[str, str]] | None = None, costs: dict[str, float] | None = None
):
    """A fake complete that plays `script` (None: every reply is unusable). `costs`: price by role."""

    async def fake_complete(model, system, messages, *, role, **kwargs):
        content = messages[0]["content"]
        if seen is not None:
            seen.append((role, content))
        if role == "narrator":
            return reply("The village wakes.")
        kind, name = role.split(":")

        def say(text: str) -> ModelResponse:
            return reply(text, (costs or {}).get(kind, 0.01))

        if script is None:
            return say("hmm")
        n = int(re.findall(r"It is (?:night|day) (\d+)", content)[-1])
        last = content.splitlines()[-1]
        if "TARGET:" in last:
            return say(f"Quiet one first.\nTARGET: {script['kills'][n]}")
        if "CHECK:" in last:
            return say(f"CHECK: {script['checks'][n]}")
        if "VOTE:" in last:
            target = script["votes"].get(n) if kind != "mafia" else None
            return say(f"VOTE: {target or 'none'}")
        return say(f"{name} speaks on day {n}.")

    return fake_complete


def play_mafia(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    script: dict | None = TOWN_WINS,
    game: str = "01",
    roles: dict[str, str] | None = None,
    costs: dict[str, float] | None = None,
) -> tuple[RunLog, int]:
    monkeypatch.setattr(core_plugin, "complete", mafia_complete(script, costs=costs))
    lineup = Lineup.model_validate(
        {"name": "mafia-fake", "plugin": "mafia", "mode": "game", "roles": roles or MAFIA_ROLES}
    )
    log = RunLog(tmp_path / "arena.db")
    board = SqliteTaskBoard(tmp_path / "arena.db")
    asyncio.run(orchestrator.run_lineup(MAFIA, lineup, MAFIA.load_tasks(game), board, log, game))
    assert log.last_run_id is not None
    return log, log.last_run_id
