"""arena watch / replay / run --watch: what the viewer draws from a run's events."""

import asyncio
import gc
import os
import re
import signal
import sqlite3
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from scripted import CODE_BOUNTY, events, play_mafia, reply, run_bounty

from arena import cli
from arena.core import orchestrator
from arena.core import plugin as core_plugin
from arena.core.events import Event, boot_id
from arena.core.lineup import Lineup
from arena.core.plugin import Outcome, Session, Wording
from arena.core.runlog import RunLog
from arena.core.taskboard import SqliteTaskBoard
from arena.watch import app, views
from arena.watch.render import screen
from arena.watch.state import RunState
from arena.watch.views import GenericView, ViewOptions, epoch, view_for

GOOD, BAD = reply("```python\ngood\n```"), reply("```python\nbad\n```")


def team_script() -> dict[str, list]:
    return {
        "planner": [reply("brute force"), reply("use a dict")],
        "coder": [BAD, BAD, GOOD],
        "reviewer": [reply("off by one"), reply("too slow")],
        "orchestrator": [reply("DECISION: RETRY\nclose"), reply("DECISION: REPLAN\nwrong approach")],
    }


# distinct model names, so a test can tell whose calls a frame shows
NAMED_MODELS = {
    "narrator": "x/model-n",
    "mafia": "x/model-m",
    "detective": "x/model-d",
    "villager": "x/model-v",
}


def replay_text(db: Path, run_id: int, options: ViewOptions | None = None) -> str:
    console = Console(record=True, width=130)
    assert asyncio.run(app.replay(db, run_id, 0, options or ViewOptions(), console)) == 0
    return console.export_text()


def frame(evs: list[Event], options: ViewOptions | None = None) -> tuple[str, RunState]:
    """The screen after folding `evs`, at the time of the last one."""
    state = RunState(options or ViewOptions())
    for e in evs:
        state.apply(e)
    console = Console(record=True, width=130)
    console.print(screen(state, epoch(evs[-1].ts)))
    return console.export_text(), state


def status(text: str) -> str:
    """The run status in the last header drawn."""
    found = re.findall(r"· (running|finished|interrupted|unfinished \(no run_finished\))", text)
    assert found, text
    return found[-1]


def upto(evs: list[Event], pred) -> list[Event]:
    """The events up to and including the first one matching pred."""
    i = next(i for i, e in enumerate(evs) if pred(e))
    return evs[: i + 1]


# --- replaying scripted runs --------------------------------------------------------


def test_replay_of_a_solo_run(monkeypatch, tmp_path) -> None:
    _, run_id = run_bounty(monkeypatch, tmp_path, "solo", [BAD, GOOD], [False, True])
    text = replay_text(tmp_path / "arena.db", run_id)

    assert f"run {run_id} · fake-solo · code_bounty · task 1/1" in text
    assert status(text) == "finished" and "$0.0200" in text
    assert "✔ 001-two-sum PASS in 2 attempt(s) 2 call(s)" in " ".join(text.split())
    assert "001-two-sum · attempt 2/3" in text and "plan" not in text.split("attempt 2/3")[1].split("\n")[0]
    assert "solo ⇄ grader" in " ".join(text.split())
    assert "last: attempt 2 ✔ 6/6 tests" in text


def test_replay_of_a_team_run(monkeypatch, tmp_path) -> None:
    _, run_id = run_bounty(monkeypatch, tmp_path, "team", team_script(), [False, False, True])
    text = replay_text(tmp_path / "arena.db", run_id)

    assert "001-two-sum · attempt 3/3 · plan 2" in text
    assert "planner ──▶ coder ──▶ grader ──▶ reviewer ──▶ orchestrator" in " ".join(text.split())
    assert "PASS in 3 attempt(s)" in text
    calls = text.split("outcome")[1]  # the call log
    for role, model in [
        ("planner", "plan"),
        ("coder", "code"),
        ("reviewer", "review"),
        ("orchestrator", "orch"),
    ]:
        assert re.search(rf"^\+0:00 +{role} +{model} +10 +20 .* \$0\.0100 +ok", calls, re.MULTILINE), role


