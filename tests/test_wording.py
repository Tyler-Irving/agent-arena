"""What the CLI prints about runs and the scoreboard, in each plugin's own words.

code_bounty's output is pinned here as main printed it before plugins had a
say in the wording, so any change to it shows up as a diff.
"""

import asyncio
import re

import pytest
import yaml
from scripted import TOWN_WINS, grade, mafia_complete, reply

from arena import cli
from arena.core import plugin as core_plugin
from arena.core.lineup import LINEUPS_FILE
from arena.core.plugin import Wording
from arena.core.runlog import RunLog
from arena.core.taskboard import SqliteTaskBoard
from arena.plugins.code_bounty import loop

GOOD = reply("```python\ndef two_sum(nums, target):\n    return [0, 1]\n```")
BAD = reply("```python\ndef two_sum(nums, target):\n    return []\n```")

BOUNTY_RUNS = """\
lineup haiku-solo (solo: solo=anthropic/claude-haiku-5-5; max 3 attempts), 1 problem(s) x 2 runs

== 001-two-sum
   attempt 1: 5/6 tests passed (model Ns, 20 out tokens)
   attempt 2: 6/6 tests passed (model Ns, 20 out tokens)
   PASS in 2 attempt(s), 2 call(s), $0.0200, Ns

run 1: 1/1 passed, 2 calls, $0.0200, Ns

== 001-two-sum
   model call failed: RuntimeError: down
   ERROR, not scored in 0 attempt(s), 1 call(s), $0.0000, Ns

run 2: 0/0 passed (1 not scored: model errors), 1 calls, $0.0000, Ns

2 runs finished (1-2):
  run 1: 1/1 passed, 2 calls, $0.0200, Ns
  run 2: 0/0 passed (1 not scored: model errors), 1 calls, $0.0000, Ns
compare with earlier runs: arena scoreboard --since 1
"""

BOUNTY_FOOTER = """
limit = max attempts per problem; per run = lowest-highest pass rate of a single full run
(- if none); attempts, tokens, cost and time are per-problem averages; total is all runs;
? = unpriced model, or limit not recorded
"""

BOUNTY_SCOREBOARDS = (
    """\
lineup      limit  runs  passed  pass  per run  attempts  in tok  out tok  reasoning     cost    total  time
----------  -----  ----  ------  ----  -------  --------  ------  -------  ---------  -------  -------  ----
haiku-solo      3     2     2/2  100%        -      1.50       0        0          0  $0.0150  $0.0300  Ns
"""
    + BOUNTY_FOOTER
    + """\
runs 2 and later

lineup      limit  runs  passed  pass  per run  attempts  in tok  out tok  reasoning     cost    total  time
----------  -----  ----  ------  ----  -------  --------  ------  -------  ---------  -------  -------  ----
haiku-solo      3     1     1/1  100%        -      1.00       0        0          0  $0.0100  $0.0100  Ns
"""
    + BOUNTY_FOOTER
    + """\
no runs from run 3 on
"""
)


def first_lineup(plugin: str) -> str:
    """The plugin's first lineup in config/lineups.yaml, which the empty-scoreboard hint names."""
    lineups = yaml.safe_load(LINEUPS_FILE.read_text())
    return next(name for name, cfg in lineups.items() if cfg.get("plugin", "code_bounty") == plugin)


def wall_times(text: str) -> str:
    return re.sub(r"\d+\.\ds\b", "Ns", text)


def fake_bounty(monkeypatch, tmp_path, replies: list, grades: list[bool]) -> RunLog:
    """haiku-solo through the CLI, with scripted replies (an exception is raised) and grades."""

    async def fake_complete(model, system, messages, **kwargs):
        r = replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r

    async def fake_grade(code, problem_dir):
        return grade(grades.pop(0))

    log = RunLog(tmp_path / "arena.db")
    monkeypatch.setattr(core_plugin, "complete", fake_complete)
    monkeypatch.setattr(loop, "grade", fake_grade)
    monkeypatch.setattr(cli, "get_runlog", lambda: log)
    monkeypatch.setattr(cli, "SqliteTaskBoard", lambda: SqliteTaskBoard(tmp_path / "arena.db"))
    return log


