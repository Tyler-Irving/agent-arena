# Agent Arena

A local orchestrator/worker multi-agent system for running tasks across different model lineups (local Ollama models and the Anthropic API) and comparing how they do. Each kind of task is a **plugin**. Core runs the bookkeeping (runs, task board, call limit, cost, run log, Ctrl-C); a plugin owns its tasks and the loop of model calls for each one. There are two plugins. The **code bounty board**: the orchestrator posts coding problems, workers solve them, a sandboxed test runner grades them. **Mafia**: 5-7 agents play a social deduction game, each seeing only its own view, and the score is which side won.

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
  core/                # never imports a plugin
    plugin.py          # Plugin protocol, Session, Outcome, PluginError: the core <-> plugin contract
    orchestrator.py    # run_lineup: run bookkeeping, task board, results, Ctrl-C/SIGTERM
    taskboard.py       # TaskBoard protocol + SQLite implementation
    runlog.py          # logs every model call, result and event to SQLite (WAL mode)
    events.py          # Event model, read_events, EVENTS_VERSION: what `arena watch` reads
    scoreboard.py      # per-lineup aggregates over one plugin's runs for `arena scoreboard`
    lineup.py          # loads lineup configs, checks them against their plugin
  plugins/
    __init__.py        # PLUGINS: plain dict of name -> plugin, DEFAULT_PLUGIN, get_plugin
    code_bounty/
      plugin.py        # CodeBounty: modes/roles, 20-call limit, lineup rules, run_task -> solo | team
      loop.py          # the solo and team loops, grading a reply, the attempts table
      roles.py         # system prompts for orchestrator, solo, planner, coder, reviewer
      grader.py        # runs tests in Docker, returns pass/fail + output
      problems.py      # loads problems/, extracts code from model replies
      view.py          # its arena watch panel: the solo/team chain, attempt, last grade/review/decision
    mafia/
      plugin.py        # Mafia: modes game + mixed, 60-call limit, settings.players, game-01..game-10
      game.py          # pure rules: deal, seat groups (mixed), speaking order, votes, choice parsing, win check, call bound
      loop.py          # MafiaGame: nights and days via session.call, the mafia_events table
      roles.py         # player and narrator prompts, code-written fact lines
      view.py          # its arena watch panel: seats, actor, revealed votes, facts, --hide-roles
      report.py        # arena report: wins by model and side, deal and ending; town play; cost
  watch/               # the terminal viewer (Rich); core never imports it
    state.py           # RunState: folds a run's events into what the screen shows
    views.py           # TaskView protocol, GenericView, view_for(plugin) -> plugins/<name>/view.py
    render.py          # header, task list, task panel, call log
    app.py             # watch_run (run --watch), attach (arena watch), replay (arena replay)
  cli.py               # entry point; the only place that wires core to the plugin registry
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
    reference.py       # known-good solution; never mounted into the sandbox
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

Anthropic prices in `pricing.py` are copied from the Anthropic pricing page and dated in its docstring. Update them from that page; never guess numbers.

### Reasoning models

`gpt-oss:20b` and `qwen3.6:35b-a3b` both think before answering. The router must:

- Keep reasoning out of the `text` returned to the orchestrator (it can arrive as a separate field or inline `<think>` tags depending on the model, so handle both)
- Store the reasoning separately in the run log so runs can be read back
- Count reasoning tokens in `output_tokens` and record them separately as `reasoning_tokens`
- Support a per-role `think: true|false` option in lineups so we can compare thinking on vs off

Verified VRAM fit with `OLLAMA_CONTEXT_LENGTH=16384`: qwen2.5-coder:14b 100% GPU, gpt-oss:20b 100% GPU, qwen3.6:35b-a3b 98% GPU.

## Plugins

`core/plugin.py` defines the contract. A plugin is a plain object with:

