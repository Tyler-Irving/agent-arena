# Agent Arena

A small, hand-written multi-agent system for comparing model **lineups** — mixes of
local Ollama models and Claude models via the Anthropic API — on the same tasks.

The first task type is a **code bounty board**: the orchestrator posts coding problems,
workers write solutions, and a locked-down Docker sandbox grades them with pytest. Every
model call, attempt and result goes into SQLite, so you can compare pass rate, attempts,
tokens, cost and time per lineup, and read any run back like a transcript.

There is no agent framework (no LangChain, LangGraph or CrewAI). The whole loop is
a few hundred lines of plain async Python, because the point is to see how
multi-agent setups behave.

## How a problem gets solved

**Solo mode:** one model gets the problem, writes `solution.py`, sees the test
failures, and retries. Up to 3 attempts.

**Team mode:** four roles, each of which can be a different model.

```
           ┌─────────┐   plan   ┌───────┐  solution.py  ┌────────┐
problem ──▶│ planner │────────▶│ coder │──────────────▶│ grader │──▶ pass ✔
           └─────────┘          └───────┘               └────────┘
                ▲                   ▲                        │ fail
                │ replan            │ retry                  ▼
                │              ┌──────────────┐  review  ┌──────────┐
                └──────────────│ orchestrator │◀─────────│ reviewer │
                               └──────────────┘          └──────────┘
                                      │ give up ──▶ fail ✘
```

The reviewer explains why the tests failed. The orchestrator then decides: **retry**
(the coder fixes the code using the review), **replan** (the planner writes a new
approach), or **give up**. Hard limits: 3 attempts and 20 model calls per problem.

## Quick start

Requirements: Python 3.12 with [uv](https://docs.astral.sh/uv/), Docker, and either an
Anthropic API key or [Ollama](https://ollama.com) (or both).
Run every command from the repo root; config, problems and the database are found relative to it.

```bash
uv sync
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env        # only needed for anthropic/* models
docker build -t arena-grader docker/arena-grader   # the grading sandbox

uv run arena smoke                                 # one call to each configured model
uv run arena run --lineup haiku-solo               # run every problem with a lineup
uv run arena run --lineup all-local-gptoss --problem 002
uv run arena scoreboard                            # compare lineups across all runs
```

For local models, pull them first (`ollama pull qwen2.5-coder:14b`) and keep Ollama
on `http://localhost:11434`.

## Lineups

Lineups live in [`config/lineups.yaml`](config/lineups.yaml). Each one is a mode plus a
model per role. Model names are prefixed by provider:

```yaml
sonnet-orch-qwen14b:
  mode: team
  roles:
    orchestrator: anthropic/claude-sonnet-5-5
    planner: anthropic/claude-sonnet-5-5
    coder: ollama/qwen2.5-coder:14b
    reviewer: ollama/qwen2.5-coder:14b

qwen36-nothink-solo:
  mode: solo
  roles: { solo: { model: "ollama/qwen3.6:35b-a3b", think: false } }
```

For reasoning models (`gpt-oss`, `qwen3.6`), the thinking is kept out of the answer
and stored separately, and its tokens are counted. `think: true|false` turns
thinking on or off per role.

## Problems

Each problem is a folder in [`problems/`](problems):

| File | Purpose |
| --- | --- |
| `prompt.md` | The task and the exact function signature |
| `test_solution.py` | Hidden pytest suite (`from solution import <fn>`) |
| `reference.py` | Known-good solution, used only to prove the tests are right |

## Safety

Model-written code never runs on the host. Each grade runs in a fresh container with
no network, 512 MB of memory, 1 CPU, a capped process count, a read-only filesystem,
all capabilities dropped and a non-root user. Containers are killed after 30 seconds.

## Project layout

```
src/arena/
  cli.py                     arena smoke | run | scoreboard
  models/router.py           one async complete() over Anthropic and Ollama
  models/pricing.py          per-model token prices (local models are free)
  core/orchestrator.py       solo and team loops
  core/taskboard.py          TaskBoard protocol + SQLite implementation
  core/runlog.py             SQLite log of runs, calls, attempts and results
  core/scoreboard.py         per-lineup aggregates
  core/lineup.py             lineup config loading
  plugins/code_bounty/       prompts (roles.py), grader, problem loading
docker/arena-grader/         grading sandbox image
config/lineups.yaml          lineups
problems/                    coding problems
tests/                       pytest suite (grader tests skip without Docker)
```

## Development

```bash
uv run ruff check src tests && uv run pyright src tests && uv run pytest -q
```

## Status

| Step | | Tag |
| --- | --- | --- |
| 1. Model router + `arena smoke` | ✅ | `step-1-router` |
| 2. Docker grader | ✅ | `step-2-grader` |
| 3. Solo mode end to end | ✅ | `step-3-solo` |
| 4. Run log + scoreboard | ✅ | `step-4-scoreboard` |
| 5. Team mode | ✅ | `step-5-team` |
| 6. Ten problems, mostly medium or harder | ⏳ | |

So far every lineup solves the three starter problems on the first try, so step 6 is
about harder problems that actually separate the lineups.
