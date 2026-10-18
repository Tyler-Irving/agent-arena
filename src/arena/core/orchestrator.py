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

from pydantic import BaseModel

from arena.core.lineup import Lineup, RoleConfig
from arena.core.runlog import RunLog
from arena.core.taskboard import TaskBoard
from arena.models.router import ModelResponse, complete
from arena.plugins.code_bounty import roles
from arena.plugins.code_bounty.grader import GradeResult, grade
from arena.plugins.code_bounty.problems import Problem, extract_code

PLUGIN = "code_bounty"
MAX_ATTEMPTS = 3
MAX_CALLS = 20


class Outcome(BaseModel):
    problem_id: str
    passed: bool
    attempts: int
    calls: int
    cost_usd: float | None  # None if any call used a model with no known price
    wall_time_ms: int
    error: str | None = None
    gave_up: bool = False  # the orchestrator stopped before MAX_ATTEMPTS


class StopProblem(Exception):
    """A model call failed or the call limit was hit; the problem ends unsolved."""


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
            raise StopProblem(f"hit the {MAX_CALLS}-call limit")
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
        except Exception as e:  # provider error; the router already logged it
            raise StopProblem(f"{type(e).__name__}: {e}") from e
        self.cost = None if self.cost is None or reply.cost_usd is None else self.cost + reply.cost_usd
        return reply

    async def grade(self, attempt: int, code: str | None, model_note: str) -> GradeResult | None:
        """Grade and log one attempt. None if the reply had no code to grade."""
        if code is None:
            self.log.log_attempt(
                run_id=self.run_id, problem_id=self.problem.id, attempt=attempt, code=None, passed=False
            )
            print(f"   attempt {attempt}: no code block in reply ({model_note})")
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
        print(f"   attempt {attempt}: {describe(result)} ({model_note})")
        return result

    def outcome(
        self, *, passed: bool, attempts: int, error: str | None = None, gave_up: bool = False
    ) -> Outcome:
        return Outcome(
            problem_id=self.problem.id,
            passed=passed,
            attempts=attempts,
            calls=self.calls,
            cost_usd=self.cost,
            wall_time_ms=int((time.perf_counter() - self.start) * 1000),
            error=error,
            gave_up=gave_up,
        )


async def run_lineup(
    lineup: Lineup, problems: list[Problem], board: TaskBoard, log: RunLog
) -> tuple[int, list[Outcome]]:
    """Run every problem through the lineup. Returns (run_id, outcomes)."""
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
            if lineup.mode == "solo":
                outcome = await solve_solo(problem, lineup.roles["solo"], run_id, log)
            else:
                outcome = await solve_team(problem, lineup.roles, run_id, log)
            outcomes.append(outcome)

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
                reason = "orchestrator gave up" if outcome.gave_up else "tests failing"
                await board.fail(task.id, outcome.error or f"{reason} after {outcome.attempts} attempts")

            verdict = "PASS" if outcome.passed else "FAIL"
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
    passed, error = False, None

    while attempt < MAX_ATTEMPTS:
        attempt += 1
        try:
            reply = await session.call("solo", role, roles.SOLO_SYSTEM, messages)
        except StopProblem as e:
            error = str(e)
            print(f"   attempt {attempt}: model call failed: {error}")
            break

        result = await session.grade(attempt, extract_code(reply.text), model_note(reply))
        if result is None:
            feedback = roles.NO_CODE_FEEDBACK
        elif result.passed:
            passed = True
            break
        else:
            feedback = roles.grade_feedback(result)

        messages += [
            {"role": "assistant", "content": reply.text or "(empty reply)"},
            {"role": "user", "content": feedback},
        ]

    return session.outcome(passed=passed, attempts=attempt, error=error)


async def solve_team(problem: Problem, team: dict[str, RoleConfig], run_id: int, log: RunLog) -> Outcome:
    """planner -> coder -> grader, and on failure reviewer -> orchestrator decides.

    Every call is a fresh single-message conversation built from the current
    plan and the latest attempt only, so local models' context stays small.
    The reviewer and orchestrator are skipped after the last attempt, since
    nothing could act on what they say.
    """
    session = Session(problem, run_id, log)
    prompt = problem.prompt
    attempt = 0
    passed, gave_up, error = False, False, None

    async def ask(role_name: str, system: str, content: str) -> ModelResponse:
        return await session.call(role_name, team[role_name], system, [{"role": "user", "content": content}])

    try:
        reply = await ask("planner", roles.PLANNER_SYSTEM, roles.plan_task(prompt))
        plan = reply.text or "(no plan)"
        print(f"   plan ({model_note(reply)})")
        last_code: str | None = None
        last_feedback: str | None = None

        while attempt < MAX_ATTEMPTS:
            attempt += 1
            reply = await ask(
                "coder", roles.CODER_SYSTEM, roles.coder_task(prompt, plan, last_code, last_feedback)
            )
            code = extract_code(reply.text)
            result = await session.grade(attempt, code, model_note(reply))
            if result is None or code is None:
                last_code, last_feedback = None, roles.NO_CODE_FEEDBACK
                continue
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
                gave_up = True
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
    except StopProblem as e:
        error = str(e)
        print(f"   stopped: {error}")

    return session.outcome(passed=passed, attempts=attempt, error=error, gave_up=gave_up)


def model_note(reply: ModelResponse) -> str:
    return f"model {reply.latency_ms / 1000:.1f}s, {reply.output_tokens} out tokens"


def describe(result: GradeResult) -> str:
    if result.timed_out:
        return "timed out"
    return f"{result.tests_passed}/{result.tests_total} tests"


def format_cost(cost: float | None) -> str:
    return "cost unknown" if cost is None else f"${cost:.4f}"
