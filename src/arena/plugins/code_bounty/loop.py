"""The solo and team loops for one problem.

Solo mode: one model gets the problem, writes code, sees the test failures and
retries.

Team mode: a planner writes the approach, a coder implements it, the grader
runs the tests, and on failure a reviewer explains what went wrong and the
orchestrator model decides whether to retry, replan, or give up.
"""

from typing import NamedTuple

from arena.core.events import brief
from arena.core.lineup import RoleConfig
from arena.core.plugin import CallLimitReached, ModelCallFailed, Outcome, Session, model_note
from arena.core.runlog import RunLog, utc_now
from arena.models.router import ModelResponse
from arena.plugins.code_bounty import roles
from arena.plugins.code_bounty.grader import GradeResult, grade
from arena.plugins.code_bounty.problems import Problem, extract_code

# One row per graded attempt, so the final failing grade (which no later
# call sees) is still in the transcript. code is NULL if the reply had none.
ATTEMPTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    problem_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    code TEXT,
    passed INTEGER NOT NULL,
    tests_passed INTEGER,
    tests_total INTEGER,
    timed_out INTEGER,
    output TEXT,
    created_at TEXT NOT NULL
);
"""


def log_attempt(
    log: RunLog,
    *,
    run_id: int,
    problem_id: str,
    attempt: int,
    code: str | None,
    passed: bool,
    tests_passed: int | None = None,
    tests_total: int | None = None,
    timed_out: bool | None = None,
    output: str | None = None,
) -> None:
    log.db.execute(
        """INSERT INTO attempts (run_id, problem_id, attempt, code, passed, tests_passed,
               tests_total, timed_out, output, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            problem_id,
            attempt,
            code,
            int(passed),
            tests_passed,
            tests_total,
            None if timed_out is None else int(timed_out),
            output,
            utc_now(),
        ),
    )
    log.db.commit()


class Graded(NamedTuple):
    code: str
    result: GradeResult


async def grade_reply(
    session: Session, problem: Problem, attempt: int, reply: ModelResponse
) -> Graded | None:
    """Grade and log the code in one reply. None if it had no code to grade."""
    code = extract_code(reply.text)
    if code is None:
        log_attempt(
            session.log,
            run_id=session.run_id,
            problem_id=problem.id,
            attempt=attempt,
            code=None,
            passed=False,
        )
        session.emit(
            "graded",
            attempt=attempt,
            passed=False,
            tests_passed=None,
            tests_total=None,
            timed_out=None,
            has_code=False,
        )
        print(f"   attempt {attempt}: no code block in reply ({model_note(reply)})")
        return None
    session.emit("grading", attempt=attempt)
    result = await grade(code, problem.path)
    log_attempt(
        session.log,
        run_id=session.run_id,
        problem_id=problem.id,
        attempt=attempt,
        code=code,
        passed=result.passed,
        tests_passed=result.tests_passed,
        tests_total=result.tests_total,
        timed_out=result.timed_out,
        output=result.stdout + result.stderr,
    )
    session.emit(
        "graded",
        attempt=attempt,
        passed=result.passed,
        tests_passed=result.tests_passed,
        tests_total=result.tests_total,
        timed_out=result.timed_out,
        has_code=True,
    )
    print(f"   attempt {attempt}: {roles.attempt_outcome(result)} ({model_note(reply)})")
    return Graded(code, result)


def finish(
    session: Session, *, passed: bool, attempts: int, error: str | None = None, stop_reason: str | None = None
) -> Outcome:
    """The problem's outcome: score 1.0 if the tests passed, else 0.0."""
    verdict = "PASS" if passed else "ERROR, not scored" if error else "FAIL"
    fail_reason = None if passed else f"{stop_reason or 'tests failing'} after {attempts} attempts"
    return session.outcome(
        score=1.0 if passed else 0.0,
        verdict=f"{verdict} in {attempts} attempt(s)",
        attempts=attempts,
        fail_reason=fail_reason,
        error=error,
        stop_reason=stop_reason,
    )


