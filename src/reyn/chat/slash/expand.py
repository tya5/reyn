"""/expand — toggle the latest long agent reply (expand/collapse).

Sends a sentinel OutboxMessage (kind ``__expand_last_reply__``) that the TUI
app intercepts to toggle the most recent FoldableMarkdown widget. Repeated
invocations cycle between expanded and collapsed states (= true toggle).
Falls back gracefully in ``--cui`` mode (the sentinel is simply ignored).

Usage::

    /expand          # toggle expand/collapse of the latest long reply
"""
from __future__ import annotations

from reyn.chat.outbox import OutboxMessage
from reyn.chat.slash import slash


@slash("expand", summary="Toggle the latest long agent reply (expand/collapse)")
async def expand_cmd(session: "object", args: str) -> None:
    await session._put_outbox(OutboxMessage(kind="__expand_last_reply__", text=""))
