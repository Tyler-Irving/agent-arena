"""The Mafia report: which model is better at which side.

The scoreboard groups games by lineup, so a lineup with one model on mafia and
another on town is one blended number. This report splits every scored game by
side, model, deal and ending. It stores nothing new: it reads what games
already keep, for every run since the plugin's first:
- results: the score (1.0 = town won), calls, cost, wall time, and the metrics
  (winner, nights/days, who left when, town votes, checks, invalid replies)
- each player's model: metrics["models"] where the game recorded it, else the calls (or
  call_started events) its player made ("<role>:<name>")
- mafia_events: who held which role, each vote, and the detective's speeches

Mixed-seat lineups (mode mixed) give each model one mafia and two or three town
seats, so their games get their own per-player tables instead of side-level rates.

(The events table alone would do for most of it, but runs from before
`arena watch` have none.) Every rate is shown with its count, since a handful of games is
noise.
"""

import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from arena.core.runlog import RunLog
from arena.core.scoreboard import cost_cell, format_table, settings_label
from arena.plugins.mafia.game import DEFAULT_PLAYERS

GAMES = """
SELECT results.run_id, results.problem_id, results.score, results.metrics, results.total_calls,
       results.total_cost_usd, results.wall_time_ms, runs.lineup, runs.settings
FROM results
JOIN runs ON runs.id = results.run_id
WHERE runs.plugin = :plugin AND runs.id >= :since AND results.score IS NOT NULL
ORDER BY results.id
"""

# The model behind each player's calls in each game ("mafia:Blake" -> a model, and its think
# setting). The router logs every real call to the calls table; a game with no call rows there
# (a scripted test) falls back to its call_started events.
MODELS = """
SELECT calls.run_id, calls.problem_id, calls.role, calls.model, calls.think
FROM calls
JOIN runs ON runs.id = calls.run_id
WHERE runs.plugin = :plugin AND runs.id >= :since
UNION
SELECT events.run_id, events.problem_id, events.role, events.model, NULL
FROM events
JOIN runs ON runs.id = events.run_id
WHERE runs.plugin = :plugin AND runs.id >= :since AND events.kind = 'call_started'
  AND NOT EXISTS (
      SELECT 1 FROM calls WHERE calls.run_id = events.run_id AND calls.problem_id = events.problem_id
  )
"""

DEALT = """
SELECT mafia_events.run_id, mafia_events.problem_id, mafia_events.actor, mafia_events.text
FROM mafia_events
JOIN runs ON runs.id = mafia_events.run_id
WHERE runs.plugin = :plugin AND runs.id >= :since AND mafia_events.kind = 'role'
ORDER BY mafia_events.id
"""

VOTES = """
SELECT mafia_events.run_id, mafia_events.problem_id, mafia_events.actor, mafia_events.target
FROM mafia_events
JOIN runs ON runs.id = mafia_events.run_id
WHERE runs.plugin = :plugin AND runs.id >= :since AND mafia_events.kind = 'vote'
"""

DETECTIVE_SPEECHES = """
SELECT speech.run_id, speech.problem_id, speech.text
FROM mafia_events AS speech
JOIN mafia_events AS dealt
  ON dealt.run_id = speech.run_id AND dealt.problem_id = speech.problem_id
 AND dealt.kind = 'role' AND dealt.actor = speech.actor AND dealt.text = 'detective'
JOIN runs ON runs.id = speech.run_id
WHERE runs.plugin = :plugin AND runs.id >= :since AND speech.kind = 'speech'
"""

# A detective claims by naming the role ("I'm the detective") or reporting a
# check in the first person ("I checked Blake", "I've checked Blake", "my check"). A text match, so
# a hint rather than a fact: a claim made only by implication ("Finley was not
# mafia") is missed. "I checked" counts only before a capitalised name, so "I
# checked the votes" isn't a claim.
CLAIM = re.compile(
    r"\b(?i:I(?:'|’)?m|I am) (?i:the |your )?(?i:detective)\b"
    r"|\bI(?:(?:'|’)ve| have)? (?i:checked|investigated) [A-Z]"
    r"|\b(?i:my (?:last |first |night \d )?checks?)\b"
)


@dataclass
class Seat:
    name: str
    role: str
    model: str  # "?" if unknown (an older game, and the player never made a call)
    out: str | None  # "night 2" (killed), "day 1" (voted out); None if alive at the end
    votes: int  # votes this player cast for someone (not "none")
    votes_on_mafia: int


