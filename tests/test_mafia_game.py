"""Mafia rules: the deal, speaking order, votes, parsing choices, the win check and the call bound."""

import random

import pytest

from arena.plugins.mafia.game import (
    NAMES,
    Choice,
    GameState,
    Player,
    Role,
    check_targets,
    deal,
    kill_targets,
    parse_choice,
    play_rng,
    resolve_votes,
    role_counts,
    seat_groups,
    speaking_order,
    vote_targets,
    winner,
    worst_case_calls,
)

MAX_CALLS = 60  # the Mafia plugin's call limit


def _state(*players: tuple[str, Role, bool]) -> GameState:
    return GameState(game_id="g", players=[Player(name=n, role=r, alive=a) for n, r, a in players])


# --- the deal --------------------------------------------------------------------


@pytest.mark.parametrize(("players", "mafia", "villagers"), [(5, 1, 3), (6, 1, 4), (7, 2, 4)])
def test_role_counts(players: int, mafia: int, villagers: int) -> None:
    state = deal("game-01", players)
    roles = [p.role for p in state.players]
    assert (roles.count("mafia"), roles.count("detective"), roles.count("villager")) == (mafia, 1, villagers)
    assert len({p.name for p in state.players}) == players
    assert {p.name for p in state.players} <= set(NAMES)


@pytest.mark.parametrize("players", [4, 8])
def test_player_count_out_of_range(players: int) -> None:
    with pytest.raises(ValueError, match="5-7 players"):
        role_counts(players)


def test_deal_depends_only_on_the_game_id() -> None:
    first = deal("game-03", 7)
    random.seed(12345)  # the global generator plays no part
    assert deal("game-03", 7) == first
    # different games deal differently (seats or roles)
    deals = {tuple((p.name, p.role) for p in deal(f"game-{i:02}", 7).players) for i in range(1, 11)}
    assert len(deals) == 10


def test_play_rng_is_seeded_by_the_game_id() -> None:
    assert play_rng("game-03").random() == play_rng("game-03").random()
    assert play_rng("game-03").random() != play_rng("game-04").random()


# --- speaking order ------------------------------------------------------------------


def test_first_speaker_rotates_each_day() -> None:
    state = deal("game-01", 7)
    seats = [p.name for p in state.players]
    for day in (1, 2, 3, 8):
        start = (day - 1) % 7
        assert [p.name for p in speaking_order(state, day)] == seats[start:] + seats[:start]


def test_dead_starter_passes_to_the_next_living_seat() -> None:
    state = deal("game-01", 7)
    seats = [p.name for p in state.players]
    # day 2 would start at seat 1; with seats 1 and 2 dead, seat 3 starts and the dead are skipped
    state.get(seats[1]).alive = False
    state.get(seats[2]).alive = False
    assert [p.name for p in speaking_order(state, 2)] == [seats[i] for i in (3, 4, 5, 6, 0)]
    # day 3 would start at seat 2 (dead), so seat 3 starts again
    assert [p.name for p in speaking_order(state, 3)] == [seats[i] for i in (3, 4, 5, 6, 0)]
    # day 4 starts at seat 3 as scheduled
    assert speaking_order(state, 4)[0].name == seats[3]


# --- targets and votes ------------------------------------------------------------------

STATE = _state(
    ("Avery", "mafia", True),
    ("Blake", "villager", True),
    ("Casey", "detective", True),
    ("Drew", "villager", False),
    ("Emery", "mafia", True),
)


def test_targets() -> None:
    avery, casey = STATE.get("Avery"), STATE.get("Casey")
    assert vote_targets(STATE, avery) == ["Blake", "Casey", "Emery"]  # not yourself, not the dead
    assert kill_targets(STATE) == ["Blake", "Casey"]  # living town only
    assert check_targets(STATE, casey) == ["Avery", "Blake", "Emery"]


def test_plurality_is_eliminated() -> None:
    votes = {"Avery": "Blake", "Blake": "Avery", "Casey": "Avery", "Emery": None}
    assert resolve_votes(votes, random.Random(0)) == "Avery"


