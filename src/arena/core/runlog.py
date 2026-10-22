"""SQLite run log: every model call and every task result.

The calls table keeps the full prompt, response and reasoning so a run can be
read back later like a transcript. Plugins add their own tables (Plugin.schema).
"""

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    interrupted INTEGER,  -- 1 if stopped before finishing: Ctrl-C/SIGTERM, or an error aborted it
    settings TEXT  -- the lineup's plugin settings as JSON, NULL if it has none
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
    wall_time_ms INTEGER NOT NULL,
    score REAL,  -- 0.0-1.0; NULL in rows logged before plugins, where passed is the score
    metrics TEXT  -- JSON, whatever the plugin reports beyond the score
);

-- What happens in a run, for watching it live or replaying it (core/events.py).
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,  -- also the cursor a viewer polls from
    run_id INTEGER NOT NULL REFERENCES runs(id),
    problem_id TEXT,  -- the task id; NULL for run_started and run_finished
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    role TEXT,  -- call events only: the Session role_name
    model TEXT,  -- call events only
    data TEXT NOT NULL  -- JSON object
);
CREATE INDEX IF NOT EXISTS events_run ON events (run_id, id);
"""


# Columns added after the first release, as (table, column, type).
ADDED_COLUMNS = [
    ("calls", "truncated", "INTEGER"),
    ("runs", "problem", "TEXT"),
    ("runs", "interrupted", "INTEGER"),
    ("runs", "max_attempts", "INTEGER"),
    ("results", "score", "REAL"),
    ("results", "metrics", "TEXT"),
    ("runs", "settings", "TEXT"),
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
        # WAL, so a viewer reading events in another process never blocks the run.
        # The mode is stored in the file, so this only changes it once.
        mode = self.db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode != "wal":
            raise RuntimeError(f"could not put {path} in WAL mode (got {mode!r})")
        self.db.executescript(SCHEMA)
        # Columns added after the first release; CREATE TABLE IF NOT EXISTS skips
        # existing tables, so add them to older databases here.
        for table, column, kind in ADDED_COLUMNS:
            columns = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        self.db.commit()
        self.last_run_id: int | None = None  # the newest run this RunLog started
        self._anchor = (time.time(), time.monotonic())  # see event_time

    def start_run(
        self,
        lineup: str,
        plugin: str,
        problem: str | None = None,
        max_attempts: int | None = None,
        settings: dict[str, Any] | None = None,
    ) -> int:
        settings_json = json.dumps(settings, sort_keys=True) if settings else None
        cur = self.db.execute(
            "INSERT INTO runs (lineup, plugin, started_at, problem, max_attempts, settings)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (lineup, plugin, utc_now(), problem, max_attempts, settings_json),
        )
        self.db.commit()
        assert cur.lastrowid is not None
        self.last_run_id = cur.lastrowid
        self._anchor = (time.time(), time.monotonic())
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

    def log_result(
        self,
        *,
        run_id: int,
        problem_id: str,
        score: float,
        attempts: int,
        total_calls: int,
        total_cost_usd: float | None,
        wall_time_ms: int,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        # passed and attempts predate plugins; passed means a full score.
        self.db.execute(
            """INSERT INTO results (run_id, problem_id, passed, attempts, total_calls,
                   total_cost_usd, wall_time_ms, score, metrics)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                problem_id,
                int(score == 1.0),
                attempts,
                total_calls,
                total_cost_usd,
                wall_time_ms,
                score,
                json.dumps(metrics or {}),
            ),
        )
        self.db.commit()

    def log_event(
        self,
        run_id: int,
        task_id: str | None,
        kind: str,
        data: dict[str, Any],
        role: str | None = None,
        model: str | None = None,
    ) -> None:
        """One event, committed at once so a viewer sees it right away."""
        self.db.execute(
            "INSERT INTO events (run_id, problem_id, ts, kind, role, model, data)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            # default=str: an odd value in the data can't make logging fail
            (run_id, task_id, self.event_time(), kind, role, model, json.dumps(data, default=str)),
        )
        self.db.commit()

    def event_time(self) -> str:
        """Wall-clock time at the run's start plus monotonic time since, as an ISO timestamp.

        Under WSL2 the wall clock can jump seconds forward and back mid-run (VM
        time sync), which would make replay gaps and elapsed times wrong. A
        monotonic clock keeps every gap between events right.
        """
        wall, mono = self._anchor
        return datetime.fromtimestamp(wall + time.monotonic() - mono, UTC).isoformat()


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