def test_team_frame_mid_run_shows_the_decision_and_the_coder_at_work(monkeypatch, tmp_path) -> None:
    log, run_id = run_bounty(monkeypatch, tmp_path, "team", team_script(), [False, False, True])
    evs = events(log, run_id)
    coders = [i for i, e in enumerate(evs) if e.kind == "call_started" and e.role == "coder"]
    text, _ = frame(evs[: coders[1] + 1])  # the coder's second attempt is in flight

    assert "attempt 2/3 · plan 1" in text
    assert "last: attempt 1 ✘ 5/6 tests · review: off by one · RETRY: close" in text
    assert "● 0.0s…" in text  # the coder, in flight, in the chain and in the call log
    assert "running" in text


def test_replay_of_a_mafia_game_god_view(monkeypatch, tmp_path) -> None:
    _, run_id = play_mafia(monkeypatch, tmp_path, roles=NAMED_MODELS)
    text = replay_text(tmp_path / "arena.db", run_id)
    flat = " ".join(text.split())

    assert "TOWN WON on day 2" in text and "✔ game-01 TOWN WON on day 2 29 call(s)" in flat
    assert "game-01 · day 2 · votes alive 3/7 · mafia 0 · town 3" in flat
    for row in (
        "1 Finley villager ✝ night 1",
        "2 Blake mafia ✗ out day 1",
        "3 Casey villager ✝ night 2",
        "4 Gray villager alive Emery",
        "6 Avery detective alive Emery",
        "7 Emery mafia ✗ out day 2 none",
    ):
        assert row in flat
    assert "night 2: Emery chose Casey · Casey (villager) killed" in text
    assert "day 2: Emery (mafia) voted out" in text
    assert "detective:Avery" in text and "model-d" in text


def test_god_view_shows_the_proposal_and_check(monkeypatch, tmp_path) -> None:
    log, run_id = play_mafia(monkeypatch, tmp_path)
    evs = events(log, run_id)
    text, _ = frame(upto(evs, lambda e: e.kind == "kill"))

    assert "night 1: Blake proposed Finley, Emery chose Finley · Finley (villager) killed" in text
    assert "night 1: Avery checked Blake: mafia" in text


def test_hide_roles_mid_night_hides_everything_that_gives_a_player_away(monkeypatch, tmp_path) -> None:
    log, run_id = play_mafia(monkeypatch, tmp_path, roles=NAMED_MODELS, costs={"mafia": 0.05})
    evs = events(log, run_id)
    night2 = next(
        i
        for i, e in enumerate(evs)
        if e.kind == "phase" and e.data == {"phase": "night", "n": 2, "step": None}
    )
    mafia_call = next(i for i in range(night2, len(evs)) if evs[i].kind == "call_started")
    hidden = ViewOptions(hide_roles=True)
    text, state = frame(evs[: mafia_call + 1], hidden)  # Emery choosing night 2's kill
    flat = " ".join(text.split())

    # Finley (killed) and Blake (voted out) are revealed; nobody else is.
    assert "1 Finley villager ✝ night 1" in flat and "2 Blake mafia ✗ out day 1" in flat
    for seat, name in [(3, "Casey"), (4, "Gray"), (5, "Drew"), (6, "Avery"), (7, "Emery")]:
        assert f"{seat} {name} ? alive" in flat
    assert "detective" not in text and "mafia:Emery" not in text and "villager:" not in text
    assert "model-d" not in text and "model-v" not in text
    assert "proposed" not in text and "chose" not in text and "checked" not in text
    assert "night 2 · night actions ●" in flat and "(night)" in text
    # costs leave out every hidden call, in the header and in the task's row alike
    header = re.search(r"(\$\d+\.\d{4}) \+ hidden · running", text)
    row = re.search(r"game-01 +\d+ call\(s\) (\$\d+\.\d{4}) \+ hidden", text)
    assert header and row and header[1] == row[1]
    # A hidden player's calls are masked, and so is every night call, even of a player out since:
    # which of night 1's calls was Blake's would show whether he proposed (his partner sits later).
    view = state.views["game-01"]
    for c in state.calls:
        name = (c.started.role or "").partition(":")[2]
        night = c.started.id in view.night_calls  # type: ignore[attr-defined]
        assert (state.mask(c.started) is not None) == bool(
            name and (night or name not in ("Finley", "Blake"))
        )
    blake = [c for c in state.calls if c.started.role == "mafia:Blake"]
    assert state.mask(blake[0].started) == "(night)"  # his night 1 call
    assert state.mask(blake[1].started) is None  # his day 1 speech

    # once the game is over, everything is shown
    text = replay_text(tmp_path / "arena.db", run_id, hidden)
    assert "6 Avery detective alive Emery" in " ".join(text.split()) and "detective:Avery" in text
    assert "+ hidden" not in text


