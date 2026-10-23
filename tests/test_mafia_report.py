"""The Mafia report (`arena report --plugin mafia`), over scripted games."""

import re

import pytest
from scripted import MAFIA_WINS, MIXED_ROLES, SWAPPED_ROLES, TOWN_WINS, play_mafia

from arena import cli
from arena.core.events import read_events
from arena.core.runlog import RunLog
from arena.plugins.mafia.report import CLAIM, format_report, load_games, report

STRONG_MAFIA = {
    "narrator": "fake/narr",
    "mafia": "fake/strong",
    "detective": "fake/weak",
    "villager": "fake/weak",
}
MIRROR = {"narrator": "fake/narr", "mafia": "fake/weak", "detective": "fake/weak", "villager": "fake/weak"}
CLAIMS = {"detective": "I'm the detective, and I checked Blake last night."}

# Run 1: town wins on game-01 and its detective claims; the scripted roles each have their own
# model, so the town side is two models. Run 2: a strong mafia model beats a weak town on game-01.
# Run 3: one model on both sides of game-02, where the game-01 script's names make some replies
# invalid.
REPORT = """\
3 game(s) from 3 run(s), runs 1-3

Town win rate by lineup
lineup        mafia   town                              games  town won
------------  ------  --------------------------------  -----  --------
mafia-mirror  weak    weak                                  1  0/1   0%
mafia-split   mafia   det (detective) + vil (villager)      1  1/1 100%
mafia-strong  strong  weak                                  1  0/1   0%

Win rate by model and side, across lineups
model                             won as mafia  vs other models  won as town  vs other models
--------------------------------  ------------  ---------------  -----------  ---------------
det (detective) + vil (villager)             -                -     1/1 100%         1/1 100%
mafia                                 0/1   0%         0/1   0%            -                -
strong                                1/1 100%         1/1 100%            -                -
weak                                  1/1 100%                -     0/2   0%         0/1   0%

By deal (each game id deals the same seats in every lineup)
game     mafia          games  town won
-------  -------------  -----  --------
game-01  Blake, Emery       2  1/2  50%
game-02  Finley, Emery      1  0/1   0%

How games end
ended on      games  town won  mafia won
---------  --------  --------  ---------
night 2           1         0          1
day 2             1         1          0
night 3           1         0          1
any night  2/3  67%         0          2
any day    1/3  33%         1          0

Town play by lineup
lineup        games  town votes on mafia  checks found mafia  det. claimed  det. survived  det. voted out
------------  -----  -------------------  ------------------  ------------  -------------  --------------
mafia-mirror      1             0/3   0%            1/2  50%      0/1   0%       0/1   0%        0/1   0%
mafia-split       1             7/7 100%            2/2 100%      1/1 100%       1/1 100%        0/1   0%
mafia-strong      1                    -            0/3   0%      0/1   0%       1/1 100%        0/1   0%

Invalid replies, calls and cost per game, by lineup
lineup        games  invalid (kill/check/vote)  calls     cost  time
------------  -----  -------------------------  -----  -------  ----
mafia-mirror      1                  3 (2/0/1)   21.0  $0.2100    Ns
mafia-split       1                  0 (0/0/0)   29.0  $0.2900    Ns
mafia-strong      1                  0 (0/0/0)   36.0  $0.3600    Ns
"""


def three_games(monkeypatch, tmp_path) -> RunLog:
    play_mafia(monkeypatch, tmp_path, TOWN_WINS, name="mafia-split", speeches=CLAIMS)
    play_mafia(monkeypatch, tmp_path, MAFIA_WINS, roles=STRONG_MAFIA, name="mafia-strong")
    log, _ = play_mafia(monkeypatch, tmp_path, TOWN_WINS, roles=MIRROR, name="mafia-mirror", game="02")
    return log


