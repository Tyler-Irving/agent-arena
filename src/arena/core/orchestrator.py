"""Run a plugin's tasks through a lineup: the run, the task board, and the results.

Everything about solving a task lives in the plugin's run_task. This loop
only does the bookkeeping around it: it records the run, puts the tasks on the
board, hands each one to the plugin with a fresh Session, logs the result, and
leaves the run in a clean state if it is stopped or a plugin's infrastructure
breaks.
"""

import asyncio
import os
import time
from typing import Any

from arena.core.events import EVENTS_VERSION, boot_id
from arena.core.lineup import Lineup, check_roles
from arena.core.plugin import ModelCallFailed, Outcome, Plugin, PluginError, Session, TaskSpec
from arena.core.runlog import RunLog, set_runlog
from arena.core.taskboard import Task, TaskBoard


async def run_lineup[T: TaskSpec](
    plugin: Plugin[T],
    lineup: Lineup,
    tasks: list[T],
    board: TaskBoard,
    log: RunLog,
    selector: str | None = None,
) -> tuple[int, list[Outcome]]:
    """Run every task through the lineup. Returns (run_id, outcomes).

    `selector` is the --problem the tasks were picked with, recorded on the run.
    """
    # The plugin's own rules (validate_lineup) are checked when lineups load.
    check_roles(lineup, plugin)
    set_runlog(log)  # the router logs calls to the same database as the run
    log.db.executescript(plugin.schema)
    run_id = log.start_run(lineup.name, plugin.name, selector, lineup.max_attempts, lineup.settings)
    by_id = {t.id: t for t in tasks}
    for t in tasks:
        await board.put(run_id, t.id)

    outcomes: list[Outcome] = []
    claimed: Task | None = None  # the task that is "running" on the board, if any
    session: Session | None = None  # its session, for the task_finished event if it is cut short
    interrupted = False

    def event(kind: str, task_id: str | None = None, **data: Any) -> None:
        log.log_event(run_id, task_id, kind, data)

    def cut_short(status: str, reason: str) -> None:
        """task_finished for a task that ended without an outcome."""
        assert session is not None
        event(
            "task_finished",
            session.task_id,
            status=status,
            verdict=None,
            score=None,
            reason=reason,
            calls=session.calls,
            cost_usd=session.cost,
            wall_time_ms=int((time.perf_counter() - session.start) * 1000),
        )

    # Logged right before the try, so the finally's run_finished always pairs with it.
    event(
        "run_started",
        events_version=EVENTS_VERSION,
        pid=os.getpid(),  # so a viewer can tell a killed run from a slow one
        boot=boot_id(),
        lineup=lineup.name,
        plugin=plugin.name,
        mode=lineup.mode,
        roles={role: cfg.model for role, cfg in lineup.roles.items()},
        tasks=[t.id for t in tasks],
        selector=selector,
        max_attempts=lineup.max_attempts,
        settings=lineup.settings,
        max_calls=plugin.max_calls,
    )
    try:
        # Sequential on purpose: only one local model fits in VRAM at a time.
        while (claimed := await board.next(run_id)) is not None:
            board_task = claimed
            task = by_id[board_task.problem_id]
            print(f"== {task.id}")
            session = Session(task.id, run_id, log, plugin.max_calls)
            event("task_started", task.id, index=tasks.index(task) + 1, total=len(tasks))
            try:
                outcome = await plugin.run_task(task, lineup, session)
            except ModelCallFailed as e:
                # The plugin didn't handle a provider error itself: the task is
                # unscored, like any other provider error.
                print(f"   model call failed: {e}")
                outcome = session.outcome(score=0.0, verdict="ERROR, not scored", error=str(e))
            except PluginError as e:
                # E.g. the grader sandbox broke, so nothing after this can be scored either.
                await board.fail(board_task.id, f"{e.what} error: {e}")
                claimed = None
                cut_short("failed", f"{e.what} error: {e}")
                session = None
                raise
            outcomes.append(outcome)

            if outcome.error is None:
                log.log_result(
                    run_id=run_id,
                    problem_id=task.id,
                    score=outcome.score,
                    attempts=outcome.attempts,
                    total_calls=outcome.calls,
                    total_cost_usd=outcome.cost_usd,
                    wall_time_ms=outcome.wall_time_ms,
                    metrics=outcome.metrics,
                )
            reason = outcome.error or outcome.fail_reason
            if reason is None:
                await board.complete(board_task.id)
            else:
                await board.fail(board_task.id, reason)
            claimed = None
            session = None
            event(
                "task_finished",
                task.id,
                status="error" if outcome.error else "failed" if reason else "done",
                verdict=outcome.verdict,
                score=None if outcome.error else outcome.score,
                reason=reason,
                calls=outcome.calls,
                cost_usd=outcome.cost_usd,
                wall_time_ms=outcome.wall_time_ms,
            )

            print(
                f"   {outcome.verdict}, {outcome.calls} call(s), "
                f"{format_cost(outcome.cost_usd)}, {outcome.wall_time_ms / 1000:.1f}s\n"
            )
    except asyncio.CancelledError:
        # Ctrl-C or SIGTERM, at any await. The half-done task gets no
        # result row, and its board entry shouldn't be left "running" forever.
        interrupted = True
        if session is not None:  # before the await, which a second stop could cut short
            cut_short("interrupted", "interrupted")
        if claimed is not None:
            await board.fail(claimed.id, "interrupted")
        raise
    except Exception as e:
        # A broken sandbox, or a bug. The run stopped short, so it mustn't
        # count as a full run, and no task should be left "running".
        interrupted = True
        if session is not None:  # before the await, which Ctrl-C could cut short
            cut_short("error", f"{type(e).__name__}: {e}")
        if claimed is not None:
            await board.fail(claimed.id, f"{type(e).__name__}: {e}")
        raise
    finally:
        log.finish_run(run_id, interrupted=interrupted)
        event("run_finished", interrupted=interrupted)
    return run_id, outcomes


def format_cost(cost: float | None) -> str:
    return "cost unknown" if cost is None else f"${cost:.4f}"
