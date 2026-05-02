"""/agents, /attach slash commands."""
from __future__ import annotations

from reyn.chat.slash import REGISTRY, SlashCommand


async def _handle_agents(session: "object", args: str) -> None:
    await session._slash_agents(args)


async def _handle_attach(session: "object", args: str) -> None:
    await session._slash_attach(args)


def _attach_completer(session: "object") -> list[str]:
    """Return known agent names for tab completion."""
    if getattr(session, "_registry", None) is None:
        return []
    return session._registry.list_names()


REGISTRY.register(SlashCommand(
    name="agents",
    summary="List all agents (* = attached, · = loaded)",
    handler=_handle_agents,
))

REGISTRY.register(SlashCommand(
    name="attach",
    summary="Switch attached agent: /attach <name>",
    handler=_handle_attach,
    completer=_attach_completer,
))
