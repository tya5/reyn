"""/list, /cancel, /answer slash commands."""
from __future__ import annotations

from reyn.chat.slash import slash


@slash("list", summary="List running skills and pending interventions")
async def list_cmd(session: "object", args: str) -> None:
    await session._slash_list(args)


@slash("cancel", summary="Cancel a running skill: /cancel <id-prefix>")
async def cancel_cmd(session: "object", args: str) -> None:
    await session._slash_cancel(args)


@slash("answer", summary="Answer a pending intervention: /answer <id-prefix> <text>")
async def answer_cmd(session: "object", args: str) -> None:
    await session._slash_answer(args)
