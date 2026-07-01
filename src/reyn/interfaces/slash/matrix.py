"""/matrix — hidden easter egg.

Not listed in /help or the Tab palette. Type `/matrix` to invoke.
"""
from __future__ import annotations

from reyn.interfaces.slash import reply, slash


@slash("matrix", summary="Wake up, Neo.", hidden=True)
async def matrix_cmd(session: "object", args: str) -> None:
    await reply(session, "There is no spoon.")