def test_blind_votes_stay_blind_in_the_view(monkeypatch, tmp_path) -> None:
    log, run_id = play_mafia(monkeypatch, tmp_path)
    evs = events(log, run_id)
    votes = next(i for i, e in enumerate(evs) if e.kind == "votes")
    last_vote_call = max(i for i in range(votes) if evs[i].kind == "call_started")
    text, state = frame(evs[: last_vote_call + 1])

    assert "day 1 · votes" in text and "● voting" in text
    view = state.views["game-01"]
    assert all(view.vote(name) == "" for name in ("Casey", "Gray", "Drew", "Avery", "Blake", "Emery"))  # type: ignore[attr-defined]
    seat_rows = [line for line in text.splitlines() if re.match(r"│ \d ", line)]
    assert len(seat_rows) == 7
    assert not any(re.search(r"(alive|…)\s+(Blake|Emery|none|\(invalid\))\s*│", row) for row in seat_rows)

    text, _ = frame(evs[: votes + 1])
    assert re.search(r"Casey\s+villager\s+alive\s+Blake\s*│", text)


def test_invalid_votes_are_marked(monkeypatch, tmp_path) -> None:
    _, run_id = play_mafia(monkeypatch, tmp_path, None)
    assert "(invalid)" in replay_text(tmp_path / "arena.db", run_id)


# --- the generic view, and finding a plugin's view ----------------------------------


class Echo:
    """A plugin with no view.py: one call, one plugin event."""

    name = "echo"
    modes: dict[str, set[str]] = {"echo": {"speaker"}}
    max_calls = 2
    uses_attempts = False
    wording = Wording(
        task="echo", tasks="echoes", passed="passed", rate="pass", summary="{passed}/{scored} passed"
    )
    schema = ""

    def load_tasks(self, selector: str | None) -> list:
        return []

    def validate_lineup(self, lineup: Lineup) -> None: ...

    async def run_task(self, task, lineup: Lineup, session: Session) -> Outcome:
        said = await session.call(
            "speaker", lineup.roles["speaker"], "say", [{"role": "user", "content": "hi"}]
        )
        session.emit("echoed", text=said.text, loud=True)
        return session.outcome(score=1.0, verdict="echoed")


def test_a_plugin_without_a_view_gets_the_generic_panel(monkeypatch, tmp_path) -> None:
    class Task:
        id = "t1"

    async def fake_complete(model, system, messages, **kwargs):
        return reply("hello there")

    monkeypatch.setattr(core_plugin, "complete", fake_complete)
    lineup = Lineup.model_validate(
        {"name": "echo-x", "plugin": "echo", "mode": "echo", "roles": {"speaker": "f/s"}}
    )
    log = RunLog(tmp_path / "arena.db")
    board = SqliteTaskBoard(tmp_path / "arena.db")
    asyncio.run(orchestrator.run_lineup(Echo(), lineup, [Task()], board, log))
    text = replay_text(tmp_path / "arena.db", 1)

    assert view_for("echo") is GenericView
    assert "echoed" in text and "text=hello there, loud=True" in text
    assert "✔ t1 echoed" in " ".join(text.split())


def test_view_lookup_finds_plugin_views() -> None:
    assert view_for("code_bounty").__module__ == "arena.plugins.code_bounty.view"
    assert view_for("mafia").__module__ == "arena.plugins.mafia.view"
    assert view_for("smoke") is GenericView  # a run whose plugin isn't a package at all


def test_a_broken_view_is_an_error_not_a_fallback(monkeypatch) -> None:
    def broken(name):
        raise ModuleNotFoundError("No module named 'missing_dep'", name="missing_dep")

    monkeypatch.setattr(views.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError, match="missing_dep"):
        view_for("code_bounty")


# --- replay: timing, and runs it can't show -----------------------------------------


