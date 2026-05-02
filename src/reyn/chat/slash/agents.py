"""/agents, /attach slash commands."""
from __future__ import annotations

from reyn.chat.slash import slash


def _attach_completer(session: "object") -> list[str]:
    """Return known agent names for tab completion."""
    if getattr(session, "_registry", None) is None:
        return []
    return session._registry.list_names()


@slash("agents", summary="List all agents (* = attached, · = loaded)")
async def agents_cmd(session: "object", args: str) -> None:
    await session._slash_agents(args)


@slash(
    "attach",
    summary="Switch attached agent: /attach <name>",
    completer=_attach_completer,
)
async def attach_cmd(session: "object", args: str) -> None:
    await session._slash_attach(args)
