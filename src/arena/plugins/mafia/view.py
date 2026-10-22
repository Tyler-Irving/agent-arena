"""The Mafia panel for arena watch: the seats, who is acting, the day's votes, the last facts.

Roles are shown by default (god view). With --hide-roles a role stays hidden
until that player is out or the game ends, and so does everything that would
give a living player away: night actions, the mafia's proposal and who chose
the kill, the detective's checks, and which model a living player's calls used.

Only arena.watch imports this module (by name), so the plugin itself never loads Rich.
"""

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from arena.core.events import Event, brief
from arena.watch.views import RunInfo, ViewOptions, epoch, seconds

DOING = {"mafia": "choosing a target", "detective": "checking"}  # night calls, god view only


class MafiaView:
    def __init__(self, task_id: str, run: RunInfo, options: ViewOptions) -> None:
        self.task_id = task_id
        self.hide = options.hide_roles
        self.seats: list[dict] = []  # {seat, name, role} from the deal
        self.out: dict[str, str] = {}  # name -> "✝ night 2" | "✗ out day 1"
        self.phase, self.n, self.step = "setup", 0, None
        self.calling: Event | None = None
        self.night_calls: set[int] = set()  # call_started ids made at night
        self.votes: dict[str, str | None] = {}  # the last revealed votes
        self.invalid: set[str] = set()
        self.speech: tuple[str, str] | None = None  # (speaker, text)
        self.facts: list[str] = []
        self.proposal: dict | None = None
        self.over = False  # game over (won, or the task ended): every role is revealed
        self.result: str | None = None

    # --- folding events ---

    def update(self, e: Event) -> None:
        d = e.data
        if e.kind == "call_started":
            self.calling = e
            if self.phase == "night":
                self.night_calls.add(e.id)
        elif e.kind == "call_finished":
            self.calling = None
        elif e.kind == "deal":
            self.seats = d["players"]
        elif e.kind == "phase":
            self.phase, self.n, self.step = d["phase"], d["n"], d.get("step")
            if self.step == "speeches":
                self.votes, self.invalid = {}, set()
        elif e.kind == "proposal":
            self.proposal = d
        elif e.kind == "check":
            if not self.hide:
                target = d["target"]
                result = f"{target}: {'mafia' if d['is_mafia'] else 'not mafia'}" if target else "wasted"
                self.facts.append(f"night {d['n']}: {d['detective']} checked {result}")
        elif e.kind == "kill":
            self.out[d["victim"]] = f"✝ night {d['n']}"
            self.facts.append(self.kill_fact(d))
            self.proposal = None
        elif e.kind == "speech":
            self.speech = (d["speaker"], d["text"])
        elif e.kind == "votes":
            self.votes, self.invalid = d["votes"], set(d["invalid"])
        elif e.kind == "elimination":
            if d["name"] is None:
                self.facts.append(f"day {d['n']}: no one voted out")
            else:
                self.out[d["name"]] = f"✗ out day {d['n']}"
                self.facts.append(f"day {d['n']}: {d['name']} ({d['role']}) voted out")
        elif e.kind == "win":
            self.over = True
            self.result = f"{d['winner'].upper()} WON on {d['ended']}"
        elif e.kind == "task_finished":
            self.over = True  # also a game cut short, so its roles show
            self.calling = None
            self.result = self.result or e.data.get("verdict") or e.data.get("reason")

    def kill_fact(self, d: dict) -> str:
        killed = f"{d['victim']} ({d['role']}) killed"
        if self.hide:
            return f"night {d['n']}: {killed}"
        p = self.proposal
        if p is not None:
            proposed = f"{p['proposer']} proposed {p['target'] or 'no one valid'}, "
            return f"night {d['n']}: {proposed}{d['by']} chose {d['victim']} · {killed}"
        return f"night {d['n']}: {d['by']} chose {d['victim']} · {killed}"

    # --- hiding ---

    def revealed(self, name: str) -> bool:
        return not self.hide or self.over or name in self.out

    def mask(self, event: Event) -> str | None:
        role = event.role or ""
        if ":" not in role:  # the narrator
            return None
        name = role.split(":", 1)[1]
        if self.revealed(name):
            return None
        return "(night)" if event.id in self.night_calls else name

    # --- drawing ---

    def render(self, now: float) -> RenderableType:
        lines: list[RenderableType] = [self.title(now), self.table(now)]
        if self.speech is not None:
            lines.append(Text(f"last speech: {self.speech[0]}: {brief(self.speech[1], 90)}"))
        for fact in self.facts[-2:]:
            lines.append(Text(f"fact: {fact}"))
        if self.result:
            lines.append(Text(self.result, style="bold"))
        return Group(*lines)

    def title(self, now: float) -> Text:
        where = (
            "setup"
            if self.phase == "setup"
            else f"{self.phase} {self.n}" + (f" · {self.step}" if self.step else "")
        )
        if self.calling is not None and self.phase == "night" and self.hide:
            where += f" · night actions ● {seconds(now - epoch(self.calling.ts))}…"
        elif self.calling is not None and self.calling.role == "narrator":
            where += " · narrating"
        alive = [s for s in self.seats if s["name"] not in self.out]
        mafia = sum(s["role"] == "mafia" for s in alive)  # public: the rules fix how many mafia there are
        counts = f"alive {len(alive)}/{len(self.seats)} · mafia {mafia} · town {len(alive) - mafia}"
        return Text.assemble((f"{self.task_id} · {where}", "bold"), "   ", (counts, "dim"))

    def table(self, now: float) -> Table:
        table = Table(box=None, padding=(0, 2), pad_edge=False)
        for column in ("seat", "name", "role", "status", "vote"):
            table.add_column(column, style="dim" if column == "seat" else None, no_wrap=True)
        acting = self.acting()
        for s in self.seats:
            name = s["name"]
            role = s["role"] if self.revealed(name) else "?"
            if name in self.out:
                status = Text(self.out[name], style="red")
            elif name == acting and self.calling is not None:
                status = Text(
                    f"● {self.doing(s['role'])} {seconds(now - epoch(self.calling.ts))}…", style="yellow"
                )
            else:
                status = Text("alive")
            table.add_row(str(s["seat"]), name, role, status, self.vote(name))
        return table

    def acting(self) -> str | None:
        """The seat to mark as acting now: day calls always, night calls only in god view."""
        if self.calling is None or ":" not in (self.calling.role or ""):
            return None
        if self.phase == "night" and self.hide:
            return None
        return (self.calling.role or "").split(":", 1)[1]

    def doing(self, role: str) -> str:
        if self.phase == "night":
            return DOING.get(role, "acting")
        return "voting" if self.step == "votes" else "speaking"

    def vote(self, name: str) -> str:
        if name in self.invalid:
            return "(invalid)"
        if name in self.votes:
            return self.votes[name] or "none"
        return ""


def make_view(task_id: str, run: RunInfo, options: ViewOptions) -> MafiaView:
    return MafiaView(task_id, run, options)
