"""/matrix — hidden easter egg. Triggers the Matrix rain modal in the TUI.

Not listed in /help or the Tab palette. Type `/matrix` to invoke. Sends
a special outbox kind that the TUI app intercepts (`__matrix__`) so this
module stays decoupled from the renderer — `reyn chat --cui` falls back
to a friendly inline message.
"""
from __future__ import annotations

from reyn.chat.outbox import OutboxMessage
from reyn.slash import slash


@slash("matrix", summary="Wake up, Neo.", hidden=True)
async def matrix_cmd(session: "object", args: str) -> None:
    await session._put_outbox(OutboxMessage(
        kind="__matrix__",
        text="There is no spoon.",
    ))