async def solve_solo(problem: Problem, role: RoleConfig, session: Session, max_attempts: int) -> Outcome:
    """One model writes code, sees test failures, and retries up to max_attempts."""
    messages = [{"role": "user", "content": roles.solo_task(problem.prompt)}]
    attempt = 0
    passed, error, stop_reason = False, None, None

    while attempt < max_attempts:
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

        graded = await grade_reply(session, problem, attempt, reply)
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

    return finish(session, passed=passed, attempts=attempt, error=error, stop_reason=stop_reason)


async def solve_team(
    problem: Problem, team: dict[str, RoleConfig], session: Session, max_attempts: int
) -> Outcome:
    """planner -> coder -> grader, and on failure reviewer -> orchestrator decides.

    Every call is a fresh single-message conversation. The coder and reviewer
    see the current plan and the latest graded attempt only, so local models'
    context stays small; the orchestrator also gets a one-line summary of each
    earlier attempt. The reviewer and orchestrator are skipped after the last attempt,
    since nothing could act on what they say.
    """
    prompt = problem.prompt
    attempt = 0
    passed, error, stop_reason = False, None, None

    async def ask(role_name: str, system: str, content: str) -> ModelResponse:
        return await session.call(role_name, team[role_name], system, [{"role": "user", "content": content}])

    try:
        reply = await ask("planner", roles.PLANNER_SYSTEM, roles.plan_task(prompt))
        plan = reply.text or "(no plan)"
        session.emit("plan", plan=1, text=brief(plan))
        print(f"   plan ({model_note(reply)})")
        # The last graded attempt and what was wrong with it, kept until the
        # coder produces new code (a reply without code doesn't replace them).
        last_code: str | None = None
        last_feedback: str | None = None
        no_code_note: str | None = None
        plan_number = 1
        history: list[roles.PastAttempt] = []  # what the orchestrator sees of earlier attempts

        while attempt < max_attempts:
            feedback = "\n\n".join(f for f in (no_code_note, last_feedback) if f) or None
            reply = await ask(
                "coder", roles.CODER_SYSTEM, roles.coder_task(prompt, plan, last_code, feedback)
            )
            attempt += 1
            no_code_note = None

            graded = await grade_reply(session, problem, attempt, reply)
            if graded is None:
                no_code_note = roles.no_code_feedback(reply.truncated)
                history.append(roles.PastAttempt(attempt, plan_number, roles.attempt_outcome(None), None))
                continue
            code, result = graded
            if result.passed:
                passed = True
                break
            if attempt == max_attempts:
                break

            reply = await ask(
                "reviewer", roles.REVIEWER_SYSTEM, roles.review_task(prompt, plan, code, result)
            )
            review = reply.text or "(no feedback)"
            session.emit("review", attempt=attempt, text=brief(review))
            print(f"   review ({model_note(reply)})")

            attempts_left = max_attempts - attempt
            reply = await ask(
                "orchestrator",
                roles.ORCHESTRATOR_SYSTEM,
                roles.decide_task(prompt, plan, plan_number, result, review, attempt, attempts_left, history),
            )
            decision = roles.parse_decision(reply.text)
            decided = decision is not None
            if decision is None:
                print(f"   orchestrator: no decision in reply, retrying ({model_note(reply)})")
                decision = "retry"
            else:
                print(f"   orchestrator: {decision} ({model_note(reply)})")
            history.append(
                roles.PastAttempt(attempt, plan_number, roles.attempt_outcome(result), decision, decided)
            )
            session.emit(
                "decision",
                attempt=attempt,
                decision=decision.upper(),
                parsed=decided,
                reason=brief(roles.decision_reason(reply.text)) if decided else "no decision in reply",
                attempts_left=attempts_left,
            )

            if decision == "give_up":
                stop_reason = "orchestrator gave up"
                break
            if decision == "replan":
                lead_reason = roles.decision_reason(reply.text)
                reply = await ask(
                    "planner",
                    roles.PLANNER_SYSTEM,
                    roles.replan_task(prompt, plan, code, result, review, lead_reason),
                )
                plan = reply.text or "(no plan)"
                plan_number += 1
                session.emit("replan", plan=plan_number, reason=brief(lead_reason), text=brief(plan))
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

    return finish(session, passed=passed, attempts=attempt, error=error, stop_reason=stop_reason)
