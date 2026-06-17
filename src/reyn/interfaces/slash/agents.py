"""/agents and /attach slash commands.

Migrated out of ``session.py`` per the cli-redesign plan (`docs/deep-dives/
contributing/cli-redesign.md`). The session still owns the AgentRegistry
reference and the REPL listens for ``__attach_request__`` outbox messages
to perform the actual swap; this module just turns user input into the
right outbox shape.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from reyn.chat.outbox import OutboxMessage
from reyn.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.chat.session import ChatSession


_NO_REGISTRY_AGENTS = (
    "agent registry not wired; /agents only works in `reyn chat`"
)
_NO_REGISTRY_ATTACH = (
    "agent registry not wired; /attach only works in `reyn chat`"
)


def _attach_completer(session: "object", arg_partial: str = "") -> list[str]:
    """Return known agent names for tab completion.

    Accepts ``arg_partial`` for forward-compat with the CompleterFn
    signature evolution (multi-arg commands like ``/plan`` need it) —
    ``/attach`` itself is single-arg so the partial is unused.
    """
    if getattr(session, "_registry", None) is None:
        return []
    return session._registry.list_names()


@slash("agents", summary="List all agents (* = attached, · = loaded)")
async def agents_cmd(session: "ChatSession", args: str) -> None:
    """``/agents`` — list known agents with attach / loaded markers."""
    if session._registry is None:
        await reply_error(session, _NO_REGISTRY_AGENTS)
        return
    names = session._registry.list_names()
    if not names:
        # Default agent auto-creates on first chat start, so an empty list
        # is unexpected — surface as system note rather than swallowing.
        await reply(
            session,
            "no agents (this should not happen — default auto-creates)",
        )
        return
    attached = session._registry.attached_name
    loaded = set(session._registry.loaded_names())
    # Header with column labels + legend. Compact ``HH:MM`` for today's
    # activity (vs full ``YYYY-MM-DDTHH:MM`` for older entries) keeps the
    # column readable when most agents were active in the current session.
    from datetime import date as _date

    today = _date.today()
    lines = [
        "agents:  (* = attached, · = loaded, blank = not yet loaded)",
        f"    {'name':<24} {'last_active':<17} role",
    ]
    for n in names:
        try:
            profile = session._registry.load_profile(n)
            role_excerpt = (profile.role or "").strip().splitlines()
            role = role_excerpt[0] if role_excerpt else ""
        except Exception:
            role = "(profile load failed)"
        last = session._registry.last_activity_at(n)
        if last is None:
            last_str = "—"
        elif last.date() == today:
            last_str = last.strftime("%H:%M")
        else:
            last_str = last.strftime("%Y-%m-%d %H:%M")
        mark = "*" if n == attached else (" " if n not in loaded else "·")
        lines.append(f"  {mark} {n:<24} {last_str:<17} {role[:60]}")
    await reply(session, "\n".join(lines))


@slash(
    "attach",
    summary="Switch attached agent",
    usage="/attach <name>",
    completer=_attach_completer,
    see_also=("docs/concepts/multi-agent/multi-agent.md",),
)
async def attach_cmd(session: "ChatSession", args: str) -> None:
    """``/attach <name>`` — request the REPL switch to a different agent.

    The actual switch happens in ``repl._input_loop`` (which owns the
    display wiring). This handler just validates the name and posts a
    ``__attach_request__`` outbox message; the REPL listens for that
    kind and performs the swap.
    """
    name = args.strip()
    if not name:
        await reply_error(session, "usage: /attach <name>")
        return
    if session._registry is None:
        await reply_error(session, _NO_REGISTRY_ATTACH)
        return
    if not session._registry.exists(name):
        # The user is already in the TUI — direct them at the slash form,
        # not the CLI shell command, so they don't have to drop out of
        # chat to create the agent.
        await reply_error(
            session,
            f"agent {name!r} not found; use /agent new {name} to create it",
        )
        return
    if name == session._registry.attached_name:
        await reply(session, f"already attached to {name!r}")
        return
    # Surface the switch in the conv pane. Without this, ``/attach``
    # produced no in-pane feedback — the user had to run ``/agents``
    # to confirm the switch happened. The actual attach is still
    # driven by the ``__attach_request__`` sentinel below; this is a
    # separate, visible breadcrumb. (The header label refresh is
    # blocked by a separate registry-forwarder bug — see #191.)
    await reply(session, f"attached to {name!r}")
    # Sentinel kind — see repl._input_loop for the receiver.
    await session._put_outbox(OutboxMessage(
        kind="__attach_request__", text=name,
    ))
