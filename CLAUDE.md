# Agent Arena

A local orchestrator/worker multi-agent system for running tasks across different model lineups (local Ollama models and the Anthropic API) and comparing how they do. First plugin is the **code bounty board**: the orchestrator posts coding problems, workers solve them, a sandboxed test runner grades them.

The point of this project is learning how multi-agent systems behave, so keep the core small and readable. No agent frameworks (no LangChain, LangGraph, CrewAI). Write the loop ourselves.

## Environment

- Windows 11 + WSL2 Ubuntu, RTX 4080 (16GB VRAM)
- Python 3.12, managed with `uv`
- Ollama runs inside WSL at `http://localhost:11434/v1` (OpenAI-compatible API)
- Docker Desktop with WSL integration, used only for the grader sandbox
- `ANTHROPIC_API_KEY` lives in `.env` (gitignored)

## Layout

```
src/arena/             # package (src layout; `arena` script -> arena.cli:main)
  models/
    router.py          # one interface over Anthropic + Ollama
    pricing.py         # per-model token prices, local = 0
  core/
    orchestrator.py    # the plan -> dispatch -> collect -> decide loop
    taskboard.py       # TaskBoard protocol + SQLite implementation
    runlog.py          # logs every model call and result to SQLite
    lineup.py          # loads lineup configs, maps role -> model
  plugins/
    code_bounty/
      roles.py         # system prompts for orchestrator, solo, planner, coder, reviewer
      grader.py        # runs tests in Docker, returns pass/fail + output
  cli.py               # entry point
tests/                 # pytest unit tests; grader tests skip if Docker/image missing
docker/
  arena-grader/
    Dockerfile         # grader sandbox image
config/
  lineups.yaml
problems/
  001-two-sum/
    prompt.md
    test_solution.py
data/
  arena.db           # gitignored
```

## Model router

Single async interface so the orchestrator never knows which provider it's talking to:

```python
async def complete(model: str, system: str, messages: list[dict]) -> ModelResponse
# ModelResponse: text, input_tokens, output_tokens, latency_ms, model
```

- Model strings prefixed by provider: `anthropic/claude-sonnet-5-5`, `ollama/qwen2.5-coder:14b`
- Anthropic via the `anthropic` SDK, Ollama via the `openai` SDK pointed at the local base URL
- Every call goes through `runlog` automatically

Models to support from day one:

| Provider | Model |
| --- | --- |
| anthropic | claude-haiku-5-5, claude-sonnet-5-5, claude-opus-5-5 |
| ollama | qwen2.5-coder:14b, gpt-oss:20b, qwen3.6:35b-a3b |

Leave Anthropic prices as placeholders in `pricing.py` with a TODO. Do not guess numbers.

### Reasoning models

`gpt-oss:20b` and `qwen3.6:35b-a3b` both think before answering. The router must:

- Keep reasoning out of the `text` returned to the orchestrator (it can arrive as a separate field or inline `<think>` tags depending on the model, so handle both)
- Store the reasoning separately in the run log so runs can be read back
- Count reasoning tokens in `output_tokens` and record them separately as `reasoning_tokens`
- Support a per-role `think: true|false` option in lineups so we can compare thinking on vs off

Verified VRAM fit with `OLLAMA_CONTEXT_LENGTH=16384`: qwen2.5-coder:14b 100% GPU, gpt-oss:20b 100% GPU, qwen3.6:35b-a3b 98% GPU.

## Lineups

Defined in `config/lineups.yaml`. A lineup is a mode plus a model per role:

```yaml
opus-solo:
  mode: solo
  roles: { solo: anthropic/claude-opus-5-5 }

sonnet-orch-qwen14b:
  mode: team
  roles:
    orchestrator: anthropic/claude-sonnet-5-5
    planner: anthropic/claude-sonnet-5-5
    coder: ollama/qwen2.5-coder:14b
    reviewer: ollama/qwen2.5-coder:14b

all-local-gptoss:
  mode: team
  roles:
    orchestrator: ollama/gpt-oss:20b
    planner: ollama/gpt-oss:20b
    coder: ollama/gpt-oss:20b
    reviewer: ollama/gpt-oss:20b
```

## Orchestrator loop (team mode)

1. Pull next problem from the task board
2. Planner writes a short approach
3. Coder writes `solution.py` from prompt + plan
4. Grader runs the tests
5. If failing, reviewer reads the code and test output and writes feedback, coder retries
6. Orchestrator decides: retry, replan, or give up
7. Hard limits: max 3 attempts per problem, max 20 model calls per problem

Solo mode skips planner/reviewer: one model gets the prompt, writes code, sees test failures, retries up to 3 times.

Only one Ollama model should be loaded at a time (16GB VRAM). Run problems sequentially within a lineup.

## Grader sandbox

Model-written code never runs directly on the host.

- Image: `python:3.12-slim` with pytest preinstalled, runs as a non-root user. Build with `docker build -t arena-grader docker/arena-grader`
- `docker run --rm --network none --memory 512m --cpus 1`, plus `--read-only`, `--pids-limit`, `--cap-drop ALL`, `no-new-privileges`, `--pull never`
- Mount a temp dir containing `solution.py` and `test_solution.py`, read-only
- 30 second timeout, kill container on timeout
- Return: passed (bool), tests passed/total, truncated stdout/stderr

## Problem format

`problems/<id>-<slug>/prompt.md` describes the task and the exact function signature.
`test_solution.py` does `from solution import <fn>` and uses plain pytest asserts.
Start with 10 problems, Advent of Code difficulty, mix of easy and medium.

## SQLite schema

- `runs`: id, lineup, plugin, started_at, finished_at
- `calls`: id, run_id, problem_id, role, model, input_tokens, output_tokens, latency_ms, cost_usd, created_at
- `results`: id, run_id, problem_id, passed, attempts, total_calls, total_cost_usd, wall_time_ms

## TaskBoard

Define `TaskBoard` as a Protocol (`put`, `next`, `complete`, `fail`). Implement it with SQLite now. Redis Streams can be swapped in later without touching the orchestrator.

## CLI

```
uv run arena smoke                         # one call to each configured model
uv run arena run --lineup opus-solo        # run all problems with a lineup
uv run arena run --lineup X --problem 001  # single problem
uv run arena scoreboard                    # pass rate, avg attempts, tokens, cost, time per lineup
```

## Build order

1. Router + `arena smoke` working for one Anthropic and one Ollama model
2. Grader image + grader, tested with a known-good and known-bad solution
3. Solo mode end to end on 3 problems
4. Run log + scoreboard
5. Team mode
6. Remaining 7 problems

Stop after each step and confirm it works before moving on.

## Conventions

- async throughout, type hints, pydantic models for configs and responses
- Keep prompts in `roles.py`, not scattered through the code
- Log enough that a run can be replayed and read like a transcript
