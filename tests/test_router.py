import pytest

from arena.core.runlog import RunLog
from arena.models.pricing import cost_usd
from arena.models.router import estimate_reasoning_tokens, split_think_tags


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


def test_cost_local_is_free_and_anthropic_is_unknown() -> None:
    assert cost_usd("ollama/qwen2.5-coder:14b", 1000, 1000) == 0.0
    assert cost_usd("anthropic/claude-haiku-5-5", 1000, 1000) is None


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
