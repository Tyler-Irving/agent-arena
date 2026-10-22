"""Draw a RunState: header, task list, the current task's panel, and the call log."""

from rich import box
from rich.console import Console, Group, JustifyMethod, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from arena.core.orchestrator import format_cost
from arena.watch.state import CallRow, RunState, TaskRow
from arena.watch.views import epoch, seconds

ICONS = {
    "pending": ("·", "dim"),
    "running": ("●", "yellow"),
    "done": ("✔", "green"),
    "failed": ("✘", "red"),
    "error": ("⚠", "yellow"),
    "interrupted": ("■", "magenta"),
}
CALL_COLUMNS: list[tuple[str, JustifyMethod]] = [
    ("time", "right"),
    ("role", "left"),
    ("model", "left"),
    ("in", "right"),
    ("out", "right"),
    ("latency", "right"),
    ("cost", "right"),
    ("outcome", "left"),
]
STATIC_CALL_ROWS = 15  # call log length when the frame is printed once, not live
SPARE_ROWS = 8  # terminal rows left free under a live view, so printed lines stay visible


def clock(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 3600}:{s // 60 % 60:02}:{s % 60:02}" if s >= 3600 else f"{s // 60}:{s % 60:02}"


def short_model(model: str | None) -> str:
    return (model or "").split("/", 1)[-1]


def header(state: RunState, now: float, staleness: bool) -> Text:
    run = state.run
    if run is None or state.started_at is None:
        return Text("waiting for the run to start…", style="dim")
    end = state.finished_at or (state.last_at if state.unfinished else None) or now
    n, total = state.task_index()
    cost, hidden = state.cost()
    status = state.status()
    parts = [
        f"run {run.run_id}",
        run.lineup,
        run.plugin,
        f"task {n}/{total}",
        clock(end - state.started_at),
        format_cost(cost) + (" + hidden" if hidden else ""),
    ]
    text = Text(" · ".join(parts), style="bold")
    style = {"finished": "green", "running": "yellow"}.get(status, "red")
    text.append(f" · {status}", style=style)
    if staleness and status == "running" and state.last_at is not None:
        text.append(f"  (last event {seconds(now - state.last_at)} ago)", style="dim")
    return text


def task_list(state: RunState) -> Table:
    grid = Table.grid(padding=(0, 1))
    for task in state.tasks.values():
        grid.add_row(*task_row(state, task))
    return grid


def task_row(state: RunState, task: TaskRow) -> list[Text]:
    icon, style = ICONS.get(task.status, ("?", ""))
    if task.status == "done" and task.score is not None and task.score < 1.0:
        icon, style = ICONS["failed"]  # finished, but scored below 1 (e.g. mafia won)
    calls = f"{task.calls} call(s)" if task.calls else ""
    total, hidden = state.cost(task.id)
    cost = format_cost(total) + (" + hidden" if hidden else "") if task.calls else ""
    verdict = Text(task.verdict or "", style=style)
    return [Text(icon, style=style), Text(task.id), verdict, Text(calls), Text(cost)]


def call_log(state: RunState, now: float, rows: int) -> Table:
    table = Table(box=box.SIMPLE_HEAD, padding=(0, 1), pad_edge=False, show_edge=False)
    for name, justify in CALL_COLUMNS:
        if name == "outcome":
            table.add_column(name, max_width=48, no_wrap=True, overflow="ellipsis")
        else:
            table.add_column(name, justify=justify, no_wrap=True, min_width=len(name))
    start = state.started_at or now
    for c in state.calls[-rows:] if rows > 0 else []:
        table.add_row(*call_cells(state, c, start, now))
    return table


def call_cells(state: RunState, c: CallRow, start: float, now: float) -> list[str | Text]:
    when = "+" + clock(epoch(c.started.ts) - start)
    label = state.mask(c.started)
    if label is not None:  # a hidden role: nothing that could give the player away
        flight = Text("●", style="yellow") if c.finished is None else ""
        outcome = "" if c.finished is None else "ok" if c.finished.data.get("error") is None else "error"
        return [when, label, "·", "·", "·", flight, "·", outcome]
    role, model = c.started.role or "", short_model(c.started.model)
    if c.finished is None:
        waited = Text(f"● {seconds(now - epoch(c.started.ts))}…", style="yellow")
        return [when, role, model, "", "", waited, "", ""]
    d = c.finished.data
    error = d.get("error")
    if error is not None:
        outcome = Text("interrupted" if error == "interrupted" else f"error: {error}", style="red")
        return [when, role, model, "", "", seconds(d.get("latency_ms", 0) / 1000), "", outcome]
    outcome = Text("cut off", style="yellow") if d.get("truncated") else Text("ok", style="green")
    return [
        when,
        role,
        model,
        str(d.get("input_tokens", "")),
        str(d.get("output_tokens", "")),
        seconds(d.get("latency_ms", 0) / 1000),
        format_cost(d.get("cost_usd")),
        outcome,
    ]


def screen(
    state: RunState, now: float, console: Console | None = None, staleness: bool = False
) -> RenderableType:
    """The whole view. With a console, the call log is cut to fit its height (for Live)."""
    parts: list[RenderableType] = [header(state, now, staleness)]
    if state.tasks:
        parts.append(task_list(state))
    view = state.views.get(state.current or "")
    if view is not None:
        parts.append(Panel(view.render(now), box=box.ROUNDED, padding=(0, 1)))
    top = Group(*parts)
    rows = STATIC_CALL_ROWS
    if console is not None:
        used = len(console.render_lines(top, console.options, pad=False))
        rows = max(3, console.height - SPARE_ROWS - used - 3)  # 3: the call log's header lines
    if not state.calls:
        return top
    return Group(top, call_log(state, now, rows))
