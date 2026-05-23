"""/agent slash command — agent lifecycle from chat (currently: create).

Sub-commands (initial scope):
  /agent new <name>          — create a new agent and attach to it

Removal / rename / role-edit are intentionally NOT here yet — the
registry's ``remove()`` exists but cascade safety (= topology
references, in-flight tasks) deserves explicit confirmation UX that
isn't a one-liner. Filing as follow-ups if surfaced.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from reyn.chat.outbox import OutboxMessage
from reyn.chat.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.chat.session import ChatSession


_USAGE = (
    "Usage: /agent new <name>\n"
    "  new <name>   — create a new agent and attach to it\n"
    "                 (name: 1-32 chars of [a-z0-9_-], starting with [a-z0-9])"
)
_NO_REGISTRY = (
    "agent registry not wired; /agent only works in `reyn chat`"
)


@slash(
    "agent",
    summary="Create new agent",
    usage="/agent new <name>",
)
async def agent_cmd(session: "ChatSession", args: str) -> None:
    """Dispatch ``/agent <sub>`` subcommands."""
    parts = args.strip().split(maxsplit=1)
    if not parts:
        await reply(session, _USAGE)
        return
    sub = parts[0]
    sub_args = parts[1] if len(parts) > 1 else ""
    if sub == "new":
        await _create_agent(session, sub_args)
    else:
        await reply_error(session, _USAGE)


async def _create_agent(session: "ChatSession", name: str) -> None:
    """Create a new agent profile and attach to it.

    Uses the same ``__attach_request__`` sentinel as ``/attach`` so the
    registry's forwarder picks up the switch (until #191 lands, the
    header label won't refresh — same constraint as /attach).
    """
    name = name.strip()
    if not name:
        await reply_error(session, "Usage: /agent new <name>")
        return
    if session._registry is None:
        await reply_error(session, _NO_REGISTRY)
        return
    try:
        session._registry.create(name)
    except FileExistsError:
        await reply_error(
            session,
            f"agent {name!r} already exists; use /attach {name} instead",
        )
        return
    except ValueError as exc:
        # _validate_agent_name raises with the rule embedded — surface
        # verbatim so the user sees exactly what's wrong with the name.
        await reply_error(session, str(exc))
        return
    await reply(session, f"created agent {name!r}; attaching…")
    await session._put_outbox(OutboxMessage(
        kind="__attach_request__", text=name,
    ))
