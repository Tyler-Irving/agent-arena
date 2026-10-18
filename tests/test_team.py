"""The team loop (with fake models and grader) and orchestrator decision parsing."""

import asyncio
from pathlib import Path

import pytest

from arena.core import orchestrator
from arena.core.lineup import Lineup
from arena.core.runlog import RunLog
from arena.core.taskboard import SqliteTaskBoard
from arena.models.router import ModelResponse
from arena.plugins.code_bounty import roles
from arena.plugins.code_bounty.grader import GradeResult
from arena.plugins.code_bounty.problems import load_problems

TEAM = {"orchestrator": "fake/orch", "planner": "fake/plan", "coder": "fake/code", "reviewer": "fake/review"}


def _reply(text: str) -> ModelResponse:
    return ModelResponse(
        text=text, input_tokens=10, output_tokens=20, latency_ms=5, cost_usd=0.01, model="fake/model"
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


@pytest.mark.parametrize(
    ("reply", "decision"),
    [
        ("DECISION: RETRY\nThe plan is fine.", "retry"),
        ("decision: replan - too slow", "replan"),
        ("**DECISION:** GIVE_UP\nNo hope.", "give_up"),
        ("DECISION: give up", "give_up"),
        ("I think we should REPLAN because the approach is quadratic.", "replan"),
        ("Let's try again.", None),
    ],
)
def test_parse_decision(reply: str, decision: str | None) -> None:
    assert roles.parse_decision(reply) == decision


def _run_team(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, replies: dict[str, list], grades: list[bool]):
    """Run problem 001 through the team loop with scripted replies per role and grades."""
    seen: list[tuple[str, str]] = []  # (role, prompt) for each call, in order

    async def fake_complete(model, system, messages, *, role, **kwargs):
        assert len(messages) == 1  # every team call is a fresh conversation
        seen.append((role, messages[0]["content"]))
        reply = replies[role].pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def fake_grade(code, problem_dir):
        return _grade(grades.pop(0))

    monkeypatch.setattr(orchestrator, "complete", fake_complete)
    monkeypatch.setattr(orchestrator, "grade", fake_grade)
    log = RunLog(tmp_path / "arena.db")
    lineup = Lineup.model_validate({"name": "fake-team", "mode": "team", "roles": TEAM})
    run_id, outcomes = asyncio.run(
        orchestrator.run_lineup(lineup, load_problems("001"), SqliteTaskBoard(tmp_path / "arena.db"), log)
    )
    assert all(not v for v in replies.values()), f"unused replies: {replies}"
    assert not grades, "unused grades"
    return log, outcomes[0], seen


def _task_reason(tmp_path: Path) -> str | None:
    return SqliteTaskBoard(tmp_path / "arena.db").db.execute("SELECT reason FROM tasks").fetchone()[0]


def test_team_passes_first_try(monkeypatch, tmp_path) -> None:
    replies = {"planner": [_reply("use a dict")], "coder": [_reply("```python\ngood\n```")]}
    _, outcome, seen = _run_team(monkeypatch, tmp_path, replies, [True])

    assert (outcome.passed, outcome.attempts, outcome.calls) == (True, 1, 2)
    assert [role for role, _ in seen] == ["planner", "coder"]
    assert "Plan:\nuse a dict" in seen[1][1]


def test_team_retry_sends_code_tests_and_review_to_coder(monkeypatch, tmp_path) -> None:
    replies = {
        "planner": [_reply("use a dict")],
        "coder": [_reply("```python\nbad\n```"), _reply("```python\ngood\n```")],
        "reviewer": [_reply("off by one on line 3")],
        "orchestrator": [_reply("DECISION: RETRY\nclose")],
    }
    log, outcome, seen = _run_team(monkeypatch, tmp_path, replies, [False, True])

    assert (outcome.passed, outcome.attempts, outcome.calls) == (True, 2, 5)
    assert outcome.cost_usd == pytest.approx(0.05)
    assert [role for role, _ in seen] == ["planner", "coder", "reviewer", "orchestrator", "coder"]
    review_prompt, decide_prompt, retry_prompt = seen[2][1], seen[3][1], seen[4][1]
    assert "```python\nbad\n```" in review_prompt and "FAILED test_x" in review_prompt
    assert "off by one" in decide_prompt and "2 attempt(s) left" in decide_prompt
    assert "Plan:\nuse a dict" in retry_prompt
    assert "```python\nbad\n```" in retry_prompt
    assert "5/6 tests" in retry_prompt and "off by one on line 3" in retry_prompt

    attempts = log.db.execute("SELECT attempt, passed FROM attempts ORDER BY attempt").fetchall()
    assert [tuple(a) for a in attempts] == [(1, 0), (2, 1)]


def test_team_replan_gives_coder_the_new_plan_only(monkeypatch, tmp_path) -> None:
    replies = {
        "planner": [_reply("brute force"), _reply("use a dict")],
        "coder": [_reply("```python\nslow\n```"), _reply("```python\ngood\n```")],
        "reviewer": [_reply("too slow")],
        "orchestrator": [_reply("DECISION: REPLAN\nwrong approach")],
    }
    _, outcome, seen = _run_team(monkeypatch, tmp_path, replies, [False, True])

    assert (outcome.passed, outcome.attempts, outcome.calls) == (True, 2, 6)
    assert [role for role, _ in seen] == ["planner", "coder", "reviewer", "orchestrator", "planner", "coder"]
    replan_prompt, coder_prompt = seen[4][1], seen[5][1]
    assert "brute force" in replan_prompt and "too slow" in replan_prompt and "slow\n```" in replan_prompt
    assert "Plan:\nuse a dict" in coder_prompt and "slow" not in coder_prompt


def test_team_orchestrator_gives_up(monkeypatch, tmp_path) -> None:
    replies = {
        "planner": [_reply("plan")],
        "coder": [_reply("```python\nbad\n```")],
        "reviewer": [_reply("hopeless")],
        "orchestrator": [_reply("DECISION: GIVE_UP")],
    }
    _, outcome, _ = _run_team(monkeypatch, tmp_path, replies, [False])

    assert (outcome.passed, outcome.attempts, outcome.calls, outcome.gave_up) == (False, 1, 4, True)
    assert _task_reason(tmp_path) == "orchestrator gave up after 1 attempts"


def test_team_skips_review_after_last_attempt(monkeypatch, tmp_path) -> None:
    replies = {
        "planner": [_reply("plan")],
        "coder": [_reply("```python\nbad\n```"), _reply("no code"), _reply("```python\nbad\n```")],
        "reviewer": [_reply("fix it")],
        # an unparseable decision falls back to retry
        "orchestrator": [_reply("hmm, keep going")],
    }
    _, outcome, seen = _run_team(monkeypatch, tmp_path, replies, [False, False])

    assert (outcome.passed, outcome.attempts, outcome.calls) == (False, 3, 6)
    # attempt 2 had no code: straight back to the coder, no review or decision
    assert [role for role, _ in seen] == ["planner", "coder", "reviewer", "orchestrator", "coder", "coder"]
    assert "did not contain" in seen[5][1]
    assert _task_reason(tmp_path) == "tests failing after 3 attempts"


def test_team_model_error_fails_problem(monkeypatch, tmp_path) -> None:
    replies = {"planner": [_reply("plan")], "coder": [RuntimeError("boom")]}
    _, outcome, _ = _run_team(monkeypatch, tmp_path, replies, [])

    assert not outcome.passed and outcome.error == "RuntimeError: boom"
    assert (outcome.attempts, outcome.calls) == (1, 2)
    assert _task_reason(tmp_path) == "RuntimeError: boom"


def test_team_respects_call_limit(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(orchestrator, "MAX_CALLS", 3)
    replies = {
        "planner": [_reply("plan")],
        "coder": [_reply("```python\nbad\n```")],
        "reviewer": [_reply("fix it")],
    }
    _, outcome, _ = _run_team(monkeypatch, tmp_path, replies, [False])

    assert not outcome.passed and outcome.calls == 3
    assert outcome.error == "hit the 3-call limit"
