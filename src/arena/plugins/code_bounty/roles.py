"""System prompts and message templates for the code bounty roles.

All prompt text lives here so lineups can be compared on the same wording.
"""

import re
from typing import Literal, NamedTuple

from arena.plugins.code_bounty.grader import TIMEOUT_S, GradeResult

# What every role that writes solution.py is told about the file itself. Solo
# and team coders share it so both modes are graded on the same instructions.
SOLUTION_RULES = """\
Write a complete Python module that will be saved as solution.py. A hidden \
pytest suite imports it with `from solution import <function>`, so define the \
function exactly as the problem's signature specifies.

Rules:
- Python 3.12 standard library only.
- No input(), no printing, no network or file access.
- Reply with exactly one ```python code block containing the whole module. \
Keep any explanation outside the block short."""

SOLO_SYSTEM = f"""\
You are an expert Python programmer solving a coding problem.

{SOLUTION_RULES}"""


def solo_task(prompt: str) -> str:
    return f"{prompt.strip()}\n\nWrite solution.py."


NO_CODE_FEEDBACK = """\
Your reply did not contain a ```python code block, so there was nothing to \
test. Reply with the complete solution.py in one ```python block."""

TRUNCATED_FEEDBACK = """\
Your reply hit the output token limit before it finished, so there was no \
complete ```python code block to test. Think less and keep the reply short: \
reply with only the complete solution.py in one ```python block."""


def no_code_feedback(truncated: bool) -> str:
    """What to tell a model whose reply had no code block to grade."""
    return TRUNCATED_FEEDBACK if truncated else NO_CODE_FEEDBACK


def grade_report(result: GradeResult, subject: str = "The solution") -> str:
    """How an attempt did on the tests: a summary line plus pytest's output."""
    if result.timed_out:
        summary = (
            f"{subject} timed out: the tests did not finish within {TIMEOUT_S} seconds. "
            "Look for infinite loops or an algorithm that is too slow for large inputs."
        )
    else:
        summary = f"{subject} passed {result.tests_passed}/{result.tests_total} tests."

    parts = [summary]
    if result.stdout.strip():
        parts.append(f"pytest output:\n```\n{result.stdout.strip()}\n```")
    if result.stderr.strip():
        parts.append(f"stderr:\n```\n{result.stderr.strip()}\n```")
    return "\n\n".join(parts)


def grade_feedback(result: GradeResult) -> str:
    """Tell the solo model how its last attempt did on the tests."""
    return (
        f"{grade_report(result, 'Your solution')}\n\n"
        "Fix the problem and reply with the complete corrected solution.py in one ```python block."
    )


# --- team mode ---------------------------------------------------------------
#
# planner -> coder -> grader -> (on failure) reviewer -> orchestrator decides.
# Each prompt carries everything the role needs, so every call is a fresh
# one-message conversation and local models' context stays small.

PLANNER_SYSTEM = """\
You are the planner on a small team solving a Python coding problem. A coder \
will write the code from your plan, and a hidden pytest suite will grade it.

Write a short plan: the algorithm, the data structures, and every edge case \
the problem statement mentions. At most 12 lines. Do not write the code."""


def plan_task(prompt: str) -> str:
    return f"{prompt.strip()}\n\nWrite the plan."


def replan_task(prompt: str, plan: str, code: str, result: GradeResult, review: str, lead_reason: str) -> str:
    return (
        f"{prompt.strip()}\n\n"
        f"Your previous plan:\n{plan.strip()}\n\n"
        f"The coder followed it and the tests failed.\n\n"
        f"Code:\n```python\n{code.strip()}\n```\n\n"
        f"{grade_report(result)}\n\n"
        f"Reviewer's feedback:\n{review.strip()}\n\n"
        f"The team lead decided the plan needs to change:\n{lead_reason.strip()}\n\n"
        "Write a new plan."
    )


CODER_SYSTEM = f"""\
You are the coder on a small team solving a Python coding problem. A planner \
has written the approach; implement it. If you spot a bug in the plan, fix it \
in the code.

{SOLUTION_RULES}"""