```python
class Plugin[T: TaskSpec](Protocol):   # TaskSpec: anything with an `id: str`
    name: str
    modes: dict[str, set[str]]         # mode -> roles a lineup in that mode must define
    max_calls: int                     # model calls per task (code_bounty: 20)
    uses_attempts: bool                # does a lineup's max_attempts mean anything? (Mafia: no)
    wording: Wording                   # task noun and score words for the CLI and scoreboard
    schema: str                        # SQL for the plugin's own tables, run at the start of each run
    def load_tasks(self, selector: str | None) -> list[T]: ...
    def validate_lineup(self, lineup: Lineup) -> None: ...      # raise ValueError
    async def run_task(self, task: T, lineup: Lineup, session: Session) -> Outcome: ...
```

- `run_lineup(plugin, lineup, tasks, board, log, selector)` checks the lineup's plugin, mode and roles (`check_roles`), creates one `Session` per task and calls `run_task`. It knows nothing about grading, attempts or roles.
- `Session.call(role_name, role_config, system, messages)` makes every model call: it enforces `max_calls` (raises `CallLimitReached`), tracks cost, and tags the run log with `role_name`, which can name a player ("villager-2") rather than a lineup role. Provider errors raise `ModelCallFailed`. It also logs `call_started`/`call_finished` events. `Session.emit(kind, **data)` logs a plugin event, so a plugin never touches the events table itself.
- The CLI and scoreboard talk in the plugin's `wording`: `task` and `tasks` ("problem"/"problems", "game"/"games"), `passed` and `rate` (scoreboard columns, "passed"/"pass", "town won"/"town win") and `summary` (a run's line, "{passed}/{scored} passed", "town won {passed}/{scored}"). `summary` is checked when the plugin loads. The attempt limit and attempts columns only show, and the limit only splits scoreboard rows, when `uses_attempts`.
- `run_task` returns `session.outcome(...)`: `score` 0.0-1.0, `attempts` (1 if the plugin doesn't retry), `verdict` (printed before the call count, e.g. "PASS in 2 attempt(s)"), `fail_reason` (the task board marks the task failed with it; None = done), `error` (provider error: not scored), `stop_reason`, and a free-form `metrics` dict stored as JSON.
- A plugin normally catches `ModelCallFailed` and `CallLimitReached` itself. A `ModelCallFailed` that escapes `run_task` becomes an unscored error outcome. Any other exception fails the task with its message, marks the run interrupted (so the scoreboard doesn't count it as a full run) and propagates.
- A plugin's infrastructure failure (e.g. `GraderError`) subclasses `PluginError`, with `what = "grader"`. `run_lineup` fails the task with "<what> error: ..." and re-raises, and the CLI aborts the run with "<what> unavailable, run aborted: ...".

### Events (what `arena watch` shows)

Every step of a run is a row in `events`, committed as it happens. The database is in WAL mode, so a viewer in another process never blocks the run. Core logs:
- `run_started`: events_version, pid and boot (so a viewer can tell a killed run from a slow one), lineup, plugin, mode, roles, tasks, selector, max_attempts, settings, max_calls
- `task_started`: index, total
- `call_started` and `call_finished`: from Session.call, with tokens, latency, cost, truncated and error ("interrupted" on Ctrl-C), but never the reply text
- `task_finished`: status done/failed/error/interrupted, verdict, score, reason, calls, cost, wall time
- `run_finished`: interrupted

Every task_started gets exactly one task_finished, and the run_started one run_finished, on every path (plugin error, crash, Ctrl-C).

A plugin logs its own steps with `session.emit(kind, **data)`:
- code_bounty: grading, graded, plan, review, decision, replan
- mafia: deal, phase, proposal, check, kill, speech, votes (once, after the day's last vote, with `invalid`), elimination, win

Data must be JSON-friendly and must never hold anything some agent isn't meant to see yet, since the viewer shows every event. Event strings can hold model replies: the viewer strips control characters from them (`watch.views.plain`, in `RunState.apply`), and a view draws them only with `rich.text.Text`, never as a plain `str` table cell, which Rich would parse as markup. Changing an event's shape means bumping `EVENTS_VERSION` in `core/events.py`. The viewer refuses runs with another version ("run N uses event format vX, this viewer reads v1").

### Adding a plugin

1. Create `src/arena/plugins/<name>/` with a class that has the attributes and methods above, and a module-level instance.
2. Add the instance to the list in `plugins/__init__.py`. That's the whole registry; there are no entry points.
3. Keep its prompts in its own `roles.py`. If it needs tables, put `CREATE TABLE IF NOT EXISTS` SQL in `schema`. Key rows by `run_id` and `problem_id` (which holds the task id).
4. Add lineups with `plugin: <name>` to `config/lineups.yaml`. Compare them with `arena scoreboard --plugin <name>`.
5. Test it the way `tests/test_plugin.py` tests its fake plugin: patch `arena.core.plugin.complete` and call `run_lineup`.
6. Optional, for `arena watch`:
   - `session.emit(...)` the steps worth seeing.
   - A `plugins/<name>/view.py` with `make_view(task_id, run, options)` returning a `TaskView`: `update(event)`, `render(now)`, and `mask(event)`, which returns None unless a call must be hidden.
   - It is found by name; nothing registers it.
   - Only `arena.watch` imports it, so the plugin itself never loads Rich.
   - Without one, the generic panel lists the plugin's events.
   - Test it the way `tests/test_watch.py` does: replay a scripted run with `--speed 0` into `Console(record=True)`.
7. Optional, for `arena report --plugin <name>`: a `report(self, log, since) -> str` method (`core.plugin.Reports`, checked with isinstance). Use it for what the scoreboard's one row per lineup can't show. Derive it from what runs already store (results, metrics, calls, events, the plugin's own tables) rather than storing more.

## Lineups

Defined in `config/lineups.yaml`. A lineup is a plugin (optional, defaults to `code_bounty`), a mode, and a model per role. Unknown keys are rejected, so a typo like `plugn:` fails to load instead of quietly running as code_bounty. Lineups load with `load_lineups(PLUGINS, DEFAULT_PLUGIN)`, which runs `check_lineup` on each: its plugin's modes and roles (`check_roles`), then the plugin's `validate_lineup`. One invalid lineup fails the whole load, and `arena run` reports it and exits 2.

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

## Code bounty loop (team mode)

Lives in `plugins/code_bounty/loop.py`.

1. Pull next problem from the task board
2. Planner writes a short approach
3. Coder writes `solution.py` from prompt + plan
4. Grader runs the tests
5. If failing, reviewer reads the code and test output and writes feedback, coder retries
6. Orchestrator decides: retry, replan, or give up
7. Hard limits: max 3 attempts per problem (a lineup can set `max_attempts`), max 20 model calls per problem (`CodeBounty.max_calls`). `validate_lineup` rejects a `max_attempts` whose worst case can't fit in 20 calls (team: 4n-2, so at most 5; solo: n).

Solo mode skips planner/reviewer: one model gets the prompt, writes code, sees test failures, and retries, up to 3 attempts (or the lineup's `max_attempts`).

A problem scores 1.0 if its tests pass, else 0.0.

Only one Ollama model should be loaded at a time (16GB VRAM). Run tasks sequentially within a lineup, and run a plugin's calls within a task one at a time when they use different local models.

## Mafia

One task is one game: `game-01`..`game-10` (`--problem 03`, `--game 03` or `--problem game-03`). Code applies every rule in `game.py`; models only choose kills, checks, speeches and votes. Score is 1.0 if town won, else 0.0, so the scoreboard's "town won" and "town win" columns are the town wins and win rate. The results metrics hold the winner, nights/days, the mafia, who left when, detective checks and mafia found, town votes and town votes on mafia, invalid replies by kind, and each player's model (`models`: name -> model and think).

Two modes:
- `game`: one model per role (`narrator`, `mafia`, `detective`, `villager`).
- `mixed`: two seat models (`narrator`, `seats_a`, `seats_b`), 7 players only. `game.seat_groups` splits the seats from `random.Random(f"{game_id}:seats")` so each group holds exactly one mafia and two or three town players; a model's voice then doesn't point at a side (with one model per side, a capable town wins ~all games against another model's mafia and ~none against its own). Lineups come in pairs with `seats_a`/`seats_b` swapped, so every deal is played with each model in each seat. `arena report` gives mixed games per-player tables (each model's mafia voted out / killed / alive, its town players' votes on mafia, town wins by the detective's model) instead of per-side rates.

Rules:
- Lineups take `settings.players` 5-7 (default 7; mixed: 7) and no `max_attempts` (a game has no attempts), and only their mode's roles. 7 players: 2 mafia, 1 detective, 4 villagers; 5-6: 1 mafia, 1 detective, the rest villagers. Names come from a fixed list.
- Seeds depend only on the game id: the deal uses `random.Random(f"{game_id}:deal")`, play (tie-breaks, replacing invalid targets) `random.Random(f"{game_id}:play")`, mixed seat groups `random.Random(f"{game_id}:seats")`. game-03 deals the same seats and roles in every lineup. Never seed from the run id or lineup.
- Night first. Each night the first living mafia proposes `TARGET: <name>`; the second sees the parsed proposal ("proposed killing X") plus the partner's reasoning, and makes the final choice. The detective names `CHECK: <name>` and privately learns mafia / not mafia. The check resolves before the kill.
- Each day every living player speaks once, collapsed to one line and cut to 600 characters, so a speech can't add lines that look like code-written facts. Day N starts N-1 seats after seat 0, or at the next living seat. Then everyone votes `VOTE: <name>|none`.
- **Votes are blind**: each voter sees the day's speeches but no one else's vote; all votes are revealed together in one fact line after everyone has voted. Plurality is eliminated; a tie is a seeded pick among the tied; no valid votes, no elimination.
- Dead and eliminated players' roles are revealed. Town wins with no mafia left; mafia wins once mafia >= town.
- Invalid replies (no line, a dead/unknown name, yourself) are an abstention, a wasted check, or a seeded random town kill, so every night kills and every game ends. No re-asks.

What each agent sees: every call is a fresh single message: private info (role; mafia partner; the detective's results), the seating, the shared record, and the task. The shared record is only code-written fact lines (deaths, votes, eliminations, role reveals) and speeches. **Players never see narrator text.** The narrator gets only that phase's fact lines and its reply goes only to `mafia_events`, so it can neither leak nor distort the game. `calls.role` is `<role>:<name>` (e.g. `mafia:Blake`) or `narrator`, and `calls.messages` is exactly what that agent saw.

Call bound: a night is one call per living mafia + detective + narrator; a day is a speech and a vote per living player + narrator. `worst_case_calls` searches every sequence of kills and eliminations: 25/39/45 calls for 5/6/7 players, under `max_calls = 60`. Lineups may use at most one distinct `ollama/*` model, since two would swap in VRAM on nearly every call.

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
`reference.py` is a known-good solution; `tests/test_solo.py` grades every reference to prove the tests are right.
10 problems in Advent of Code style: 001-003 are easy warm-ups, 004-010 are medium to hard, each with edge cases the prompt states but that are easy to miss. Every test expectation is checked against an independent brute force or by hand, and each trap has a deliberately wrong solution that must fail.

## SQLite schema

`problem_id` means the task id everywhere. Columns added later are listed in `ADDED_COLUMNS` in `runlog.py` and added to older databases when the run log opens. Never rename tables or columns.

- `runs`: id, lineup, plugin, started_at, finished_at, problem (the --problem selector), max_attempts, interrupted (1 if stopped before finishing: Ctrl-C/SIGTERM or an error that aborted the run), settings (the lineup's plugin settings as JSON, NULL if none). The scoreboard gives each (lineup, max_attempts, settings) its own row, shown as `name [players=5]` when there are settings.
- `calls`: id, run_id, problem_id, role, model, input_tokens, output_tokens, reasoning_tokens, latency_ms, cost_usd, think, system, messages, response, reasoning, error, truncated, created_at
- `results`: id, run_id, problem_id, passed, attempts, total_calls, total_cost_usd, wall_time_ms, score, metrics. `score` is 0.0-1.0, `passed` = (score == 1.0), `metrics` is the plugin's JSON. Rows from before plugins have score NULL, and the scoreboard reads `COALESCE(score, passed)`.
- `tasks` (taskboard): id, run_id, problem_id, status (pending | running | done | failed), reason
- `attempts` (code_bounty's `schema`): one row per graded attempt, holding code, passed, tests_passed/total, timed_out and output
- `mafia_events` (mafia's `schema`): id, run_id, problem_id (game id), day (night/day number, 0 for the deal), phase (setup | night | day), kind (role | kill | check | speech | vote | elimination | announcement), actor, target, text
- `events` (core): id (the viewer's polling cursor), run_id, problem_id (NULL for run events), ts (utc_now), kind, role and model (call events only), data (JSON). See "Events" above. Runs from before it have no events.

## TaskBoard

Define `TaskBoard` as a Protocol (`put`, `next`, `complete`, `fail`). Implement it with SQLite now. Redis Streams can be swapped in later without touching the orchestrator.

## CLI

```
uv run arena smoke                         # one call to each configured model
uv run arena run --lineup opus-solo        # run all problems with a lineup
uv run arena run --lineup X --problem 001  # single problem
uv run arena run --lineup X --repeat 5     # five runs, to see run-to-run noise
uv run arena scoreboard                    # pass rate, avg attempts, tokens, cost, time per lineup
uv run arena scoreboard --since 17         # only run 17 and later
uv run arena scoreboard --plugin X         # another plugin's lineups (default code_bounty)
uv run arena run --lineup mafia-haiku --problem 03   # one Mafia game
uv run arena scoreboard --plugin mafia     # town won / town win rate per lineup
uv run arena report --plugin mafia [--since RUN_ID]   # win rate per model per side, per deal, endings, town play, cost
uv run arena run --lineup X --watch        # live view; the usual lines still print above it
uv run arena watch [RUN_ID]                # attach read-only from another terminal (default: the run in progress, else the next)
uv run arena replay RUN_ID [--speed N]     # gaps divided by N; 0 = final frame only; exits 2 for runs with no events
uv run arena replay RUN_ID --hide-roles    # Mafia: hide each role until that player is out or the game ends (on run, needs --watch)
```

Back up the database with `sqlite3 data/arena.db ".backup <file>"`, not `cp`: in WAL mode recent writes can still be in `arena.db-wal`. On a filesystem without WAL support the run log prints a note and keeps its journal mode; runs work, but a viewer may make them wait.

## Build order

1. Router + `arena smoke` working for one Anthropic and one Ollama model
2. Grader image + grader, tested with a known-good and known-bad solution
3. Solo mode end to end on 3 problems
4. Run log + scoreboard
5. Team mode
6. Remaining 7 problems
7. Plugin interface (code_bounty behind `core/plugin.py`), to make room for Mafia
8. Mafia plugin

Stop after each step and confirm it works before moving on.

## Checks

Run before every commit (the edit hook only runs ruff on the edited file):

```
uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright src tests && uv run pytest -q
```

## Conventions

- async throughout, type hints, pydantic models for configs and responses
- Keep prompts in `roles.py`, not scattered through the code
- Log enough that a run can be replayed and read like a transcript
