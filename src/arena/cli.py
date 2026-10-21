"""arena command line entry point."""

import argparse
import asyncio
import signal
import sys
from collections.abc import Coroutine
from typing import Any

from dotenv import load_dotenv

from arena.core.lineup import get_lineup
from arena.core.orchestrator import format_cost, run_lineup
from arena.core.plugin import Outcome, PluginError
from arena.core.runlog import get_runlog
from arena.core.scoreboard import format_scoreboard, scoreboard
from arena.core.taskboard import SqliteTaskBoard
from arena.models.router import complete
from arena.plugins import DEFAULT_PLUGIN, PLUGINS, get_plugin

SMOKE_MODELS = ["anthropic/claude-haiku-5-5", "ollama/qwen2.5-coder:14b"]
SMOKE_SYSTEM = "You are a terse assistant."
SMOKE_PROMPT = "Write a one-line Python expression that reverses the string s. Reply with only the code."


async def smoke(models: list[str]) -> int:
    log = get_runlog()
    run_id = log.start_run(lineup="smoke", plugin="smoke")
    failures = 0

    # Sequential on purpose: only one Ollama model fits in VRAM at a time.
    try:
        for model in models:
            print(f"== {model}")
            try:
                r = await complete(
                    model,
                    SMOKE_SYSTEM,
                    [{"role": "user", "content": SMOKE_PROMPT}],
                    run_id=run_id,
                    role="smoke",
                )
            except Exception as e:
                failures += 1
                print(f"   FAILED   {type(e).__name__}: {e}\n")
                continue
            print("   text:    " + r.text.replace("\n", "\n            "))
            print(f"   tokens:  in={r.input_tokens} out={r.output_tokens} reasoning={r.reasoning_tokens}")
            print(f"   latency: {r.latency_ms} ms\n")
    except asyncio.CancelledError:
        log.finish_run(run_id, interrupted=True)
        raise

    log.finish_run(run_id)
    print(f"{len(models) - failures}/{len(models)} models OK (smoke run {run_id})")
    return 1 if failures else 0


async def run(lineup_name: str, problem: str | None, repeat: int) -> int:
    try:
        lineup = get_lineup(lineup_name, PLUGINS, DEFAULT_PLUGIN)
    except KeyError as e:
        print(e.args[0], file=sys.stderr)
        return 2
    plugin = get_plugin(lineup.plugin)  # load_lineups already rejected unknown plugins
    problems = plugin.load_tasks(problem)
    if not problems:
        print(f"no problems match {problem!r}", file=sys.stderr)
        return 2

    roles = ", ".join(f"{role}={cfg.model}" for role, cfg in lineup.roles.items())
    times = f" x {repeat} runs" if repeat > 1 else ""
    print(
        f"lineup {lineup.name} ({lineup.mode}: {roles}; max {lineup.max_attempts} attempts), "
        f"{len(problems)} problem(s){times}\n"
    )

    board = SqliteTaskBoard()
    summaries: list[tuple[int, list[Outcome]]] = []
    try:
        for _ in range(repeat):
            try:
                run_id, outcomes = await run_lineup(plugin, lineup, problems, board, get_runlog(), problem)
            except PluginError as e:
                print(f"{e.what} unavailable, run aborted: {e}", file=sys.stderr)
                return 2
            summaries.append((run_id, outcomes))
            print(summarize(run_id, outcomes) + "\n")
    finally:
        # Also when stopped early, so the finished runs aren't lost in the scrollback.
        if repeat > 1 and summaries:
            first, last = summaries[0][0], summaries[-1][0]
            done = f"{repeat}" if len(summaries) == repeat else f"{len(summaries)} of {repeat}"
            print(f"{done} runs finished ({first}-{last}):")
            for run_id, outcomes in summaries:
                print("  " + summarize(run_id, outcomes))
            print(f"compare with earlier runs: arena scoreboard --since {first}")
    return 0


def summarize(run_id: int, outcomes: list[Outcome]) -> str:
    passed = sum(o.score for o in outcomes)
    scored = sum(o.error is None for o in outcomes)
    errors = f" ({len(outcomes) - scored} not scored: model errors)" if scored < len(outcomes) else ""
    costs = [o.cost_usd for o in outcomes]
    total_cost = None if None in costs else sum(c for c in costs if c is not None)
    return (
        f"run {run_id}: {passed:g}/{scored} passed{errors}, "
        f"{sum(o.calls for o in outcomes)} calls, {format_cost(total_cost)}, "
        f"{sum(o.wall_time_ms for o in outcomes) / 1000:.1f}s"
    )


def run_stoppable(command: Coroutine[Any, Any, int]) -> int:
    """asyncio.run(command), stopping it cleanly on Ctrl-C or SIGTERM.

    The first signal cancels the command, so runs are closed and any sandbox
    container is killed. Later signals are ignored: cancelling that cleanup
    could leave a container running model-written code. Returns 128 + signal
    number when stopped, the shell convention.
    """
    received: list[signal.Signals] = []

    async def main() -> int:
        task = asyncio.current_task()
        assert task is not None

        def on_signal(sig: signal.Signals) -> None:
            if received:
                print("already stopping, waiting for cleanup", file=sys.stderr)
                return
            received.append(sig)
            task.cancel()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, on_signal, sig)
        return await command

    try:
        return asyncio.run(main())
    except asyncio.CancelledError:
        if not received:
            raise
        print("\ninterrupted; anything unfinished is not scored", file=sys.stderr)
        return 128 + received[0]


def show_scoreboard(plugin: str, since: int | None) -> int:
    if plugin not in PLUGINS:
        print(f"unknown plugin {plugin!r}; choose from: {', '.join(PLUGINS)}", file=sys.stderr)
        return 2
    rows = scoreboard(get_runlog(), plugin, since=since)
    if not rows:
        hint = f" from run {since} on" if since else " yet; try `arena run --lineup haiku-solo`"
        print(f"no runs{hint}")
        return 0
    if since:
        print(f"runs {since} and later\n")
    print(format_scoreboard(rows))
    return 0


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="arena")
    sub = parser.add_subparsers(dest="command", required=True)

    p_smoke = sub.add_parser("smoke", help="one call to each model")
    p_smoke.add_argument(
        "--model",
        action="append",
        dest="models",
        help=f"provider/model to call, repeatable (default: {', '.join(SMOKE_MODELS)})",
    )

    p_run = sub.add_parser("run", help="run problems with a lineup")
    p_run.add_argument("--lineup", required=True, help="lineup name from config/lineups.yaml")
    p_run.add_argument("--problem", help='one problem, e.g. "001" (default: all)')
    p_run.add_argument(
        "--repeat", type=positive_int, default=1, help="run the problems this many times (default: 1)"
    )

    p_score = sub.add_parser("scoreboard", help="compare lineups across all runs")
    p_score.add_argument(
        "--since", type=positive_int, metavar="RUN_ID", help="only count this run and later ones"
    )
    p_score.add_argument(
        "--plugin", default=DEFAULT_PLUGIN, help=f"whose runs to compare (default: {DEFAULT_PLUGIN})"
    )

    args = parser.parse_args()
    if args.command == "smoke":
        sys.exit(run_stoppable(smoke(args.models or SMOKE_MODELS)))
    elif args.command == "run":
        sys.exit(run_stoppable(run(args.lineup, args.problem, args.repeat)))
    elif args.command == "scoreboard":
        sys.exit(show_scoreboard(args.plugin, args.since))


def positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {n}")
    return n
