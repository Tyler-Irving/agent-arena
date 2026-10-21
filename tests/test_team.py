"""The team loop (with fake models and grader) and orchestrator decision parsing."""

import asyncio
from pathlib import Path

import pytest

from arena.core import orchestrator
from arena.core import plugin as core_plugin
from arena.core.lineup import Lineup
from arena.core.runlog import RunLog
from arena.core.taskboard import SqliteTaskBoard
from arena.models.router import ModelResponse
from arena.plugins.code_bounty import loop, roles
from arena.plugins.code_bounty.grader import GradeResult
from arena.plugins.code_bounty.plugin import PLUGIN
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


def _run_team(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    replies: dict[str, list],
    grades: list[bool],
    max_attempts: int = 3,
):
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

    monkeypatch.setattr(core_plugin, "complete", fake_complete)
    monkeypatch.setattr(loop, "grade", fake_grade)
    log = RunLog(tmp_path / "arena.db")
    lineup = Lineup.model_validate(
        {
            "name": "fake-team",
            "plugin": "code_bounty",
            "mode": "team",
            "roles": TEAM,
            "max_attempts": max_attempts,
        }
    )
    board = SqliteTaskBoard(tmp_path / "arena.db")
    run_id, outcomes = asyncio.run(orchestrator.run_lineup(PLUGIN, lineup, load_problems("001"), board, log))
    assert all(not v for v in replies.values()), f"unused replies: {replies}"
    assert not grades, "unused grades"
    return log, outcomes[0], seen


def _task_reason(tmp_path: Path) -> str | None:
    return SqliteTaskBoard(tmp_path / "arena.db").db.execute("SELECT reason FROM tasks").fetchone()[0]


def test_team_passes_first_try(monkeypatch, tmp_path) -> None:
    replies = {"planner": [_reply("use a dict")], "coder": [_reply("```python\ngood\n```")]}
    _, outcome, seen = _run_team(monkeypatch, tmp_path, replies, [True])

    assert (outcome.score, outcome.attempts, outcome.calls) == (1.0, 1, 2)
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

    assert (outcome.score, outcome.attempts, outcome.calls) == (1.0, 2, 5)
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

    assert (outcome.score, outcome.attempts, outcome.calls) == (1.0, 2, 6)
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

    assert (outcome.score, outcome.attempts, outcome.calls) == (0.0, 1, 4)
    assert outcome.stop_reason == "orchestrator gave up"
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

    assert (outcome.score, outcome.attempts, outcome.calls) == (0.0, 3, 6)
    # attempt 2 had no code: straight back to the coder, no review or decision
    assert [role for role, _ in seen] == ["planner", "coder", "reviewer", "orchestrator", "coder", "coder"]
    assert "did not contain" in seen[5][1]
    assert _task_reason(tmp_path) == "tests failing after 3 attempts"


def test_team_no_code_reply_keeps_earlier_code_and_review(monkeypatch, tmp_path) -> None:
    replies = {
        "planner": [_reply("plan")],
        "coder": [_reply("```python\nbad\n```"), _reply("no code"), _reply("```python\ngood\n```")],
        "reviewer": [_reply("off by one on line 3")],
        "orchestrator": [_reply("DECISION: RETRY")],
    }
    _, outcome, seen = _run_team(monkeypatch, tmp_path, replies, [False, True])

    assert (outcome.score, outcome.attempts) == (1.0, 3)
    third_coder_prompt = seen[-1][1]
    assert "did not contain" in third_coder_prompt
    assert "```python\nbad\n```" in third_coder_prompt and "off by one on line 3" in third_coder_prompt


def test_team_truncated_reply_gets_token_limit_feedback(monkeypatch, tmp_path) -> None:
    cut_off = _reply("```python\ndef two_sum(")
    cut_off.truncated = True
    replies = {"planner": [_reply("plan")], "coder": [cut_off, _reply("```python\ngood\n```")]}
    _, outcome, seen = _run_team(monkeypatch, tmp_path, replies, [True])

    assert (outcome.score, outcome.attempts) == (1.0, 2)
    assert "output token limit" in seen[-1][1]


def test_team_model_error_fails_problem_without_scoring_it(monkeypatch, tmp_path) -> None:
    replies = {"planner": [_reply("plan")], "coder": [RuntimeError("boom")]}
    log, outcome, _ = _run_team(monkeypatch, tmp_path, replies, [])

    assert outcome.score == 0.0 and outcome.error == "RuntimeError: boom"
    assert (outcome.attempts, outcome.calls) == (0, 2)
    assert _task_reason(tmp_path) == "RuntimeError: boom"
    assert log.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0