def test_replay_sleeps_the_gaps_divided_by_speed(monkeypatch, tmp_path) -> None:
    log, run_id = run_bounty(monkeypatch, tmp_path, "solo", [BAD, GOOD], [False, True])
    evs = events(log, run_id)
    # spread the events out, 2 s apart
    for i, e in enumerate(evs):
        log.db.execute("UPDATE events SET ts = ? WHERE id = ?", (f"2026-10-21T12:00:{2 * i:02}+00:00", e.id))
    log.db.commit()
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    console = Console(record=True, width=120)
    assert asyncio.run(app.replay(tmp_path / "arena.db", run_id, 4, ViewOptions(), console, fake_sleep)) == 0
    assert sum(slept) == pytest.approx(2 * (len(evs) - 1) / 4)
    assert max(slept) <= app.POLL_S + 1e-9  # short steps, so timers keep moving
    assert "PASS in 2 attempt(s)" in console.export_text()


def test_replay_and_watch_refuse_what_they_cannot_show(tmp_path, capsys) -> None:
    db = tmp_path / "arena.db"
    assert asyncio.run(app.replay(db, 1, 0, ViewOptions())) == 2
    assert "no database at" in capsys.readouterr().err

    log = RunLog(db)
    old = log.start_run("haiku-solo", "code_bounty")  # a run from before events
    assert asyncio.run(app.replay(db, old, 0, ViewOptions())) == 2
    assert f"run {old} has no events" in capsys.readouterr().err
    assert asyncio.run(app.attach(db, old, ViewOptions())) == 2
    assert f"run {old} has no events" in capsys.readouterr().err
    assert asyncio.run(app.replay(db, 99, 0, ViewOptions())) == 2
    assert "no run 99" in capsys.readouterr().err

    newer = log.start_run("x", "code_bounty")
    log.log_event(newer, None, "run_started", {"events_version": 2, "lineup": "x"})
    for command in (app.replay(db, newer, 0, ViewOptions()), app.attach(db, newer, ViewOptions())):
        assert asyncio.run(command) == 2
        assert f"run {newer} uses event format v2, this viewer reads v1" in capsys.readouterr().err


def test_replay_marks_interrupted_and_unfinished_runs(monkeypatch, tmp_path) -> None:
    with pytest.raises(asyncio.CancelledError):
        run_bounty(monkeypatch, tmp_path, "solo", [asyncio.CancelledError()], [])
    text = replay_text(tmp_path / "arena.db", 1)
    assert "· interrupted" in text and "■ 001-two-sum" in text and "interrupted" in text.split("outcome")[1]

    log = RunLog(tmp_path / "arena.db")
    log.db.execute("DELETE FROM events WHERE kind = 'run_finished'")  # as if the process was killed
    log.db.commit()
    assert "unfinished (no run_finished)" in replay_text(tmp_path / "arena.db", 1)


def test_replay_reads_a_closed_database_read_only(monkeypatch, tmp_path) -> None:
    log, run_id = run_bounty(monkeypatch, tmp_path, "solo", [GOOD], [True])
    log.db.close()
    gc.collect()  # and the task board's connection, which nothing references any more
    db = tmp_path / "arena.db"
    assert not Path(f"{db}-wal").exists() and not Path(f"{db}-shm").exists()
    before = db.read_bytes()

    # In a read-only directory SQLite can't create the -shm file, so it is read as immutable.
    db.chmod(0o444)
    tmp_path.chmod(0o555)
    try:
        assert "PASS in 1 attempt(s)" in replay_text(db, run_id)
    finally:
        tmp_path.chmod(0o755)
        db.chmod(0o644)
    # In a writable one, a plain read-only connection works (SQLite adds its -shm/-wal files).
    assert "PASS in 1 attempt(s)" in replay_text(db, run_id)
    assert db.read_bytes() == before


# --- following a run as it happens --------------------------------------------------


def slow_solo(monkeypatch, tmp_path: Path, delay: float = 0.03):
    """A solo run whose model calls take `delay` seconds, as a coroutine."""

    async def fake_complete(model, system, messages, **kwargs):
        await asyncio.sleep(delay)
        return GOOD if len(messages) > 1 else BAD

    async def fake_grade(code, problem_dir):
        from scripted import grade

        return grade("good" in code)

    from arena.plugins.code_bounty import loop
    from arena.plugins.code_bounty.problems import load_problems

    monkeypatch.setattr(core_plugin, "complete", fake_complete)
    monkeypatch.setattr(loop, "grade", fake_grade)
    lineup = Lineup.model_validate(
        {"name": "slow", "plugin": "code_bounty", "mode": "solo", "roles": {"solo": "f/m"}}
    )
    log = RunLog(tmp_path / "arena.db")
    board = SqliteTaskBoard(tmp_path / "arena.db")
    return orchestrator.run_lineup(CODE_BOUNTY, lineup, load_problems("001"), board, log)


