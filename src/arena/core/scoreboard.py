"""Scoreboard: compare one plugin's lineups on score, attempts, tokens, cost and time.

Every task result in every run counts as one trial, so running a lineup
again adds data rather than replacing it. Runs of one lineup under different
attempt limits, or different plugin settings, get separate rows. A trial's score is 0.0-1.0, and "passed"
sums the scores, so for pass/fail plugins it counts the trials passed. Token
and time figures are per-task averages. The per-run range shows how much a
lineup's pass rate moves between runs, which is the noise any comparison has
to beat. It only uses runs that covered every task and weren't interrupted, so
runs of different task sets aren't compared.
"""

import json

from pydantic import BaseModel

from arena.core.runlog import RunLog

# Results logged before plugins have no score; their passed flag is the score.
SCORE = "COALESCE(results.score, results.passed)"

QUERY = f"""
WITH call_totals AS (
    SELECT run_id, problem_id,
           SUM(input_tokens) AS input_tokens,
           SUM(output_tokens) AS output_tokens,
           SUM(reasoning_tokens) AS reasoning_tokens
    FROM calls
    GROUP BY run_id, problem_id
)
SELECT runs.lineup,
       runs.max_attempts,
       runs.settings,
       COUNT(DISTINCT runs.id) AS runs,
       COUNT(*) AS trials,
       SUM({SCORE}) AS passed,
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
WHERE runs.plugin = ? AND runs.id >= ?
GROUP BY runs.lineup, runs.max_attempts, runs.settings
"""

RUN_RANGE = f"""
WITH full_runs AS (
    SELECT runs.lineup, runs.max_attempts, runs.settings, AVG({SCORE}) AS pass_rate
    FROM results
    JOIN runs ON runs.id = results.run_id
    WHERE runs.plugin = ? AND runs.id >= ?
      AND runs.problem IS NULL AND runs.finished_at IS NOT NULL AND NOT COALESCE(runs.interrupted, 0)
    GROUP BY runs.id
)
SELECT lineup, max_attempts, settings, MIN(pass_rate) AS low, MAX(pass_rate) AS high
FROM full_runs
GROUP BY lineup, max_attempts, settings
"""


class ScoreRow(BaseModel):
    lineup: str
    max_attempts: int | None  # None for runs logged before the limit was recorded
    settings: str | None  # the lineup's plugin settings as JSON; None if it has none
    runs: int
    trials: int
    passed: float  # sum of scores
    avg_attempts: float
    avg_input_tokens: float
    avg_output_tokens: float  # includes reasoning
    avg_reasoning_tokens: float
    total_cost_usd: float | None  # None if any trial used a model with no known price
    avg_wall_time_ms: float
    # lowest and highest single-run pass rate over full runs; None if there are none
    run_low: float | None
    run_high: float | None

    @property
    def label(self) -> str:
        """The lineup name, plus its settings if it has any: "some-lineup [players=5]"."""
        if not self.settings:
            return self.lineup
        return f"{self.lineup} [{', '.join(f'{k}={v}' for k, v in json.loads(self.settings).items())}]"

    @property
    def pass_rate(self) -> float:
        return self.passed / self.trials

    @property
    def avg_cost_usd(self) -> float | None:
        return None if self.total_cost_usd is None else self.total_cost_usd / self.trials


def scoreboard(log: RunLog, plugin: str, since: int | None = None) -> list[ScoreRow]:
    """One row per lineup of `plugin`, best pass rate first, then cheaper, then faster.

    `since` limits the board to that run id and later, e.g. to leave out runs
    made before a change to the problems or the loop.
    """
    params = (plugin, since or 0)
    ranges = {
        (r["lineup"], r["max_attempts"], r["settings"]): (r["low"], r["high"])
        for r in log.db.execute(RUN_RANGE, params)
    }
    rows = []
    for r in log.db.execute(QUERY, params):
        cost_known = r["priced_trials"] == r["trials"]
        low, high = ranges.get((r["lineup"], r["max_attempts"], r["settings"]), (None, None))
        rows.append(
            ScoreRow(
                lineup=r["lineup"],
                max_attempts=r["max_attempts"],
                settings=r["settings"],
                runs=r["runs"],
                trials=r["trials"],
                passed=r["passed"],
                avg_attempts=r["avg_attempts"],
                avg_input_tokens=r["avg_input_tokens"] or 0,
                avg_output_tokens=r["avg_output_tokens"] or 0,
                avg_reasoning_tokens=r["avg_reasoning_tokens"] or 0,
                total_cost_usd=(r["total_cost_usd"] or 0.0) if cost_known else None,
                avg_wall_time_ms=r["avg_wall_time_ms"],
                run_low=low,
                run_high=high,
            )
        )
    return sorted(
        rows,
        key=lambda s: (-s.pass_rate, s.avg_cost_usd is None, s.avg_cost_usd or 0.0, s.avg_wall_time_ms),
    )


def format_scoreboard(rows: list[ScoreRow]) -> str:
    headers = [
        "lineup",
        "limit",
        "runs",
        "passed",
        "pass",
        "per run",
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
            r.label,
            "?" if r.max_attempts is None else str(r.max_attempts),
            str(r.runs),
            f"{_count(r.passed)}/{r.trials}",
            f"{r.pass_rate:.0%}",
            _range(r.run_low, r.run_high),
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
            "limit = max attempts per problem; per run = lowest-highest pass rate of a single full run",
            "(- if none); attempts, tokens, cost and time are per-problem averages; total is all runs;",
            "? = unpriced model, or limit not recorded",
        ]
    )


def _range(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return "-"
    return f"{low:.0%}" if low == high else f"{low:.0%}-{high:.0%}"


def _count(total: float) -> str:
    return str(int(total)) if total.is_integer() else f"{total:.1f}"


def _cost(cost: float | None) -> str:
    return "?" if cost is None else f"${cost:.4f}"
