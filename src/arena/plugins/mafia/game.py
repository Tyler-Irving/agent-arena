"""Mafia rules: the deal, speaking order, votes, choices in replies and the win check.

Pure functions over the game state, with no model calls, so the rules can be
tested on their own. Everything random comes from a generator seeded with the
game id alone, so game-03 deals the same roles and seats in every lineup.
"""

import random
import re
from collections import Counter
from functools import cache
from typing import Literal, NamedTuple

from pydantic import BaseModel

Role = Literal["mafia", "detective", "villager"]
Side = Literal["town", "mafia"]

NAMES = ["Avery", "Blake", "Casey", "Drew", "Emery", "Finley", "Gray"]
MIN_PLAYERS, MAX_PLAYERS = 5, len(NAMES)
DEFAULT_PLAYERS = MAX_PLAYERS


def role_counts(players: int) -> dict[Role, int]:
    """7 players: 2 mafia; 5-6 players: 1. Always one detective."""
    if not MIN_PLAYERS <= players <= MAX_PLAYERS:
        raise ValueError(f"a game needs {MIN_PLAYERS}-{MAX_PLAYERS} players, got {players}")
    mafia = 2 if players == 7 else 1
    return {"mafia": mafia, "detective": 1, "villager": players - mafia - 1}


class Player(BaseModel):
    name: str
    role: Role
    alive: bool = True


class GameState(BaseModel):
    game_id: str
    players: list[Player]  # in seat order

    def alive(self) -> list[Player]:
        return [p for p in self.players if p.alive]

    def get(self, name: str) -> Player:
        return next(p for p in self.players if p.name == name)

    def mafia(self) -> list[Player]:
        """Living mafia, in seat order."""
        return [p for p in self.alive() if p.role == "mafia"]

    def town(self) -> list[Player]:
        """Living town players (detective and villagers), in seat order."""
        return [p for p in self.alive() if p.role != "mafia"]


def deal(game_id: str, players: int) -> GameState:
    """Seats and roles for a game, from the game id alone."""
    rng = random.Random(f"{game_id}:deal")
    names = rng.sample(NAMES, players)
    roles: list[Role] = [role for role, n in role_counts(players).items() for _ in range(n)]
    rng.shuffle(roles)
    return GameState(
        game_id=game_id, players=[Player(name=n, role=r) for n, r in zip(names, roles, strict=True)]
    )


def play_rng(game_id: str) -> random.Random:
    """The generator for tie-breaks and replacing invalid targets during play."""
    return random.Random(f"{game_id}:play")


def speaking_order(state: GameState, day: int) -> list[Player]:
    """Living players in seat order. Day N starts N-1 seats after seat 0, or at the next living seat."""
    n = len(state.players)
    start = (day - 1) % n
    seats = [state.players[(start + i) % n] for i in range(n)]
    return [p for p in seats if p.alive]


def vote_targets(state: GameState, voter: Player) -> list[str]:
    return [p.name for p in state.alive() if p is not voter]


def kill_targets(state: GameState) -> list[str]:
    return [p.name for p in state.town()]


def check_targets(state: GameState, detective: Player) -> list[str]:
    return [p.name for p in state.alive() if p is not detective]


def resolve_votes(votes: dict[str, str | None], rng: random.Random) -> str | None:
    """Who is eliminated: the plurality of the votes cast, a seeded pick among a tie, or nobody.

    `votes` maps each voter to a valid target, or None for an abstention.
    """
    tally = Counter(target for target in votes.values() if target is not None)
    if not tally:
        return None
    top = max(tally.values())
    tied = sorted(name for name, count in tally.items() if count == top)
    return tied[0] if len(tied) == 1 else rng.choice(tied)


def winner(state: GameState) -> Side | None:
    """Town wins with no mafia left; mafia wins once they are at least as many as town."""
    mafia, town = len(state.mafia()), len(state.town())
    if mafia == 0:
        return "town"
    if mafia >= town:
        return "mafia"
    return None


class Choice(NamedTuple):
    name: str | None  # the chosen player; None for an abstention or an invalid reply
    valid: bool  # False if the reply had no usable choice


def parse_choice(reply: str, keyword: str, valid_names: list[str], allow_none: bool = False) -> Choice:
    """The player named on the reply's last `KEYWORD: <name>` line, matched case-insensitively.

    `**VOTE:** Blake` and `VOTE: **blake**.` both count. With allow_none,
    `VOTE: none` is a valid abstention. A name not in valid_names (dead,
    unknown, yourself) makes the reply invalid.
    """
    matches = re.findall(rf"\b{keyword}\s*:\s*([A-Za-z]+)", reply.replace("*", ""), re.IGNORECASE)
    if not matches:
        return Choice(None, False)
    word = matches[-1].lower()
    if allow_none and word == "none":
        return Choice(None, True)
    by_lower = {name.lower(): name for name in valid_names}
    return Choice(by_lower[word], True) if word in by_lower else Choice(None, False)


# --- the call bound ------------------------------------------------------------
# A night is one call per living mafia, one for the detective if alive, and one
# narrator announcement. A day is a speech and a vote per living player, and one
# announcement. There are no re-asks, so a game's calls are bounded by the
# longest possible game.


def night_calls(mafia: int, detective: int) -> int:
    return mafia + detective + 1


def day_calls(alive: int) -> int:
    return 2 * alive + 1


def worst_case_calls(players: int) -> int:
    """The most calls any game of this size can take, over every kill and elimination."""
    counts = role_counts(players)

    def over(m: int, d: int, v: int) -> bool:
        return m == 0 or m >= d + v

    @cache
    def from_night(m: int, d: int, v: int) -> int:
        # Night kills always land on town; the game isn't over, so town outnumbers mafia.
        after_kill = [(m, 0, v)] if d else []
        after_kill += [(m, d, v - 1)] if v else []
        return night_calls(m, d) + max(from_day(*s) for s in after_kill)

    @cache
    def from_day(m: int, d: int, v: int) -> int:
        if over(m, d, v):
            return 0
        after_vote = [
            (m, d, v),
            (m - 1, d, v) if m else None,
            (m, 0, v) if d else None,
            (m, d, v - 1) if v else None,
        ]
        rest = [0 if over(*s) else from_night(*s) for s in after_vote if s is not None]
        return day_calls(m + d + v) + max(rest)

    return from_night(counts["mafia"], counts["detective"], counts["villager"])
