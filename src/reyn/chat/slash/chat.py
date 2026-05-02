"""/list, /cancel, /answer slash commands."""
from __future__ import annotations

from reyn.chat.slash import REGISTRY, SlashCommand


async def _handle_list(session: "object", args: str) -> None:
    """Implementation delegated to the session method for now."""
    await session._slash_list(args)


async def _handle_cancel(session: "object", args: str) -> None:
    await session._slash_cancel(args)


async def _handle_answer(session: "object", args: str) -> None:
    await session._slash_answer(args)


REGISTRY.register(SlashCommand(
    name="list",
    summary="List running skills and pending interventions",
    handler=_handle_list,
))

REGISTRY.register(SlashCommand(
    name="cancel",
    summary="Cancel a running skill: /cancel <id-prefix>",
    handler=_handle_cancel,
))

REGISTRY.register(SlashCommand(
    name="answer",
    summary="Answer a pending intervention: /answer <id-prefix> <text>",
    handler=_handle_answer,
))
