"""/expand — show full content of the last folded agent reply.

Sends a sentinel OutboxMessage (kind ``__expand_last_reply__``) that the TUI
app intercepts to unfold / reveal the most recent folded agent message.
Falls back gracefully in ``--cui`` mode (the sentinel is simply ignored).

Usage::

    /expand          # expand the last folded reply
"""
from __future__ import annotations

from reyn.chat.outbox import OutboxMessage
from reyn.chat.slash import slash


@slash("expand", summary="Show full content of the last folded agent reply")
async def expand_cmd(session: "object", args: str) -> None:
    await session._put_outbox(OutboxMessage(kind="__expand_last_reply__", text=""))
