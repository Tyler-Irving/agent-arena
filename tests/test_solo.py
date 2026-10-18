"""Lineups, task board, problem loading, and the solo loop (with fake model and grader)."""

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from arena.core import orchestrator
from arena.core.lineup import Lineup, load_lineups
from arena.core.runlog import RunLog
from arena.core.taskboard import SqliteTaskBoard
from arena.models.router import ModelResponse
from arena.plugins.code_bounty.grader import GraderError, GradeResult, grade
from arena.plugins.code_bounty.problems import PROBLEMS_DIR, extract_code, load_problems

# --- lineups ---------------------------------------------------------------


def test_role_shorthand_and_think() -> None:
    lineup = Lineup.model_validate(
        {"name": "x", "mode": "solo", "roles": {"solo": {"model": "ollama/qwen3.6:35b-a3b", "think": False}}}
    )
    assert lineup.roles["solo"].think is False
    lineup = Lineup.model_validate(
        {"name": "x", "mode": "solo", "roles": {"solo": "ollama/qwen2.5-coder:14b"}}
    )
    assert lineup.roles["solo"].model == "ollama/qwen2.5-coder:14b"
    assert lineup.roles["solo"].think is None


def test_missing_roles_rejected() -> None:
    with pytest.raises(ValidationError, match="missing roles"):
        Lineup.model_validate({"name": "x", "mode": "team", "roles": {"coder": "ollama/x"}})


def test_think_false_rejected_where_thinking_cannot_be_disabled() -> None:
    def no_think(model: str) -> dict:
        return {"name": "x", "mode": "solo", "roles": {"solo": {"model": model, "think": False}}}

    with pytest.raises(ValidationError, match="cannot turn thinking off"):
        Lineup.model_validate(no_think("anthropic/claude-opus-5-5"))
    for model in ("anthropic/claude-sonnet-5-5", "anthropic/claude-haiku-5-5", "ollama/gpt-oss:20b"):
        Lineup.model_validate(no_think(model))


def test_config_lineups_load() -> None:
    lineups = load_lineups()
    assert lineups["opus-solo"].roles["solo"].model == "anthropic/claude-opus-5-5"
    assert lineups["sonnet-orch-qwen14b"].mode == "team"


# --- task board ------------------------------------------------------------


def test_taskboard_claims_in_order_per_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        board = SqliteTaskBoard(tmp_path / "arena.db")
        await board.put(1, "001")
        await board.put(1, "002")
        await board.put(2, "003")
        first = await board.next(1)
        second = await board.next(1)
        assert first and second
        assert (first.problem_id, second.problem_id) == ("001", "002")
        assert await board.next(1) is None
        await board.complete(first.id)
        await board.fail(second.id, "tests failing")
        rows = board.db.execute("SELECT problem_id, status, reason FROM tasks ORDER BY id").fetchall()
        assert rows == [("001", "done", None), ("002", "failed", "tests failing"), ("003", "pending", None)]

    asyncio.run(scenario())


# --- problems ----------------------------------------------------------------


def test_load_problems_selector() -> None:
    assert [p.id for p in load_problems("001")] == ["001-two-sum"]
    assert [p.id for p in load_problems("001-two-sum")] == ["001-two-sum"]
    assert load_problems("999") == []
    assert len(load_problems()) >= 3


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        ("```python\ndef f(): pass\n```", "def f(): pass\n"),
        ("Here:\n```py\nx = 1\n```\nDone.", "x = 1\n"),
        ("```\nx = 1\n```", "x = 1\n"),
        # the solution, then a short usage example: take the longer python block
        ("```python\ndef f():\n    return 1\n```\n```python\nf()\n```", "def f():\n    return 1\n"),
        ("```text\nnotes\n```\n```python\nx = 1\n```", "x = 1\n"),
        ("no code here", None),
    ],
)
def test_extract_code(reply: str, code: str | None) -> None:
    assert extract_code(reply) == code


REFERENCES = sorted(PROBLEMS_DIR.glob("*/reference.py"))


@pytest.mark.docker
@pytest.mark.parametrize("reference", REFERENCES, ids=[r.parent.name for r in REFERENCES])
def test_reference_solution_passes(reference: Path) -> None:
    r = asyncio.run(grade(reference.read_text(), reference.parent))
    assert r.passed, r.stdout
    assert r.tests_passed == r.tests_total > 0


# --- solo loop ---------------------------------------------------------------


def _reply(text: str, cost: float | None = 0.01) -> ModelResponse:
    return ModelResponse(
        text=text, input_tokens=10, output_tokens=20, latency_ms=5, cost_usd=cost, model="fake/model"
    )