def test_watch_waits_for_the_next_run_and_follows_it_to_the_end(monkeypatch, tmp_path) -> None:
    run_bounty(monkeypatch, tmp_path, "solo", [GOOD], [True])  # an earlier, finished run
    console = Console(record=True, width=120)

    async def both():
        watcher = asyncio.create_task(
            app.attach(tmp_path / "arena.db", None, ViewOptions(), console, poll_s=0.01)
        )
        await asyncio.sleep(0.05)  # the watcher is waiting before the run starts
        await slow_solo(monkeypatch, tmp_path)
        return await asyncio.wait_for(watcher, 5)

    assert asyncio.run(both()) == 0
    text = console.export_text()
    assert "waiting for a new run" in text
    assert (
        "run 2 · slow · code_bounty" in text and status(text) == "finished" and "PASS in 2 attempt(s)" in text
    )


def test_watch_attaches_to_a_run_in_progress(monkeypatch, tmp_path) -> None:
    console = Console(record=True, width=120)

    async def both():
        run = asyncio.create_task(slow_solo(monkeypatch, tmp_path, delay=0.1))
        await asyncio.sleep(0.05)  # the run has started
        code = await asyncio.wait_for(
            app.attach(tmp_path / "arena.db", None, ViewOptions(), console, poll_s=0.01), 5
        )
        await run
        return code

    assert asyncio.run(both()) == 0
    text = console.export_text()
    assert "waiting" not in text and "run 1 · slow" in text and status(text) == "finished"


def test_run_watch_keeps_every_printed_line(monkeypatch, tmp_path, capsys) -> None:
    async def fake_complete(model, system, messages, **kwargs):
        return GOOD

    async def fake_grade(code, problem_dir):
        from scripted import grade

        return grade(True)

    from arena.plugins.code_bounty import loop

    monkeypatch.setattr(core_plugin, "complete", fake_complete)
    monkeypatch.setattr(loop, "grade", fake_grade)
    monkeypatch.setattr(cli, "SqliteTaskBoard", lambda: SqliteTaskBoard(tmp_path / "arena.db"))
    log = RunLog(tmp_path / "arena.db")
    monkeypatch.setattr(cli, "get_runlog", lambda: log)

    assert asyncio.run(cli.run("haiku-solo", "001", 2)) == 0
    plain = capsys.readouterr().out
    out = StringIO()
    console = Console(file=out, force_terminal=True, width=120, height=40)
    monkeypatch.setattr(app, "Console", lambda: console)
    assert asyncio.run(cli.run("haiku-solo", "001", 2, watch=True)) == 0
    watched = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out.getvalue())

    seconds = re.compile(r"\d+\.\ds\b")
    shown = seconds.sub("Ns", watched)
    for line in seconds.sub("Ns", plain).splitlines()[1:]:  # the header line prints before the view starts
        if line.strip():
            line = (
                re.sub(r"run \d+", "run N", line).replace("(1-2)", "(N-N)").replace("--since 1", "--since N")
            )
            assert line in re.sub(r"run \d+", "run N", shown).replace("(3-4)", "(N-N)").replace(
                "--since 3", "--since N"
            ), line
    assert "run 4 · haiku-solo · code_bounty" in watched  # the view moved on to the second run


