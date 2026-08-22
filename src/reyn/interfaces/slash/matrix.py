"""/matrix — hidden easter egg.

Not listed in /help or the Tab palette. Type `/matrix` to invoke.
"""
from __future__ import annotations

from reyn.interfaces.slash import SlashContext, reply, slash


@slash("matrix", summary="Wake up, Neo.", locus="client", hidden=True)
async def matrix_cmd(ctx: "SlashContext", args: str) -> None:
    await reply(ctx, "There is no spoon.")
