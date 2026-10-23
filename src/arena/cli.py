"""arena command line entry point."""

import argparse
import asyncio
import signal
import sys
from collections.abc import Coroutine
from typing import Any

from dotenv import load_dotenv

from arena.core.lineup import get_lineup, load_lineups
from arena.core.orchestrator import format_cost, run_lineup
from arena.core.plugin import Outcome, PluginError, Reports, Wording
from arena.core.runlog import DEFAULT_DB, get_runlog
from arena.core.scoreboard import format_scoreboard, scoreboard
from arena.core.taskboard import SqliteTaskBoard
from arena.models.router import complete
from arena.plugins import DEFAULT_PLUGIN, PLUGINS, get_plugin

SMOKE_MODELS = ["anthropic/claude-haiku-5-5", "ollama/qwen2.5-coder:14b"]
SMOKE_SYSTEM = "You are a terse assistant."
HIDE_ROLES_HELP = "mafia: hide each role until that player is out or the game ends"
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


async def run(
    lineup_name: str, problem: str | None, repeat: int, watch: bool = False, hide_roles: bool = False
) -> int:
    try:
        lineup = get_lineup(lineup_name, PLUGINS, DEFAULT_PLUGIN)
    except KeyError as e:
        print(e.args[0], file=sys.stderr)
        return 2
    except ValueError as e:  # a lineup in the file doesn't suit its plugin, or is malformed
        print(f"invalid lineup config: {e}", file=sys.stderr)
        return 2
    plugin = get_plugin(lineup.plugin)  # load_lineups already rejected unknown plugins
    words = plugin.wording
    problems = plugin.load_tasks(problem)
    if not problems:
        print(f"no {words.tasks} match {problem!r}", file=sys.stderr)
        return 2

    roles = ", ".join(f"{role}={cfg.model}" for role, cfg in lineup.roles.items())
    limit = f"; max {lineup.max_attempts} attempts" if plugin.uses_attempts else ""
    times = f" x {repeat} runs" if repeat > 1 else ""
    print(f"lineup {lineup.name} ({lineup.mode}: {roles}{limit}), {len(problems)} {words.task}(s){times}\n")

    board = SqliteTaskBoard()
    log = get_runlog()

    async def runs() -> int:
        summaries: list[tuple[int, list[Outcome]]] = []
        try:
            for _ in range(repeat):
                try:
                    run_id, outcomes = await run_lineup(plugin, lineup, problems, board, log, problem)
                except PluginError as e:
                    print(f"{e.what} unavailable, run aborted: {e}", file=sys.stderr)
                    return 2
                summaries.append((run_id, outcomes))
                print(summarize(run_id, outcomes, words) + "\n")
        finally:
            # Also when stopped early, so the finished runs aren't lost in the scrollback.
            if repeat > 1 and summaries:
                first, last = summaries[0][0], summaries[-1][0]
                done = f"{repeat}" if len(summaries) == repeat else f"{len(summaries)} of {repeat}"
                print(f"{done} runs finished ({first}-{last}):")
                for run_id, outcomes in summaries:
                    print("  " + summarize(run_id, outcomes, words))
                print(f"compare with earlier runs: arena scoreboard --since {first}")
        return 0

    if not watch:
        return await runs()
    from arena.watch.app import watch_run  # only --watch loads the viewer (and Rich)
    from arena.watch.views import ViewOptions

    return await watch_run(runs(), log, ViewOptions(hide_roles=hide_roles))


def summarize(run_id: int, outcomes: list[Outcome], words: Wording) -> str:
    passed = sum(o.score for o in outcomes)
    scored = sum(o.error is None for o in outcomes)
    errors = f" ({len(outcomes) - scored} not scored: model errors)" if scored < len(outcomes) else ""
    costs = [o.cost_usd for o in outcomes]
    total_cost = None if None in costs else sum(c for c in costs if c is not None)
    return (
        f"run {run_id}: {words.summary.format(passed=f'{passed:g}', scored=scored)}{errors}, "
        f"{sum(o.calls for o in outcomes)} calls, {format_cost(total_cost)}, "
        f"{sum(o.wall_time_ms for o in outcomes) / 1000:.1f}s"
    )


