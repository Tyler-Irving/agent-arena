"""The plan -> dispatch -> collect -> decide loop.

Only solo mode so far: one model gets the problem, writes code, sees the test
failures and retries. Team mode is build step 5.

The orchestrator calls the code bounty plugin directly because it is the only
plugin. Pull out a plugin interface when a second one arrives.
"""

import time

from pydantic import BaseModel

from arena.core.lineup import Lineup, RoleConfig
from arena.core.runlog import RunLog
from arena.core.taskboard import TaskBoard
from arena.models.router import complete
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


async def run_lineup(
    lineup: Lineup, problems: list[Problem], board: TaskBoard, log: RunLog
) -> tuple[int, list[Outcome]]:
    """Run every problem through the lineup. Returns (run_id, outcomes)."""
    if lineup.mode != "solo":
        raise NotImplementedError("team mode is build step 5")

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
            outcome = await solve_solo(problem, lineup.roles["solo"], run_id, log)
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
                await board.fail(task.id, outcome.error or f"tests failing after {outcome.attempts} attempts")

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
    start = time.perf_counter()
    messages = [{"role": "user", "content": roles.solo_task(problem.prompt)}]
    attempt, calls = 0, 0
    cost: float | None = 0.0
    passed, error = False, None

    while attempt < MAX_ATTEMPTS and calls < MAX_CALLS:
        attempt += 1
        calls += 1
        try:
            reply = await complete(
                role.model,
                roles.SOLO_SYSTEM,
                messages,
                think=role.think,
                run_id=run_id,
                problem_id=problem.id,
                role="solo",
            )
        except Exception as e:  # provider error; the router already logged it
            error = f"{type(e).__name__}: {e}"
            print(f"   attempt {attempt}: model call failed: {error}")
            break
        cost = None if cost is None or reply.cost_usd is None else cost + reply.cost_usd
        model_note = f"model {reply.latency_ms / 1000:.1f}s, {reply.output_tokens} out tokens"

        code = extract_code(reply.text)
        if code is None:
            log.log_attempt(run_id=run_id, problem_id=problem.id, attempt=attempt, code=None, passed=False)
            print(f"   attempt {attempt}: no code block in reply ({model_note})")
            feedback = roles.NO_CODE_FEEDBACK
        else:
            result = await grade(code, problem.path)
            log.log_attempt(
                run_id=run_id,
                problem_id=problem.id,
                attempt=attempt,
                code=code,
                passed=result.passed,
                tests_passed=result.tests_passed,
                tests_total=result.tests_total,
                timed_out=result.timed_out,
                output=result.stdout + result.stderr,
            )
            print(f"   attempt {attempt}: {describe(result)} ({model_note})")
            if result.passed:
                passed = True
                break
            feedback = roles.grade_feedback(result)

        messages += [
            {"role": "assistant", "content": reply.text or "(empty reply)"},
            {"role": "user", "content": feedback},
        ]

    return Outcome(
        problem_id=problem.id,
        passed=passed,
        attempts=attempt,
        calls=calls,
        cost_usd=cost,
        wall_time_ms=int((time.perf_counter() - start) * 1000),
        error=error,
    )


def describe(result: GradeResult) -> str:
    if result.timed_out:
        return "timed out"
    return f"{result.tests_passed}/{result.tests_total} tests"


def format_cost(cost: float | None) -> str:
    return "cost unknown" if cost is None else f"${cost:.4f}"
