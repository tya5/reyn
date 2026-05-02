"""Slash command registry for `reyn chat`.

Each slash module registers its commands by calling `register()` on this
registry instance. `session.py`'s `_maybe_handle_slash` delegates here
instead of hard-coding command names.

Usage::

    from reyn.chat.slash import REGISTRY
    handler = REGISTRY.get("list")
    if handler:
        await handler.handle(session, args)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Awaitable


@dataclass
class SlashCommand:
    """Descriptor for a single slash command."""

    name: str           # command name without leading /  (e.g. "list")
    summary: str        # one-line description shown in palette
    handler: Callable   # async (session, args: str) -> None
    completer: Callable | None = None  # optional: (session) -> list[str]


class SlashRegistry:
    """Registry mapping command names to SlashCommand descriptors."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, cmd: SlashCommand) -> None:
        self._commands[cmd.name] = cmd

    def get(self, name: str) -> SlashCommand | None:
        return self._commands.get(name)

    def all_commands(self) -> list[SlashCommand]:
        return list(self._commands.values())

    def names(self) -> list[str]:
        return sorted(self._commands.keys())


REGISTRY: SlashRegistry = SlashRegistry()

# Import sub-modules to trigger registration
from reyn.chat.slash import chat as _chat_mod  # noqa: E402, F401
from reyn.chat.slash import agents as _agents_mod  # noqa: E402, F401
from reyn.chat.slash import budget as _budget_mod  # noqa: E402, F401
from reyn.chat.slash import skills as _skills_mod  # noqa: E402, F401
from reyn.chat.slash import help as _help_mod  # noqa: E402, F401


__all__ = ["REGISTRY", "SlashRegistry", "SlashCommand"]