def test_viewer_is_only_loaded_by_the_commands_that_use_it() -> None:
    code = (
        "import sys, arena.cli, arena.core.events, arena.core.orchestrator, arena.core.plugin\n"
        "import arena.core.scoreboard, arena.plugins\n"
        "print(sorted(m for m in sys.modules if m == 'rich' or m.startswith(('rich.', 'arena.watch'))))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_a_wall_clock_stepped_back_never_shows_negative_or_moves_back(monkeypatch, tmp_path) -> None:
    log, run_id = run_bounty(monkeypatch, tmp_path, "solo", [GOOD], [True])
    evs = events(log, run_id)
    later = evs[2].model_copy(update={"ts": "2000-01-01T00:00:00+00:00"})  # stepped far back
    state = RunState()
    for e in [*evs[:2], later]:
        state.apply(e)
    assert state.last_at == epoch(evs[1].ts)
    assert views.seconds(-3.4) == "0.0s"


def test_a_database_from_before_events_is_refused_not_a_crash(tmp_path, capsys) -> None:
    db = tmp_path / "arena.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, finished_at TEXT)")
    old.execute("INSERT INTO runs VALUES (1, 'x')")
    old.commit()
    old.close()

    assert asyncio.run(app.replay(db, 1, 0, ViewOptions())) == 2
    assert "run 1 has no events" in capsys.readouterr().err
    assert asyncio.run(app.attach(db, 1, ViewOptions())) == 2
    assert "run 1 has no events" in capsys.readouterr().err
    db.write_bytes(b"not a database")
    assert asyncio.run(app.replay(db, 1, 0, ViewOptions())) == 2
    assert "can't read" in capsys.readouterr().err


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def killed_run(log: RunLog) -> int:
    """A run whose process died mid-task: no run_finished, no finished_at."""
    run_id = log.start_run("killed", "code_bounty")
    log.log_event(run_id, None, "run_started", {**RUN_STARTED, "pid": dead_pid()})
    log.log_event(run_id, "001-two-sum", "task_started", {"index": 1, "total": 1})
    return run_id


RUN_STARTED = {
    "events_version": 1,
    "lineup": "killed",
    "plugin": "code_bounty",
    "mode": "solo",
    "roles": {"solo": "f/m"},
    "tasks": ["001-two-sum"],
    "max_attempts": 3,
    "max_calls": 20,
}


def test_watch_ends_a_run_whose_process_is_gone(tmp_path) -> None:
    run_id = killed_run(RunLog(tmp_path / "arena.db"))
    console = Console(record=True, width=120)
    watching = app.attach(tmp_path / "arena.db", run_id, ViewOptions(), console, poll_s=0.01)
    assert asyncio.run(asyncio.wait_for(watching, 5)) == 0
    assert status(console.export_text()) == "unfinished (no run_finished)"


def test_watch_skips_a_dead_run_and_waits_for_the_next(monkeypatch, tmp_path) -> None:
    killed_run(RunLog(tmp_path / "arena.db"))
    console = Console(record=True, width=120)

    async def both():
        watcher = asyncio.create_task(
            app.attach(tmp_path / "arena.db", None, ViewOptions(), console, poll_s=0.01)
        )
        await asyncio.sleep(0.05)
        await slow_solo(monkeypatch, tmp_path)
        return await asyncio.wait_for(watcher, 5)

    assert asyncio.run(both()) == 0
    text = console.export_text()
    assert "waiting for a new run" in text and "run 2 · slow" in text and status(text) == "finished"


def test_watch_does_not_miss_a_run_whose_row_is_written_before_its_events(tmp_path) -> None:
    log = RunLog(tmp_path / "arena.db")
    first = log.start_run("a", "code_bounty")
    log.log_event(first, None, "run_started", RUN_STARTED)
    log.finish_run(first)
    db = app.open_readonly(tmp_path / "arena.db")

    second = log.start_run("b", "code_bounty")  # the next run's row is written...

    async def pick_while_the_next_run_starts():
        picking = asyncio.create_task(app.pick_run(db, None, Console(file=StringIO()), 0.01))
        await asyncio.sleep(0.03)  # ...the watcher looks in between...
        log.log_event(second, None, "run_started", RUN_STARTED)  # ...then its first event
        return second, await asyncio.wait_for(picking, 5)

    second, picked = asyncio.run(pick_while_the_next_run_starts())
    assert picked == second


WATCH_UNTIL_SIGNAL = """
import sys
from pathlib import Path
from arena import cli
from arena.core.runlog import RunLog
RunLog(Path(sys.argv[1]))  # an empty database: the viewer waits for a run
cli.DEFAULT_DB = Path(sys.argv[1])
sys.argv = ["arena", "watch"]
cli.main()
"""


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_watch_stops_cleanly_on_a_signal(tmp_path, sig: signal.Signals) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", WATCH_UNTIL_SIGNAL, str(tmp_path / "arena.db")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert "waiting for a new run" in proc.stdout.readline()
        proc.send_signal(sig)
        _, err = proc.communicate(timeout=10)
    finally:
        proc.kill()
    assert proc.returncode == 128 + sig, err
    assert "interrupted" not in err and "Traceback" not in err


