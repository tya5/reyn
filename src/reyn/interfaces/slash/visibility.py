"""``/visibility`` — toggle this session's LLM tool-visibility (#2285).

The status-bar rows submit this command; it maps 1:1 to ``Session.set_capability_visible``. Hiding a
capability removes it from the LLM catalog on the next turn; showing it restores it — but only UP TO
the agent's authorized envelope (toggling ON a capability the envelope denies is a no-op; the
re-resolve-from-base gate stops at the envelope, so ``visible ⊆ authorized`` always holds).
Session-scoped (this session only); live next turn; not persisted (step1).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from reyn.interfaces.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.runtime.session import Session

_KINDS = ("tool", "skill", "mcp", "category")


@slash(
    "visibility",
    summary="Toggle this session's visibility of a tool / skill / mcp / category",
    usage="/visibility on|off <tool|skill|mcp|category> <name>",
)
async def visibility_cmd(session: "Session", args: str) -> None:
    """``/visibility on|off <kind> <name>`` — hide/show a capability from the LLM for THIS session.

    ``off`` hides it next turn; ``on`` restores it (up to the agent envelope — an envelope-denied
    capability stays hidden). Kind ∈ tool / skill / mcp / category."""
    parts = args.split()
    if len(parts) != 3 or parts[0] not in ("on", "off") or parts[1] not in _KINDS:
        await reply_error(
            session, "usage: /visibility on|off <tool|skill|mcp|category> <name>",
        )
        return
    on = parts[0] == "on"
    kind, name = parts[1], parts[2]
    setter = getattr(session, "set_capability_visible", None)
    if setter is None:
        await reply_error(session, "visibility toggle is not available in this session")
        return
    setter(kind, name, on)
    await reply(
        session,
        f"{kind} {name!r} is now {'visible' if on else 'hidden'} for this session "
        f"(applies next turn; session-scoped, not persisted)",
    )
