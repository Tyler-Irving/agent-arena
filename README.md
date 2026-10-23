# Agent Arena

A small, hand-written multi-agent system for comparing model **lineups** — mixes of
local Ollama models and Claude models via the Anthropic API — on the same tasks.

Each task type is a **plugin**, and the core loop doesn't know which one it is running.
There are two:

- **Code bounty board**: the orchestrator posts coding problems, workers write solutions,
  and a locked-down Docker sandbox grades them with pytest.
- **Mafia**: 5-7 agents play a social deduction game, each seeing only its own view, and the
  score is which side won.

Every model call, attempt and result goes into SQLite, so you can compare pass rate, attempts,
tokens, cost and time per lineup, and read any run back like a transcript. Every step is also
logged as an event, so you can watch a run live in the terminal (`--watch`, `arena watch`) or
replay it afterwards (`arena replay`).

There is no agent framework (no LangChain, LangGraph or CrewAI). The whole loop is
a few hundred lines of plain async Python, because the point is to see how
multi-agent setups behave.

## How a problem gets solved

**Solo mode:** one model gets the problem, writes `solution.py`, sees the test
failures, and retries. Up to 3 attempts (a lineup can set `max_attempts`).

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
approach), or **give up**. Hard limits: 3 attempts (or the lineup's `max_attempts`) and 20 model calls per problem.

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
uv run arena run --lineup qwen14b-solo --repeat 5  # five runs, to see run-to-run noise
uv run arena scoreboard                            # compare lineups across all runs
uv run arena scoreboard --since 17                 # only run 17 and later
uv run arena scoreboard --plugin code_bounty       # one plugin's lineups (code_bounty is the default)
uv run arena run --lineup gptoss-solo --watch      # run with a live view
uv run arena replay 32 --speed 10                  # replay a finished run, 10x as fast
```

Ctrl-C or SIGTERM stops a run cleanly: the run is closed and marked interrupted, and the
unfinished problem is left unscored. A second signal is ignored so it can't cut short the
sandbox cleanup. The scoreboard's "per run" range only compares full, uninterrupted runs.

For local models, pull them first (`ollama pull qwen2.5-coder:14b`) and keep Ollama
on `http://localhost:11434`.

## Commands

| Command | Flag / argument | Meaning |
| --- | --- | --- |
| `arena smoke` | `--model M` | One call to each model, to check keys and Ollama. Repeatable; defaults to Haiku and qwen2.5-coder:14b. |
| `arena run` | `--lineup NAME` | Required. A lineup from `config/lineups.yaml`; it decides the plugin and the model for each role. |
| | `--problem X` | One task instead of all: a problem number (`004`) or a Mafia game (`3`, `03` or `game-03`). `--game` is the same flag. |
| | `--repeat N` | Run everything N times, to see run-to-run noise. |
| | `--watch` | Show a live view while it runs. The usual lines still print above it. |
| | `--hide-roles` | Mafia, with `--watch`: hide each role until that player is out or the game ends. |
| `arena watch` | `RUN_ID` | Optional. Follow a run from another terminal; with no id, the run in progress, or the next one to start. |
| | `--hide-roles` | As above. |
| `arena replay` | `RUN_ID` | Required. Play a finished run back. |
| | `--speed N` | How fast: `1` (default) is real time, `10` ten times as fast, `0.5` half speed, `0` only the final state. |
| | `--hide-roles` | As above. |
| `arena scoreboard` | `--plugin P` | Whose lineups to compare (default `code_bounty`). For `mafia`, the columns are town wins and the town win rate. |
| | `--since RUN_ID` | Only count that run and later ones, e.g. to compare before and after a change. |

A run prints its id when it ends (`run 32: 1/1 passed, ...`), and the live view shows it in its
header. Ctrl-C stops a watcher or a replay without touching the run being watched.

## Lineups

Lineups live in [`config/lineups.yaml`](config/lineups.yaml). Each one is a plugin
(`plugin:`, optional, defaults to `code_bounty`), a mode, and a model per role. The plugin
decides which modes exist and which roles each one needs. Unknown keys are an error, so a
typo can't quietly fall back to the defaults. Model names are prefixed by provider:

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

The lineups that ship in the config (`anthropic/*` models cost money; `ollama/*` models are free):

| Plugin | Lineups |
| --- | --- |
| code_bounty, solo | `haiku-solo`, `sonnet-solo`, `opus-solo`, `qwen14b-solo`, `gptoss-solo`, `qwen36-solo`, `qwen36-nothink-solo` |
| code_bounty, team | `sonnet-orch-qwen14b` (Sonnet plans and decides, qwen14b codes and reviews), `sonnet-orch-qwen14b-5att` (the same with 5 attempts), `all-local-gptoss` |
| mafia | `mafia-haiku`, `mafia-gptoss` (all local), `mafia-sonnet-vs-haiku` (Sonnet mafia, Haiku town), `mafia-haiku-vs-sonnet` (the reverse) |

## Problems

Each problem is a folder in [`problems/`](problems):

| File | Purpose |
| --- | --- |
| `prompt.md` | The task and the exact function signature |
| `test_solution.py` | Hidden pytest suite (`from solution import <fn>`) |
| `reference.py` | Known-good solution, used only to prove the tests are right |

| Problem | Level |
| --- | --- |
| 001 two sum, 002 merge intervals, 003 weighted maze | easy |
| 004 bracket expand, 005 version order, 006 wraparound regions, 008 rate limiter | medium |
| 007 integer calculator | medium-hard |
| 009 next cron run, 010 settle debts | hard |

## Mafia

The second plugin: 5-7 agents play Mafia (2 mafia, a detective and villagers by default), and
a game's score is 1.0 if town won. Code applies the rules; models only choose kills, checks,
speeches and votes. Each agent sees only its own view: its role, its private knowledge (mafia
partner, detective results), and a shared record of code-written facts and speeches. Votes are
blind until everyone has voted, and the narrator's flavor text never reaches the players.
Every game is seeded by its id, so `game-03` deals the same seats and roles in every lineup.

```bash
uv run arena run --lineup mafia-haiku --problem 03   # one game
uv run arena run --lineup mafia-gptoss               # all ten games, locally
uv run arena scoreboard --plugin mafia               # town wins and town win rate per lineup
```

Lineups such as `mafia-sonnet-vs-haiku` put a different model on each side. Every event goes to
the `mafia_events` table, and every prompt to `calls`, so a game can be read back per player.

To watch a game as it is played, or to replay one:

```bash
uv run arena run --lineup mafia-gptoss --problem 3 --watch                 # free and local, 2-4 minutes
uv run arena run --lineup mafia-gptoss --problem 3 --watch --hide-roles    # no spoilers
uv run arena replay 33 --speed 10                                          # a finished game, 10x as fast
```

```
game-04 · day 2 · votes   alive 4/7 · mafia 1 · town 3
seat  name    role       status          vote
1     Finley  mafia      alive
2     Drew    villager   ● voting 6s…
3     Casey   villager   alive
4     Gray    villager   ✝ night 1
5     Blake   detective  ✝ night 2
6     Avery   villager   alive
7     Emery   mafia      ✗ out day 1
last speech: Finley: I've seen no solid evidence yet...
fact: night 2: Blake checked Finley: mafia
fact: night 2: Finley chose Blake · Blake (detective) killed
```

## Watching runs

Every step of a run is also written to an `events` table as it happens. That covers each task
starting and finishing, each model call starting and finishing, and each plugin's own steps:
plans, grades, reviews and decisions; nights, speeches, votes and kills. One terminal viewer
reads them, so watching live and replaying later look the same.

```bash
uv run arena run --lineup all-local-gptoss --watch   # run with a live view; the usual lines still print above it
uv run arena watch                                   # from another terminal: the run in progress, or the next one
uv run arena watch 32                                # a given run
uv run arena replay 32 --speed 10                    # play a finished run back 10x as fast
uv run arena replay 32 --speed 0                     # just its final state
uv run arena replay 33 --hide-roles                  # Mafia: hide each role until that player is out
```

The view shows the run (lineup, task N/M, elapsed time, cost so far), the task list, a panel
for the current task, and a call log (role, model, tokens, latency, cost, outcome). In flight,
the current call has a running timer.

```
004-bracket-expand · attempt 2/3 · plan 1
planner ──▶ coder ──▶ grader ──▶ reviewer ──▶ orchestrator
sonnet      qwen14b   docker     qwen14b      sonnet
  ✔         ● 14s…
last: attempt 1 ✘ 7/9 tests · review: off by one on nested braces · RETRY: small fix
```

A Mafia game shows the seats, who is speaking or voting, the votes once they are revealed
(blind votes stay blind in the view too), and the latest kill or elimination. Roles are shown
by default. With `--hide-roles`, a role stays hidden until that player is out or the game ends.
So do night actions, and the model, tokens and cost of a hidden player's calls, since a
mafia-only model would give them away.

`arena watch` and `arena replay` open the database read-only. Runs from before events were
added (and `arena smoke` runs) have none, so they can't be replayed. The database is in SQLite's
WAL mode so a viewer never blocks a run. Back it up with
`sqlite3 data/arena.db ".backup data/backup.db"` rather than `cp`, which can miss writes still in
`arena.db-wal`, and don't open it from Windows over `\\wsl$`.

## Safety

Model-written code never runs on the host. Each grade runs in a fresh container with
no network, 512 MB of memory, 1 CPU, a capped process count, a read-only filesystem,
all capabilities dropped and a non-root user. Containers are killed after 30 seconds.

## Project layout

```
src/arena/
  cli.py                     arena smoke | run [--watch] | watch | replay | scoreboard
  models/router.py           one async complete() over Anthropic and Ollama
  models/pricing.py          per-model token prices (local models are free)
  core/plugin.py             the Plugin protocol, and the Session/Outcome a plugin works with
  core/orchestrator.py       run_lineup: runs a plugin's tasks, logs results, handles Ctrl-C
  core/taskboard.py          TaskBoard protocol + SQLite implementation
  core/runlog.py             SQLite log of runs, calls, results and events
  core/events.py             the Event model and how to read a run's events back
  core/scoreboard.py         per-lineup aggregates
  core/lineup.py             lineup config loading, checked against each lineup's plugin
  plugins/__init__.py        the plugin registry: a plain dict of name -> plugin
  plugins/code_bounty/       solo/team loops (loop.py), prompts (roles.py), grader, problems, view.py
  plugins/mafia/             game rules (game.py), game loop (loop.py), prompts (roles.py), view.py
  watch/                     the terminal viewer (Rich): run --watch, watch, replay
docker/arena-grader/         grading sandbox image
config/lineups.yaml          lineups
problems/                    coding problems
tests/                       pytest suite (grader tests skip without Docker)
```

## Adding a task type

A plugin is a plain object with a name, its modes and the roles each one needs, a call
limit per task, `load_tasks`, `validate_lineup` and `run_task`. It is listed in
`src/arena/plugins/__init__.py`. Its model calls show up in `arena watch` straight away. It can
log its own steps with `session.emit(...)` and draw them with an optional `view.py`. See
"Adding a plugin" in [CLAUDE.md](CLAUDE.md).

## Development

```bash
uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright src tests && uv run pytest -q
```

## Status

| Step | | Tag |
| --- | --- | --- |
| 1. Model router + `arena smoke` | ✅ | `step-1-router` |
| 2. Docker grader | ✅ | `step-2-grader` |
| 3. Solo mode end to end | ✅ | `step-3-solo` |
| 4. Run log + scoreboard | ✅ | `step-4-scoreboard` |
| 5. Team mode | ✅ | `step-5-team` |
| 6. Ten problems, mostly medium or harder | ✅ | `step-6-problems` |
| 7. Plugin interface, making room for a second task type | ✅ | `step-7-plugins` |
| 8. Mafia plugin | ✅ | `step-8-mafia` |
| 9. Live view and replay (`--watch`, `arena watch`, `arena replay`) | ✅ | `step-9-watch` |

The first three problems are warm-ups. Problems 004-010 are medium to hard, and
each one has edge cases that its prompt states but that are easy to miss
(right-associative `^`, cron's day-of-month OR day-of-week rule, rejected requests
not counting toward a rate limit, ...). Several also have tests that a correct but
slow approach fails, such as checking every minute for the next cron run.
