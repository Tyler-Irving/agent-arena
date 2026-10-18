"""System prompts and message templates for the code bounty roles.

All prompt text lives here so lineups can be compared on the same wording.
"""

from arena.plugins.code_bounty.grader import TIMEOUT_S, GradeResult

SOLO_SYSTEM = """\
You are an expert Python programmer solving a coding problem.

Write a complete Python module that will be saved as solution.py. A hidden \
pytest suite imports it with `from solution import <function>`, so define the \
function exactly as the problem's signature specifies.

Rules:
- Python 3.12 standard library only.
- No input(), no printing, no network or file access.
- Reply with exactly one ```python code block containing the whole module. \
Keep any explanation outside the block short."""


def solo_task(prompt: str) -> str:
    return f"{prompt.strip()}\n\nWrite solution.py."


NO_CODE_FEEDBACK = """\
Your reply did not contain a ```python code block, so there was nothing to \
test. Reply with the complete solution.py in one ```python block."""


def grade_feedback(result: GradeResult) -> str:
    """Tell the model how its last attempt did on the tests."""
    if result.timed_out:
        summary = (
            f"Your solution timed out: the tests did not finish within {TIMEOUT_S} seconds. "
            "Look for infinite loops or an algorithm that is too slow for large inputs."
        )
    else:
        summary = f"Your solution passed {result.tests_passed}/{result.tests_total} tests."

    parts = [summary]
    if result.stdout.strip():
        parts.append(f"pytest output:\n```\n{result.stdout.strip()}\n```")
    if result.stderr.strip():
        parts.append(f"stderr:\n```\n{result.stderr.strip()}\n```")
    parts.append("Fix the problem and reply with the complete corrected solution.py in one ```python block.")
    return "\n\n".join(parts)