@dataclass
class Game:
    run_id: int
    game_id: str
    lineup: str  # with settings, as on the scoreboard: "mafia-x [players=5]"
    deal: str  # the game id, plus the player count if it isn't the default
    town_won: bool
    mafia: list[str]  # the mafia players' names
    ended: str  # "night 2", "day 3"
    mafia_model: str  # one side's model(s), as shown
    town_model: str
    mafia_models: frozenset[str]  # every model that played a side
    town_models: frozenset[str]
    calls: int
    cost_usd: float | None
    wall_time_ms: int
    town_votes: int
    town_votes_on_mafia: int
    checks: int
    mafia_found: int
    detective_out: str | None  # "night 2", "day 1"; None if alive at the end
    detective_claimed: bool
    invalid: dict[str, int]
    seats: list[Seat]
    mixed: bool  # mixed seats: more than one model on the mafia side


def report(log: RunLog, plugin: str, since: int | None) -> str:
    return format_report(load_games(log.db, plugin, since), since)


def load_games(db: sqlite3.Connection, plugin: str, since: int | None) -> list[Game]:
    params = {"plugin": plugin, "since": since or 0}
    rows = db.execute(GAMES, params).fetchall()
    if not rows:  # also before any Mafia run has created mafia_events
        return []
    called: dict[tuple[int, str], dict[str, str]] = defaultdict(dict)  # name -> model, from calls
    for run_id, game_id, player, model, think in db.execute(MODELS, params):
        _, _, name = player.partition(":")
        if name:  # not the narrator
            called[run_id, game_id][name] = model_label(model, think)
    dealt: dict[tuple[int, str], dict[str, str]] = defaultdict(dict)  # name -> role, in seat order
    for run_id, game_id, name, role in db.execute(DEALT, params):
        dealt[run_id, game_id][name] = role
    votes: dict[tuple[int, str], list[tuple[str, str | None]]] = defaultdict(list)
    for run_id, game_id, voter, target in db.execute(VOTES, params):
        votes[run_id, game_id].append((voter, target))
    claimed = {
        (run_id, game_id)
        for run_id, game_id, text in db.execute(DETECTIVE_SPEECHES, params)
        if CLAIM.search(text)
    }
    games = []
    for r in rows:
        run_id, game_id, score, metrics, calls, cost, wall, lineup, settings = r
        m = json.loads(metrics)
        key = run_id, game_id
        recorded = {name: model_label(v["model"], v["think"]) for name, v in m.get("models", {}).items()}
        out = {o["name"]: o["when"] for o in m["out"]}
        role_of = dealt[key]
        seats = [
            Seat(
                name=name,
                role=role,
                model=recorded.get(name) or called[key].get(name, "?"),
                out=out.get(name),
                votes=sum(voter == name and target is not None for voter, target in votes[key]),
                votes_on_mafia=sum(
                    voter == name and role_of.get(target or "") == "mafia" for voter, target in votes[key]
                ),
            )
            for name, role in role_of.items()
        ]
        models: dict[str, set[str]] = defaultdict(set)
        for seat in seats:
            if seat.model != "?":
                models[seat.role].add(seat.model)
        mixed = len(models["mafia"]) > 1
        detective = out.get(next((s.name for s in seats if s.role == "detective"), ""))
        games.append(
            Game(
                run_id=run_id,
                game_id=game_id,
                lineup=settings_label(lineup, settings),
                deal=game_id if m["players"] == DEFAULT_PLAYERS else f"{game_id} [players={m['players']}]",
                town_won=score == 1.0,
                mafia=m["mafia"],
                ended=f"day {m['days']}" if m["days"] == m["nights"] else f"night {m['nights']}",
                mafia_model=mixed_label(models) if mixed else side_model(models, ["mafia"]),
                town_model=mixed_label(models) if mixed else side_model(models, ["detective", "villager"]),
                mafia_models=frozenset(models["mafia"]),
                town_models=frozenset(models["detective"] | models["villager"]),
                calls=calls,
                cost_usd=cost,
                wall_time_ms=wall,
                town_votes=m["town_votes"],
                town_votes_on_mafia=m["town_votes_on_mafia"],
                checks=m["detective_checks"],
                mafia_found=m["detective_found_mafia"],
                detective_out=detective,
                detective_claimed=(run_id, game_id) in claimed,
                invalid=m["invalid"],
                seats=seats,
                mixed=mixed,
            )
        )
    return games


