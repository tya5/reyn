"""``/hook`` — toggle this session's applicability of a hook (#2285).

The status-bar hook rows submit this command; it maps 1:1 to ``Session.set_hook_enabled``. Disabling
a hook skips it at dispatch for THIS session (live, next dispatch) — session-scoped, so a hook
disabled here still fires in the agent's other sessions. Persists across restart (step2).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from reyn.interfaces.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.runtime.session import Session


@slash(
    "hook",
    summary="Enable/disable a hook for this session",
    usage="/hook on|off <name>",
)
async def hook_cmd(session: "Session", args: str) -> None:
    """``/hook on|off <name>`` — enable/disable a named hook for THIS session (live next dispatch)."""
    parts = args.split()
    if len(parts) != 2 or parts[0] not in ("on", "off"):
        await reply_error(session, "usage: /hook on|off <name>")
        return
    on, name = parts[0] == "on", parts[1]
    setter = getattr(session, "set_hook_enabled", None)
    if setter is None:
        await reply_error(session, "hook toggle is not available in this session")
        return
    setter(name, on)
    await reply(
        session,
        f"hook {name!r} is now {'enabled' if on else 'disabled'} for this session "
        f"(applies next dispatch; session-scoped; persists across restart)",
    )