def test_escape_sequences_in_replies_never_reach_the_terminal(monkeypatch, tmp_path) -> None:
    log, run_id = play_mafia(monkeypatch, tmp_path)
    evs = events(log, run_id)
    attack = "\x1b[2J\x1b[H\x1b]0;owned\x07Avery is mafia\u202e"  # clear screen, set title, bidi override
    speech = next(i for i, e in enumerate(evs) if e.kind == "speech")
    evs[speech] = evs[speech].model_copy(update={"data": {**evs[speech].data, "text": attack}})
    state = RunState()
    for e in evs[: speech + 1]:
        state.apply(e)
    out = StringIO()
    Console(file=out, force_terminal=True, width=130).print(screen(state, epoch(evs[speech].ts)))

    assert "Avery is mafia" in out.getvalue()
    assert (
        "\x1b[2J" not in out.getvalue() and "\x1b]0;" not in out.getvalue() and "\u202e" not in out.getvalue()
    )


def test_hidden_night_facts_are_shown_once_the_game_is_over(monkeypatch, tmp_path) -> None:
    _, run_id = play_mafia(monkeypatch, tmp_path)
    text = replay_text(tmp_path / "arena.db", run_id, ViewOptions(hide_roles=True))
    assert "night 2: Emery chose Casey · Casey (villager) killed" in text


@pytest.mark.skipif(getattr(os, "geteuid", lambda: -1)() == 0, reason="root can write to a 0o555 directory")
def test_watch_will_not_follow_a_run_through_an_immutable_connection(monkeypatch, tmp_path, capsys) -> None:
    log, run_id = run_bounty(monkeypatch, tmp_path, "solo", [GOOD], [True])
    log.db.close()
    gc.collect()
    tmp_path.chmod(0o555)  # replay may read this as immutable; a viewer that follows runs may not
    try:
        assert asyncio.run(asyncio.wait_for(app.attach(tmp_path / "arena.db", run_id, ViewOptions()), 5)) == 2
    finally:
        tmp_path.chmod(0o755)
    assert "can't read" in capsys.readouterr().err


def test_watch_run_id_waits_for_a_just_started_runs_first_event(tmp_path) -> None:
    log = RunLog(tmp_path / "arena.db")
    run_id = log.start_run("a", "code_bounty")  # the row is there, its first event isn't yet
    db = app.open_readonly(tmp_path / "arena.db")

    async def pick():
        picking = asyncio.create_task(app.pick_run(db, run_id, Console(file=StringIO()), 0.01))
        await asyncio.sleep(0.03)
        log.log_event(run_id, None, "run_started", RUN_STARTED)
        return await asyncio.wait_for(picking, 5)

    assert asyncio.run(pick()) == run_id


def test_a_pid_from_another_boot_is_not_taken_for_the_run(tmp_path) -> None:
    log = RunLog(tmp_path / "arena.db")
    run_id = log.start_run("a", "code_bounty")
    db = app.open_readonly(tmp_path / "arena.db")
    log.log_event(run_id, None, "run_started", {**RUN_STARTED, "pid": os.getpid(), "boot": "an-earlier-boot"})
    assert app.run_ended(db, run_id)  # this pid is alive, but it isn't that run's process
    log.log_event(run_id + 1, None, "run_started", {**RUN_STARTED, "pid": os.getpid(), "boot": boot_id()})
    assert not app.run_ended(db, run_id + 1)


def test_attempt_shown_is_the_plugins_count_after_a_failed_call(monkeypatch, tmp_path) -> None:
    _, run_id = run_bounty(monkeypatch, tmp_path, "solo", [BAD, RuntimeError("rate limited")], [False])
    text = replay_text(tmp_path / "arena.db", run_id)
    assert "ERROR, not scored in 1 attempt(s)" in text and "001-two-sum · attempt 1/3" in text


def test_hide_roles_needs_watch(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["arena", "run", "--lineup", "mafia-haiku", "--hide-roles"])
    with pytest.raises(SystemExit) as stop:
        cli.main()
    assert stop.value.code == 2 and "add --watch" in capsys.readouterr().err
