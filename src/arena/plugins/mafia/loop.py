"""One game of Mafia: nights and days until a side wins.

Code applies the rules (game.py). Models only choose: the mafia a kill, the
detective a check, every player a speech and a vote. Each call is a fresh
single message holding that player's own view (prompt in roles.py), so the
calls table records exactly what each agent saw. Every event also goes to the
mafia_events table, so a game can be read back as a transcript.
"""

from typing import Any

from arena.core.lineup import Lineup
from arena.core.plugin import CallLimitReached, ModelCallFailed, Outcome, Session
from arena.core.runlog import utc_now
from arena.plugins.mafia import roles
from arena.plugins.mafia.game import (
    Player,
    Side,
    check_targets,
    deal,
    kill_targets,
    parse_choice,
    play_rng,
    resolve_votes,
    speaking_order,
    vote_targets,
    winner,
)

EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS mafia_events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    problem_id TEXT NOT NULL,  -- the game id
    day INTEGER NOT NULL,  -- the night or day number; 0 for the deal
    phase TEXT NOT NULL,  -- setup | night | day
    kind TEXT NOT NULL,  -- role | kill | check | speech | vote | elimination | announcement
    actor TEXT,  -- the player acting, or narrator
    target TEXT,  -- the player acted on, if any
    text TEXT,  -- the role, check result, speech, or narrator's announcement
    created_at TEXT NOT NULL
);
"""


class MafiaGame:
    def __init__(self, game_id: str, lineup: Lineup, session: Session, players: int) -> None:
        self.game_id = game_id
        self.lineup = lineup
        self.session = session
        self.state = deal(game_id, players)
        self.rng = play_rng(game_id)
        self.public: list[str] = []  # fact lines and speeches: all any player sees of the game
        self.checks: list[str] = []  # the detective's results, seen only by the detective
        self.out: list[dict[str, Any]] = []  # who left the game, in order
        self.invalid = {"target": 0, "check": 0, "vote": 0}
        self.checks_made = self.mafia_found = 0
        self.town_votes = self.town_votes_on_mafia = 0

    async def play(self) -> Outcome:
        for p in self.state.players:
            self.event("setup", 0, "role", p.name, None, p.role)
        n, side, ended, error = 0, None, "", None
        try:
            while True:
                n += 1
                await self.night(n)
                if side := winner(self.state):
                    ended = f"night {n}"
                    break
                await self.day(n)
                if side := winner(self.state):
                    ended = f"day {n}"
                    break
        except ModelCallFailed as e:
            error = str(e)
            print(f"   model call failed: {error}")
        except CallLimitReached as e:
            error = f"game unfinished: {e}"
            print(f"   stopped: {error}")
        return self.outcome(side, ended, n, error)

    async def night(self, n: int) -> None:
        state = self.state
        targets = kill_targets(state)
        victim: str | None = None
        chooser = "random"  # who picked the victim; "random" if no mafia reply named a valid target
        proposal: roles.Proposal | None = None
        mafia = state.mafia()
        # With two mafia alive, the first proposes and the second, seeing that, decides.
        for i, m in enumerate(mafia):
            partner_decides = i == 0 and len(mafia) > 1
            reply = await self.ask(m, roles.kill_task(n, targets, proposal, partner_decides))
            choice = parse_choice(reply, "TARGET", targets)
            if choice.name is not None:
                victim, chooser = choice.name, m.name  # the last valid choice stands
            else:
                self.invalid["target"] += 1
            proposal = roles.Proposal(m.name, choice.name, roles.one_line(reply))
        if victim is None:
            victim = self.rng.choice(targets)  # every night kills, so every game ends

        detective = next((p for p in state.alive() if p.role == "detective"), None)
        if detective is not None:
            options = check_targets(state, detective)
            reply = await self.ask(detective, roles.check_task(n, options))
            checked = parse_choice(reply, "CHECK", options).name
            is_mafia = checked is not None and state.get(checked).role == "mafia"
            if checked is None:
                self.invalid["check"] += 1
            else:
                self.checks_made += 1
                self.mafia_found += is_mafia
            result = roles.check_result(n, checked, is_mafia)
            self.checks.append(result)  # the check resolves before the kill
            self.event("night", n, "check", detective.name, checked, result)

        self.event("night", n, "kill", chooser, victim, None)
        dead = state.get(victim)
        dead.alive = False
        self.out.append({"name": dead.name, "role": dead.role, "when": f"night {n}"})
        print(f"   night {n}: {dead.name} killed ({dead.role})")
        await self.announce("night", n, [roles.death_fact(n, dead)])

    async def day(self, n: int) -> None:
        state = self.state
        speakers = speaking_order(state, n)
        for p in speakers:
            reply = await self.ask(p, roles.speech_task(n))
            text = roles.one_line(reply) or roles.SILENT
            self.public.append(roles.speech_line(n, p.name, text))
            self.event("day", n, "speech", p.name, None, text)

        # Blind: nobody's vote is shared until everyone has voted.
        votes: dict[str, str | None] = {}
        for p in speakers:
            options = vote_targets(state, p)
            choice = parse_choice(
                await self.ask(p, roles.vote_task(n, options)), "VOTE", options, allow_none=True
            )
            votes[p.name] = choice.name
            if not choice.valid:
                self.invalid["vote"] += 1
            if p.role != "mafia" and choice.name is not None:
                self.town_votes += 1
                self.town_votes_on_mafia += state.get(choice.name).role == "mafia"
            self.event("day", n, "vote", p.name, choice.name, None)

        chosen = resolve_votes(votes, self.rng)
        eliminated = state.get(chosen) if chosen else None
        if eliminated is not None:
            eliminated.alive = False
            self.out.append({"name": eliminated.name, "role": eliminated.role, "when": f"day {n}"})
            self.event("day", n, "elimination", None, eliminated.name, eliminated.role)
            print(f"   day {n}: {eliminated.name} voted out ({eliminated.role})")
        else:
            print(f"   day {n}: no one voted out")
        await self.announce("day", n, [roles.votes_fact(n, votes), roles.elimination_fact(n, eliminated)])

    async def ask(self, player: Player, task: str) -> str:
        content = roles.player_prompt(self.state, player, self.checks, self.public, task)
        role = self.lineup.roles[player.role]
        reply = await self.session.call(
            f"{player.role}:{player.name}", role, roles.PLAYER_SYSTEM, [{"role": "user", "content": content}]
        )
        return reply.text

    async def announce(self, phase: str, n: int, facts: list[str]) -> None:
        """Add the facts to the public record; the narrator's telling goes only to the transcript."""
        self.public += facts
        reply = await self.session.call(
            "narrator",
            self.lineup.roles["narrator"],
            roles.NARRATOR_SYSTEM,
            [{"role": "user", "content": roles.narrate_task(facts)}],
        )
        self.event(phase, n, "announcement", "narrator", None, reply.text)

    def event(
        self, phase: str, n: int, kind: str, actor: str | None, target: str | None, text: str | None
    ) -> None:
        self.session.log.db.execute(
            """INSERT INTO mafia_events
                   (run_id, problem_id, day, phase, kind, actor, target, text, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.session.run_id, self.game_id, n, phase, kind, actor, target, text, utc_now()),
        )
        self.session.log.db.commit()

    def outcome(self, side: Side | None, ended: str, nights: int, error: str | None) -> Outcome:
        metrics = {
            "winner": side,
            "nights": nights,
            "days": nights if ended.startswith("day") else nights - 1,
            "players": len(self.state.players),
            "mafia": [p.name for p in self.state.players if p.role == "mafia"],
            "out": self.out,
            "detective_checks": self.checks_made,
            "detective_found_mafia": self.mafia_found,
            "town_votes": self.town_votes,
            "town_votes_on_mafia": self.town_votes_on_mafia,
            "invalid": self.invalid,
        }
        if error is not None:
            return self.session.outcome(score=0.0, verdict="ERROR, not scored", error=error, metrics=metrics)
        assert side is not None
        return self.session.outcome(
            score=1.0 if side == "town" else 0.0, verdict=f"{side.upper()} WON on {ended}", metrics=metrics
        )
