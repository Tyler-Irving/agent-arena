"""The contract between core and a plugin.

A plugin is a kind of task (code bounties, a game, ...). Core runs the
bookkeeping: the run, the task board, the call limit, cost tracking, the run log
and Ctrl-C handling. The plugin owns everything about its tasks: loading them,
which modes and roles a lineup needs, and the whole loop of model calls for one
task, which it makes through the Session it is handed.
"""

import asyncio
import time
from typing import Any, Protocol

from pydantic import BaseModel, Field

from arena.core.lineup import Lineup, RoleConfig
from arena.core.runlog import RunLog
from arena.models.router import ModelResponse, complete


class TaskSpec(Protocol):
    """One unit of work, e.g. a coding problem or a whole game."""

    @property
    def id(self) -> str: ...


class Wording(BaseModel):
    """How the CLI and the scoreboard talk about a plugin's tasks and scores."""

    task: str  # one task: "3 problem(s)", "no problems match", "per-problem averages"
    passed: str  # the scoreboard column that sums the scores, e.g. "passed"
    rate: str  # the score rate: its scoreboard column, and "<rate> rate" in the notes, e.g. "pass"
    summary: str  # a run's score in its summary line, from {passed} and {scored}: "{passed}/{scored} passed"


class Plugin[T: TaskSpec](Protocol):
    name: str
    # mode -> roles a lineup in that mode must define
    modes: dict[str, set[str]]
    # model calls per task, whatever the lineup
    max_calls: int
    # whether a lineup's max_attempts means anything here; the attempt limit is only shown if so
    uses_attempts: bool
    wording: Wording
    # SQL for the plugin's own tables (CREATE TABLE IF NOT EXISTS), run at the start of every run
    schema: str

    def load_tasks(self, selector: str | None) -> list[T]:
        """Every task in order, or those matching `selector` (the CLI's --problem)."""
        ...

    def validate_lineup(self, lineup: Lineup) -> None:
        """Raise ValueError for lineups this plugin can't run, beyond the mode and role checks."""
        ...

    async def run_task(self, task: T, lineup: Lineup, session: "Session") -> "Outcome": ...


class PluginError(Exception):
    """Infrastructure a plugin depends on broke (e.g. the grader sandbox), so the run can't go on."""

    what = "plugin"  # names the broken piece in messages: "<what> error: ...", "<what> unavailable"


class Outcome(BaseModel):
    task_id: str
    score: float = Field(ge=0.0, le=1.0)
    attempts: int = 1  # tries the lineup got at the task; 1 for plugins that don't retry
    calls: int
    cost_usd: float | None  # None if any call used a model with no known price
    wall_time_ms: int
    # A provider error (outage, auth, rate limit). The task isn't scored,
    # since the failure says nothing about the lineup.
    error: str | None = None
    # Why a scored task stopped early, if it did.
    stop_reason: str | None = None
    # Printed before the call count, e.g. "PASS in 2 attempt(s)".
    verdict: str
    # The task board marks the task failed with this reason (or with `error`); None marks it done.
    fail_reason: str | None = None
    # Anything else the plugin wants kept with the result, stored as JSON.
    metrics: dict[str, Any] = {}


class ModelCallFailed(Exception):
    """The provider returned an error; the router has already logged it."""


class CallLimitReached(Exception):
    """The task used up the plugin's max_calls."""


class Session:
    """One task's model calls and cost, under the plugin's call limit."""

    def __init__(self, task_id: str, run_id: int, log: RunLog, max_calls: int) -> None:
        self.task_id = task_id
        self.run_id = run_id
        self.log = log
        self.max_calls = max_calls
        self.calls = 0
        self.cost: float | None = 0.0  # None once any call has no known price
        self.start = time.perf_counter()

    async def call(
        self, role_name: str, role: RoleConfig, system: str, messages: list[dict]
    ) -> ModelResponse:
        """One model call. `role_name` tags the run log, so it can name a player, not just a role."""
        if self.calls >= self.max_calls:
            raise CallLimitReached(f"hit the {self.max_calls}-call limit")
        self.calls += 1
        n = self.calls

        def event(kind: str, data: dict[str, Any]) -> None:
            self.log.log_event(self.run_id, self.task_id, kind, {"n": n, **data}, role_name, role.model)

        def failed(error: str) -> None:
            event("call_finished", {"latency_ms": int((time.perf_counter() - start) * 1000), "error": error})

        event("call_started", {"max_calls": self.max_calls})
        start = time.perf_counter()

        # Events are logged outside the try body, so a logging failure can't be
        # mistaken for a provider error.
        try:
            reply = await complete(
                role.model,
                system,
                messages,
                think=role.think,
                run_id=self.run_id,
                problem_id=self.task_id,
                role=role_name,
            )
        except asyncio.CancelledError:
            failed("interrupted")
            raise
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            failed(error)
            raise ModelCallFailed(error) from e
        event(
            "call_finished",
            {
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
                "reasoning_tokens": reply.reasoning_tokens,
                "latency_ms": reply.latency_ms,
                "cost_usd": reply.cost_usd,
                "truncated": reply.truncated,
                "error": None,
            },
        )
        self.cost = None if self.cost is None or reply.cost_usd is None else self.cost + reply.cost_usd
        return reply

    def emit(self, kind: str, /, **data: Any) -> None:
        """A plugin event for the viewer (arena watch), e.g. emit("graded", attempt=2, passed=False).

        The data must be JSON-friendly. Never put a reply in it that some
        player isn't meant to see yet: the viewer shows every event.
        """
        self.log.log_event(self.run_id, self.task_id, kind, data)

    def outcome(
        self,
        *,
        score: float,
        verdict: str,
        attempts: int = 1,
        fail_reason: str | None = None,
        error: str | None = None,
        stop_reason: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> Outcome:
        return Outcome(
            task_id=self.task_id,
            score=score,
            attempts=attempts,
            calls=self.calls,
            cost_usd=self.cost,
            wall_time_ms=int((time.perf_counter() - self.start) * 1000),
            error=error,
            stop_reason=stop_reason,
            verdict=verdict,
            fail_reason=fail_reason,
            metrics=metrics or {},
        )


def model_note(reply: ModelResponse) -> str:
    note = f"model {reply.latency_ms / 1000:.1f}s, {reply.output_tokens} out tokens"
    return f"{note}, cut off at the token limit" if reply.truncated else note
