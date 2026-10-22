"""The three ways to look at a run: live under `arena run --watch`, attached from another
terminal (`arena watch`), or replayed afterwards (`arena replay`). All three fold the same
events with RunState and draw them with render.screen.
"""

import asyncio
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

from rich.console import Console
from rich.live import Live

from arena.core.events import Event, latest_event_run, read_events
from arena.core.runlog import RunLog
from arena.watch.render import screen
from arena.watch.state import RunState, VersionMismatch
from arena.watch.views import ViewOptions, epoch

POLL_S = 0.25
NO_EVENTS = "run {} has no events (runs from before events were added, and smoke runs, have none)"


class WatchError(Exception):
    """Nothing to show: no database, no such run, no events, an unknown event format."""


def open_readonly(path: Path) -> sqlite3.Connection:
    """The run database, read-only, so a viewer can never change or create it."""
    if not path.exists():
        raise WatchError(f"no database at {path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    try:
        db.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.OperationalError:
        # A WAL database needs a -shm file beside it, which a read-only
        # connection can create only in a writable directory. Without a -wal
        # file nobody is writing the database, so it is safe to read as immutable.
        db.close()
        if Path(f"{path}-wal").exists():
            raise
        db = sqlite3.connect(uri + "&immutable=1", uri=True)
    return db


def run_exists(db: sqlite3.Connection, run_id: int) -> bool:
    return db.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is not None


def run_closed(db: sqlite3.Connection, run_id: int) -> bool:
    """Whether the run's process has finished it (runs.finished_at), events or not."""
    row = db.execute("SELECT finished_at FROM runs WHERE id = ?", (run_id,)).fetchone()
    return row is not None and row[0] is not None


class Follower:
    """Reads a run's new events and folds them into a RunState."""

    def __init__(self, db: sqlite3.Connection, run_id: int, options: ViewOptions) -> None:
        self.db = db
        self.run_id = run_id
        self.state = RunState(options)
        self.cursor = 0  # id of the last event read

    def poll(self) -> list[Event]:
        new = read_events(self.db, self.run_id, self.cursor)
        for e in new:
            self.state.apply(e)
            self.cursor = e.id
        return new

    @property
    def done(self) -> bool:
        return self.state.finished_at is not None


# --- arena run --watch -------------------------------------------------------------


async def watch_run(
    command: Awaitable[int], log: RunLog, options: ViewOptions, console: Console | None = None
) -> int:
    """Run `command` (the CLI's runs) under a live view of whichever run `log` started last.

    The run's own print lines still appear, above the live region. The view is
    drawn from the events the run writes, read through the run's own connection.
    """
    console = console or Console()
    follower: Follower | None = None
    failure: list[BaseException] = []

    def refresh(live: Live) -> None:
        nonlocal follower
        run_id = log.last_run_id
        if run_id is not None and (follower is None or follower.run_id != run_id):
            follower = Follower(log.db, run_id, options)  # a new run (--repeat): start over
        if follower is not None:
            follower.poll()
        state = follower.state if follower is not None else RunState(options)
        live.update(screen(state, time.time(), console), refresh=True)

    async def poll(live: Live) -> None:
        try:
            while True:
                refresh(live)
                await asyncio.sleep(POLL_S)
        except Exception as e:  # a viewer bug must not stop the run
            failure.append(e)

    try:
        with Live(console=console, auto_refresh=False, redirect_stdout=True, redirect_stderr=True) as live:
            poller = asyncio.create_task(poll(live))
            try:
                return await command
            finally:
                poller.cancel()
                with suppress(asyncio.CancelledError):
                    await poller
                if not failure:
                    with suppress(Exception):
                        refresh(live)  # the final frame
    finally:
        if failure:
            print(f"watch view stopped: {type(failure[0]).__name__}: {failure[0]}", file=sys.stderr)


# --- arena watch ---------------------------------------------------------------------


async def attach(
    path: Path,
    run_id: int | None,
    options: ViewOptions,
    console: Console | None = None,
    poll_s: float = POLL_S,
) -> int:
    """Follow a run from another process until it finishes. Returns the exit code."""
    console = console or Console()
    try:
        db = open_readonly(path)
        run_id = await pick_run(db, run_id, console, poll_s)
        follower = Follower(db, run_id, options)
        closed_before = False
        with Live(console=console, auto_refresh=False) as live:
            while True:
                # finished_at is set just before run_finished is logged, so a run
                # counts as ended without run_finished only if it stays that way for a poll.
                closed = run_closed(db, run_id)
                new = follower.poll()
                if follower.done or (closed_before and not new):
                    follower.state.unfinished = not follower.done
                    live.update(screen(follower.state, time.time(), console), refresh=True)
                    return 0
                closed_before = closed
                live.update(screen(follower.state, time.time(), console, staleness=True), refresh=True)
                await asyncio.sleep(poll_s)
    except (WatchError, VersionMismatch) as e:
        print(e, file=sys.stderr)
        return 2


async def pick_run(db: sqlite3.Connection, run_id: int | None, console: Console, poll_s: float) -> int:
    """The run to follow: the one asked for, the latest one still going, or the next one to start."""
    if run_id is not None:
        if not run_exists(db, run_id):
            raise WatchError(f"no run {run_id}")
        if db.execute("SELECT 1 FROM events WHERE run_id = ? LIMIT 1", (run_id,)).fetchone() is None:
            raise WatchError(NO_EVENTS.format(run_id))
        return run_id
    latest = latest_event_run(db)
    if latest is not None and not run_closed(db, latest):
        return latest
    console.print("waiting for a new run… (Ctrl-C to stop)", style="dim")
    newest = db.execute("SELECT max(id) FROM runs").fetchone()[0] or 0
    while True:
        row = db.execute("SELECT min(run_id) FROM events WHERE run_id > ?", (newest,)).fetchone()
        if row[0] is not None:
            return row[0]
        await asyncio.sleep(poll_s)


# --- arena replay --------------------------------------------------------------------


async def replay(
    path: Path,
    run_id: int,
    speed: float,
    options: ViewOptions,
    console: Console | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Play a run's events back, with the gaps between them divided by `speed` (0: final frame only)."""
    console = console or Console()
    try:
        db = open_readonly(path)
        if not run_exists(db, run_id):
            raise WatchError(f"no run {run_id}")
        events = read_events(db, run_id)
        if not events:
            raise WatchError(NO_EVENTS.format(run_id))
        state = RunState(options)
        if speed == 0:
            for e in events:
                state.apply(e)
            state.unfinished = state.finished_at is None
            console.print(screen(state, epoch(events[-1].ts)))
            return 0
        with Live(console=console, auto_refresh=False) as live:
            clock = epoch(events[0].ts)  # the virtual time being shown
            for e in events:
                # Sleep through the gap in short steps, so in-flight timers keep moving.
                while (gap := epoch(e.ts) - clock) > 1e-6:
                    step = min(gap, POLL_S * speed)
                    await sleep(step / speed)
                    clock += step
                    live.update(screen(state, clock, console), refresh=True)
                state.apply(e)
                clock = max(clock, epoch(e.ts))
                live.update(screen(state, clock, console), refresh=True)
            state.unfinished = state.finished_at is None
            live.update(screen(state, clock, console), refresh=True)
        return 0
    except (WatchError, VersionMismatch) as e:
        print(e, file=sys.stderr)
        return 2
