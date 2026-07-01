"""/donut — hidden easter egg. Andy Sloane's spinning ASCII torus.

Not listed in /help or the Tab palette. Type `/donut` to invoke.
"""
from __future__ import annotations

from reyn.interfaces.slash import reply, slash


@slash("donut", summary="Andy Sloane's spinning ASCII donut", hidden=True)
async def donut_cmd(session: "object", args: str) -> None:
    await reply(session, "🍩")
