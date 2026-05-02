"""/cost, /budget slash commands."""
from __future__ import annotations

from reyn.chat.slash import slash


@slash("cost", summary="Quick token + USD cost summary for this agent")
async def cost_cmd(session: "object", args: str) -> None:
    await session._slash_cost(args)


@slash("budget", summary="Full budget breakdown; /budget reset to clear counters")
async def budget_cmd(session: "object", args: str) -> None:
    await session._slash_budget(args)
