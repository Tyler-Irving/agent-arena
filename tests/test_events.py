"""The events table: what core and the plugins log for arena watch / replay, in order."""

import asyncio
import json
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

import pytest
from scripted import CODE_BOUNTY, TOWN_WINS, events, kinds, play_mafia, reply, run_bounty

from arena.core.events import EVENTS_VERSION, Event, brief
from arena.core.runlog import RunLog
from arena.plugins.code_bounty.grader import GraderError

CORE = {"run_started", "task_started", "call_started", "call_finished", "task_finished", "run_finished"}
GOOD, BAD = reply("```python\ngood\n```"), reply("```python\nbad\n```")


def assert_paired(evs: list[Event]) -> None:
    """Each run and task is opened once and closed once, and nothing is logged after the run closes."""
    count = Counter(kinds(evs))
    assert count["run_started"] == count["run_finished"] == 1
    assert evs[0].kind == "run_started" and evs[-1].kind == "run_finished"
    started = [e.task_id for e in evs if e.kind == "task_started"]
    assert started == [e.task_id for e in evs if e.kind == "task_finished"]
    calls = [e for e in evs if e.kind in ("call_started", "call_finished")]
    assert kinds(calls) == ["call_started", "call_finished"] * (len(calls) // 2)


# --- core events ---------------------------------------------------------------------


def test_solo_run_logs_core_events_in_order(monkeypatch, tmp_path) -> None:
    log, run_id = run_bounty(monkeypatch, tmp_path, "solo", [BAD, GOOD], [False, True])
    evs = events(log, run_id)

    assert_paired(evs)
    assert kinds(evs, CORE) == [
        "run_started",
        "task_started",
        *["call_started", "call_finished"] * 2,
        "task_finished",
        "run_finished",
    ]
    run = evs[0]
    assert (run.task_id, run.role, run.model) == (None, None, None)
    assert run.data == {
        "events_version": EVENTS_VERSION,
        "lineup": "fake-solo",
        "plugin": "code_bounty",
        "mode": "solo",
        "roles": {"solo": "fake/model"},
        "tasks": ["001-two-sum"],
        "selector": "001",
        "max_attempts": 3,
        "settings": {},
        "max_calls": CODE_BOUNTY.max_calls,
        "pid": os.getpid(),
    }
    task = next(e for e in evs if e.kind == "task_started")
    assert (task.task_id, task.data) == ("001-two-sum", {"index": 1, "total": 1})

    started, finished = [e for e in evs if e.kind.startswith("call_")][:2]
    assert (started.task_id, started.role, started.model) == ("001-two-sum", "solo", "fake/model")
    assert started.data == {"n": 1, "max_calls": 20}
    assert (finished.role, finished.model) == ("solo", "fake/model")
    assert finished.data == {
        "n": 1,
        "input_tokens": 10,
        "output_tokens": 20,
        "reasoning_tokens": 0,
        "latency_ms": 5,
        "cost_usd": 0.01,
        "truncated": False,
        "error": None,
    }
    done = next(e for e in evs if e.kind == "task_finished")
    assert done.task_id == "001-two-sum"
    assert done.data == {
        "status": "done",
        "verdict": "PASS in 2 attempt(s)",
        "score": 1.0,
        "reason": None,
        "calls": 2,
        "cost_usd": pytest.approx(0.02),
        "wall_time_ms": done.data["wall_time_ms"],
    }
    assert evs[-1].data == {"interrupted": False}


def test_team_run_logs_each_role_call(monkeypatch, tmp_path) -> None:
    replies = {
        "planner": [reply("brute force"), reply("use a dict")],
        "coder": [BAD, BAD, GOOD],
        "reviewer": [reply("off by one"), reply("too slow")],
        "orchestrator": [reply("DECISION: RETRY\nclose"), reply("DECISION: REPLAN\nwrong approach")],
    }
    log, run_id = run_bounty(monkeypatch, tmp_path, "team", replies, [False, False, True])
    evs = events(log, run_id)

    assert_paired(evs)
    roles = [e.role for e in evs if e.kind == "call_started"]
    assert roles == ["planner", "coder", "reviewer", "orchestrator", "coder"] + [
        "reviewer", "orchestrator", "planner", "coder",
    ]  # fmt: skip
    assert [e.data["n"] for e in evs if e.kind == "call_finished"] == list(range(1, 10))
    assert next(e for e in evs if e.kind == "call_started" and e.role == "reviewer").model == "fake/review"
    assert next(e for e in evs if e.kind == "task_finished").data["verdict"] == "PASS in 3 attempt(s)"


def test_failed_task_is_logged_as_failed(monkeypatch, tmp_path) -> None:
    log, run_id = run_bounty(monkeypatch, tmp_path, "solo", [BAD], [False], max_attempts=1)
    done = next(e for e in events(log, run_id) if e.kind == "task_finished")
    assert (done.data["status"], done.data["score"]) == ("failed", 0.0)
    assert done.data["reason"] == "tests failing after 1 attempts"


def test_provider_error_is_logged_on_the_call_and_task(monkeypatch, tmp_path) -> None:
    log, run_id = run_bounty(monkeypatch, tmp_path, "solo", [RuntimeError("boom")], [])
    evs = events(log, run_id)

    assert_paired(evs)
    call = next(e for e in evs if e.kind == "call_finished")
    assert call.data["error"] == "RuntimeError: boom" and "input_tokens" not in call.data
    done = next(e for e in evs if e.kind == "task_finished")
    assert (done.data["status"], done.data["score"], done.data["reason"]) == (
        "error",
        None,
        "RuntimeError: boom",
    )


def test_grader_error_closes_the_task_and_run(monkeypatch, tmp_path) -> None:
    with pytest.raises(GraderError):
        run_bounty(monkeypatch, tmp_path, "solo", [GOOD], [GraderError("docker went away")])
    log = RunLog(tmp_path / "arena.db")
    evs = events(log, 1)

    assert_paired(evs)
    done = next(e for e in evs if e.kind == "task_finished")
    assert (done.data["status"], done.data["reason"]) == ("failed", "grader error: docker went away")
    assert done.data["calls"] == 1 and done.data["verdict"] is None
    assert evs[-1].data == {"interrupted": True}


def test_plugin_crash_closes_the_task_and_run(monkeypatch, tmp_path) -> None:
    with pytest.raises(ZeroDivisionError):
        run_bounty(monkeypatch, tmp_path, "solo", [GOOD], [ZeroDivisionError("a bug")])
    evs = events(RunLog(tmp_path / "arena.db"), 1)

    assert_paired(evs)
    done = next(e for e in evs if e.kind == "task_finished")
    assert (done.data["status"], done.data["reason"]) == ("error", "ZeroDivisionError: a bug")
    assert evs[-1].data == {"interrupted": True}


def test_ctrl_c_during_a_call_marks_call_task_and_run_interrupted(monkeypatch, tmp_path) -> None:
    with pytest.raises(asyncio.CancelledError):
        run_bounty(monkeypatch, tmp_path, "solo", [BAD, asyncio.CancelledError()], [False])
    evs = events(RunLog(tmp_path / "arena.db"), 1)

    assert_paired(evs)
    assert kinds(evs, CORE)[-4:] == ["call_started", "call_finished", "task_finished", "run_finished"]
    assert evs[-4].data["n"] == 2 and evs[-3].data["error"] == "interrupted"
    assert evs[-2].data["status"] == "interrupted" and evs[-2].data["calls"] == 2
    assert evs[-1].data == {"interrupted": True}


def test_ctrl_c_during_grading_marks_task_and_run_interrupted(monkeypatch, tmp_path) -> None:
    with pytest.raises(asyncio.CancelledError):
        run_bounty(monkeypatch, tmp_path, "solo", [BAD], [asyncio.CancelledError()])
    evs = events(RunLog(tmp_path / "arena.db"), 1)

    assert_paired(evs)
    assert next(e for e in evs if e.kind == "call_finished").data["error"] is None
    assert [e.data.get("status") for e in evs[-2:]] == ["interrupted", None]
    assert evs[-1].data == {"interrupted": True}


# --- the database ------------------------------------------------------------------


def test_database_is_in_wal_mode(tmp_path) -> None:
    RunLog(tmp_path / "arena.db")
    assert sqlite3.connect(tmp_path / "arena.db").execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_each_event_is_visible_to_another_connection_at_once(monkeypatch, tmp_path) -> None:
    seen: list[tuple] = []

    def look_then_reply():
        reader = sqlite3.connect(tmp_path / "arena.db")
        seen.append(reader.execute("SELECT kind, role, data FROM events ORDER BY id DESC LIMIT 1").fetchone())
        reader.close()
        return GOOD

    run_bounty(monkeypatch, tmp_path, "solo", [look_then_reply], [True])
    assert seen == [("call_started", "solo", '{"n": 1, "max_calls": 20}')]


def test_an_open_reader_does_not_block_the_run(monkeypatch, tmp_path) -> None:
    RunLog(tmp_path / "arena.db")
    reader = sqlite3.connect(tmp_path / "arena.db", isolation_level=None)
    reader.execute("BEGIN")
    assert reader.execute("SELECT count(*) FROM events").fetchone() == (0,)  # holds a read snapshot

    log, run_id = run_bounty(monkeypatch, tmp_path, "solo", [GOOD], [True])

    assert reader.execute("SELECT count(*) FROM events").fetchone() == (0,)  # still its snapshot
    reader.execute("COMMIT")
    assert reader.execute("SELECT count(*) FROM events").fetchone()[0] == len(events(log, run_id))


def test_a_logging_failure_is_a_crash_not_a_provider_error(monkeypatch, tmp_path) -> None:
    real = RunLog.log_event

    def failing(self, run_id, task_id, kind, data, role=None, model=None):
        if kind == "call_finished":
            raise sqlite3.OperationalError("disk I/O error")
        real(self, run_id, task_id, kind, data, role, model)

    monkeypatch.setattr(RunLog, "log_event", failing)
    with pytest.raises(sqlite3.OperationalError):
        run_bounty(monkeypatch, tmp_path, "solo", [GOOD], [True])
    log = RunLog(tmp_path / "arena.db")
    reason = log.db.execute("SELECT reason FROM tasks").fetchone()[0]
    assert reason == "OperationalError: disk I/O error"  # not "model call failed"
    done = next(e for e in events(log, 1) if e.kind == "task_finished")
    assert (done.data["status"], done.data["reason"]) == ("error", "OperationalError: disk I/O error")


def test_event_data_that_json_cannot_encode_is_stringified(tmp_path) -> None:
    log = RunLog(tmp_path / "arena.db")
    run_id = log.start_run("x", "p")
    log.log_event(run_id, "t", "odd", {"value": {1, 2}.__class__})  # a type, not JSON
    assert events(log, run_id)[0].data == {"value": "<class 'set'>"}


def test_brief_collapses_and_cuts() -> None:
    assert brief("  two\n lines  ") == "two lines"
    assert brief("x" * 10, 5) == "xxxx…"
    assert brief(None) == ""


# --- plugin events -----------------------------------------------------------------


def flow(evs: list[Event]) -> list[str]:
    """Each call as its role, each plugin event as its kind (call_finished and core events left out)."""
    return [
        e.role or "" if e.kind == "call_started" else e.kind
        for e in evs
        if e.kind not in CORE - {"call_started"}
    ]


def test_solo_logs_grading_and_graded(monkeypatch, tmp_path) -> None:
    log, run_id = run_bounty(monkeypatch, tmp_path, "solo", [reply("no code"), BAD, GOOD], [False, True])
    evs = events(log, run_id)

    assert flow(evs) == ["solo", "graded", "solo", "grading", "graded", "solo", "grading", "graded"]
    graded = [e.data for e in evs if e.kind == "graded"]
    no_grade = {"tests_passed": None, "tests_total": None, "timed_out": None}
    assert graded[0] == {"attempt": 1, "passed": False, **no_grade, "has_code": False}
    five_of_six = {"tests_passed": 5, "tests_total": 6, "timed_out": False}
    assert graded[1] == {"attempt": 2, "passed": False, **five_of_six, "has_code": True}
    assert graded[2]["passed"] is True and graded[2]["attempt"] == 3
    assert [e.data for e in evs if e.kind == "grading"] == [{"attempt": 2}, {"attempt": 3}]


def test_team_logs_plan_review_decision_and_replan(monkeypatch, tmp_path) -> None:
    replies = {
        "planner": [reply("brute force"), reply("use a\n  dict")],
        "coder": [BAD, BAD, GOOD],
        "reviewer": [reply("off by one"), reply("too slow")],
        "orchestrator": [reply("DECISION: RETRY\nclose"), reply("DECISION: REPLAN\nwrong approach")],
    }
    log, run_id = run_bounty(monkeypatch, tmp_path, "team", replies, [False, False, True])
    evs = events(log, run_id)

    assert flow(evs) == [
        "planner", "plan",
        "coder", "grading", "graded", "reviewer", "review", "orchestrator", "decision",
        "coder", "grading", "graded", "reviewer", "review", "orchestrator", "decision", "planner", "replan",
        "coder", "grading", "graded",
    ]  # fmt: skip
    data = {
        kind: [e.data for e in evs if e.kind == kind] for kind in ("plan", "review", "decision", "replan")
    }
    assert data["plan"] == [{"plan": 1, "text": "brute force"}]
    assert data["review"] == [{"attempt": 1, "text": "off by one"}, {"attempt": 2, "text": "too slow"}]
    assert data["decision"] == [
        {"attempt": 1, "decision": "RETRY", "parsed": True, "reason": "close", "attempts_left": 2},
        {"attempt": 2, "decision": "REPLAN", "parsed": True, "reason": "wrong approach", "attempts_left": 1},
    ]
    assert data["replan"] == [{"plan": 2, "reason": "wrong approach", "text": "use a dict"}]


def test_unparseable_decision_is_logged_as_a_retry(monkeypatch, tmp_path) -> None:
    replies = {
        "planner": [reply("plan")],
        "coder": [BAD, GOOD],
        "reviewer": [reply("hm")],
        "orchestrator": [reply("Let's try again.")],
    }
    log, run_id = run_bounty(monkeypatch, tmp_path, "team", replies, [False, True])
    decision = next(e for e in events(log, run_id) if e.kind == "decision")
    assert decision.data == {
        "attempt": 1,
        "decision": "RETRY",
        "parsed": False,
        "reason": "no decision in reply",
        "attempts_left": 2,
    }


def test_mafia_game_logs_its_events_in_order(monkeypatch, tmp_path) -> None:
    log, run_id = play_mafia(monkeypatch, tmp_path)
    evs = events(log, run_id)

    assert_paired(evs)
    plugin_kinds = kinds(evs, None)
    plugin_kinds = [k for k in plugin_kinds if k not in CORE]
    day = ["phase", *["speech"] * 6, "phase", "votes", "elimination"]
    assert plugin_kinds == [
        "deal",
        "phase", "proposal", "check", "kill",
        *day,
        "phase", "check", "kill",
        "phase", *["speech"] * 4, "phase", "votes", "elimination",
        "win",
    ]  # fmt: skip
    data = lambda kind: [e.data for e in evs if e.kind == kind]  # noqa: E731
    assert data("deal")[0]["players"][:2] == [
        {"seat": 1, "name": "Finley", "role": "villager"},
        {"seat": 2, "name": "Blake", "role": "mafia"},
    ]
    assert data("phase")[:3] == [
        {"phase": "night", "n": 1, "step": None},
        {"phase": "day", "n": 1, "step": "speeches"},
        {"phase": "day", "n": 1, "step": "votes"},
    ]
    assert data("proposal") == [{"n": 1, "proposer": "Blake", "target": "Finley"}]
    assert data("check") == [
        {"n": 1, "detective": "Avery", "target": "Blake", "is_mafia": True},
        {"n": 2, "detective": "Avery", "target": "Emery", "is_mafia": True},
    ]
    assert data("kill") == [
        {"n": 1, "victim": "Finley", "role": "villager", "by": "Emery"},
        {"n": 2, "victim": "Casey", "role": "villager", "by": "Emery"},
    ]
    assert data("speech")[0] == {"n": 1, "speaker": "Blake", "text": "Blake speaks on day 1."}
    votes = data("votes")[0]
    assert votes["invalid"] == [] and votes["votes"]["Blake"] is None and votes["votes"]["Casey"] == "Blake"
    assert data("elimination") == [
        {"n": 1, "name": "Blake", "role": "mafia"},
        {"n": 2, "name": "Emery", "role": "mafia"},
    ]
    assert data("win") == [{"winner": "town", "ended": "day 2"}]


def test_proposal_comes_between_the_two_mafia_calls(monkeypatch, tmp_path) -> None:
    log, run_id = play_mafia(monkeypatch, tmp_path)
    night1 = flow(events(log, run_id))[: flow(events(log, run_id)).index("kill")]
    assert night1 == ["deal", "phase", "mafia:Blake", "proposal", "mafia:Emery", "detective:Avery", "check"]


def test_votes_are_logged_only_at_the_reveal(monkeypatch, tmp_path) -> None:
    log, run_id = play_mafia(monkeypatch, tmp_path)
    evs = events(log, run_id)

    for day in (1, 2):
        start = next(
            i
            for i, e in enumerate(evs)
            if e.kind == "phase" and e.data == {"phase": "day", "n": day, "step": "votes"}
        )
        reveal = next(i for i, e in enumerate(evs) if e.kind == "votes" and e.data["n"] == day)
        voting = evs[start + 1 : reveal]
        # only the vote calls themselves, and they carry no reply text
        assert set(kinds(voting)) == {"call_started", "call_finished"}
        assert all("Blake" not in json.dumps(e.data) and "Emery" not in json.dumps(e.data) for e in voting)
        voters = {e.role.split(":")[1] for e in voting if e.kind == "call_started" and e.role}
        assert set(evs[reveal].data["votes"]) == voters
        assert evs[reveal - 1].kind == "call_finished"  # right after the last vote
    assert kinds(evs).count("votes") == 2


def test_votes_list_invalid_replies_apart_from_none(monkeypatch, tmp_path) -> None:
    log, run_id = play_mafia(monkeypatch, tmp_path, None)  # every reply unusable
    votes = next(e.data for e in events(log, run_id) if e.kind == "votes")
    assert votes["invalid"] == list(votes["votes"]) and set(votes["votes"].values()) == {None}

    log, run_id = play_mafia(monkeypatch, tmp_path / "second", {**TOWN_WINS, "votes": {}})  # all vote none
    votes = next(e.data for e in events(log, run_id) if e.kind == "votes")
    assert votes["invalid"] == [] and set(votes["votes"].values()) == {None}
    elimination = next(e.data for e in events(log, run_id) if e.kind == "elimination")
    assert elimination == {"n": 1, "name": None, "role": None}


# --- events change nothing else ----------------------------------------------------

VARYING = {"started_at", "finished_at", "created_at", "updated_at", "wall_time_ms"}


def dump(path: Path) -> dict[str, list[dict]]:
    """Every table but events, without the columns that differ from run to run."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    tables = [
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'events'")
    ]
    return {
        t: [
            {k: row[k] for k in row.keys() if k not in VARYING}
            for row in db.execute(f"SELECT * FROM {t} ORDER BY id")
        ]
        for t in sorted(tables)
    }


SCRIPTS = {
    "solo": lambda mp, tmp: run_bounty(mp, tmp, "solo", [reply("x"), BAD, GOOD], [False, True]),
    "team": lambda mp, tmp: run_bounty(
        mp,
        tmp,
        "team",
        {
            "planner": [reply("brute force"), reply("use a dict")],
            "coder": [BAD, BAD, GOOD],
            "reviewer": [reply("off by one"), reply("too slow")],
            "orchestrator": [reply("DECISION: RETRY\nclose"), reply("DECISION: REPLAN\nwrong")],
        },
        [False, False, True],
    ),
    "mafia": lambda mp, tmp: play_mafia(mp, tmp),
    "mafia-unusable": lambda mp, tmp: play_mafia(mp, tmp, None),
}


@pytest.mark.parametrize("script", SCRIPTS)
def test_events_change_no_output_or_other_rows(monkeypatch, tmp_path, capsys, script: str) -> None:
    SCRIPTS[script](monkeypatch, tmp_path / "with")
    with_events = capsys.readouterr()
    with monkeypatch.context() as m:
        m.setattr(RunLog, "log_event", lambda *args, **kwargs: None)
        SCRIPTS[script](m, tmp_path / "without")
    without_events = capsys.readouterr()

    seconds = re.compile(r"\d+\.\ds$", re.MULTILINE)  # wall time, the one thing that varies
    assert seconds.sub("Ns", with_events.out) == seconds.sub("Ns", without_events.out)
    assert with_events.err == without_events.err
    assert dump(tmp_path / "with" / "arena.db") == dump(tmp_path / "without" / "arena.db")
    assert len(events(RunLog(tmp_path / "with" / "arena.db"), 1)) > 0
    assert len(events(RunLog(tmp_path / "without" / "arena.db"), 1)) == 0
