"""The code bounty panel for arena watch: where a problem is in the solo or team loop.

Only arena.watch imports this module (by name), so the plugin itself never loads Rich.
"""

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from arena.core.events import Event, brief
from arena.watch.views import RunInfo, ViewOptions, epoch, seconds

TEAM = ["planner", "coder", "grader", "reviewer", "orchestrator"]
SOLO = ["solo", "grader"]
MARKS = {"done": ("✔", "green"), "failed": ("✘", "red")}


class CodeBountyView:
    def __init__(self, task_id: str, run: RunInfo, options: ViewOptions) -> None:
        self.task_id = task_id
        self.solo = run.mode == "solo"
        self.stages = SOLO if self.solo else TEAM
        self.models = {**{r: m.split("/", 1)[-1] for r, m in run.roles.items()}, "grader": "docker"}
        self.max_attempts = run.max_attempts
        self.attempt = 0  # the attempt being written or graded
        self.plan = 0
        self.marks: dict[str, str] = {}  # stage -> done | failed | flight
        self.since: dict[str, float] = {}  # stage in flight -> when it started
        self.graded: dict | None = None  # the last graded event's data
        self.review: str | None = None
        self.decision: dict | None = None
        self.verdict: str | None = None

    def update(self, e: Event) -> None:
        stage = (
            e.role if e.kind.startswith("call_") else "grader" if e.kind in ("grading", "graded") else None
        )
        if e.kind == "call_started" and stage in ("coder", "solo"):
            self.attempt += 1
            self.marks = {"planner": "done"} if not self.solo else {}  # a new cycle after the plan
        if e.kind == "call_started" and stage == "planner":
            self.marks = {}
        if e.kind in ("call_started", "grading") and stage is not None:
            self.marks[stage] = "flight"
            self.since[stage] = epoch(e.ts)
        elif e.kind == "call_finished" and stage is not None:
            self.marks[stage] = "failed" if e.data.get("error") else "done"
        elif e.kind == "graded":
            self.marks["grader"] = "done" if e.data["passed"] else "failed"
            self.graded = e.data
            self.review = self.decision = None
        elif e.kind in ("plan", "replan"):
            self.plan = e.data["plan"]
        elif e.kind == "review":
            self.review = e.data["text"]
        elif e.kind == "decision":
            self.decision = e.data
        elif e.kind == "task_finished":
            self.verdict = e.data.get("verdict") or e.data.get("reason")
            self.marks = {s: m for s, m in self.marks.items() if m != "flight"}

    def render(self, now: float) -> RenderableType:
        title = f"{self.task_id} · attempt {self.attempt}/{self.max_attempts}"
        if not self.solo:
            title += f" · plan {self.plan}"
        lines: list[RenderableType] = [Text(title, style="bold"), self.chain(now)]
        if last := self.last():
            lines.append(Text(f"last: {last}"))
        if self.verdict:
            lines.append(Text(self.verdict, style="bold"))
        return Group(*lines)

    def chain(self, now: float) -> Table:
        arrow = " ⇄ " if self.solo else " ──▶ "
        grid = Table.grid()
        for i, _ in enumerate(self.stages):
            if i:
                grid.add_column()  # the arrow
            grid.add_column()
        names, models, marks = [], [], []
        for i, stage in enumerate(self.stages):
            if i:
                names.append(Text(arrow, style="dim"))
                models.append(Text(""))
                marks.append(Text(""))
            names.append(Text(stage, style="bold"))
            models.append(Text(self.models.get(stage, ""), style="cyan"))
            marks.append(self.mark(stage, now))
        grid.add_row(*names)
        grid.add_row(*models)
        grid.add_row(*marks)
        return grid

    def mark(self, stage: str, now: float) -> Text:
        mark = self.marks.get(stage)
        if mark == "flight":
            return Text(f"● {seconds(now - self.since[stage])}…", style="yellow")
        symbol, style = MARKS.get(mark or "", ("", ""))
        return Text(f"  {symbol}", style=style)

    def last(self) -> str:
        if self.graded is None:
            return ""
        g = self.graded
        parts = [f"attempt {g['attempt']} " + ("✔" if g["passed"] else "✘")]
        if not g["has_code"]:
            parts[0] += " no code in reply"
        elif g["timed_out"]:
            parts[0] += " timed out"
        else:
            parts[0] += f" {g['tests_passed']}/{g['tests_total']} tests"
        if self.review:
            parts.append(f"review: {brief(self.review, 70)}")
        if self.decision:
            d = self.decision
            parts.append(
                f"{d['decision']}: {brief(d['reason'], 50)}" if d["parsed"] else "RETRY (no decision)"
            )
        return " · ".join(parts)

    def mask(self, event: Event) -> str | None:
        return None


def make_view(task_id: str, run: RunInfo, options: ViewOptions) -> CodeBountyView:
    return CodeBountyView(task_id, run, options)
