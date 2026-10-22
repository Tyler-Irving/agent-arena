"""Task views: the panel that shows one task's progress, and how a plugin provides its own.

A plugin can ship `arena/plugins/<name>/view.py` with a `make_view(task_id, run,
options)` that returns a TaskView. A plugin without one gets GenericView, which
lists its events, so a new plugin is watchable from the first event it emits.
"""

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from rich.console import Group, RenderableType
from rich.text import Text

from arena.core.events import Event, brief


@dataclass
class RunInfo:
    """What run_started says about the run."""

    run_id: int
    lineup: str
    plugin: str
    mode: str
    roles: dict[str, str]  # role -> model
    tasks: list[str]
    max_attempts: int
    max_calls: int
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_event(cls, e: Event) -> "RunInfo":
        d = e.data
        return cls(
            run_id=e.run_id,
            lineup=d["lineup"],
            plugin=d["plugin"],
            mode=d["mode"],
            roles=d["roles"],
            tasks=d["tasks"],
            max_attempts=d["max_attempts"],
            max_calls=d["max_calls"],
            settings=d.get("settings") or {},
        )


@dataclass
class ViewOptions:
    hide_roles: bool = False  # Mafia: hide each role until that player is out or the game ends


class TaskView(Protocol):
    def update(self, event: Event) -> None:
        """Every event of this task, in order: its calls, its plugin events, task_started/finished."""
        ...

    def render(self, now: float) -> RenderableType:
        """The task panel at time `now` (epoch seconds; a virtual clock when replaying)."""
        ...

    def mask(self, event: Event) -> str | None:
        """For a call event: a label to show instead of its role, model and numbers, or None to show them."""
        ...


ViewFactory = Callable[[str, RunInfo, ViewOptions], TaskView]


def view_for(plugin: str) -> ViewFactory:
    """The plugin's own make_view, or GenericView if it has none."""
    module = f"arena.plugins.{plugin}.view"
    try:
        return importlib.import_module(module).make_view
    except ModuleNotFoundError as e:
        # Only a missing view (or plugin) falls back; a view that fails to import is a bug.
        if e.name not in (module, f"arena.plugins.{plugin}"):
            raise
        return GenericView


def epoch(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


def seconds(s: float) -> str:
    return f"{s:.0f}s" if s >= 10 else f"{s:.1f}s"


class GenericView:
    """The task's recent plugin events, as `kind  key=value, ...`, and the call in flight."""

    SHOWN = 10

    def __init__(self, task_id: str, run: RunInfo, options: ViewOptions) -> None:
        self.task_id = task_id
        self.events: list[Event] = []
        self.calling: Event | None = None  # call_started with no call_finished yet

    def update(self, event: Event) -> None:
        if event.kind == "call_started":
            self.calling = event
        elif event.kind == "call_finished":
            self.calling = None
        elif event.kind not in ("task_started", "task_finished"):
            self.events.append(event)

    def render(self, now: float) -> RenderableType:
        lines = [Text(self.task_id, style="bold")]
        for e in self.events[-self.SHOWN :]:
            fields = ", ".join(f"{k}={brief(str(v), 60)}" for k, v in e.data.items())
            lines.append(Text.assemble((f" {e.kind:<12}", "cyan"), brief(fields, 100)))
        if self.calling is not None:
            waited = seconds(now - epoch(self.calling.ts))
            lines.append(Text(f" ● {self.calling.role} ({self.calling.model}) {waited}…", style="yellow"))
        return Group(*lines)

    def mask(self, event: Event) -> str | None:
        return None
