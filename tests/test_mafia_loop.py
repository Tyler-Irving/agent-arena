"""A Mafia game end to end with scripted players: outcomes, events, and what each player can see."""

import asyncio
import json
import re
from pathlib import Path

import pytest

from arena.core import orchestrator
from arena.core import plugin as core_plugin
from arena.core.lineup import Lineup, load_lineups
from arena.core.runlog import RunLog
from arena.core.taskboard import SqliteTaskBoard
from arena.models.router import ModelResponse
from arena.plugins import DEFAULT_PLUGIN, PLUGINS, get_plugin
from arena.plugins.mafia.game import deal

MAFIA = get_plugin("mafia")
NARRATOR_MARK = "NARRATOR-MARK-7f3a"  # appears only in narrator replies
ROLES = {"narrator": "fake/narrator", "mafia": "fake/mafia", "detective": "fake/det", "villager": "fake/vil"}

# game-01 seats: Finley (villager), Blake (mafia), Casey (villager), Gray (villager),
# Drew (villager), Avery (detective), Emery (mafia)
TOWN_WINS = {
    "kills": {1: "Finley", 2: "Casey"},
    "votes": {1: "Blake", 2: "Emery"},
    "checks": {1: "Blake", 2: "Emery"},
}
# Seat 0 (Finley) survives to day 2 here, so a speaking order that doesn't rotate would show.
MAFIA_WINS = {
    "kills": {1: "Casey", 2: "Gray", 3: "Drew"},
    "votes": {},
    "checks": {1: "Drew", 2: "Finley", 3: "Finley"},
}


def _reply(text: str) -> ModelResponse:
    return ModelResponse(
        text=text, input_tokens=5, output_tokens=5, latency_ms=1, cost_usd=0.001, model="fake"
    )


def _scripted(script: dict | None, fail_on: str | None = None):
    """A fake complete: plays `script` (None: every reply is unusable), recording (role, prompt)."""
    seen: list[tuple[str, str]] = []

    async def fake_complete(model, system, messages, *, role, **kwargs):
        content = messages[0]["content"]
        seen.append((role, content))
        if role == fail_on:
            raise RuntimeError("provider down")
        if role == "narrator":
            return _reply(NARRATOR_MARK)
        if script is None:
            return _reply("hmm")
        kind, name = role.split(":")
        n = int(re.findall(r"It is (?:night|day) (\d+)", content)[-1])
        if "TARGET:" in content.splitlines()[-1]:
            return _reply(f"Quiet one first.\nTARGET: {script['kills'][n]}")
        if "CHECK:" in content.splitlines()[-1]:
            return _reply(f"CHECK: {script['checks'][n]}")
        if "VOTE:" in content.splitlines()[-1]:
            target = script["votes"].get(n) if kind != "mafia" else None
            return _reply(f"VOTE: {target or 'none'}")
        return _reply(f"{name} speaks on day {n}.")

    return fake_complete, seen


def _lineup(name: str = "mafia-fake", roles: dict | None = None) -> Lineup:
    return Lineup.model_validate({"name": name, "plugin": "mafia", "mode": "game", "roles": roles or ROLES})


def _play(
    monkeypatch, tmp_path: Path, script: dict | None, game: str = "01", lineup: Lineup | None = None, **kw
):
    fake, seen = _scripted(script, **kw)
    monkeypatch.setattr(core_plugin, "complete", fake)
    log = RunLog(tmp_path / "arena.db")
    board = SqliteTaskBoard(tmp_path / "arena.db")
    run_id, outcomes = asyncio.run(
        orchestrator.run_lineup(MAFIA, lineup or _lineup(), MAFIA.load_tasks(game), board, log, game)
    )
    return log, run_id, outcomes[0], seen


def _speakers(seen: list[tuple[str, str]], day: int) -> list[str]:
    return [
        role.split(":")[1]
        for role, content in seen
        if role != "narrator" and f"It is day {day} and your turn to speak" in content
    ]


def _events(log: RunLog, run_id: int, kind: str) -> list[tuple]:
    rows = log.db.execute(
        "SELECT day, actor, target, text FROM mafia_events WHERE run_id = ? AND kind = ? ORDER BY id",
        (run_id, kind),
    )
    return [tuple(r) for r in rows]


# --- whole games ------------------------------------------------------------------