def coder_task(prompt: str, plan: str, last_code: str | None = None, last_feedback: str | None = None) -> str:
    """The problem and plan, plus the previous attempt and what was wrong with it."""
    parts = [prompt.strip(), f"Plan:\n{plan.strip()}"]
    if last_feedback is not None:
        if last_code is not None:
            parts.append(f"Your previous attempt:\n```python\n{last_code.strip()}\n```")
        parts.append(f"What went wrong:\n{last_feedback.strip()}")
        parts.append("Write the complete corrected solution.py.")
    else:
        parts.append("Write solution.py.")
    return "\n\n".join(parts)


REVIEWER_SYSTEM = """\
You are the code reviewer on a small team solving a Python coding problem. The \
coder's solution failed the hidden tests. Read the problem, the plan, the code \
and the pytest output, and work out why it fails.

Reply with short, specific feedback for the coder: what is wrong and how to \
fix it, pointing at the lines or input cases involved. Say so if the plan \
itself is wrong. Do not rewrite the whole solution."""


def review_task(prompt: str, plan: str, code: str, result: GradeResult) -> str:
    return (
        f"{prompt.strip()}\n\n"
        f"Plan:\n{plan.strip()}\n\n"
        f"Code:\n```python\n{code.strip()}\n```\n\n"
        f"{grade_report(result)}\n\n"
        "Why does it fail, and how should the coder fix it?"
    )


ORCHESTRATOR_SYSTEM = """\
You lead a small team solving a Python coding problem. A planner writes the \
approach, a coder writes the code, a hidden pytest suite grades it, and a \
reviewer explains failures.

The latest attempt failed. You are shown every earlier attempt and what you \
decided after it. Decide what happens next:
- RETRY: the plan is sound and the coder can fix the code using the review. \
The coder keeps following the current plan, so only retry if the fix fits \
within it.
- REPLAN: the planner writes a new plan. Choose this when the approach is \
wrong or too slow, when the fix needs a different approach than the plan \
describes, or when the coder has failed more than once with this plan and a \
simpler approach would be easier to get right.
- GIVE_UP: more attempts will not solve it.

Reply with one line `DECISION: RETRY`, `DECISION: REPLAN` or \
`DECISION: GIVE_UP`, then one sentence explaining why."""

Decision = Literal["retry", "replan", "give_up"]


class PastAttempt(NamedTuple):
    """An earlier attempt, as the orchestrator sees it when deciding."""

    attempt: int
    plan: int  # 1 for the first plan, 2 after one replan, ...
    outcome: str  # e.g. "3/13 tests passed"
    decision: Decision | None  # None when the coder was simply asked again (no code)


def attempt_outcome(result: GradeResult | None) -> str:
    if result is None:
        return "the reply had no code block"
    if result.timed_out:
        return "timed out"
    return f"{result.tests_passed}/{result.tests_total} tests passed"


def decide_task(
    prompt: str,
    plan: str,
    plan_number: int,
    result: GradeResult,
    review: str,
    attempt: int,
    attempts_left: int,
    history: list[PastAttempt],
) -> str:
    parts = [prompt.strip(), f"Current plan (plan {plan_number}):\n{plan.strip()}"]
    if history:
        lines = []
        for past in history:
            line = f"- Attempt {past.attempt} (plan {past.plan}): {past.outcome}."
            if past.decision is not None:
                line += f" You chose {past.decision.upper()}."
            lines.append(line)
        parts.append("Earlier attempts:\n" + "\n".join(lines))
    parts += [
        f"Attempt {attempt} (plan {plan_number}) failed. {attempts_left} attempt(s) left.",
        grade_report(result),
        f"Reviewer's feedback:\n{review.strip()}",
        "RETRY, REPLAN or GIVE_UP?",
    ]
    return "\n\n".join(parts)


_DECISION_LINE = re.compile(r"DECISION:\s*\**\s*(RETRY|REPLAN|GIVE[ _-]?UP)", re.IGNORECASE)
_DECISION_WORD = re.compile(r"\b(RETRY|REPLAN|GIVE[ _-]?UP)\b")


def parse_decision(reply: str) -> Decision | None:
    """The orchestrator's choice, or None if the reply doesn't contain one.

    Takes the `DECISION:` line if present, else the first upper-case keyword.
    """
    m = _DECISION_LINE.search(reply) or _DECISION_WORD.search(reply)
    if m is None:
        return None
    word = m.group(1).upper()
    if word.startswith("GIVE"):
        return "give_up"
    return "retry" if word == "RETRY" else "replan"
