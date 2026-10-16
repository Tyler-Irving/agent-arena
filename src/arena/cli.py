"""arena command line entry point."""

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from arena.core.lineup import get_lineup
from arena.core.orchestrator import format_cost, run_lineup
from arena.core.runlog import get_runlog
from arena.core.taskboard import SqliteTaskBoard
from arena.models.router import complete
from arena.plugins.code_bounty.grader import GraderError
from arena.plugins.code_bounty.problems import load_problems

SMOKE_MODELS = ["anthropic/claude-haiku-5-5", "ollama/qwen2.5-coder:14b"]
SMOKE_SYSTEM = "You are a terse assistant."
SMOKE_PROMPT = "Write a one-line Python expression that reverses the string s. Reply with only the code."


async def smoke(models: list[str]) -> int:
    log = get_runlog()
    run_id = log.start_run(lineup="smoke", plugin="smoke")
    failures = 0

    # Sequential on purpose: only one Ollama model fits in VRAM at a time.
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

    log.finish_run(run_id)
    print(f"{len(models) - failures}/{len(models)} models OK (smoke run {run_id})")
    return 1 if failures else 0


async def run(lineup_name: str, problem: str | None) -> int:
    try:
        lineup = get_lineup(lineup_name)
    except KeyError as e:
        print(e.args[0], file=sys.stderr)
        return 2
    problems = load_problems(problem)
    if not problems:
        print(f"no problems match {problem!r}", file=sys.stderr)
        return 2

    roles = ", ".join(f"{role}={cfg.model}" for role, cfg in lineup.roles.items())
    print(f"lineup {lineup.name} ({lineup.mode}: {roles}), {len(problems)} problem(s)\n")
    try:
        run_id, outcomes = await run_lineup(lineup, problems, SqliteTaskBoard(), get_runlog())
    except GraderError as e:
        print(f"grader unavailable, run aborted: {e}", file=sys.stderr)
        return 2

    passed = sum(o.passed for o in outcomes)
    costs = [o.cost_usd for o in outcomes]
    total_cost = None if None in costs else sum(c for c in costs if c is not None)
    print(
        f"run {run_id}: {passed}/{len(outcomes)} passed, "
        f"{sum(o.calls for o in outcomes)} calls, {format_cost(total_cost)}, "
        f"{sum(o.wall_time_ms for o in outcomes) / 1000:.1f}s"
    )
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

    args = parser.parse_args()
    if args.command == "smoke":
        sys.exit(asyncio.run(smoke(args.models or SMOKE_MODELS)))
    elif args.command == "run":
        sys.exit(asyncio.run(run(args.lineup, args.problem)))
