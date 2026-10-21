"""The Mafia plugin: one task is a whole game, and its score is 1.0 if town won."""

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arena.core.lineup import Lineup
from arena.core.plugin import Outcome, Session
from arena.plugins.mafia.game import MAX_PLAYERS, MIN_PLAYERS
from arena.plugins.mafia.loop import EVENTS_SCHEMA, MafiaGame

GAMES = 10  # game-01 .. game-10, each its own seeded deal


class MafiaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    players: int = Field(7, ge=MIN_PLAYERS, le=MAX_PLAYERS)


class Game(BaseModel):
    id: str


class Mafia:
    name = "mafia"
    modes: dict[str, set[str]] = {"game": {"narrator", "mafia", "detective", "villager"}}
    # The longest 7-player game takes 45 calls (game.worst_case_calls), so a
    # legal game never reaches this.
    max_calls = 60
    schema = EVENTS_SCHEMA

    def load_tasks(self, selector: str | None) -> list[Game]:
        """Every game, or the one matching `selector` ("game-03" or "03")."""
        games = [Game(id=f"game-{i:02}") for i in range(1, GAMES + 1)]
        return [g for g in games if not selector or g.id == selector or g.id == f"game-{selector}"]

    def validate_lineup(self, lineup: Lineup) -> None:
        settings(lineup)
        # Only one local model fits in VRAM; two would swap on nearly every call.
        local = sorted({cfg.model for cfg in lineup.roles.values() if cfg.model.startswith("ollama/")})
        if len(local) > 1:
            raise ValueError(
                f"lineup {lineup.name!r}: at most one ollama model per game, got {', '.join(local)}"
            )

    async def run_task(self, task: Game, lineup: Lineup, session: Session) -> Outcome:
        return await MafiaGame(task.id, lineup, session, settings(lineup).players).play()


def settings(lineup: Lineup) -> MafiaSettings:
    try:
        return MafiaSettings.model_validate(lineup.settings)
    except ValidationError as e:
        raise ValueError(f"lineup {lineup.name!r} settings: {e}") from e


PLUGIN = Mafia()
