"""The plan -> dispatch -> collect -> decide loop.

Solo mode: one model gets the problem, writes code, sees the test failures and
retries.

Team mode: a planner writes the approach, a coder implements it, the grader
runs the tests, and on failure a reviewer explains what went wrong and the
orchestrator model decides whether to retry, replan, or give up.

The orchestrator calls the code bounty plugin directly because it is the only
plugin. Pull out a plugin interface when a second one arrives.
"""

import time
from typing import NamedTuple

from pydantic import BaseModel

from arena.core.lineup import Lineup, RoleConfig
from arena.core.runlog import RunLog, set_runlog
from arena.core.taskboard import TaskBoard
from arena.models.router import ModelResponse, complete
from arena.plugins.code_bounty import roles
from arena.plugins.code_bounty.grader import GraderError, GradeResult, grade
from arena.plugins.code_bounty.problems import Problem, extract_code

PLUGIN = "code_bounty"
MAX_ATTEMPTS = 3
MAX_CALLS = 20


class Outcome(BaseModel):
    problem_id: str
    passed: bool
    attempts: int  # replies that were graded (or had no code to grade)
    calls: int
    cost_usd: float | None  # None if any call used a model with no known price
    wall_time_ms: int
    # A provider error (outage, auth, rate limit). The problem isn't scored,
    # since the failure says nothing about the lineup.
    error: str | None = None
    # Why a scored problem stopped before MAX_ATTEMPTS, if it did.
    stop_reason: str | None = None


class ModelCallFailed(Exception):
    """The provider returned an error; the router has already logged it."""


class CallLimitReached(Exception):
    """The problem used up MAX_CALLS."""


class Graded(NamedTuple):
    code: str
    result: GradeResult


class Session:
    """One problem's model calls, cost and graded attempts, under the hard limits."""

    def __init__(self, problem: Problem, run_id: int, log: RunLog) -> None:
        self.problem = problem
        self.run_id = run_id
        self.log = log
        self.calls = 0
        self.cost: float | None = 0.0  # None once any call has no known price
        self.start = time.perf_counter()

    async def call(
        self, role_name: str, role: RoleConfig, system: str, messages: list[dict]
    ) -> ModelResponse:
        if self.calls >= MAX_CALLS:
            raise CallLimitReached(f"hit the {MAX_CALLS}-call limit")
        self.calls += 1
        try:
            reply = await complete(
                role.model,
                system,
                messages,
                think=role.think,
                run_id=self.run_id,
                problem_id=self.problem.id,
                role=role_name,
            )
        except Exception as e:
            raise ModelCallFailed(f"{type(e).__name__}: {e}") from e
        self.cost = None if self.cost is None or reply.cost_usd is None else self.cost + reply.cost_usd
        return reply

    async def grade(self, attempt: int, reply: ModelResponse) -> Graded | None:
        """Grade and log the code in one reply. None if it had no code to grade."""
        code = extract_code(reply.text)
        if code is None:
            self.log.log_attempt(
                run_id=self.run_id, problem_id=self.problem.id, attempt=attempt, code=None, passed=False
            )
            print(f"   attempt {attempt}: no code block in reply ({model_note(reply)})")
            return None
        result = await grade(code, self.problem.path)
        self.log.log_attempt(
            run_id=self.run_id,
            problem_id=self.problem.id,
            attempt=attempt,
            code=code,
            passed=result.passed,
            tests_passed=result.tests_passed,
            tests_total=result.tests_total,
            timed_out=result.timed_out,
            output=result.stdout + result.stderr,
        )
        print(f"   attempt {attempt}: {describe(result)} ({model_note(reply)})")
        return Graded(code, result)

    def outcome(
        self, *, passed: bool, attempts: int, error: str | None = None, stop_reason: str | None = None
    ) -> Outcome:
        return Outcome(
            problem_id=self.problem.id,
            passed=passed,
            attempts=attempts,
            calls=self.calls,
            cost_usd=self.cost,
            wall_time_ms=int((time.perf_counter() - self.start) * 1000),
            error=error,
            stop_reason=stop_reason,
        )


async def run_lineup(
    lineup: Lineup, problems: list[Problem], board: TaskBoard, log: RunLog
) -> tuple[int, list[Outcome]]:
    """Run every problem through the lineup. Returns (run_id, outcomes)."""
    set_runlog(log)  # the router logs calls to the same database as the run
    run_id = log.start_run(lineup.name, PLUGIN)
    by_id = {p.id: p for p in problems}
    for p in problems:
        await board.put(run_id, p.id)

    outcomes: list[Outcome] = []
    try:
        # Sequential on purpose: only one local model fits in VRAM at a time.
        while (task := await board.next(run_id)) is not None:
            problem = by_id[task.problem_id]
            print(f"== {problem.id}")
            try:
                if lineup.mode == "solo":
                    outcome = await solve_solo(problem, lineup.roles["solo"], run_id, log)
                else:
                    outcome = await solve_team(problem, lineup.roles, run_id, log)
            except GraderError as e:
                # The sandbox broke, so nothing after this can be graded either.
                await board.fail(task.id, f"grader error: {e}")
                raise
            outcomes.append(outcome)

            if outcome.error is None:
                log.log_result(
                    run_id=run_id,
                    problem_id=problem.id,
                    passed=outcome.passed,
                    attempts=outcome.attempts,
                    total_calls=outcome.calls,
                    total_cost_usd=outcome.cost_usd,
                    wall_time_ms=outcome.wall_time_ms,
                )
            if outcome.passed:
                await board.complete(task.id)
            else:
                reason = outcome.stop_reason or "tests failing"
                await board.fail(task.id, outcome.error or f"{reason} after {outcome.attempts} attempts")

            verdict = "PASS" if outcome.passed else "ERROR, not scored" if outcome.error else "FAIL"
            print(
                f"   {verdict} in {outcome.attempts} attempt(s), {outcome.calls} call(s), "
                f"{format_cost(outcome.cost_usd)}, {outcome.wall_time_ms / 1000:.1f}s\n"
            )
    finally:
        log.finish_run(run_id)
    return run_id, outcomes


