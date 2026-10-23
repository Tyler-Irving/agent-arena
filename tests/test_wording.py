"""What the CLI prints about runs and the scoreboard, in each plugin's own words.

code_bounty's output is pinned here as main printed it before plugins had a
say in the wording, so any change to it shows up as a diff.
"""

import asyncio
import re

from scripted import grade, reply

from arena import cli
from arena.core import plugin as core_plugin
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
    assert out.out == "no runs yet; try `arena run --lineup haiku-solo`\n"

    assert asyncio.run(cli.run("haiku-solo", "001", repeat=2)) == 0
    capsys.readouterr()
    for since in (None, 2, 3):
        assert cli.show_scoreboard("code_bounty", since) == 0
    assert wall_times(capsys.readouterr().out) == BOUNTY_SCOREBOARDS
