import sqlite3

import pytest

from arena.core.runlog import RunLog
from arena.models.pricing import cost_usd
from arena.models.router import anthropic_thinking, estimate_reasoning_tokens, split_think_tags


@pytest.mark.parametrize(
    ("content", "text", "reasoning"),
    [
        ("just an answer", "just an answer", None),
        ("<think>hmm</think>\nanswer", "answer", "hmm"),
        ("<think>a</think>x<think>b</think>y", "xy", "a\n\nb"),
        # template opened the block in the prompt, only the close tag arrives
        ("mid-thought</think>answer", "answer", "mid-thought"),
        # cut off by max_tokens while thinking
        ("<think>never finished", "", "never finished"),
        ("<think></think>answer", "answer", None),
    ],
)
def test_split_think_tags(content: str, text: str, reasoning: str | None) -> None:
    assert split_think_tags(content) == (text, reasoning)


def test_estimate_reasoning_tokens() -> None:
    assert estimate_reasoning_tokens(100, None, "answer") == 0
    assert estimate_reasoning_tokens(100, "x" * 75, "y" * 25) == 75


def test_cost() -> None:
    assert cost_usd("ollama/qwen2.5-coder:14b", 1000, 1000) == 0.0
    assert cost_usd("anthropic/claude-sonnet-5-5", 1_000_000, 1_000_000) == pytest.approx(12.0)
    assert cost_usd("anthropic/unknown-model", 1000, 1000) is None


def test_haiku_long_prompt_tier() -> None:
    # at the threshold: short-prompt rate; above it: long-prompt rate for the whole call
    assert cost_usd("anthropic/claude-haiku-5-5", 100_000, 1_000_000) == pytest.approx(0.01 + 0.50)
    assert cost_usd("anthropic/claude-haiku-5-5", 200_000, 1_000_000) == pytest.approx(0.10 + 2.50)


def test_runlog_roundtrip(tmp_path) -> None:
    log = RunLog(tmp_path / "arena.db")
    run_id = log.start_run("smoke", "smoke")
    log.log_call(
        run_id=run_id,
        problem_id=None,
        role="smoke",
        model="ollama/x",
        think=False,
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        output_tokens=10,
        reasoning_tokens=4,
        reasoning="thought",
    )
    log.finish_run(run_id)
    row = log.db.execute("SELECT * FROM calls").fetchone()
    assert (row["run_id"], row["think"], row["reasoning"]) == (run_id, 0, "thought")
    assert row["reasoning_tokens"] == 4
    assert log.db.execute("SELECT finished_at FROM runs").fetchone()[0] is not None


def test_runlog_adds_columns_to_older_databases(tmp_path) -> None:
    db = tmp_path / "arena.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY, model TEXT NOT NULL, created_at TEXT NOT NULL)")
    old.commit()
    old.close()

    log = RunLog(db)
    columns = {row["name"] for row in log.db.execute("PRAGMA table_info(calls)")}
    assert "truncated" in columns
    RunLog(db)  # opening again is a no-op


SUMMARIZED = {"type": "adaptive", "display": "summarized"}


@pytest.mark.parametrize(
    ("name", "think", "expected"),
    [
        # thinks by default: send the same adaptive setting, asking for a summary
        ("claude-haiku-5-5", None, SUMMARIZED),
        ("claude-opus-5-5", None, SUMMARIZED),
        ("claude-opus-5-5", True, SUMMARIZED),
        # unknown default: leave it alone unless thinking is asked for
        ("claude-some-future-model", None, None),
        ("claude-some-future-model", True, SUMMARIZED),
        # each model's own off switch
        ("claude-haiku-5-5", False, {"type": "disabled"}),
        ("claude-sonnet-5-5", False, {"type": "between_tools"}),
    ],
)
def test_anthropic_thinking(name: str, think: bool | None, expected: dict | None) -> None:
    assert anthropic_thinking(name, think) == expected


def test_anthropic_thinking_off_unsupported() -> None:
    with pytest.raises(ValueError, match="cannot turn thinking off"):
        anthropic_thinking("claude-opus-5-5", False)
