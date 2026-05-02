"""/donut — hidden easter egg. Andy Sloane's spinning ASCII torus.

Not listed in /help or the Tab palette. Type `/donut` to invoke. Sends
the special outbox kind `__donut__`; the TUI app intercepts it and
pushes the modal screen. CUI mode silently ignores the unknown kind.
"""
from __future__ import annotations

from reyn.chat.outbox import OutboxMessage
from reyn.chat.slash import slash


@slash("donut", summary="Andy Sloane's spinning ASCII donut", hidden=True)
async def donut_cmd(session: "object", args: str) -> None:
    await session._put_outbox(OutboxMessage(
        kind="__donut__",
        text="",
    ))