async def solve_solo(problem: Problem, role: RoleConfig, run_id: int, log: RunLog) -> Outcome:
    """One model writes code, sees test failures, and retries up to MAX_ATTEMPTS."""
    session = Session(problem, run_id, log)
    messages = [{"role": "user", "content": roles.solo_task(problem.prompt)}]
    attempt = 0
    passed, error, stop_reason = False, None, None

    while attempt < MAX_ATTEMPTS:
        try:
            reply = await session.call("solo", role, roles.SOLO_SYSTEM, messages)
        except ModelCallFailed as e:
            error = str(e)
            print(f"   model call failed: {error}")
            break
        except CallLimitReached as e:
            stop_reason = str(e)
            print(f"   stopped: {stop_reason}")
            break
        attempt += 1

        graded = await session.grade(attempt, reply)
        if graded is None:
            feedback = roles.no_code_feedback(reply.truncated)
        elif graded.result.passed:
            passed = True
            break
        else:
            feedback = roles.grade_feedback(graded.result)

        messages += [
            {"role": "assistant", "content": reply.text or "(empty reply)"},
            {"role": "user", "content": feedback},
        ]

    return session.outcome(passed=passed, attempts=attempt, error=error, stop_reason=stop_reason)


async def solve_team(problem: Problem, team: dict[str, RoleConfig], run_id: int, log: RunLog) -> Outcome:
    """planner -> coder -> grader, and on failure reviewer -> orchestrator decides.

    Every call is a fresh single-message conversation built from the current
    plan and the latest graded attempt only, so local models' context stays
    small. The reviewer and orchestrator are skipped after the last attempt,
    since nothing could act on what they say.
    """
    session = Session(problem, run_id, log)
    prompt = problem.prompt
    attempt = 0
    passed, error, stop_reason = False, None, None

    async def ask(role_name: str, system: str, content: str) -> ModelResponse:
        return await session.call(role_name, team[role_name], system, [{"role": "user", "content": content}])

    try:
        reply = await ask("planner", roles.PLANNER_SYSTEM, roles.plan_task(prompt))
        plan = reply.text or "(no plan)"
        print(f"   plan ({model_note(reply)})")
        # The last graded attempt and what was wrong with it, kept until the
        # coder produces new code (a reply without code doesn't replace them).
        last_code: str | None = None
        last_feedback: str | None = None
        no_code_note: str | None = None

        while attempt < MAX_ATTEMPTS:
            feedback = "\n\n".join(f for f in (no_code_note, last_feedback) if f) or None
            reply = await ask(
                "coder", roles.CODER_SYSTEM, roles.coder_task(prompt, plan, last_code, feedback)
            )
            attempt += 1
            no_code_note = None

            graded = await session.grade(attempt, reply)
            if graded is None:
                no_code_note = roles.no_code_feedback(reply.truncated)
                continue
            code, result = graded
            if result.passed:
                passed = True
                break
            if attempt == MAX_ATTEMPTS:
                break

            reply = await ask(
                "reviewer", roles.REVIEWER_SYSTEM, roles.review_task(prompt, plan, code, result)
            )
            review = reply.text or "(no feedback)"
            print(f"   review ({model_note(reply)})")

            attempts_left = MAX_ATTEMPTS - attempt
            reply = await ask(
                "orchestrator",
                roles.ORCHESTRATOR_SYSTEM,
                roles.decide_task(prompt, plan, result, review, attempt, attempts_left),
            )
            decision = roles.parse_decision(reply.text)
            if decision is None:
                print(f"   orchestrator: no decision in reply, retrying ({model_note(reply)})")
                decision = "retry"
            else:
                print(f"   orchestrator: {decision} ({model_note(reply)})")

            if decision == "give_up":
                stop_reason = "orchestrator gave up"
                break
            if decision == "replan":
                reply = await ask(
                    "planner", roles.PLANNER_SYSTEM, roles.replan_task(prompt, plan, code, result, review)
                )
                plan = reply.text or "(no plan)"
                print(f"   replan ({model_note(reply)})")
                # New approach: start the coder fresh rather than anchoring it on the old code.
                last_code, last_feedback = None, None
            else:
                last_code = code
                last_feedback = f"{roles.grade_report(result)}\n\nReviewer's feedback:\n{review}"
    except ModelCallFailed as e:
        error = str(e)
        print(f"   model call failed: {error}")
    except CallLimitReached as e:
        stop_reason = str(e)
        print(f"   stopped: {stop_reason}")

    return session.outcome(passed=passed, attempts=attempt, error=error, stop_reason=stop_reason)


def model_note(reply: ModelResponse) -> str:
    note = f"model {reply.latency_ms / 1000:.1f}s, {reply.output_tokens} out tokens"
    return f"{note}, cut off at the token limit" if reply.truncated else note


def describe(result: GradeResult) -> str:
    if result.timed_out:
        return "timed out"
    return f"{result.tests_passed}/{result.tests_total} tests"


def format_cost(cost: float | None) -> str:
    return "cost unknown" if cost is None else f"${cost:.4f}"