def test_town_wins(monkeypatch, tmp_path, capsys) -> None:
    log, run_id, outcome, _ = _play(monkeypatch, tmp_path, TOWN_WINS)

    # night 1: 2 mafia + detective + narrator; day 1: 6 speeches + 6 votes + narrator;
    # night 2: 1 mafia + detective + narrator; day 2: 4 + 4 + narrator
    assert (outcome.score, outcome.verdict, outcome.calls, outcome.error) == (
        1.0,
        "TOWN WON on day 2",
        29,
        None,
    )
    assert outcome.metrics == {
        "winner": "town",
        "nights": 2,
        "days": 2,
        "players": 7,
        "mafia": ["Blake", "Emery"],
        "out": [
            {"name": "Finley", "role": "villager", "when": "night 1"},
            {"name": "Blake", "role": "mafia", "when": "day 1"},
            {"name": "Casey", "role": "villager", "when": "night 2"},
            {"name": "Emery", "role": "mafia", "when": "day 2"},
        ],
        "detective_checks": 2,
        "detective_found_mafia": 2,
        "town_votes": 7,
        "town_votes_on_mafia": 7,
        "invalid": {"target": 0, "check": 0, "vote": 0},
    }
    score, passed, metrics = log.db.execute("SELECT score, passed, metrics FROM results").fetchone()
    assert (score, passed, json.loads(metrics)["winner"]) == (1.0, 1, "town")
    assert log.db.execute("SELECT plugin FROM runs").fetchone()[0] == "mafia"
    assert _events(log, run_id, "kill") == [(1, "Emery", "Finley", None), (2, "Emery", "Casey", None)]
    assert _events(log, run_id, "elimination") == [(1, None, "Blake", "mafia"), (2, None, "Emery", "mafia")]
    assert len(_events(log, run_id, "role")) == 7
    assert len(_events(log, run_id, "speech")) == len(_events(log, run_id, "vote")) == 6 + 4
    assert [e[3] for e in _events(log, run_id, "announcement")] == [NARRATOR_MARK] * 4
    out = capsys.readouterr().out
    assert "   night 1: Finley killed (villager)\n   day 1: Blake voted out (mafia)" in out
    assert "   TOWN WON on day 2, 29 call(s)" in out


def test_mafia_wins(monkeypatch, tmp_path) -> None:
    log, _, outcome, _ = _play(monkeypatch, tmp_path, MAFIA_WINS)

    assert (outcome.score, outcome.verdict, outcome.calls) == (
        0.0,
        "MAFIA WON on night 3",
        4 + 13 + 4 + 11 + 4,
    )
    assert outcome.metrics["detective_found_mafia"] == 0 and outcome.metrics["days"] == 2
    assert log.db.execute("SELECT score, passed FROM results").fetchone()[:] == (0.0, 0)


def test_unusable_replies_still_end_the_game(monkeypatch, tmp_path) -> None:
    _, _, outcome, _ = _play(monkeypatch, tmp_path, None)

    # every night still kills (a seeded random town player), and with no votes nobody is
    # eliminated, so mafia reach parity on night 3
    assert outcome.verdict == "MAFIA WON on night 3"
    assert outcome.calls <= 45
    assert outcome.metrics["invalid"]["target"] == 6  # both mafia, three nights
    assert outcome.metrics["invalid"]["vote"] == 6 + 5


def test_provider_error_leaves_the_game_unscored(monkeypatch, tmp_path) -> None:
    log, _, outcome, _ = _play(monkeypatch, tmp_path, TOWN_WINS, fail_on="villager:Gray")

    assert (outcome.error, outcome.verdict) == ("RuntimeError: provider down", "ERROR, not scored")
    assert log.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    board = SqliteTaskBoard(tmp_path / "arena.db")
    assert board.db.execute("SELECT status, reason FROM tasks").fetchone() == (
        "failed",
        "RuntimeError: provider down",
    )


# --- what each player sees ----------------------------------------------------------


def test_players_never_see_narrator_text(monkeypatch, tmp_path) -> None:
    _, _, _, seen = _play(monkeypatch, tmp_path, TOWN_WINS)
    assert any(role == "narrator" for role, _ in seen)
    assert not [role for role, content in seen if role != "narrator" and NARRATOR_MARK in content]


def test_votes_are_blind_until_everyone_has_voted(monkeypatch, tmp_path) -> None:
    _, _, _, seen = _play(monkeypatch, tmp_path, TOWN_WINS)
    for day in (1, 2):
        ballots = [c for r, c in seen if f"It is day {day} and everyone has spoken" in c]
        assert len(ballots) == {1: 6, 2: 4}[day]
        for content in ballots:
            assert f"Day {day} votes:" not in content  # no one's vote is shared mid-vote
            # but every speech of the day is
            assert all(f"Day {day}, {s} said:" in content for s in _speakers(seen, day))
    later = [c for r, c in seen if "It is day 2 and your turn to speak" in c]
    assert all("Day 1 votes: Blake -> none, Casey -> Blake," in c for c in later)


def test_private_information_stays_private(monkeypatch, tmp_path) -> None:
    _, _, _, seen = _play(monkeypatch, tmp_path, TOWN_WINS)
    players = [(role.split(":")[0], c) for role, c in seen if role != "narrator"]

    assert all("partner" not in c for kind, c in players if kind != "mafia")
    assert any("Your mafia partner is Emery." in c for role, c in seen if role == "mafia:Blake")
    assert all(" check: " not in c for kind, c in players if kind != "detective")
    assert any("Night 1 check: Blake is mafia." in c for role, c in seen if role == "detective:Avery")
    for role, content in seen:
        if role == "narrator":
            assert content.startswith("Facts:\n- ")
            assert "partner" not in content and " check: " not in content and "said:" not in content


