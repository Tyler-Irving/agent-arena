"""The task board the orchestrator pulls work from.

TaskBoard is a Protocol so the SQLite version can be swapped for Redis
Streams later without touching the orchestrator.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from arena.core.runlog import DEFAULT_DB


class Task(BaseModel):
    id: int
    run_id: int
    problem_id: str


class TaskBoard(Protocol):
    async def put(self, run_id: int, problem_id: str) -> int: ...
    async def next(self, run_id: int) -> Task | None: ...
    async def complete(self, task_id: int) -> None: ...
    async def fail(self, task_id: int, reason: str) -> None: ...


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    problem_id TEXT NOT NULL,
    status TEXT NOT NULL,  -- pending | running | done | failed
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqliteTaskBoard:
    def __init__(self, path: Path = DEFAULT_DB) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    async def put(self, run_id: int, problem_id: str) -> int:
        now = _now()
        cur = self.db.execute(
            "INSERT INTO tasks (run_id, problem_id, status, created_at, updated_at) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (run_id, problem_id, now, now),
        )
        self.db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def next(self, run_id: int) -> Task | None:
        # Claim the oldest pending task in one statement so two workers can't take the same one.
        row = self.db.execute(
            """UPDATE tasks SET status = 'running', updated_at = ?
               WHERE id = (SELECT id FROM tasks WHERE run_id = ? AND status = 'pending'
                           ORDER BY id LIMIT 1)
               RETURNING id, run_id, problem_id""",
            (_now(), run_id),
        ).fetchone()
        self.db.commit()
        return Task(id=row[0], run_id=row[1], problem_id=row[2]) if row else None

    async def complete(self, task_id: int) -> None:
        self._finish(task_id, "done", None)

    async def fail(self, task_id: int, reason: str) -> None:
        self._finish(task_id, "failed", reason)

    def _finish(self, task_id: int, status: str, reason: str | None) -> None:
        self.db.execute(
            "UPDATE tasks SET status = ?, reason = ?, updated_at = ? WHERE id = ?",
            (status, reason, _now(), task_id),
        )
        self.db.commit()
