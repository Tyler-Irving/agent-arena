"""The code bounty plugin: coding problems, solved solo or by a team, graded in a sandbox."""

from arena.core.lineup import Lineup
from arena.core.plugin import Outcome, Session, Wording
from arena.plugins.code_bounty.loop import ATTEMPTS_SCHEMA, solve_solo, solve_team
from arena.plugins.code_bounty.problems import Problem, load_problems


class CodeBounty:
    name = "code_bounty"
    modes: dict[str, set[str]] = {
        "solo": {"solo"},
        "team": {"orchestrator", "planner", "coder", "reviewer"},
    }
    max_calls = 20  # model calls per problem, whatever the lineup
    uses_attempts = True
    wording = Wording(
        task="problem", tasks="problems", passed="passed", rate="pass", summary="{passed}/{scored} passed"
    )
    schema = ATTEMPTS_SCHEMA

    def load_tasks(self, selector: str | None) -> list[Problem]:
        return load_problems(selector)

    def validate_lineup(self, lineup: Lineup) -> None:
        if lineup.settings:
            got = sorted(lineup.settings)
            raise ValueError(f"lineup {lineup.name!r}: code_bounty takes no settings, got {got}")
        # A lineup must be able to use every attempt it asks for. Solo: one call
        # per attempt. Team: the plan, a coder call per attempt, and between
        # attempts a review, a decision and possibly a replan: 1 + n + 3(n - 1).
        n = lineup.max_attempts
        worst = n if lineup.mode == "solo" else 4 * n - 2
        if worst > self.max_calls:
            raise ValueError(
                f"lineup {lineup.name!r}: max_attempts {n} can need {worst} calls, "
                f"over the {self.max_calls}-call limit"
            )

    async def run_task(self, task: Problem, lineup: Lineup, session: Session) -> Outcome:
        if lineup.mode == "solo":
            return await solve_solo(task, lineup.roles["solo"], session, lineup.max_attempts)
        return await solve_team(task, lineup.roles, session, lineup.max_attempts)


PLUGIN = CodeBounty()