def mixed_label(models: dict[str, set[str]]) -> str:
    return "mixed: " + " + ".join(sorted(set().union(*models.values())))


def side_model(models: dict[str, set[str]], roles: list[str]) -> str:
    """One side's model: "gpt-oss:20b", or each role's if they differ."""
    by_role = {role: ", ".join(sorted(models.get(role, ()))) or "?" for role in roles}
    if len(set(by_role.values())) == 1:
        return next(iter(by_role.values()))
    return " + ".join(f"{model} ({role})" for role, model in by_role.items())


def model_label(model: str, think: int | None) -> str:
    """ "gpt-oss:20b", with "(think)" or "(no think)" if the role set it, so those don't blend."""
    name = model.split("/", 1)[-1]
    return name if think is None else f"{name} ({'think' if think else 'no think'})"


def format_report(games: list[Game], since: int | None) -> str:
    if not games:
        return f"no games from run {since} on" if since else "no games yet"
    runs = sorted({g.run_id for g in games})
    title = f"{len(games)} game(s) from {len(runs)} run(s), runs {runs[0]}-{runs[-1]}"
    sided = [g for g in games if not g.mixed]
    mixed = [g for g in games if g.mixed]
    sections = [
        ("Town win rate by lineup", by_lineup(games)),
        *([("Win rate by model and side, across lineups", by_model(sided))] if sided else []),
        ("By deal (each game id deals the same seats in every lineup)", by_deal(games)),
        ("How games end", endings(games)),
        ("Town play by lineup", town_play(games)),
        ("Invalid replies, calls and cost per game, by lineup", costs(games)),
        *(
            [
                ("Mixed seats: each model's players, by role", mixed_players(mixed)),
                ("Mixed seats: town win rate by the detective's model", mixed_detectives(mixed)),
            ]
            if mixed
            else []
        ),
    ]
    notes = [
        "Rates come with their counts (won/games); one game per cell is noise, not evidence.",
        'A game with one model on both sides counts for it as mafia and as town; "vs other models"',
        "leaves those games out. Mixed-seat games (both models on both sides) are left out of",
        "the per-side table and get the two mixed-seat tables instead.",
        'claimed = the detective said "I\'m the detective" or "I checked <name>" in a speech (a',
        "text match: a claim made only by implication is missed).",
    ]
    out = [title]
    for heading, table in sections:
        out += ["", heading, *table]
    return "\n".join([*out, "", *notes])


def rate(k: int, n: int) -> str:
    return f"{k}/{n} {k / n:>4.0%}" if n else "-"


def grouped(games: Iterable[Game], key: Callable[[Game], str]) -> dict[str, list[Game]]:
    groups: dict[str, list[Game]] = defaultdict(list)
    for g in games:
        groups[key(g)].append(g)
    return dict(sorted(groups.items()))


def by_lineup(games: list[Game]) -> list[str]:
    rows = [
        [
            lineup,
            " / ".join(sorted({g.mafia_model for g in gs})),
            " / ".join(sorted({g.town_model for g in gs})),
            str(len(gs)),
            rate(sum(g.town_won for g in gs), len(gs)),
        ]
        for lineup, gs in grouped(games, lambda g: g.lineup).items()
    ]
    return format_table(["lineup", "mafia", "town", "games", "town won"], rows, left=3)


def by_model(games: list[Game]) -> list[str]:
    """Each model's wins on each side, over all its games and over those against another model."""
    models = sorted({g.mafia_model for g in games} | {g.town_model for g in games})
    rows = []
    for model in models:
        as_mafia = [g for g in games if g.mafia_model == model]
        as_town = [g for g in games if g.town_model == model]
        mafia_won = [not g.town_won for g in as_mafia]
        town_won = [g.town_won for g in as_town]
        # against another model: no model plays on both sides
        vs_town = [not g.town_won for g in as_mafia if g.mafia_models.isdisjoint(g.town_models)]
        vs_mafia = [g.town_won for g in as_town if g.mafia_models.isdisjoint(g.town_models)]
        rows.append(
            [
                model,
                rate(sum(mafia_won), len(mafia_won)),
                rate(sum(vs_town), len(vs_town)),
                rate(sum(town_won), len(town_won)),
                rate(sum(vs_mafia), len(vs_mafia)),
            ]
        )
    headers = ["model", "won as mafia", "vs other models", "won as town", "vs other models"]
    return format_table(headers, rows)