def test_code_bounty_run_output_is_unchanged(monkeypatch, tmp_path, capsys) -> None:
    # run 1: 001 passes on the second try; run 2: a provider error, so it isn't scored
    fake_bounty(monkeypatch, tmp_path, [BAD, GOOD, RuntimeError("down")], [False, True])
    assert asyncio.run(cli.run("haiku-solo", "001", repeat=2)) == 0
    assert wall_times(capsys.readouterr().out) == BOUNTY_RUNS


def test_code_bounty_no_match_and_scoreboards_are_unchanged(monkeypatch, tmp_path, capsys) -> None:
    fake_bounty(monkeypatch, tmp_path, [BAD, GOOD, GOOD], [False, True, True])
    assert asyncio.run(cli.run("haiku-solo", "999", repeat=1)) == 2
    assert cli.show_scoreboard("code_bounty", None) == 0
    out = capsys.readouterr()
    assert out.err == "no problems match '999'\n"
    assert out.out == f"no runs yet; try `arena run --lineup {first_lineup('code_bounty')}`\n"

    assert asyncio.run(cli.run("haiku-solo", "001", repeat=2)) == 0
    capsys.readouterr()
    for since in (None, 2, 3):
        assert cli.show_scoreboard("code_bounty", since) == 0
    assert wall_times(capsys.readouterr().out) == BOUNTY_SCOREBOARDS


def test_mafia_speaks_of_games_and_town_wins_without_attempts(monkeypatch, tmp_path, capsys) -> None:
    log = RunLog(tmp_path / "arena.db")
    monkeypatch.setattr(core_plugin, "complete", mafia_complete(TOWN_WINS))
    monkeypatch.setattr(cli, "get_runlog", lambda: log)
    monkeypatch.setattr(cli, "SqliteTaskBoard", lambda: SqliteTaskBoard(tmp_path / "arena.db"))

    assert asyncio.run(cli.run("mafia-gptoss", "11", repeat=1)) == 2
    assert cli.show_scoreboard("mafia", None) == 0
    out = capsys.readouterr()
    assert out.err == "no games match '11'\n"
    assert out.out == f"no runs yet; try `arena run --lineup {first_lineup('mafia')}`\n"

    assert asyncio.run(cli.run("mafia-gptoss", "01", repeat=2)) == 0
    out = capsys.readouterr().out
    header = out.splitlines()[0]
    assert header.startswith("lineup mafia-gptoss (game: narrator=ollama/gpt-oss:20b, ")
    assert header.endswith("), 1 game(s) x 2 runs") and "attempts" not in header
    assert re.search(r"^run 1: town won 1/1, 29 calls, ", out, re.MULTILINE)
    assert re.search(r"^  run 2: town won 1/1, 29 calls, ", out, re.MULTILINE)

    assert cli.show_scoreboard("mafia", None) == 0
    lines = capsys.readouterr().out.splitlines()
    assert re.split(r"\s{2,}", lines[0])[:5] == ["lineup", "runs", "town won", "town win", "per run"]
    assert "limit" not in lines[0] and "attempts" not in lines[0]
    assert re.split(r"\s{2,}", lines[2])[:5] == ["mafia-gptoss", "2", "2/2", "100%", "-"]
    assert lines[4:] == [
        "per run = lowest-highest town win rate of a single full run (- if none); tokens, cost and",
        "time are per-game averages; total is all runs; ? = unpriced model",
    ]


def test_no_hint_when_the_lineups_file_does_not_load(monkeypatch, tmp_path, capsys) -> None:
    fake_bounty(monkeypatch, tmp_path, [], [])
    monkeypatch.setattr(cli, "load_lineups", lambda *args: yaml.safe_load("a: [unclosed"))
    assert cli.show_scoreboard("code_bounty", None) == 0
    assert capsys.readouterr().out == "no runs yet\n"


@pytest.mark.parametrize("summary", ["won {pased}/{scored}", "won {0}", "won {passed"])
def test_a_summary_with_unknown_placeholders_is_rejected_up_front(summary: str) -> None:
    with pytest.raises(ValueError, match="may only use"):
        Wording(task="t", tasks="ts", passed="p", rate="r", summary=summary)
