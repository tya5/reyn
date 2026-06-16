"""/agent slash command — agent lifecycle from chat.

Sub-commands:
  /agent new <name>          — create a new agent and attach to it
  /agent edit role <text>    — replace the attached agent's role text;
                                next turn picks up the new role.

Removal / rename are intentionally NOT here yet — the registry's
``remove()`` exists but cascade safety (= topology references,
in-flight tasks, history continuity on rename) deserves explicit
confirmation UX that isn't a one-liner. Filing as follow-ups if
surfaced.
"""
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from reyn.chat.outbox import OutboxMessage
from reyn.chat.profile import AgentProfile
from reyn.chat.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.chat.session import ChatSession


_USAGE = (
    "Usage:\n"
    "  /agent new <name>          — create a new agent and attach to it\n"
    "                                (name: 1-32 chars of [a-z0-9_-], "
    "starting with [a-z0-9])\n"
    "  /agent edit role <text>    — replace the attached agent's role text"
)
_NO_REGISTRY = (
    "agent registry not wired; /agent only works in `reyn chat`"
)


@slash(
    "agent",
    summary=(
        "Agent lifecycle: new <name> / edit role <text> "
        "(rm via `reyn agent rm`)"
    ),
    usage="/agent new <name> | /agent edit role <text>",
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
    elif sub == "edit":
        await _edit_agent(session, sub_args)
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


async def _edit_agent(session: "ChatSession", args: str) -> None:
    """Dispatch ``/agent edit <field> <value>`` — currently ``role`` only.

    Edits operate on the **attached agent** (= ``session.agent_name``).
    Cross-agent edit (= "edit role on some-other-agent") would need a
    name argument and clearer mental model; deferred.
    """
    parts = args.strip().split(maxsplit=1)
    if not parts:
        await reply_error(session, "Usage: /agent edit role <text>")
        return
    field = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    if field == "role":
        await _edit_role(session, rest)
    else:
        await reply_error(
            session,
            f"unknown edit field {field!r}; only `role` is supported.",
        )


async def _edit_role(session: "ChatSession", new_role: str) -> None:
    """Replace the attached agent's role text on disk + in-memory.

    Two-side update:
      1. ``profile.yaml`` rewritten via ``AgentProfile.save`` so the
         change survives restart.
      2. ``session._agent_role`` mutated so the next router turn picks
         up the new role without restart (= consumed at Agent
         construction time per line ~2655 in session.py).
    """
    new_role = new_role.strip()
    if not new_role:
        await reply_error(
            session,
            "Usage: /agent edit role <text>  (text must be non-empty; "
            "clearing the role intentionally is not yet supported)",
        )
        return

    registry = session._registry
    if registry is None:
        await reply_error(session, _NO_REGISTRY)
        return

    name = session.agent_name
    agent_dir = registry._dir / name
    try:
        profile = AgentProfile.load(agent_dir)
    except FileNotFoundError:
        await reply_error(
            session,
            f"profile for agent {name!r} not found at {agent_dir}/profile.yaml",
        )
        return

    updated = replace(profile, role=new_role)
    try:
        updated.save(agent_dir)
    except OSError as exc:
        await reply_error(session, f"failed to save profile: {exc}")
        return

    # Mutate in-memory so the next turn's Agent construction picks up
    # the new role (= session._agent_role is read at
    # ``_construct_agent`` time, not cached on a prompt object).
    session._agent_role = new_role

    await reply(
        session,
        f"✓ Updated agent {name!r} role.\n  new role: {new_role}\n"
        "Next user turn will use the new role.",
    )