def by_deal(games: list[Game]) -> list[str]:
    rows = [
        [deal, ", ".join(gs[0].mafia), str(len(gs)), rate(sum(g.town_won for g in gs), len(gs))]
        for deal, gs in grouped(games, lambda g: g.deal).items()
    ]
    return format_table(["game", "mafia", "games", "town won"], rows, left=2)


def endings(games: list[Game]) -> list[str]:
    def order(ended: str) -> tuple[int, int]:
        phase, n = ended.split()
        return int(n), phase == "day"  # night 1, day 1, night 2, ...

    rows = []
    for ended in sorted({g.ended for g in games}, key=order):
        gs = [g for g in games if g.ended == ended]
        rows.append(
            [ended, str(len(gs)), str(sum(g.town_won for g in gs)), str(sum(not g.town_won for g in gs))]
        )
    for phase in ("night", "day"):
        gs = [g for g in games if g.ended.startswith(phase)]
        rows.append(
            [
                f"any {phase}",
                rate(len(gs), len(games)),
                str(sum(g.town_won for g in gs)),
                str(sum(not g.town_won for g in gs)),
            ]
        )
    return format_table(["ended on", "games", "town won", "mafia won"], rows)


def town_play(games: list[Game]) -> list[str]:
    rows = [
        [
            lineup,
            str(len(gs)),
            rate(sum(g.town_votes_on_mafia for g in gs), sum(g.town_votes for g in gs)),
            rate(sum(g.mafia_found for g in gs), sum(g.checks for g in gs)),
            rate(sum(g.detective_claimed for g in gs), len(gs)),
            rate(sum(g.detective_out is None for g in gs), len(gs)),
            rate(sum((g.detective_out or "").startswith("day") for g in gs), len(gs)),
        ]
        for lineup, gs in grouped(games, lambda g: g.lineup).items()
    ]
    headers = [
        "lineup",
        "games",
        "town votes on mafia",
        "checks found mafia",
        "det. claimed",
        "det. survived",
        "det. voted out",
    ]
    return format_table(headers, rows)


def costs(games: list[Game]) -> list[str]:
    rows = []
    for lineup, gs in grouped(games, lambda g: g.lineup).items():
        n = len(gs)
        bad = {kind: sum(g.invalid.get(kind, 0) for g in gs) for kind in ("target", "check", "vote")}
        costs = [g.cost_usd for g in gs]
        cost = None if None in costs else sum(c for c in costs if c is not None) / n
        rows.append(
            [
                lineup,
                str(n),
                f"{sum(bad.values())} ({bad['target']}/{bad['check']}/{bad['vote']})",
                f"{sum(g.calls for g in gs) / n:.1f}",
                cost_cell(cost),
                f"{sum(g.wall_time_ms for g in gs) / n / 1000:.0f}s",
            ]
        )
    return format_table(["lineup", "games", "invalid (kill/check/vote)", "calls", "cost", "time"], rows)


def mixed_players(games: list[Game]) -> list[str]:
    """Per model and role in mixed-seat games: who was voted out, killed, alive at the end, and how
    their own votes landed. For mafia, "voted out" is being caught."""
    seats = [s for g in games for s in g.seats]
    rows = []
    for model in sorted({s.model for s in seats}):
        for role in ("mafia", "detective", "villager"):
            group = [s for s in seats if s.model == model and s.role == role]
            if not group:
                continue
            n = len(group)
            town = role != "mafia"
            rows.append(
                [
                    model,
                    role,
                    str(n),
                    rate(sum((s.out or "").startswith("day") for s in group), n),
                    rate(sum((s.out or "").startswith("night") for s in group), n),
                    rate(sum(s.out is None for s in group), n),
                    rate(sum(s.votes_on_mafia for s in group), sum(s.votes for s in group)) if town else "-",
                ]
            )
    headers = ["model", "role", "players", "voted out", "killed", "alive at end", "their votes on mafia"]
    return format_table(headers, rows, left=2)


def mixed_detectives(games: list[Game]) -> list[str]:
    by_model = grouped(games, lambda g: next(s.model for s in g.seats if s.role == "detective"))
    rows = [
        [model, str(len(gs)), rate(sum(g.town_won for g in gs), len(gs))] for model, gs in by_model.items()
    ]
    return format_table(["detective", "games", "town won"], rows)
