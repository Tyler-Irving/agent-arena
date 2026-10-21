"""Lineups: a plugin, a mode and a model per role, loaded from config/lineups.yaml.

Which modes exist and which roles each one needs come from the lineup's
plugin, so lineups are checked against the plugins they name. The plugins are
passed in, because core doesn't import any plugin.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arena.models.router import can_disable_thinking

if TYPE_CHECKING:
    from arena.core.plugin import Plugin

LINEUPS_FILE = Path("config/lineups.yaml")
DEFAULT_MAX_ATTEMPTS = 3


class RoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    think: bool | None = None  # None keeps the model's default thinking behaviour


class Lineup(BaseModel):
    # A misspelled key (`plugn: mafia`) is an error, not a lineup that quietly
    # runs with the defaults.
    model_config = ConfigDict(extra="forbid")

    name: str
    plugin: str
    mode: str
    roles: dict[str, RoleConfig]
    # Attempts per task, for plugins that retry. A different limit is a
    # different lineup, so it gets its own scoreboard row.
    max_attempts: int = Field(DEFAULT_MAX_ATTEMPTS, ge=1)
    # Plugin-specific options (e.g. a game's player count), checked by the plugin's validate_lineup.
    settings: dict[str, Any] = {}

    @field_validator("roles", mode="before")
    @classmethod
    def _role_shorthand(cls, roles: dict) -> dict:
        # `coder: ollama/x` is shorthand for `coder: {model: ollama/x}`
        return {role: {"model": cfg} if isinstance(cfg, str) else cfg for role, cfg in roles.items()}

    @model_validator(mode="after")
    def _thinking_can_be_disabled(self) -> "Lineup":
        for role, cfg in self.roles.items():
            if cfg.think is False and not can_disable_thinking(cfg.model):
                raise ValueError(f"lineup {self.name!r} role {role}: {cfg.model} cannot turn thinking off")
        return self


def check_roles(lineup: Lineup, plugin: "Plugin[Any]") -> None:
    """Raise ValueError unless the lineup is for `plugin`, in one of its modes, with that mode's roles."""
    if lineup.plugin != plugin.name:
        raise ValueError(f"lineup {lineup.name!r} is for plugin {lineup.plugin!r}, not {plugin.name!r}")
    if lineup.mode not in plugin.modes:
        raise ValueError(
            f"lineup {lineup.name!r}: {plugin.name} has no mode {lineup.mode!r}; "
            f"choose from: {', '.join(plugin.modes)}"
        )
    missing = plugin.modes[lineup.mode] - lineup.roles.keys()
    if missing:
        raise ValueError(f"{lineup.mode} lineup {lineup.name!r} is missing roles: {sorted(missing)}")


def check_lineup(lineup: Lineup, plugin: "Plugin[Any]") -> None:
    """Raise ValueError unless `plugin` can run `lineup`: check_roles, then the plugin's own rules."""
    check_roles(lineup, plugin)
    plugin.validate_lineup(lineup)


def load_lineups(
    plugins: Mapping[str, "Plugin[Any]"], default_plugin: str, path: Path = LINEUPS_FILE
) -> dict[str, Lineup]:
    """Every lineup in the file, each checked against its plugin (`default_plugin` if it names none)."""
    raw = yaml.safe_load(path.read_text()) or {}
    lineups = {}
    for name, cfg in raw.items():
        lineup = Lineup.model_validate({"name": name, "plugin": default_plugin, **cfg})
        if lineup.plugin not in plugins:
            raise ValueError(
                f"lineup {name!r}: unknown plugin {lineup.plugin!r}; choose from: {', '.join(plugins)}"
            )
        check_lineup(lineup, plugins[lineup.plugin])
        lineups[name] = lineup
    return lineups


def get_lineup(
    name: str, plugins: Mapping[str, "Plugin[Any]"], default_plugin: str, path: Path = LINEUPS_FILE
) -> Lineup:
    lineups = load_lineups(plugins, default_plugin, path)
    if name not in lineups:
        raise KeyError(f"unknown lineup {name!r}; choose from: {', '.join(lineups)}")
    return lineups[name]
