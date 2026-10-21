"""The plugin interface: the registry, lineup checks, and core running a plugin that isn't code_bounty."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from arena.core import orchestrator
from arena.core import plugin as core_plugin
from arena.core.lineup import Lineup, get_lineup, load_lineups
from arena.core.plugin import Outcome, Session
from arena.core.runlog import RunLog
from arena.core.taskboard import SqliteTaskBoard
from arena.models.router import ModelResponse
from arena.plugins import DEFAULT_PLUGIN, PLUGINS, get_plugin
from arena.plugins.code_bounty.plugin import PLUGIN as CODE_BOUNTY

# --- a tiny plugin: two players answer, then a judge picks a winner ----------


class Round(BaseModel):
    id: str
    question: str


class Duel:
    name = "duel"
    modes: dict[str, set[str]] = {"duel": {"player", "judge"}}
    max_calls = 5
    schema = "CREATE TABLE IF NOT EXISTS duel_rounds (problem_id TEXT, winner TEXT);"

    def load_tasks(self, selector: str | None) -> list[Round]:
        rounds = [Round(id="r1", question="2+2?"), Round(id="r2", question="3+3?")]
        return [r for r in rounds if selector in (None, r.id)]

    def validate_lineup(self, lineup: Lineup) -> None:
        if lineup.max_attempts != 1:
            raise ValueError(f"lineup {lineup.name!r}: a duel has no retries")

    async def run_task(self, task: Round, lineup: Lineup, session: Session) -> Outcome:
        player = lineup.roles["player"]
        # Two agents share one role's model, and each sees only its own prompt.
        question = [{"role": "user", "content": task.question}]
        answers = []
        for n in (1, 2):
            answers.append((await session.call(f"player-{n}", player, "answer", question)).text)
        verdict = await session.call(
            "judge", lineup.roles["judge"], "judge", [{"role": "user", "content": str(answers)}]
        )
        if verdict.text == "escape":
            # let the provider error through, for run_lineup to handle
            await session.call("judge", lineup.roles["judge"], "judge", [])
        winner = verdict.text
        session.log.db.execute("INSERT INTO duel_rounds VALUES (?, ?)", (task.id, winner))
        return session.outcome(score=0.5, verdict=f"{winner} won", metrics={"winner": winner})


DUEL = Duel()
DUEL_LINEUP = {"name": "duel-x", "plugin": "duel", "mode": "duel", "max_attempts": 1}
ROLES = {"player": "fake/player", "judge": "fake/judge"}


def _reply(text: str) -> ModelResponse:
    return ModelResponse(
        text=text, input_tokens=1, output_tokens=1, latency_ms=1, cost_usd=0.01, model="fake"
    )


def _run_duel(monkeypatch, tmp_path: Path, judge_says: str):
    seen: list[tuple[str, list[dict]]] = []

    async def fake_complete(model, system, messages, *, role, **kwargs):
        seen.append((role, messages))
        if not messages:  # only Duel's "escape" call sends none
            raise RuntimeError("provider down")
        return _reply(judge_says if role == "judge" else "4")

    monkeypatch.setattr(core_plugin, "complete", fake_complete)
    log = RunLog(tmp_path / "arena.db")
    board = SqliteTaskBoard(tmp_path / "arena.db")
    lineup = Lineup.model_validate({**DUEL_LINEUP, "roles": ROLES})
    run_id, outcomes = asyncio.run(orchestrator.run_lineup(DUEL, lineup, DUEL.load_tasks(None), board, log))
    return log, board, outcomes, seen


# --- registry ------------------------------------------------------------------


def test_registry_lookup() -> None:
    assert get_plugin("code_bounty") is CODE_BOUNTY
    assert DEFAULT_PLUGIN == "code_bounty"
    assert all(name == plugin.name for name, plugin in PLUGINS.items())


def test_unknown_plugin_name() -> None:
    with pytest.raises(KeyError, match="unknown plugin 'mafia'; choose from: code_bounty"):
        get_plugin("mafia")


def test_lineup_with_unknown_plugin_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "lineups.yaml"
    path.write_text("x:\n  plugin: mafia\n  mode: game\n  roles: { narrator: ollama/x }\n")
    with pytest.raises(ValueError, match="unknown plugin 'mafia'"):
        load_lineups(PLUGINS, DEFAULT_PLUGIN, path)


# --- lineups against a plugin ----------------------------------------------------


def test_lineup_without_plugin_defaults_to_code_bounty() -> None:
    lineups = load_lineups(PLUGINS, DEFAULT_PLUGIN)
    assert {lineup.plugin for lineup in lineups.values()} == {"code_bounty"}


def test_lineup_checked_against_its_plugin(tmp_path: Path) -> None:
    plugins = {"code_bounty": CODE_BOUNTY, "duel": DUEL}
    path = tmp_path / "lineups.yaml"

    def load(yaml: str) -> dict[str, Lineup]:
        path.write_text(yaml)
        return load_lineups(plugins, DEFAULT_PLUGIN, path)

    ok = load("d:\n  plugin: duel\n  mode: duel\n  max_attempts: 1\n  roles: { player: a/b, judge: a/c }\n")
    assert (ok["d"].plugin, ok["d"].mode) == ("duel", "duel")
    with pytest.raises(ValueError, match="duel has no mode 'solo'; choose from: duel"):
        load("d:\n  plugin: duel\n  mode: solo\n  roles: { solo: a/b }\n")
    with pytest.raises(ValueError, match=r"duel lineup 'd' is missing roles: \['judge'\]"):
        load("d:\n  plugin: duel\n  mode: duel\n  max_attempts: 1\n  roles: { player: a/b }\n")
    with pytest.raises(ValueError, match="a duel has no retries"):
        load("d:\n  plugin: duel\n  mode: duel\n  roles: { player: a/b, judge: a/c }\n")
    # code_bounty's modes don't leak into other plugins, or the other way round
    with pytest.raises(ValueError, match="code_bounty has no mode 'duel'"):
        load("d:\n  mode: duel\n  roles: { player: a/b, judge: a/c }\n")


def test_unknown_lineup_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "lineups.yaml"
    # a typo must not quietly fall back to the default plugin
    path.write_text("x:\n  plugn: mafia\n  mode: solo\n  roles: { solo: ollama/x }\n")
    with pytest.raises(ValidationError, match="plugn"):
        load_lineups(PLUGINS, DEFAULT_PLUGIN, path)


def test_unknown_role_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="thinking"):
        Lineup.model_validate(
            {
                "name": "x",
                "plugin": "code_bounty",
                "mode": "solo",
                "roles": {"solo": {"model": "a/b", "thinking": True}},
            }
        )


def test_get_lineup_unknown_name() -> None:
    with pytest.raises(KeyError, match="unknown lineup 'nope'"):
        get_lineup("nope", PLUGINS, DEFAULT_PLUGIN)


# --- core running a plugin that isn't code_bounty --------------------------------


def test_fake_plugin_runs_through_run_lineup(monkeypatch, tmp_path, capsys) -> None:
    log, board, outcomes, seen = _run_duel(monkeypatch, tmp_path, judge_says="player-2")

    assert [(o.task_id, o.score, o.calls, o.metrics) for o in outcomes] == [
        ("r1", 0.5, 3, {"winner": "player-2"}),
        ("r2", 0.5, 3, {"winner": "player-2"}),
    ]
    assert [role for role, _ in seen] == ["player-1", "player-2", "judge"] * 2
    assert log.db.execute("SELECT plugin, max_attempts FROM runs").fetchone()[:] == ("duel", 1)
    rows = log.db.execute(
        "SELECT problem_id, score, passed, attempts, metrics FROM results ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("r1", 0.5, 0, 1, json.dumps({"winner": "player-2"})),
        ("r2", 0.5, 0, 1, json.dumps({"winner": "player-2"})),
    ]
    # the plugin's own table was created and written to
    assert log.db.execute("SELECT COUNT(*) FROM duel_rounds").fetchone()[0] == 2
    # no fail_reason means done; the verdict is the plugin's
    tasks = board.db.execute("SELECT problem_id, status FROM tasks ORDER BY id").fetchall()
    assert [tuple(t) for t in tasks] == [("r1", "done"), ("r2", "done")]
    assert "   player-2 won, 3 call(s), $0.0300, " in capsys.readouterr().out


def test_model_error_escaping_a_plugin_is_unscored(monkeypatch, tmp_path, capsys) -> None:
    # the judge's first reply makes Duel make one more call, which fails and isn't caught
    log, board, outcomes, _ = _run_duel(monkeypatch, tmp_path, judge_says="escape")

    assert [(o.score, o.error, o.calls) for o in outcomes] == [(0.0, "RuntimeError: provider down", 4)] * 2
    assert log.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    tasks = board.db.execute("SELECT status, reason FROM tasks").fetchall()
    assert [tuple(t) for t in tasks] == [("failed", "RuntimeError: provider down")] * 2
    out = capsys.readouterr().out
    assert "   model call failed: RuntimeError: provider down" in out
    assert "   ERROR, not scored, 4 call(s)" in out
    assert log.db.execute("SELECT finished_at IS NOT NULL, interrupted FROM runs").fetchone()[:] == (1, 0)


def test_session_enforces_the_plugins_call_limit(monkeypatch, tmp_path) -> None:
    async def fake_complete(model, system, messages, **kwargs):
        return _reply("ok")

    monkeypatch.setattr(core_plugin, "complete", fake_complete)
    session = Session("r1", run_id=1, log=RunLog(tmp_path / "arena.db"), max_calls=DUEL.max_calls)
    player = Lineup.model_validate({**DUEL_LINEUP, "roles": ROLES}).roles["player"]

    async def call_until_stopped() -> None:
        for _ in range(DUEL.max_calls):
            await session.call("player-1", player, "", [])
        await session.call("player-1", player, "", [])

    with pytest.raises(core_plugin.CallLimitReached, match="hit the 5-call limit"):
        asyncio.run(call_until_stopped())
    assert session.calls == 5


def test_run_lineup_rejects_another_plugins_lineup(tmp_path: Path) -> None:
    lineup = Lineup.model_validate({**DUEL_LINEUP, "roles": ROLES})
    log = RunLog(tmp_path / "arena.db")
    with pytest.raises(ValueError, match="for plugin 'duel', not 'code_bounty'"):
        asyncio.run(orchestrator.run_lineup(CODE_BOUNTY, lineup, [], SqliteTaskBoard(tmp_path / "a.db"), log))


def test_core_imports_no_plugin() -> None:
    code = (
        "import sys, arena.core.orchestrator, arena.core.lineup, arena.core.plugin, arena.core.scoreboard\n"
        "print([m for m in sys.modules if m.startswith('arena.plugins')])"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"
