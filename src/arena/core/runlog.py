"""SQLite run log: every model call and every problem result.

The calls table keeps the full prompt, response and reasoning so a run can be
read back later like a transcript.
"""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_DB = Path("data/arena.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    lineup TEXT NOT NULL,
    plugin TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    problem TEXT,  -- the --problem selector, NULL when the run covered every problem
    max_attempts INTEGER,  -- the lineup's attempt limit for this run
    interrupted INTEGER  -- 1 if stopped by Ctrl-C/SIGTERM before finishing
);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id),
    problem_id TEXT,
    role TEXT,
    model TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    latency_ms INTEGER,
    cost_usd REAL,
    think INTEGER,
    system TEXT,
    messages TEXT,
    response TEXT,
    reasoning TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    truncated INTEGER
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    problem_id TEXT NOT NULL,
    passed INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    total_calls INTEGER NOT NULL,
    total_cost_usd REAL,
    wall_time_ms INTEGER NOT NULL
);

-- One row per graded attempt, so the final failing grade (which no later
-- call sees) is still in the transcript. code is NULL if the reply had none.
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    problem_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    code TEXT,
    passed INTEGER NOT NULL,
    tests_passed INTEGER,
    tests_total INTEGER,
    timed_out INTEGER,
    output TEXT,
    created_at TEXT NOT NULL
);
"""


# Columns added after the first release, as (table, column, type).
ADDED_COLUMNS = [
    ("calls", "truncated", "INTEGER"),
    ("runs", "problem", "TEXT"),
    ("runs", "interrupted", "INTEGER"),
    ("runs", "max_attempts", "INTEGER"),
]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class RunLog:
    # Writes are tiny and runs are sequential, so plain sqlite3 calls are fine
    # inside async code.
    def __init__(self, path: Path = DEFAULT_DB) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # Columns added after the first release; CREATE TABLE IF NOT EXISTS skips
        # existing tables, so add them to older databases here.
        for table, column, kind in ADDED_COLUMNS:
            columns = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        self.db.commit()

    def start_run(
        self, lineup: str, plugin: str, problem: str | None = None, max_attempts: int | None = None
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (lineup, plugin, started_at, problem, max_attempts) VALUES (?, ?, ?, ?, ?)",
            (lineup, plugin, utc_now(), problem, max_attempts),
        )
        self.db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def finish_run(self, run_id: int, interrupted: bool = False) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at = ?, interrupted = ? WHERE id = ?",
            (utc_now(), int(interrupted), run_id),
        )
        self.db.commit()

    def log_call(
        self,
        *,
        run_id: int | None,
        problem_id: str | None,
        role: str | None,
        model: str,
        think: bool | None,
        system: str,
        messages: list[dict],
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        latency_ms: int | None = None,
        cost_usd: float | None = None,
        response: str | None = None,
        reasoning: str | None = None,
        error: str | None = None,
        truncated: bool | None = None,
    ) -> int:
        cur = self.db.execute(
            """INSERT INTO calls (run_id, problem_id, role, model, input_tokens, output_tokens,
                   reasoning_tokens, latency_ms, cost_usd, think, system, messages, response,
                   reasoning, error, created_at, truncated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                problem_id,
                role,
                model,
                input_tokens,
                output_tokens,
                reasoning_tokens,
                latency_ms,
                cost_usd,
                None if think is None else int(think),
                system,
                json.dumps(messages),
                response,
                reasoning,
                error,
                utc_now(),
                None if truncated is None else int(truncated),
            ),
        )
        self.db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def log_attempt(
        self,
        *,
        run_id: int,
        problem_id: str,
        attempt: int,
        code: str | None,
        passed: bool,
        tests_passed: int | None = None,
        tests_total: int | None = None,
        timed_out: bool | None = None,
        output: str | None = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO attempts (run_id, problem_id, attempt, code, passed, tests_passed,
                   tests_total, timed_out, output, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                problem_id,
                attempt,
                code,
                int(passed),
                tests_passed,
                tests_total,
                None if timed_out is None else int(timed_out),
                output,
                utc_now(),
            ),
        )
        self.db.commit()

    def log_result(
        self,
        *,
        run_id: int,
        problem_id: str,
        passed: bool,
        attempts: int,
        total_calls: int,
        total_cost_usd: float | None,
        wall_time_ms: int,
    ) -> None:
        self.db.execute(
            """INSERT INTO results (run_id, problem_id, passed, attempts, total_calls,
                   total_cost_usd, wall_time_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, problem_id, int(passed), attempts, total_calls, total_cost_usd, wall_time_ms),
        )
        self.db.commit()


_default: RunLog | None = None


def get_runlog() -> RunLog:
    """The process-wide run log the router writes to, opened on first use."""
    global _default
    if _default is None:
        _default = RunLog()
    return _default


def set_runlog(log: RunLog) -> None:
    """Point the router at a different database (tests, alternate paths)."""
    global _default
    _default = log
