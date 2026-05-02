"""/cost, /budget slash commands."""
from __future__ import annotations

from reyn.chat.slash import REGISTRY, SlashCommand


async def _handle_cost(session: "object", args: str) -> None:
    await session._slash_cost(args)


async def _handle_budget(session: "object", args: str) -> None:
    await session._slash_budget(args)


REGISTRY.register(SlashCommand(
    name="cost",
    summary="Quick token + USD cost summary for this agent",
    handler=_handle_cost,
))

REGISTRY.register(SlashCommand(
    name="budget",
    summary="Full budget breakdown; /budget reset to clear counters",
    handler=_handle_budget,
))
