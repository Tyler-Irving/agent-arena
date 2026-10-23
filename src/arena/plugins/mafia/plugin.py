"""The Mafia plugin: one task is a whole game, and its score is 1.0 if town won."""

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arena.core.lineup import Lineup
from arena.core.plugin import Outcome, Session, Wording
from arena.core.runlog import RunLog
from arena.plugins.mafia import report
from arena.plugins.mafia.game import DEFAULT_PLAYERS, MAX_PLAYERS, MIN_PLAYERS
from arena.plugins.mafia.loop import EVENTS_SCHEMA, MafiaGame

GAMES = 10  # game-01 .. game-10, each its own seeded deal


class MafiaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    players: int = Field(DEFAULT_PLAYERS, ge=MIN_PLAYERS, le=MAX_PLAYERS)


class Game(BaseModel):
    id: str


class Mafia:
    name = "mafia"
    # game: one model per role. mixed: two seat models, each playing one mafia and two or three town
    # seats (game.seat_groups), so a model's voice doesn't give away a side.
    modes: dict[str, set[str]] = {
        "game": {"narrator", "mafia", "detective", "villager"},
        "mixed": {"narrator", "seats_a", "seats_b"},
    }
    # The longest 7-player game takes 45 calls (game.worst_case_calls), so a
    # legal game never reaches this.
    max_calls = 60
    uses_attempts = False
    # score 1.0 = town won, so the summed scores count town wins
    wording = Wording(
        task="game", tasks="games", passed="town won", rate="town win", summary="town won {passed}/{scored}"
    )
    schema = EVENTS_SCHEMA

    def load_tasks(self, selector: str | None) -> list[Game]:
        """Every game, or the one matching `selector` ("game-03", "03" or "3")."""
        number = selector.removeprefix("game-") if selector else None
        return [
            Game(id=f"game-{i:02}")
            for i in range(1, GAMES + 1)
            if number is None or (number.isdigit() and int(number) == i)
        ]

    def validate_lineup(self, lineup: Lineup) -> None:
        players = settings(lineup).players
        # A role the mode doesn't use would be silently ignored (e.g. `mafia:` in a mixed lineup).
        if extra := sorted(lineup.roles.keys() - self.modes[lineup.mode]):
            raise ValueError(f"lineup {lineup.name!r}: a {lineup.mode} lineup takes no roles {extra}")
        if lineup.mode == "mixed":
            if players != MAX_PLAYERS:
                raise ValueError(
                    f"lineup {lineup.name!r}: mixed seats need {MAX_PLAYERS} players (two mafia), "
                    f"got {players}"
                )
            if lineup.roles["seats_a"] == lineup.roles["seats_b"]:
                raise ValueError(f"lineup {lineup.name!r}: seats_a and seats_b are the same model")
        # A game has no attempts; an ignored max_attempts would still split the scoreboard row.
        if "max_attempts" in lineup.model_fields_set:
            raise ValueError(f"lineup {lineup.name!r}: a mafia game has no attempts, remove max_attempts")
        # Only one local model fits in VRAM; two would swap on nearly every call.
        local = sorted({cfg.model for cfg in lineup.roles.values() if cfg.model.startswith("ollama/")})
        if len(local) > 1:
            raise ValueError(
                f"lineup {lineup.name!r}: at most one ollama model per game, got {', '.join(local)}"
            )

    async def run_task(self, task: Game, lineup: Lineup, session: Session) -> Outcome:
        return await MafiaGame(task.id, lineup, session, settings(lineup).players).play()

    def report(self, log: RunLog, since: int | None) -> str:
        """Win rates by model and side, deal and ending, plus town play and cost (`arena report`)."""
        return report.report(log, self.name, since)


def settings(lineup: Lineup) -> MafiaSettings:
    try:
        return MafiaSettings.model_validate(lineup.settings)
    except ValidationError as e:
        raise ValueError(f"lineup {lineup.name!r} settings: {e}") from e


PLUGIN = Mafia()
