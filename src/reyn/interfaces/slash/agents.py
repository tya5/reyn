"""/agents and /attach slash commands.

Migrated out of ``session.py`` per the cli-redesign plan (`docs/deep-dives/
contributing/cli-redesign.md`). The session still owns the AgentRegistry
reference; the actual swap runs through ``ClientTransport.request_attach``
(#4534 PR-2, retired the ``__attach_request__`` display-channel sentinel) —
this module validates the target and asks the transport to attach.
"""
from __future__ import annotations

from reyn.interfaces.slash import SlashContext, reply, reply_error, slash

_NO_REGISTRY_AGENTS = (
    "agent registry not wired; /agents only works in `reyn chat`"
)
_NO_REGISTRY_ATTACH = (
    "agent registry not wired; /attach only works in `reyn chat`"
)


def _attach_completer(session: "object", arg_partial: str = "") -> list[str]:
    """Return known agent names for tab completion.

    Accepts ``arg_partial`` for forward-compat with the CompleterFn
    signature evolution (multi-arg commands like ``/tasks`` need it) —
    ``/attach`` itself is single-arg so the partial is unused.
    """
    if getattr(session, "_registry", None) is None:
        return []
    return session._registry.list_active_names()  # #1954: hide archived agents


@slash("agents", summary="List all agents (* = attached, · = loaded)")
async def agents_cmd(ctx: "SlashContext", args: str) -> None:
    """``/agents`` — list known agents with attach / loaded markers."""
    if ctx.session._registry is None:
        await reply_error(ctx, _NO_REGISTRY_AGENTS)
        return
    names = ctx.session._registry.list_active_names()  # #1954: hide archived agents
    if not names:
        # Default agent auto-creates on first chat start, so an empty list
        # is unexpected — surface as system note rather than swallowing.
        await reply(
            ctx,
            "no agents (this should not happen — default auto-creates)",
        )
        return
    attached = ctx.session._registry.attached_name
    loaded = set(ctx.session._registry.loaded_names())
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
            profile = ctx.session._registry.load_profile(n)
            role_excerpt = (profile.role or "").strip().splitlines()
            role = role_excerpt[0] if role_excerpt else ""
        except Exception:
            role = "(profile load failed)"
        last = ctx.session._registry.last_activity_at(n)
        if last is None:
            last_str = "—"
        elif last.date() == today:
            last_str = last.strftime("%H:%M")
        else:
            last_str = last.strftime("%Y-%m-%d %H:%M")
        mark = "*" if n == attached else (" " if n not in loaded else "·")
        lines.append(f"  {mark} {n:<24} {last_str:<17} {role[:60]}")
    await reply(ctx, "\n".join(lines))


@slash(
    "attach",
    summary="Switch attached agent",
    usage="/attach <name>",
    completer=_attach_completer,
    see_also=("docs/concepts/multi-agent/multi-agent.md",),
)
async def attach_cmd(ctx: "SlashContext", args: str) -> None:
    """``/attach <name>`` — request the client switch to a different agent.

    This handler validates the name and asks
    ``ClientTransport.request_attach`` to perform the swap (#4534 PR-2) —
    a typed request to whichever transport holds the session (local:
    direct ``registry.attach``; remote: a wire call the server executes),
    not a display-channel sentinel a REPL loop has to specially detect.

    The success reply is ordered AFTER the call and reads its return
    (lead-coder review, #4534 remainder): ``request_attach``'s ``False`` is
    ambiguous by transport (AG-UI's is "unknown" — an unconfirmed remote
    ack; in-process's is a definitive local failure), so the reply on
    ``False`` says "could not confirm", never "failed" — the wording that
    would be true for one transport and wrong for the other.
    """
    name = args.strip()
    if not name:
        await reply_error(ctx, "usage: /attach <name>")
        return
    if ctx.session._registry is None:
        await reply_error(ctx, _NO_REGISTRY_ATTACH)
        return
    if not ctx.session._registry.exists(name):
        # The user is already in the TUI — direct them at the slash form,
        # not the CLI shell command, so they don't have to drop out of
        # chat to create the agent.
        await reply_error(
            ctx,
            f"agent {name!r} not found; use /agent new {name} to create it",
        )
        return
    if name == ctx.session._registry.attached_name:
        await reply(ctx, f"already attached to {name!r}")
        return
    # Surface the switch in the conv pane. Without this, ``/attach``
    # produced no in-pane feedback — the user had to run ``/agents``
    # to confirm the switch happened. The actual attach runs through
    # ClientTransport.request_attach (#4534 PR-2); this reply is a
    # separate, visible breadcrumb, ordered AFTER the call so it reflects
    # what the call actually reports. (The header label refresh is
    # blocked by a separate registry-forwarder bug — see #191.)
    attached = await ctx.transport.request_attach(name)
    if attached:
        await reply(ctx, f"attached to {name!r}")
    else:
        await reply(ctx, f"could not confirm attach to {name!r}")