def run_stoppable(
    command: Coroutine[Any, Any, int], stopped: str | None = "interrupted; anything unfinished is not scored"
) -> int:
    """asyncio.run(command), stopping it cleanly on Ctrl-C or SIGTERM.

    The first signal cancels the command, so runs are closed and any sandbox
    container is killed. Later signals are ignored: cancelling that cleanup
    could leave a container running model-written code. Returns 128 + signal
    number when stopped, the shell convention, after printing `stopped` (if any).
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
        if stopped:
            print(f"\n{stopped}", file=sys.stderr)
        return 128 + received[0]


def show_scoreboard(plugin_name: str, since: int | None) -> int:
    try:
        plugin = get_plugin(plugin_name)
    except KeyError as e:
        print(e.args[0], file=sys.stderr)
        return 2
    rows = scoreboard(get_runlog(), plugin, since=since)
    if not rows:
        if since:
            print(f"no runs from run {since} on")
        else:
            example = first_lineup(plugin_name)
            print("no runs yet" + (f"; try `arena run --lineup {example}`" if example else ""))
        return 0
    if since:
        print(f"runs {since} and later\n")
    print(format_scoreboard(rows, plugin))
    return 0


def first_lineup(plugin_name: str) -> str | None:
    """The first lineup for the plugin in config/lineups.yaml, if the file loads and has one."""
    try:
        lineups = load_lineups(PLUGINS, DEFAULT_PLUGIN)
    except Exception:  # missing, unparseable or invalid: the hint is only a nicety, so go without
        return None
    return next((name for name, lineup in lineups.items() if lineup.plugin == plugin_name), None)


def show_report(plugin_name: str, since: int | None) -> int:
    try:
        plugin = get_plugin(plugin_name)
    except KeyError as e:
        print(e.args[0], file=sys.stderr)
        return 2
    if not isinstance(plugin, Reports):
        have = [name for name, p in PLUGINS.items() if isinstance(p, Reports)]
        print(f"{plugin_name} has no report; plugins with one: {', '.join(have)}", file=sys.stderr)
        return 2
    print(plugin.report(get_runlog(), since))
    return 0


def watch(run_id: int | None, hide_roles: bool) -> int:
    from arena.watch.app import attach
    from arena.watch.views import ViewOptions

    # Stoppable, so the live view is closed (and the cursor shown again) on Ctrl-C or SIGTERM.
    # Only the viewer stops; the run it watches goes on.
    return run_stoppable(attach(DEFAULT_DB, run_id, ViewOptions(hide_roles=hide_roles)), stopped=None)


def replay(run_id: int, speed: float, hide_roles: bool) -> int:
    from arena.watch.app import replay as play_back
    from arena.watch.views import ViewOptions

    options = ViewOptions(hide_roles=hide_roles)
    return run_stoppable(play_back(DEFAULT_DB, run_id, speed, options), stopped=None)


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

    p_run = sub.add_parser("run", help="run a lineup on its plugin's tasks")
    p_run.add_argument("--lineup", required=True, help="lineup name from config/lineups.yaml")
    p_run.add_argument("--problem", "--game", help='one problem or game, e.g. "001" or "03" (default: all)')
    p_run.add_argument(
        "--repeat", type=positive_int, default=1, help="run them all this many times (default: 1)"
    )
    p_run.add_argument("--watch", action="store_true", help="show a live view of the run")
    p_run.add_argument("--hide-roles", action="store_true", help=HIDE_ROLES_HELP)

    p_watch = sub.add_parser("watch", help="watch a run from another terminal")
    p_watch.add_argument(
        "run_id",
        nargs="?",
        type=positive_int,
        metavar="RUN_ID",
        help="default: the latest run if it is still going, else the next one to start",
    )
    p_watch.add_argument("--hide-roles", action="store_true", help=HIDE_ROLES_HELP)

    p_replay = sub.add_parser("replay", help="replay a finished run")
    p_replay.add_argument("run_id", type=positive_int, metavar="RUN_ID")
    p_replay.add_argument(
        "--speed",
        type=non_negative_float,
        default=1.0,
        help="play back N times as fast; 0 shows only the final state (default: 1)",
    )
    p_replay.add_argument("--hide-roles", action="store_true", help=HIDE_ROLES_HELP)

    p_score = sub.add_parser("scoreboard", help="compare lineups across all runs")
    p_score.add_argument(
        "--since", type=positive_int, metavar="RUN_ID", help="only count this run and later ones"
    )
    p_score.add_argument(
        "--plugin", default=DEFAULT_PLUGIN, help=f"whose runs to compare (default: {DEFAULT_PLUGIN})"
    )

    p_report = sub.add_parser("report", help="a plugin's own report, e.g. Mafia win rates by model and side")
    p_report.add_argument(
        "--since", type=positive_int, metavar="RUN_ID", help="only count this run and later ones"
    )
    p_report.add_argument(
        "--plugin", default=DEFAULT_PLUGIN, help=f"whose runs to report on (default: {DEFAULT_PLUGIN})"
    )

    args = parser.parse_args()
    if args.command == "run" and args.hide_roles and not args.watch:
        p_run.error("--hide-roles only changes the --watch view; add --watch")
    if args.command == "smoke":
        sys.exit(run_stoppable(smoke(args.models or SMOKE_MODELS)))
    elif args.command == "run":
        sys.exit(run_stoppable(run(args.lineup, args.problem, args.repeat, args.watch, args.hide_roles)))
    elif args.command == "watch":
        sys.exit(watch(args.run_id, args.hide_roles))
    elif args.command == "replay":
        sys.exit(replay(args.run_id, args.speed, args.hide_roles))
    elif args.command == "scoreboard":
        sys.exit(show_scoreboard(args.plugin, args.since))
    elif args.command == "report":
        sys.exit(show_report(args.plugin, args.since))


def non_negative_float(value: str) -> float:
    n = float(value)
    if not n >= 0:  # also rejects nan
        raise argparse.ArgumentTypeError(f"must be at least 0, got {value}")
    return n


def positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {n}")
    return n
