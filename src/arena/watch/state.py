"""Fold a run's events into what the screen shows: the run, its tasks, the calls, the task panels.

The same folding serves live watching and replay; only the clock differs.
"""

from dataclasses import dataclass, field

from arena.core.events import EVENTS_VERSION, Event
from arena.watch.views import RunInfo, TaskView, ViewOptions, epoch, view_for


class VersionMismatch(Exception):
    def __init__(self, run_id: int, found: object) -> None:
        super().__init__(f"run {run_id} uses event format v{found}, this viewer reads v{EVENTS_VERSION}")


@dataclass
class TaskRow:
    id: str
    status: str = "pending"  # pending | running | done | failed | error | interrupted
    verdict: str | None = None
    score: float | None = None
    calls: int = 0


@dataclass
class CallRow:
    started: Event
    finished: Event | None = None


@dataclass
class RunState:
    options: ViewOptions = field(default_factory=ViewOptions)
    run: RunInfo | None = None
    started_at: float | None = None
    finished_at: float | None = None
    interrupted: bool = False
    unfinished: bool = False  # the events ended without run_finished (the process was killed)
    last_at: float | None = None  # time of the latest event
    tasks: dict[str, TaskRow] = field(default_factory=dict)
    current: str | None = None  # the task that started last
    views: dict[str, TaskView] = field(default_factory=dict)
    calls: list[CallRow] = field(default_factory=list)

    def apply(self, e: Event) -> None:
        # Never backwards, even if the timestamps go back (WSL2 steps its wall clock back at times).
        self.last_at = max(epoch(e.ts), self.last_at or 0.0)
        if e.kind == "run_started":
            if e.data.get("events_version") != EVENTS_VERSION:
                raise VersionMismatch(e.run_id, e.data.get("events_version"))
            self.run = RunInfo.from_event(e)
            self.started_at = self.last_at
            self.tasks = {t: TaskRow(t) for t in self.run.tasks}
            return
        if e.kind == "run_finished":
            self.finished_at = self.last_at
            self.interrupted = bool(e.data.get("interrupted"))
            return
        if e.task_id is None or self.run is None:
            return
        if e.kind == "task_started":
            self.current = e.task_id
            self.views[e.task_id] = view_for(self.run.plugin)(e.task_id, self.run, self.options)
            self.tasks.setdefault(e.task_id, TaskRow(e.task_id)).status = "running"
        task = self.tasks.setdefault(e.task_id, TaskRow(e.task_id))
        if e.kind == "call_started":
            self.calls.append(CallRow(e))
            task.calls = e.data.get("n", task.calls + 1)
        elif e.kind == "call_finished":
            row = next(
                (c for c in reversed(self.calls) if c.finished is None and c.started.task_id == e.task_id),
                None,
            )
            if row is not None:
                row.finished = e
        elif e.kind == "task_finished":
            task.status = e.data.get("status", "done")
            task.verdict = e.data.get("verdict")
            task.score = e.data.get("score")
        view = self.views.get(e.task_id)
        if view is not None:
            view.update(e)

    def mask(self, e: Event) -> str | None:
        view = self.views.get(e.task_id or "")
        return view.mask(e) if view is not None else None

    def cost(self, task_id: str | None = None) -> tuple[float | None, bool]:
        """(cost of the calls shown, whether some calls' cost is hidden), for the run or one task.

        The total is None if a price is unknown. Hidden calls are left out
        everywhere, or their price would give their model away.
        """
        total: float | None = 0.0
        hidden = False
        for c in self.calls:
            if c.finished is None or c.finished.data.get("error") is not None:
                continue
            if task_id is not None and c.started.task_id != task_id:
                continue
            if self.mask(c.started) is not None:
                hidden = True
                continue
            cost = c.finished.data.get("cost_usd")
            total = None if total is None or cost is None else total + cost
        return total, hidden

    def task_index(self) -> tuple[int, int]:
        """(1-based position of the current task, number of tasks)."""
        if self.run is None:
            return 0, 0
        if self.current is None or self.current not in self.run.tasks:
            return 0, len(self.run.tasks)
        return self.run.tasks.index(self.current) + 1, len(self.run.tasks)

    def status(self) -> str:
        if self.finished_at is not None:
            return "interrupted" if self.interrupted else "finished"
        return "unfinished (no run_finished)" if self.unfinished else "running"
