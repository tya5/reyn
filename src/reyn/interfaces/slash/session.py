"""``/session`` — per-agent conversation session control (FP-0043 Stage 4a).

Makes multi-session usable end-to-end in the REPL: open a second conversation
under the attached agent, switch focus between them, and list them. The
structural substrate (N Sessions per Agent, keyed by session-id) landed in
Stage 3; this is the REPL wiring.

  /session new          → open a new session under the attached agent (shared
                          identity); prints the new session-id.
  /session switch <sid> → focus another session of the attached agent. Routed
                          through the registry forwarder (like ``/attach``) so
                          the focus flip + display re-wire are sequenced, not
                          raced against the output loop.
  /session list         → list the attached agent's sessions (``*`` = focused).

Byte-identical when unused: a session that never runs ``/session`` keeps the
single implicit ``"main"`` session = current single-session behaviour. Inbound
routing for non-REPL transports (web / A2A) is Stage 4b.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from reyn.chat.outbox import OutboxMessage
from reyn.interfaces.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.chat.session import ChatSession

_USAGE = "usage: /session new | /session switch <sid> | /session list"


@slash(
    "session",
    summary="Open / switch / list conversation sessions for the attached agent",
    usage="/session new | /session switch <sid> | /session list",
    see_also=("docs/concepts/multi-agent/multi-agent.md",),
)
async def session_cmd(session: "ChatSession", args: str) -> None:
    """``/session <new|switch <sid>|list>`` — per-agent multi-session control."""
    reg = session._registry
    if reg is None:
        await reply_error(session, "/session needs a multi-agent registry session")
        return
    name = reg.attached_name
    if name is None:
        await reply_error(session, "no agent attached")
        return

    parts = args.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub == "new":
        try:
            sid = reg.spawn_session(name)
        except ValueError as exc:  # dup id (spawn_session guards)
            await reply_error(session, str(exc))
            return
        await reply(session, f"opened session {sid!r} — /session switch {sid} to focus it")
        return

    if sub == "switch":
        if not rest:
            await reply_error(session, _USAGE)
            return
        if reg.get_session(name, rest) is None:
            await reply_error(
                session, f"no session {rest!r} for {name!r}; try /session list"
            )
            return
        # Visible breadcrumb; the actual focus flip is driven by the sentinel
        # below (the registry forwarder consumes it → attach_session), mirroring
        # /attach so display re-wiring is sequenced on the registry side.
        await reply(session, f"switching to session {rest!r}")
        await session._put_outbox(OutboxMessage(
            kind="__session_switch_request__", text=rest,
        ))
        return

    if sub == "list":
        sids = reg.session_ids(name)
        if not sids:
            await reply(session, f"no sessions loaded for {name!r}")
            return
        focused = reg.attached_sid
        lines = [f"  {'*' if s == focused else ' '} {s}" for s in sids]
        await reply(session, f"sessions for {name!r}:\n" + "\n".join(lines))
        return

    await reply_error(session, _USAGE)
