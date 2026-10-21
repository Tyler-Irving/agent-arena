"""Every plugin, by name. Adding a plugin means adding it here."""

from typing import Any

from arena.core.plugin import Plugin
from arena.plugins.code_bounty.plugin import PLUGIN as CODE_BOUNTY
from arena.plugins.mafia.plugin import PLUGIN as MAFIA

PLUGINS: dict[str, Plugin[Any]] = {p.name: p for p in [CODE_BOUNTY, MAFIA]}
DEFAULT_PLUGIN = CODE_BOUNTY.name  # for lineups that don't name a plugin


def get_plugin(name: str) -> Plugin[Any]:
    if name not in PLUGINS:
        raise KeyError(f"unknown plugin {name!r}; choose from: {', '.join(PLUGINS)}")
    return PLUGINS[name]
