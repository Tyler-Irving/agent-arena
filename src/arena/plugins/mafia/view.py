"""The Mafia panel for arena watch: the seats, who is acting, the day's votes, the last facts.

Roles are shown by default (god view). With --hide-roles a role stays hidden
until that player is out or the game ends, and so does everything that would
give a living player away: night actions, the mafia's proposal and who chose
the kill, the detective's checks, and which model a living player's calls used
(with their tokens and cost). Night calls stay hidden until the game ends, even
for a player who is out by then: which of a night's calls was theirs would show
whether they proposed or chose, and so where their partner sits. Who is speaking
or voting, and for how long, is public, as it would be at the table.

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
        self.facts: list[tuple[str, dict]] = []  # (kind, data) of checks, kills and eliminations
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
            self.facts.append(("check", d))
        elif e.kind == "kill":
            self.out[d["victim"]] = f"✝ night {d['n']}"
            self.facts.append(("kill", {**d, "proposal": self.proposal}))
            self.proposal = None
        elif e.kind == "speech":
            self.speech = (d["speaker"], d["text"])
        elif e.kind == "votes":
            self.votes, self.invalid = d["votes"], set(d["invalid"])
        elif e.kind == "elimination":
            if d["name"] is not None:
                self.out[d["name"]] = f"✗ out day {d['n']}"
            self.facts.append(("elimination", d))
        elif e.kind == "win":
            self.over = True
            self.result = f"{d['winner'].upper()} WON on {d['ended']}"
        elif e.kind == "task_finished":
            self.over = True  # also a game cut short, so its roles show
            self.calling = None
            self.result = self.result or e.data.get("verdict") or e.data.get("reason")

    def fact(self, kind: str, d: dict) -> str | None:
        """A fact line, or None if --hide-roles still hides it. Decided when drawn, so the end shows all."""
        hidden = self.hide and not self.over
        if kind == "check":
            if hidden:
                return None
            target = d["target"]
            result = f"{target}: {'mafia' if d['is_mafia'] else 'not mafia'}" if target else "wasted"
            return f"night {d['n']}: {d['detective']} checked {result}"
        if kind == "elimination":
            if d["name"] is None:
                return f"day {d['n']}: no one voted out"
            return f"day {d['n']}: {d['name']} ({d['role']}) voted out"
        killed = f"{d['victim']} ({d['role']}) killed"
        if hidden:
            return f"night {d['n']}: {killed}"
        p = d["proposal"]
        proposed = f"{p['proposer']} proposed {p['target'] or 'no one valid'}, " if p else ""
        return f"night {d['n']}: {proposed}{d['by']} chose {d['victim']} · {killed}"

    # --- hiding ---

    def revealed(self, name: str) -> bool:
        return not self.hide or self.over or name in self.out

    def mask(self, event: Event) -> str | None:
        role = event.role or ""
        if ":" not in role:  # the narrator
            return None
        if not self.hide or self.over:
            return None
        if event.id in self.night_calls:
            return "(night)"
        name = role.split(":", 1)[1]
        return None if self.revealed(name) else name

    # --- drawing ---

    def render(self, now: float) -> RenderableType:
        lines: list[RenderableType] = [self.title(now), self.table(now)]
        if self.speech is not None:
            lines.append(Text(f"last speech: {self.speech[0]}: {brief(self.speech[1], 90)}"))
        shown = [f for kind, d in self.facts if (f := self.fact(kind, d)) is not None]
        for fact in shown[-2:]:
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