def test_team_respects_call_limit(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(PLUGIN, "max_calls", 3)
    replies = {
        "planner": [_reply("plan")],
        "coder": [_reply("```python\nbad\n```")],
        "reviewer": [_reply("fix it")],
    }
    _, outcome, _ = _run_team(monkeypatch, tmp_path, replies, [False])

    assert outcome.score == 0.0 and outcome.calls == 3
    assert outcome.error is None and outcome.stop_reason == "hit the 3-call limit"
    assert _task_reason(tmp_path) == "hit the 3-call limit after 1 attempts"


def test_orchestrator_sees_earlier_attempts_and_decisions(monkeypatch, tmp_path) -> None:
    replies = {
        "planner": [_reply("use a dict")],
        "coder": [
            _reply("```python\nbad\n```"),
            _reply("```python\nbad\n```"),
            _reply("```python\ngood\n```"),
        ],
        "reviewer": [_reply("fix A"), _reply("fix B")],
        "orchestrator": [_reply("DECISION: RETRY"), _reply("DECISION: RETRY")],
    }
    _, outcome, seen = _run_team(monkeypatch, tmp_path, replies, [False, False, True])

    assert outcome.score == 1.0
    first, second = (prompt for role, prompt in seen if role == "orchestrator")
    assert "Earlier attempts" not in first
    assert "Attempt 1 (plan 1) failed. 2 attempt(s) left." in first
    assert "Earlier attempts:\n- Attempt 1 (plan 1): 5/6 tests passed. You chose RETRY." in second
    assert "Attempt 2 (plan 1) failed. 1 attempt(s) left." in second


def test_no_code_attempt_appears_in_history(monkeypatch, tmp_path) -> None:
    replies = {
        "planner": [_reply("plan")],
        "coder": [_reply("no code"), _reply("```python\nbad\n```"), _reply("```python\ngood\n```")],
        "reviewer": [_reply("fix it")],
        "orchestrator": [_reply("DECISION: RETRY")],
    }
    _, _, seen = _run_team(monkeypatch, tmp_path, replies, [False, True])

    decide_prompt = next(prompt for role, prompt in seen if role == "orchestrator")
    assert "- Attempt 1 (plan 1): the reply had no code block." in decide_prompt


def test_replan_passes_the_reason_and_counts_plans(monkeypatch, tmp_path) -> None:
    replies = {
        "planner": [_reply("brute force"), _reply("use a dict")],
        "coder": [
            _reply("```python\nslow\n```"),
            _reply("```python\nbad\n```"),
            _reply("```python\ngood\n```"),
        ],
        "reviewer": [_reply("too slow"), _reply("off by one")],
        "orchestrator": [
            _reply("DECISION: REPLAN\nbrute force cannot pass the big test"),
            _reply("DECISION: RETRY"),
        ],
    }
    _, outcome, seen = _run_team(monkeypatch, tmp_path, replies, [False, False, True])

    assert outcome.score == 1.0
    replan_prompt = [prompt for role, prompt in seen if role == "planner"][1]
    assert "brute force cannot pass the big test" in replan_prompt
    second_decision = [prompt for role, prompt in seen if role == "orchestrator"][1]
    assert "Current plan (plan 2):\nuse a dict" in second_decision
    assert "- Attempt 1 (plan 1): 5/6 tests passed. You chose REPLAN." in second_decision
    assert "Attempt 2 (plan 2) failed." in second_decision


def test_assumed_retry_is_not_reported_as_a_choice(monkeypatch, tmp_path) -> None:
    replies = {
        "planner": [_reply("plan")],
        "coder": [
            _reply("```python\nbad\n```"),
            _reply("```python\nbad\n```"),
            _reply("```python\ngood\n```"),
        ],
        "reviewer": [_reply("fix A"), _reply("fix B")],
        "orchestrator": [_reply("hmm, not sure"), _reply("DECISION: RETRY")],
    }
    _, _, seen = _run_team(monkeypatch, tmp_path, replies, [False, False, True])

    second = [prompt for role, prompt in seen if role == "orchestrator"][1]
    assert "Your reply had no clear decision, so the coder retried." in second
    assert "You chose RETRY" not in second


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("DECISION: REPLAN\nbrute force is too slow", "brute force is too slow"),
        ("DECISION: REPLAN – greedy is not optimal.", "greedy is not optimal"),
        ("**DECISION:** REPLAN", roles.NO_REASON),
        ("\n", roles.NO_REASON),
    ],
)
def test_decision_reason(reply: str, reason: str) -> None:
    assert roles.decision_reason(reply) == reason


def test_lineup_max_attempts_sets_the_limit(monkeypatch, tmp_path) -> None:
    bad = "```python\nbad\n```"
    replies = {
        "planner": [_reply("plan"), _reply("simpler plan")],
        "coder": [_reply(bad)] * 4 + [_reply("```python\ngood\n```")],
        "reviewer": [_reply("fix it")] * 4,
        "orchestrator": [_reply("DECISION: RETRY")] * 3 + [_reply("DECISION: REPLAN")],
    }
    _, outcome, seen = _run_team(monkeypatch, tmp_path, replies, [False] * 4 + [True], max_attempts=5)

    assert (outcome.score, outcome.attempts) == (1.0, 5)
    decide_prompts = [prompt for role, prompt in seen if role == "orchestrator"]
    assert "4 attempt(s) left" in decide_prompts[0] and "1 attempt(s) left" in decide_prompts[-1]