def section(text: str, heading: str) -> list[str]:
    """The table rows under `heading`, without its header and rule."""
    lines = text.splitlines()
    start = lines.index(heading) + 3
    return lines[start : lines.index("", start)]


def tables(report: str) -> str:
    """The report without its closing notes, wall times masked."""
    body = report[: report.index("\nRates come with")]
    return re.sub(r"\d+s$", "Ns", body, flags=re.MULTILINE)


def test_report_splits_wins_by_lineup_model_side_deal_and_ending(monkeypatch, tmp_path) -> None:
    log = three_games(monkeypatch, tmp_path)
    assert tables(format_report(load_games(log.db, "mafia", None), None)) == REPORT


def test_each_game_is_read_back_from_what_it_stored(monkeypatch, tmp_path) -> None:
    split, strong, mirror = load_games(three_games(monkeypatch, tmp_path).db, "mafia", None)

    assert (split.run_id, split.game_id, split.town_won, split.ended) == (1, "game-01", True, "day 2")
    assert (split.mafia, split.mafia_model, split.town_model) == (
        ["Blake", "Emery"],
        "mafia",
        "det (detective) + vil (villager)",
    )
    assert (split.town_votes, split.town_votes_on_mafia, split.checks, split.mafia_found) == (7, 7, 2, 2)
    assert (split.detective_out, split.detective_claimed, split.calls) == (None, True, 29)
    assert split.cost_usd == pytest.approx(0.29)
    assert (strong.town_won, strong.ended, strong.mafia_model, strong.town_model) == (
        False,
        "night 3",
        "strong",
        "weak",
    )
    assert (strong.town_votes, strong.detective_claimed) == (0, False)
    # game-02's detective (Casey) was killed on night 2
    assert (mirror.mafia_model, mirror.town_model, mirror.detective_out) == ("weak", "weak", "night 2")
    assert mirror.invalid == {"target": 2, "check": 0, "vote": 1}


def test_runs_from_before_events_take_their_models_from_the_calls_table(monkeypatch, tmp_path) -> None:
    log, run_id = play_mafia(monkeypatch, tmp_path, MAFIA_WINS, roles=STRONG_MAFIA)
    # what an older run looks like: the router logged every call, and there are no events
    for e in read_events(log.db, run_id):
        if e.kind == "call_started":
            log.log_call(
                run_id=run_id,
                problem_id=e.task_id,
                role=e.role,
                model=e.model or "",
                think=False if (e.role or "").startswith("mafia:") else None,
                system="",
                messages=[],
            )
    log.db.execute("DELETE FROM events")
    log.db.execute("UPDATE results SET metrics = json_remove(metrics, '$.models')")  # older games lack it
    [game] = load_games(log.db, "mafia", None)
    # a role's think setting is part of its model, so thinking on and off don't blend
    assert (game.mafia_model, game.town_model) == ("strong (no think)", "weak")


def test_since_and_player_count(monkeypatch, tmp_path) -> None:
    three_games(monkeypatch, tmp_path)
    log, run_id = play_mafia(monkeypatch, tmp_path, None, name="mafia-small", settings={"players": 5})

    [small] = load_games(log.db, "mafia", run_id)
    assert (small.lineup, small.deal) == ("mafia-small [players=5]", "game-01 [players=5]")
    assert len(load_games(log.db, "mafia", 2)) == 3
    assert load_games(log.db, "mafia", run_id + 1) == []
    assert format_report([], run_id + 1) == f"no games from run {run_id + 1} on"
    assert format_report([], None) == "no games yet"


def test_unscored_games_are_left_out(monkeypatch, tmp_path) -> None:
    play_mafia(monkeypatch, tmp_path, TOWN_WINS)
    log = RunLog(tmp_path / "arena.db")
    # a provider error: the run is logged but has no results row
    run_id = log.start_run("mafia-fake", "mafia")
    log.finish_run(run_id)
    assert [g.run_id for g in load_games(log.db, "mafia", None)] == [1]


