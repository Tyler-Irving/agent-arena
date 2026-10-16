"""arena command line entry point."""

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from arena.core.runlog import get_runlog
from arena.models.router import complete

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

    args = parser.parse_args()
    if args.command == "smoke":
        sys.exit(asyncio.run(smoke(args.models or SMOKE_MODELS)))
