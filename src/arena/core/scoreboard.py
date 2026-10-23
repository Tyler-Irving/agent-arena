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
import textwrap
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from arena.core.plugin import Plugin
from arena.core.runlog import RunLog

# Results logged before plugins have no score; their passed flag is the score.
SCORE = "COALESCE(results.score, results.passed)"
# The attempt limit, which splits a lineup's rows, only for plugins that use attempts.
LIMIT = "CASE WHEN :attempts THEN runs.max_attempts END"

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
       {LIMIT} AS max_attempts,
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
WHERE runs.plugin = :plugin AND runs.id >= :since
GROUP BY runs.lineup, {LIMIT}, runs.settings
"""

RUN_RANGE = f"""
WITH full_runs AS (
    SELECT runs.lineup, {LIMIT} AS max_attempts, runs.settings, AVG({SCORE}) AS pass_rate
    FROM results
    JOIN runs ON runs.id = results.run_id
    WHERE runs.plugin = :plugin AND runs.id >= :since
      AND runs.problem IS NULL AND runs.finished_at IS NOT NULL AND NOT COALESCE(runs.interrupted, 0)
    GROUP BY runs.id
)
SELECT lineup, max_attempts, settings, MIN(pass_rate) AS low, MAX(pass_rate) AS high
FROM full_runs
GROUP BY lineup, max_attempts, settings
"""


class ScoreRow(BaseModel):
    lineup: str
    # None for runs logged before the limit was recorded, and for plugins that don't use attempts
    max_attempts: int | None
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
        return settings_label(self.lineup, self.settings)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.trials

    @property
    def avg_cost_usd(self) -> float | None:
        return None if self.total_cost_usd is None else self.total_cost_usd / self.trials


def scoreboard(log: RunLog, plugin: Plugin[Any], since: int | None = None) -> list[ScoreRow]:
    """One row per lineup of `plugin`, best pass rate first, then cheaper, then faster.

    `since` limits the board to that run id and later, e.g. to leave out runs
    made before a change to the problems or the loop.
    """
    params = {"plugin": plugin.name, "since": since or 0, "attempts": plugin.uses_attempts}
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


def format_scoreboard(rows: list[ScoreRow], plugin: Plugin[Any]) -> str:
    """The table, in the plugin's words. Attempt columns only for plugins that use attempts."""
    words = plugin.wording
    # (header, cell, attempts only)
    columns: list[tuple[str, Callable[[ScoreRow], str], bool]] = [
        ("lineup", lambda r: r.label, False),
        ("limit", lambda r: "?" if r.max_attempts is None else str(r.max_attempts), True),
        ("runs", lambda r: str(r.runs), False),
        (words.passed, lambda r: f"{_count(r.passed)}/{r.trials}", False),
        (words.rate, lambda r: f"{r.pass_rate:.0%}", False),
        ("per run", lambda r: _range(r.run_low, r.run_high), False),
        ("attempts", lambda r: f"{r.avg_attempts:.2f}", True),
        ("in tok", lambda r: f"{r.avg_input_tokens:,.0f}", False),
        ("out tok", lambda r: f"{r.avg_output_tokens:,.0f}", False),
        ("reasoning", lambda r: f"{r.avg_reasoning_tokens:,.0f}", False),
        ("cost", lambda r: _cost(r.avg_cost_usd), False),
        ("total", lambda r: _cost(r.total_cost_usd), False),
        ("time", lambda r: f"{r.avg_wall_time_ms / 1000:.1f}s", False),
    ]
    # (note, attempts only), joined and wrapped
    notes = [
        (f"limit = max attempts per {words.task};", True),
        (f"per run = lowest-highest {words.rate} rate of a single full run (- if none);", False),
        ("attempts,", True),
        (f"tokens, cost and time are per-{words.task} averages; total is all runs;", False),
        ("? = unpriced model", False),
        (", or limit not recorded", True),
    ]
    shown = [c for c in columns if plugin.uses_attempts or not c[2]]
    table = format_table(
        [header for header, _, _ in shown], [[cell(r) for _, cell, _ in shown] for r in rows]
    )
    note = " ".join(n for n, only in notes if plugin.uses_attempts or not only).replace(" ,", ",")
    return "\n".join([*table, "", textwrap.fill(note, width=90, break_on_hyphens=False)])


def settings_label(name: str, settings: str | None) -> str:
    """`name`, plus the plugin settings (runs.settings JSON) if there are any: "some-lineup [players=5]"."""
    if not settings:
        return name
    return f"{name} [{', '.join(f'{k}={v}' for k, v in json.loads(settings).items())}]"


def format_table(headers: list[str], rows: list[list[str]], left: int = 1) -> list[str]:
    """Columns padded to fit, under a rule: the first `left` columns left-aligned, the rest right."""
    widths = [max(len(row[i]) for row in [headers, *rows]) for i in range(len(headers))]

    def line(cells: list[str]) -> str:
        return "  ".join(
            c.ljust(w) if i < left else c.rjust(w) for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        ).rstrip()

    return [line(headers), "  ".join("-" * w for w in widths), *(line(row) for row in rows)]


def _range(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return "-"
    return f"{low:.0%}" if low == high else f"{low:.0%}-{high:.0%}"


def _count(total: float) -> str:
    return str(int(total)) if total.is_integer() else f"{total:.1f}"


def _cost(cost: float | None) -> str:
    return "?" if cost is None else f"${cost:.4f}"