@pytest.mark.parametrize(
    "speech",
    [
        "I'm the detective. Blake is mafia.",
        "I am your detective and Gray is clear.",
        "Night 1 I checked Gray: not mafia.",
        "Good morning folks. I checked Finley—he’s not mafia.",
        "Emery is mafia per my last check.",
        "I investigated Drew on night 2.",
        "Ladies, I’ve checked Finley—he’s definitely mafia.",
        "I have checked Casey: not mafia.",
        "I’m the detective, trust me.",
    ],
)
def test_claims_are_matched(speech: str) -> None:
    assert CLAIM.search(speech)


@pytest.mark.parametrize(
    "speech",
    [
        "Finley was not mafia.",  # a claim only by implication: missed, as the report's notes say
        "Whoever is the detective should speak up.",
        "I'm a villager with no role.",
        "The detective checked nobody we know of.",
        "I checked the vote record and Blake keeps dodging.",
    ],
)
def test_non_claims_are_not_matched(speech: str) -> None:
    assert not CLAIM.search(speech)


def test_cli_report(monkeypatch, tmp_path, capsys) -> None:
    log = three_games(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "get_runlog", lambda: log)
    capsys.readouterr()

    assert cli.show_report("mafia", 3) == 0
    assert capsys.readouterr().out.startswith("1 game(s) from 1 run(s), runs 3-3\n")
    assert cli.show_report("code_bounty", None) == 2
    assert capsys.readouterr().err == "code_bounty has no report; plugins with one: mafia\n"
    assert cli.show_report("chess", None) == 2
    assert "unknown plugin 'chess'" in capsys.readouterr().err


def test_a_database_without_mafia_runs_has_no_games(tmp_path) -> None:
    log = RunLog(tmp_path / "arena.db")  # no Mafia run yet, so no mafia_events table either
    assert report(log, "mafia", None) == "no games yet"


def test_an_explicit_default_player_count_is_the_same_deal(monkeypatch, tmp_path) -> None:
    play_mafia(monkeypatch, tmp_path, TOWN_WINS)
    log, _ = play_mafia(monkeypatch, tmp_path, TOWN_WINS, name="mafia-seven", settings={"players": 7})
    games = load_games(log.db, "mafia", None)
    assert [(g.lineup, g.deal) for g in games] == [
        ("mafia-fake", "game-01"),
        ("mafia-seven [players=7]", "game-01"),
    ]


def test_a_model_on_both_sides_is_not_against_other_models(monkeypatch, tmp_path) -> None:
    roles = {"narrator": "fake/narr", "mafia": "fake/x", "detective": "fake/y", "villager": "fake/x"}
    log, _ = play_mafia(monkeypatch, tmp_path, MAFIA_WINS, roles=roles)
    text = format_report(load_games(log.db, "mafia", None), None)
    rows = {line.split("  ")[0]: line for line in section(text, "Win rate by model and side, across lineups")}
    # x won as mafia, but x also filled the town's villager seats
    assert re.split(r"\s{2,}", rows["x"]) == ["x", "1/1 100%", "-", "-", "-"]


# game-01's seat groups: a = Finley, Blake (mafia), Casey, Drew; b = Gray, Avery (detective), Emery (mafia).
# The pair swaps the models, so each model plays each seat once: 2 mafia, 1 detective, 4 villagers.
MIXED_TABLES = """\
Mixed seats: each model's players, by role
matchup  model  role       players  voted out    killed  alive at end  their votes on mafia
-------  -----  ---------  -------  ---------  --------  ------------  --------------------
a vs b   a      mafia            2   1/2  50%  0/2   0%      1/2  50%                     -
a vs b   a      detective        1   0/1   0%  0/1   0%      1/1 100%                     -
a vs b   a      villager         4   0/4   0%  3/4  75%      1/4  25%              3/3 100%
a vs b   b      mafia            2   1/2  50%  0/2   0%      1/2  50%                     -
a vs b   b      detective        1   0/1   0%  0/1   0%      1/1 100%              2/2 100%
a vs b   b      villager         4   0/4   0%  2/4  50%      2/4  50%              2/2 100%

Mixed seats: town win rate by the detective's model
matchup  detective  games  town won
-------  ---------  -----  --------
a vs b   a              1  0/1   0%
a vs b   b              1  1/1 100%
"""