def test_tie_is_broken_by_the_seeded_generator() -> None:
    votes = {"Avery": "Blake", "Blake": "Avery", "Casey": None}
    picks = {resolve_votes(votes, play_rng(f"game-{i:02}")) for i in range(1, 11)}
    assert picks == {"Avery", "Blake"}  # either can lose a tie...
    # ...but a given game always breaks it the same way
    assert resolve_votes(votes, play_rng("game-05")) == resolve_votes(votes, play_rng("game-05"))


def test_no_votes_cast_eliminates_nobody() -> None:
    assert resolve_votes({"Avery": None, "Blake": None}, random.Random(0)) is None
    assert resolve_votes({}, random.Random(0)) is None


def test_winner() -> None:
    assert winner(STATE) == "mafia"  # 2 mafia vs 2 town
    five = _state(
        ("Avery", "mafia", True),
        ("Blake", "villager", True),
        ("Casey", "detective", True),
        ("Drew", "villager", True),
        ("Emery", "mafia", True),
    )
    assert winner(five) is None  # 2 mafia vs 3 town: play on
    five.get("Avery").alive = False
    assert winner(five) is None
    five.get("Emery").alive = False
    assert winner(five) == "town"
    two = _state(("Avery", "mafia", True), ("Blake", "villager", True), ("Casey", "detective", False))
    assert winner(two) == "mafia"


# --- parsing choices -----------------------------------------------------------------

LIVING = ["Blake", "Casey", "Emery"]


@pytest.mark.parametrize(
    ("reply", "choice"),
    [
        ("I think it's Blake.\nVOTE: Blake", Choice("Blake", True)),
        ("vote: casey", Choice("Casey", True)),
        ("**VOTE:** Emery", Choice("Emery", True)),
        ("VOTE: **Emery**.", Choice("Emery", True)),
        ("VOTE: Blake... actually no.\nVOTE: Casey", Choice("Casey", True)),  # the last line counts
        ("VOTE: none", Choice(None, True)),
        ("VOTE: Drew", Choice(None, False)),  # dead or unknown
        ("VOTE: Avery", Choice(None, False)),  # yourself: not in the valid names
        ("I vote for Blake", Choice(None, False)),  # no VOTE: line
        ("", Choice(None, False)),
    ],
)
def test_parse_vote(reply: str, choice: Choice) -> None:
    assert parse_choice(reply, "VOTE", LIVING, allow_none=True) == choice


def test_none_is_invalid_where_a_choice_is_required() -> None:
    assert parse_choice("TARGET: none", "TARGET", LIVING) == Choice(None, False)
    assert parse_choice("TARGET: blake", "TARGET", LIVING) == Choice("Blake", True)
    # a different keyword doesn't count
    assert parse_choice("VOTE: Blake", "TARGET", LIVING) == Choice(None, False)


# --- the call bound ----------------------------------------------------------------------


@pytest.mark.parametrize(("players", "worst"), [(5, 25), (6, 39), (7, 45)])
def test_worst_case_game_fits_the_call_limit(players: int, worst: int) -> None:
    # e.g. 6 players with no eliminations: nights of 3 calls around days of 11, 9 and 7
    assert worst_case_calls(players) == worst <= MAX_CALLS


def test_seat_groups_split_each_side_across_both_models() -> None:
    for i in range(1, 11):
        state = deal(f"game-{i:02}", 7)
        groups = seat_groups(state)
        assert set(groups) == {p.name for p in state.players}
        mafia = sorted(groups[p.name] for p in state.players if p.role == "mafia")
        town = sorted(groups[p.name] for p in state.players if p.role != "mafia")
        assert mafia == ["a", "b"]  # one mafia per model, so neither voice points at a side
        assert sorted([town.count("a"), town.count("b")]) == [2, 3]
        assert seat_groups(deal(f"game-{i:02}", 7)) == groups  # from the game id alone
    detective_groups = {
        seat_groups(s := deal(f"game-{i:02}", 7))[next(p.name for p in s.players if p.role == "detective")]
        for i in range(1, 11)
    }
    assert detective_groups == {"a", "b"}  # the detective isn't always on one model


def test_seat_groups_need_two_mafia() -> None:
    with pytest.raises(ValueError, match="two mafia"):
        seat_groups(deal("game-01", 6))
