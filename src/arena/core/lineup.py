"""Lineups: a mode plus a model per role, loaded from config/lineups.yaml."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from arena.models.router import can_disable_thinking

LINEUPS_FILE = Path("config/lineups.yaml")
DEFAULT_MAX_ATTEMPTS = 3
MAX_CALLS = 20  # model calls per problem, whatever the lineup

REQUIRED_ROLES = {
    "solo": {"solo"},
    "team": {"orchestrator", "planner", "coder", "reviewer"},
}


class RoleConfig(BaseModel):
    model: str
    think: bool | None = None  # None keeps the model's default thinking behaviour


class Lineup(BaseModel):
    name: str
    mode: Literal["solo", "team"]
    roles: dict[str, RoleConfig]
    # Graded attempts per problem. A different limit is a different lineup, so
    # it gets its own scoreboard row.
    max_attempts: int = Field(DEFAULT_MAX_ATTEMPTS, ge=1)

    @field_validator("roles", mode="before")
    @classmethod
    def _role_shorthand(cls, roles: dict) -> dict:
        # `coder: ollama/x` is shorthand for `coder: {model: ollama/x}`
        return {role: {"model": cfg} if isinstance(cfg, str) else cfg for role, cfg in roles.items()}

    @model_validator(mode="after")
    def _has_required_roles(self) -> "Lineup":
        missing = REQUIRED_ROLES[self.mode] - self.roles.keys()
        if missing:
            raise ValueError(f"{self.mode} lineup {self.name!r} is missing roles: {sorted(missing)}")
        for role, cfg in self.roles.items():
            if cfg.think is False and not can_disable_thinking(cfg.model):
                raise ValueError(f"lineup {self.name!r} role {role}: {cfg.model} cannot turn thinking off")
        # A lineup must be able to use every attempt it asks for. Solo: one call
        # per attempt. Team: the plan, a coder call per attempt, and between
        # attempts a review, a decision and possibly a replan: 1 + n + 3(n - 1).
        n = self.max_attempts
        worst = n if self.mode == "solo" else 4 * n - 2
        if worst > MAX_CALLS:
            raise ValueError(
                f"lineup {self.name!r}: max_attempts {n} can need {worst} calls, "
                f"over the {MAX_CALLS}-call limit"
            )
        return self


def load_lineups(path: Path = LINEUPS_FILE) -> dict[str, Lineup]:
    raw = yaml.safe_load(path.read_text()) or {}
    return {name: Lineup.model_validate({"name": name, **cfg}) for name, cfg in raw.items()}


def get_lineup(name: str, path: Path = LINEUPS_FILE) -> Lineup:
    lineups = load_lineups(path)
    if name not in lineups:
        raise KeyError(f"unknown lineup {name!r}; choose from: {', '.join(lineups)}")
    return lineups[name]
