"""Events: what happens in a run, one row each, for watching it live or replaying it.

Core writes the run, task and call events; a plugin adds its own through
Session.emit. Every event is committed as it happens, and the database is in WAL
mode, so a viewer in another process sees it right away without ever blocking
the run. The table itself is in runlog.py's SCHEMA. Nothing here imports Rich:
the viewer (arena.watch) reads events, core only writes them.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# In run_started's data. Bump it whenever an event's shape changes, so an old
# viewer refuses a run it can't read instead of drawing it wrong.
EVENTS_VERSION = 1


class Event(BaseModel):
    id: int
    run_id: int
    task_id: str | None  # the problem_id column; None for run_started and run_finished
    ts: str  # utc_now() ISO timestamp
    kind: str
    role: str | None  # call events only: the Session role_name, e.g. "coder" or "mafia:Blake"
    model: str | None  # call events only
    data: dict[str, Any]


def read_events(db: sqlite3.Connection, run_id: int, after_id: int = 0) -> list[Event]:
    """The run's events with id > after_id, oldest first."""
    rows = db.execute(
        "SELECT id, run_id, problem_id, ts, kind, role, model, data FROM events"
        " WHERE run_id = ? AND id > ? ORDER BY id",
        (run_id, after_id),
    ).fetchall()  # fetch everything now, so no read stays open
    return [
        Event(
            id=r[0],
            run_id=r[1],
            task_id=r[2],
            ts=r[3],
            kind=r[4],
            role=r[5],
            model=r[6],
            data=json.loads(r[7]),
        )
        for r in rows
    ]


def latest_event_run(db: sqlite3.Connection) -> int | None:
    """The newest run that has any events."""
    return db.execute("SELECT max(run_id) FROM events").fetchone()[0]


def boot_id() -> str | None:
    """This boot of the machine (Linux), so a viewer can tell a run's pid from one reused after a restart."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def brief(text: str | None, n: int = 200) -> str:
    """A reply as one short line, for event data. The full text is in the calls table."""
    line = " ".join((text or "").split())
    return line if len(line) <= n else line[: n - 1] + "…"