def test_narrator_never_learns_a_hidden_role(monkeypatch, tmp_path) -> None:
    # nobody is voted out, so the mafia are never revealed
    _, _, _, seen = _play(monkeypatch, tmp_path, MAFIA_WINS)
    assert all("mafia" not in content for role, content in seen if role == "narrator")


def test_first_speaker_rotates_past_the_dead(monkeypatch, tmp_path) -> None:
    _, _, _, town = _play(monkeypatch, tmp_path / "a", TOWN_WINS)
    # day 1 starts at seat 0 (Finley, killed) -> Blake; day 2 starts at seat 1 (Blake, voted
    # out), then seat 2 (Casey, killed) -> Gray
    assert _speakers(town, 1) == ["Blake", "Casey", "Gray", "Drew", "Avery", "Emery"]
    assert _speakers(town, 2) == ["Gray", "Drew", "Avery", "Emery"]
    _, _, _, mafia = _play(monkeypatch, tmp_path / "b", MAFIA_WINS)
    # Casey is killed on night 1 and Gray on night 2. Day 1 starts at seat 0; day 2 starts
    # at seat 1 (Blake, alive), so Finley in seat 0 speaks last.
    assert _speakers(mafia, 1) == ["Finley", "Blake", "Gray", "Drew", "Avery", "Emery"]
    assert _speakers(mafia, 2) == ["Blake", "Drew", "Avery", "Emery", "Finley"]


def test_same_game_deals_the_same_in_every_lineup(monkeypatch, tmp_path) -> None:
    other = _lineup(
        "other", {"narrator": "fake/n2", "mafia": "fake/m2", "detective": "fake/d2", "villager": "fake/v2"}
    )
    log_a, run_a, _, _ = _play(monkeypatch, tmp_path / "a", None, game="03")
    log_b, run_b, _, _ = _play(monkeypatch, tmp_path / "b", None, game="03", lineup=other)

    deal_a, deal_b = _events(log_a, run_a, "role"), _events(log_b, run_b, "role")
    assert deal_a == deal_b == [(0, p.name, None, p.role) for p in deal("game-03", 7).players]
    assert deal_a != _events(*_play(monkeypatch, tmp_path / "c", None, game="04")[:2], "role")


# --- the plugin ----------------------------------------------------------------------


def test_load_tasks() -> None:
    assert [g.id for g in MAFIA.load_tasks(None)] == [f"game-{i:02}" for i in range(1, 11)]
    assert (
        [g.id for g in MAFIA.load_tasks("03")] == [g.id for g in MAFIA.load_tasks("game-03")] == ["game-03"]
    )
    assert MAFIA.load_tasks("99") == []


def _load(tmp_path: Path, yaml: str) -> dict[str, Lineup]:
    path = tmp_path / "lineups.yaml"
    path.write_text(yaml)
    return load_lineups(PLUGINS, DEFAULT_PLUGIN, path)


ROLE_YAML = "  roles: {{ narrator: {n}, mafia: {m}, detective: {d}, villager: {v} }}\n"


def _yaml(
    settings: str = "", n="anthropic/claude-haiku-5-5", m="ollama/x", d="ollama/x", v="ollama/x"
) -> str:
    return "g:\n  plugin: mafia\n  mode: game\n" + settings + ROLE_YAML.format(n=n, m=m, d=d, v=v)


def test_lineup_settings(tmp_path: Path) -> None:
    assert _load(tmp_path, _yaml())["g"].settings == {}
    assert _load(tmp_path, _yaml("  settings: { players: 5 }\n"))["g"].settings == {"players": 5}
    with pytest.raises(ValueError, match="lineup 'g' settings"):
        _load(tmp_path, _yaml("  settings: { players: 8 }\n"))
    with pytest.raises(ValueError, match="rounds"):
        _load(tmp_path, _yaml("  settings: { rounds: 3 }\n"))


def test_at_most_one_ollama_model(tmp_path: Path) -> None:
    _load(tmp_path, _yaml(m="anthropic/claude-sonnet-5-5"))  # Claude vs local is fine
    with pytest.raises(ValueError, match="at most one ollama model per game, got ollama/x, ollama/y"):
        _load(tmp_path, _yaml(m="ollama/y"))


def test_every_role_is_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"missing roles: \['narrator'\]"):
        _load(
            tmp_path,
            "g:\n  plugin: mafia\n  mode: game\n  roles: { mafia: a/b, detective: a/b, villager: a/b }\n",
        )


def test_five_player_game(monkeypatch, tmp_path) -> None:
    lineup = Lineup.model_validate({**_lineup().model_dump(), "settings": {"players": 5}})
    _, _, outcome, _ = _play(monkeypatch, tmp_path, None, lineup=lineup)
    assert outcome.metrics["players"] == 5 and len(outcome.metrics["mafia"]) == 1
    assert outcome.calls <= 25