def test_mixed_seat_games_get_per_player_tables(monkeypatch, tmp_path) -> None:
    play_mafia(monkeypatch, tmp_path, TOWN_WINS, roles=MIXED_ROLES, mode="mixed", name="mixed-ab")
    log, _ = play_mafia(monkeypatch, tmp_path, MAFIA_WINS, roles=SWAPPED_ROLES, mode="mixed", name="mixed-ba")
    games = load_games(log.db, "mafia", None)
    assert [(g.mixed, g.mafia_model, g.town_model) for g in games] == [
        (True, "mixed: a + b", "mixed: a + b")
    ] * 2

    text = tables(format_report(games, None))
    assert text.endswith(MIXED_TABLES)
    # both models play both sides, so there is no per-side table
    assert "Win rate by model and side" not in text
    assert section(text, "Town win rate by lineup") == [
        "mixed-ab  mixed: a + b  mixed: a + b      1  1/1 100%",
        "mixed-ba  mixed: a + b  mixed: a + b      1  0/1   0%",
    ]


def test_side_games_and_mixed_games_report_side_by_side(monkeypatch, tmp_path) -> None:
    play_mafia(monkeypatch, tmp_path, TOWN_WINS, roles=STRONG_MAFIA, name="mafia-strong")
    log, _ = play_mafia(monkeypatch, tmp_path, TOWN_WINS, roles=MIXED_ROLES, mode="mixed", name="mixed-ab")
    text = format_report(load_games(log.db, "mafia", None), None)
    # the per-side table counts only the one-model-per-side game; the mixed one gets its own tables
    rows = section(text, "Win rate by model and side, across lineups")
    assert [row.split() for row in rows] == [
        ["strong", "0/1", "0%", "0/1", "0%", "-", "-"],
        ["weak", "-", "-", "1/1", "100%", "1/1", "100%"],
    ]
    mixed_rows = section(text, "Mixed seats: each model's players, by role")
    assert mixed_rows[0].split()[:6] == ["a", "vs", "b", "a", "mafia", "1"]
    # only one half of the pair ran
    assert "UNBALANCED a vs b: 1 with a in seats_a, 0 with b in seats_a" in text


def test_mixed_games_are_kept_apart_by_matchup(monkeypatch, tmp_path) -> None:
    play_mafia(monkeypatch, tmp_path, TOWN_WINS, roles=MIXED_ROLES, mode="mixed", name="mixed-ab")
    other = {"narrator": "fake/narrator", "seats_a": "fake/a", "seats_b": "fake/c"}
    log, _ = play_mafia(monkeypatch, tmp_path, TOWN_WINS, roles=other, mode="mixed", name="mixed-ac")
    rows = section(
        format_report(load_games(log.db, "mafia", None), None),
        "Mixed seats: town win rate by the detective's model",
    )
    assert [row.split()[:4] for row in rows] == [["a", "vs", "b", "b"], ["a", "vs", "c", "c"]]


def test_a_mixed_game_without_its_deal_rows_still_reports(monkeypatch, tmp_path) -> None:
    log, _ = play_mafia(monkeypatch, tmp_path, TOWN_WINS, roles=MIXED_ROLES, mode="mixed")
    log.db.execute("DELETE FROM mafia_events WHERE kind = 'role'")
    [game] = load_games(log.db, "mafia", None)
    assert (game.mixed, game.seats, game.seats_a) == (True, [], "?")
    assert "Mixed seats: town win rate" in format_report([game], None)
