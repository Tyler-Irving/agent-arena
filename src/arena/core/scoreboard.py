"""Scoreboard: compare lineups on pass rate, attempts, tokens, cost and time.

Every problem result in every run counts as one trial, so running a lineup
again adds data rather than replacing it. Token and time figures are
per-problem averages.
"""

from pydantic import BaseModel

from arena.core.runlog import RunLog

QUERY = """
WITH call_totals AS (
    SELECT run_id, problem_id,
           SUM(input_tokens) AS input_tokens,
           SUM(output_tokens) AS output_tokens,
           SUM(reasoning_tokens) AS reasoning_tokens
    FROM calls
    GROUP BY run_id, problem_id
)
SELECT runs.lineup,
       COUNT(DISTINCT runs.id) AS runs,
       COUNT(*) AS trials,
       SUM(results.passed) AS passed,
       AVG(results.attempts) AS avg_attempts,
       AVG(call_totals.input_tokens) AS avg_input_tokens,
       AVG(call_totals.output_tokens) AS avg_output_tokens,
       AVG(call_totals.reasoning_tokens) AS avg_reasoning_tokens,
       SUM(results.total_cost_usd) AS total_cost_usd,
       COUNT(results.total_cost_usd) AS priced_trials,
       AVG(results.wall_time_ms) AS avg_wall_time_ms
FROM results
JOIN runs ON runs.id = results.run_id
LEFT JOIN call_totals
       ON call_totals.run_id = results.run_id AND call_totals.problem_id = results.problem_id
WHERE runs.plugin = ?
GROUP BY runs.lineup
"""


class ScoreRow(BaseModel):
    lineup: str
    runs: int
    trials: int
    passed: int
    avg_attempts: float
    avg_input_tokens: float
    avg_output_tokens: float  # includes reasoning
    avg_reasoning_tokens: float
    total_cost_usd: float | None  # None if any trial used a model with no known price
    avg_wall_time_ms: float

    @property
    def pass_rate(self) -> float:
        return self.passed / self.trials

    @property
    def avg_cost_usd(self) -> float | None:
        return None if self.total_cost_usd is None else self.total_cost_usd / self.trials


def scoreboard(log: RunLog, plugin: str = "code_bounty") -> list[ScoreRow]:
    """One row per lineup, best pass rate first, then cheaper, then faster."""
    rows = []
    for r in log.db.execute(QUERY, (plugin,)):
        cost_known = r["priced_trials"] == r["trials"]
        rows.append(
            ScoreRow(
                lineup=r["lineup"],
                runs=r["runs"],
                trials=r["trials"],
                passed=r["passed"],
                avg_attempts=r["avg_attempts"],
                avg_input_tokens=r["avg_input_tokens"] or 0,
                avg_output_tokens=r["avg_output_tokens"] or 0,
                avg_reasoning_tokens=r["avg_reasoning_tokens"] or 0,
                total_cost_usd=(r["total_cost_usd"] or 0.0) if cost_known else None,
                avg_wall_time_ms=r["avg_wall_time_ms"],
            )
        )
    return sorted(
        rows,
        key=lambda s: (-s.pass_rate, s.avg_cost_usd is None, s.avg_cost_usd or 0.0, s.avg_wall_time_ms),
    )


def format_scoreboard(rows: list[ScoreRow]) -> str:
    headers = [
        "lineup",
        "runs",
        "passed",
        "pass",
        "attempts",
        "in tok",
        "out tok",
        "reasoning",
        "cost",
        "total",
        "time",
    ]
    table = [
        [
            r.lineup,
            str(r.runs),
            f"{r.passed}/{r.trials}",
            f"{r.pass_rate:.0%}",
            f"{r.avg_attempts:.2f}",
            f"{r.avg_input_tokens:,.0f}",
            f"{r.avg_output_tokens:,.0f}",
            f"{r.avg_reasoning_tokens:,.0f}",
            _cost(r.avg_cost_usd),
            _cost(r.total_cost_usd),
            f"{r.avg_wall_time_ms / 1000:.1f}s",
        ]
        for r in rows
    ]
    widths = [max(len(row[i]) for row in [headers, *table]) for i in range(len(headers))]

    def line(cells: list[str]) -> str:
        # lineup name left-aligned, numbers right-aligned
        return "  ".join(
            c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        )

    return "\n".join(
        [
            line(headers),
            "  ".join("-" * w for w in widths),
            *(line(row) for row in table),
            "",
            "attempts, tokens, cost and time are per-problem averages; total is all runs; ? = unpriced model",
        ]
    )


def _cost(cost: float | None) -> str:
    return "?" if cost is None else f"${cost:.4f}"