def _grade(passed: bool) -> GradeResult:
    return GradeResult(
        passed=passed,
        tests_passed=6 if passed else 5,
        tests_total=6,
        exit_code=0 if passed else 1,
        stdout="ok" if passed else "FAILED test_x",
        stderr="",
        duration_ms=1,
    )


def _run_solo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, replies: list, grades: list[bool]):
    """Run problem 001 through the solo loop with scripted replies and grades."""
    seen_messages: list[list[dict]] = []

    async def fake_complete(model, system, messages, **kwargs):
        seen_messages.append(list(messages))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def fake_grade(code, problem_dir):
        return _grade(grades.pop(0))

    monkeypatch.setattr(orchestrator, "complete", fake_complete)
    monkeypatch.setattr(orchestrator, "grade", fake_grade)
    log = RunLog(tmp_path / "arena.db")
    lineup = Lineup.model_validate({"name": "fake-solo", "mode": "solo", "roles": {"solo": "fake/model"}})
    run_id, outcomes = asyncio.run(
        orchestrator.run_lineup(lineup, load_problems("001"), SqliteTaskBoard(tmp_path / "arena.db"), log)
    )
    return log, run_id, outcomes[0], seen_messages


def test_solo_retries_with_feedback_then_passes(monkeypatch, tmp_path) -> None:
    replies = [_reply("```python\nbad\n```"), _reply("```python\ngood\n```")]
    log, run_id, outcome, seen = _run_solo(monkeypatch, tmp_path, replies, [False, True])

    assert (outcome.passed, outcome.attempts, outcome.calls) == (True, 2, 2)
    assert outcome.cost_usd == pytest.approx(0.02)
    # second call sees the first reply and the test output
    assert seen[1][1] == {"role": "assistant", "content": "```python\nbad\n```"}
    assert "5/6 tests" in seen[1][2]["content"] and "FAILED test_x" in seen[1][2]["content"]

    result = log.db.execute("SELECT passed, attempts, total_calls FROM results").fetchone()
    assert tuple(result) == (1, 2, 2)
    attempts = log.db.execute("SELECT attempt, passed FROM attempts ORDER BY attempt").fetchall()
    assert [tuple(a) for a in attempts] == [(1, 0), (2, 1)]


def test_solo_gives_up_after_max_attempts(monkeypatch, tmp_path) -> None:
    replies = [_reply("no code"), _reply("```python\nbad\n```"), _reply("```python\nbad\n```")]
    _, _, outcome, seen = _run_solo(monkeypatch, tmp_path, replies, [False, False])

    assert (outcome.passed, outcome.attempts, outcome.calls) == (False, 3, 3)
    assert "did not contain" in seen[1][2]["content"]


def test_solo_unknown_price_makes_cost_unknown(monkeypatch, tmp_path) -> None:
    replies = [_reply("```python\nbad\n```"), _reply("```python\ngood\n```", cost=None)]
    _, _, outcome, _ = _run_solo(monkeypatch, tmp_path, replies, [False, True])
    assert outcome.cost_usd is None


def test_solo_model_error_fails_problem(monkeypatch, tmp_path) -> None:
    log, _, outcome, _ = _run_solo(monkeypatch, tmp_path, [RuntimeError("boom")], [])

    assert not outcome.passed and outcome.error == "RuntimeError: boom"
    assert outcome.attempts == 0
    reason = SqliteTaskBoard(tmp_path / "arena.db").db.execute("SELECT reason FROM tasks").fetchone()[0]
    assert reason == "RuntimeError: boom"
    # a provider error says nothing about the lineup, so it is not a scored trial
    assert log.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0


def test_grader_error_marks_task_failed_and_aborts(monkeypatch, tmp_path) -> None:
    async def fake_complete(model, system, messages, **kwargs):
        return _reply("```python\ngood\n```")

    async def broken_grade(code, problem_dir):
        raise GraderError("docker went away")

    monkeypatch.setattr(orchestrator, "complete", fake_complete)
    monkeypatch.setattr(orchestrator, "grade", broken_grade)
    log = RunLog(tmp_path / "arena.db")
    lineup = Lineup.model_validate({"name": "fake-solo", "mode": "solo", "roles": {"solo": "fake/model"}})
    board = SqliteTaskBoard(tmp_path / "arena.db")
    with pytest.raises(GraderError):
        asyncio.run(orchestrator.run_lineup(lineup, load_problems("001"), board, log))

    status, reason = board.db.execute("SELECT status, reason FROM tasks").fetchone()
    assert (status, reason) == ("failed", "grader error: docker went away")
    assert log.db.execute("SELECT finished_at FROM runs").fetchone()[0] is not None
