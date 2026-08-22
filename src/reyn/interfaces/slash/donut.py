"""/donut — hidden easter egg. Andy Sloane's spinning ASCII torus.

Not listed in /help or the Tab palette. Type `/donut` to invoke.
"""
from __future__ import annotations

from reyn.interfaces.slash import SlashContext, reply, slash


@slash("donut", summary="Andy Sloane's spinning ASCII donut", locus="client", hidden=True)
async def donut_cmd(ctx: "SlashContext", args: str) -> None:
    await reply(ctx, "🍩")
