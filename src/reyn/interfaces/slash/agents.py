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


def _attach_completer(source: "object", arg_partial: str = "") -> list[str]:
    """Return known agent names for tab completion.

    ``source`` is a ``CompletionSourceSnapshot | None`` (#5044) — a plain
    value, never a live ``Session``; :attr:`~reyn.interfaces.repl.
    read_model.CompletionSourceSnapshot.agent_names` is already the
    ``list_active_names()`` result (#1954: hide archived agents).

    Accepts ``arg_partial`` for forward-compat with the CompleterFn
    signature evolution (multi-arg commands like ``/tasks`` need it) —
    ``/attach`` itself is single-arg so the partial is unused.
    """
    agent_names = getattr(source, "agent_names", None)
    return list(agent_names) if agent_names is not None else []


@slash("agents", summary="List all agents (* = attached, · = loaded)", locus="session")
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
    locus="connection",
    usage="/attach <name>",
    completer=_attach_completer,
    see_also=("docs/concepts/multi-agent/multi-agent.md",),
)
async def attach_cmd(ctx: "SlashContext", args: str) -> None:
    """``/attach <name>`` — request the client switch to a different agent.

    This handler asks ``ClientTransport.request_attach`` to perform the
    swap (#4534 PR-2) — a typed request to whichever transport holds the
    session (local: direct ``registry.attach``; remote: a wire call the
    server executes), not a display-channel sentinel a REPL loop has to
    specially detect.

    #5096 ②, architect ruling (issuecomment-5379623427/5379638878/
    5379657592): ``locus="connection"`` — this handler MUST NOT read
    ``ctx.session`` (``maybe_dispatch_slash`` hands it ``None`` for a
    connection-locus command, and the whole point of that locus is that
    the client interprets ``/attach`` and calls the typed op DIRECTLY,
    never forwarding to server-side slash dispatch, where a
    ``SessionBoundTransport`` cannot correctly answer "attach a different
    agent" at all). The pre-#5096 revision validated the target name
    against ``ctx.session._registry`` first (existence check,
    already-attached check) for nicer replies — that validation is now
    the SERVER's job, inside ``request_attach``'s own typed-op handler
    (``endpoint.py``'s ``attach_request`` arm, and ``registry.attach``
    locally), which already performs the equivalent check. This handler's
    own reply is now driven ENTIRELY by the boolean result.

    The reply is ordered AFTER the call and reads its return (lead-coder
    review, #4534 remainder): ``request_attach``'s ``False`` is ambiguous
    by transport (AG-UI's is "unknown" — an unconfirmed remote ack;
    in-process's is a definitive local failure/not-found/already-there),
    so the reply on ``False`` says "could not confirm", never "failed" —
    the wording that would be true for one transport and wrong for the
    other.
    """
    name = args.strip()
    if not name:
        await reply_error(ctx, "usage: /attach <name>")
        return
    attached = await ctx.transport.request_attach(name)
    if attached:
        await reply(ctx, f"attached to {name!r}")
    else:
        await reply(ctx, f"could not confirm attach to {name!r}")
