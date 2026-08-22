"""``/session`` — per-agent conversation session control (FP-0043 Stage 4a).

Makes multi-session usable end-to-end in the REPL: open a second conversation
under the attached agent, switch focus between them, and list them. The
structural substrate (N Sessions per Agent, keyed by session-id) landed in
Stage 3; this is the REPL wiring.

  /session new          → open a new session under the attached agent (shared
                          identity); prints the new session-id. #3562: the new
                          session is born INSIDE the invoking session's own
                          per-session capability narrowing (#2103-S1a), composed
                          the same way the three sibling spawn sites compose
                          theirs, and the reply names what it inherited.
  /session switch <sid> → focus another session of the attached agent, via
                          ``ClientTransport.request_session_switch`` (#4534
                          PR-2b — a typed request, not a display-channel
                          sentinel a forwarder has to specially detect).
  /session list         → list the attached agent's sessions (``*`` = focused).

Byte-identical when unused: a session that never runs ``/session`` keeps the
single implicit ``"main"`` session = current single-session behaviour. Inbound
routing for non-REPL transports (web / A2A) is Stage 4b.
"""
from __future__ import annotations

from reyn.interfaces.slash import Locus, SlashContext, reply, reply_error, slash
from reyn.runtime.spawn_routing import ReviewedNA
from reyn.security.permissions.capability_profile import compose_narrowing_mappings

_USAGE = "usage: /session new | /session switch <sid> | /session list"

#: How many denied capability names the inherited-restriction line spells out before
#: it stops and just counts the rest — the line is an orientation aid in a REPL reply,
#: not the full census (``/visibility`` renders that).
_NAMED_DENIALS = 5


def _inherited_restriction_lines(reg, name: str, sid: str) -> "list[str]":
    """The operator-facing explanation of what a just-spawned session inherited.

    #3562 (architect's in-PR requirement): with ``allow ∩`` composed uniformly, a
    ``/session new`` reached from a NARROWED invoker can open a session with few or
    even zero usable capabilities. That is the safe direction, but silent — the
    operator sees a new session id and no reason for anything that later refuses. This
    reads the child's own ``capability_visibility_state()`` (the existing #2285/#3378
    read model — no new mechanism) and says so at spawn time.

    ⚠️ Explanation, NOT enforcement, and deliberately not the witness that inheritance
    works: ``capability_visibility_state`` re-resolves with the sid on every read, and
    was GREEN on the broken code #3561 fixed, where the same envelope was resolvable
    but not enforced. What proves the inheritance is a denied tool's side effect not
    happening (``tests/runtime/test_3562_slash_session_new_narrowing_inheritance.py``); this
    surface has its own separate test, for its own separate claim.

    Empty when the child session is not retrievable (nothing truthful to say).

    #3615 — ``envelope_unknown``: when the child's read model could not resolve an
    envelope to test against (no registry back-reference), ``denied_by_envelope`` and
    the tool count in ``authorized`` are BOTH silent about it (the former is
    legitimately empty — nothing could be tested — and the latter is legitimately
    zero, since #3615 moves ungated rows to ``unknown`` rather than ``authorized``).
    Read at face value, that pair says "nothing denied, no tools left", which this
    function would previously have reported as "no tools remain available in it" —
    a confident wrong answer for a session whose report is simply unconfirmed, not
    one that was actually narrowed to nothing. Checked first and reported honestly
    instead."""
    child = reg.get_session(name, sid)
    if child is None:
        return []
    state = child.capability_visibility_state()
    if state.get("envelope_unknown"):
        return [
            "  ↳ this session's capability report could not be confirmed (no "
            "envelope source); inherited narrowing may not be reflected"
        ]
    denied = sorted(row["name"] for row in state["denied_by_envelope"])
    tools_left = sum(1 for row in state["authorized"] if row["kind"] == "tool")
    lines: "list[str]" = []
    if denied:
        shown = ", ".join(denied[:_NAMED_DENIALS])
        more = len(denied) - _NAMED_DENIALS
        # Says two separate true things rather than one convenient one: the child
        # inherited this session's narrowing (a fact about what was passed), and its
        # envelope denies these capabilities (a fact read off the child). It does NOT
        # claim every denial listed is DUE to the inheritance — ``denied_by_envelope``
        # also carries the agent's own name-keyed layers, which the child would have had
        # either way, and attributing those to the operator's narrowing would be a
        # confident wrong answer to "why is this denied?".
        lines.append(
            f"  ↳ inherited this session's capability narrowing; the new session's "
            f"envelope denies {len(denied)}: {shown}"
            + (f", +{more} more" if more > 0 else "")
        )
    if not tools_left:
        lines.append(
            "  ↳ no tools remain available in it; open the session from a session "
            "that is not narrowed, or lift the narrowing, if that was not intended"
        )
    return lines


def _session_locus(args: str) -> "Locus":
    """#5096 ②: ``/session``'s 3 sub-commands need different loci in ONE
    registration (architect ruling: locus attaches to the EXECUTION UNIT,
    not the registration name) — a ``LocusFn`` instead of a bare ``Locus``.

    ``switch`` — answerable with ONLY ``ctx.transport.request_session_
    switch``, a registry/connection-level typed op; never reads
    ``ctx.session`` (see the handler body below, restructured to check
    ``sub == "switch"`` BEFORE any ``ctx.session`` access — a "connection"
    verdict here that the handler still touched ``ctx.session`` for would
    crash on ``session=None``, not merely mis-declare). → ``connection``.

    ``new`` — reads ``ctx.session.agent_name``/``ctx.session.session_id``,
    the CALLING session's own identity (not just "which agent is
    attached" — the registry alone cannot answer "which specific session
    is asking", needed for #2103-S1a narrowing inheritance). → ``session``.

    ``list`` — #5099 closes the remainder ``_session_locus``'s own prior
    revision named: ``ClientTransport.request_session_list`` is now that
    typed op (mirrors ``request_artifact_list``'s "client interprets,
    server executes a named operation" shape) — answerable via the
    registry alone (``reg.session_ids``/``reg.attached_sid``, no session-
    specific value), reached without ``ctx.session``. → ``connection``.

    Unknown/empty sub falls through to ``session`` (the existing ``reg``
    derivation produces the usage error either way).

    ⚠️ Not a structural check (architect co-vet, issuecomment-5379756281):
    an unregistered sub silently defaults to ``session`` rather than
    failing to construct — a new sub that genuinely needs ``connection``
    must be added to this branch by hand. The default lands on the
    higher-capability side (a usage error, not a crash on
    ``session=None``), so this is non-blocking, but it is not the closed
    per-execution-unit registry #5096 ② otherwise achieves; closing that
    gap needs sub-command registration, out of this PR's scope —
    remainder noted alongside #5099."""
    sub = args.strip().split(maxsplit=1)[0].lower() if args.strip() else ""
    return "connection" if sub in ("switch", "list") else "session"


@slash(
    "session",
    summary="Open / switch / list conversation sessions for the attached agent",
    locus=_session_locus,
    usage="/session new | /session switch <sid> | /session list",
    see_also=("docs/concepts/multi-agent/multi-agent.md",),
)
async def session_cmd(ctx: "SlashContext", args: str) -> None:
    """``/session <new|switch <sid>|list>`` — per-agent multi-session control."""
    parts = args.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub == "switch":
        # #5096 ②: connection locus (see _session_locus above) -- MUST NOT
        # read ctx.session, which is None here.
        if not rest:
            await reply_error(ctx, _USAGE)
            return
        # #5099: client-side pre-validation, restored via the new
        # request_session_list typed op -- richer than #5096's temporary
        # "could not confirm the switch" (names the bad sid AND every
        # accepted one), closer to the pre-#5096 server-side existence
        # check's own guidance than that interim message was. Only fires
        # when the list itself came back non-empty: an EMPTY result has
        # exactly one meaning now (request_session_list is @abstractmethod,
        # #5076 -- no transport is left able to decline the query, architect
        # co-vet issuecomment-5379990811) -- nothing is currently attached/
        # loaded to list. There is nothing useful to enumerate in that case
        # (no accepted sids to name), so falling through to
        # request_session_switch and reading ITS answer is the honest
        # degrade rather than asserting "no such session" with an empty
        # accepted-list that would just read as a bug.
        sids = [entry["sid"] for entry in await ctx.transport.request_session_list()]
        if sids and rest not in sids:
            await reply_error(
                ctx, f"no such session {rest!r}; sessions are: {', '.join(sids)}",
            )
            return
        # #4534 PR-2b: the actual focus flip goes through the named-operation
        # seam (request_session_switch -> registry.attach_session), not the
        # retired __session_switch_request__ display-channel sentinel — see
        # ClientTransport.request_session_switch's own docstring for why.
        # False IS still routed as an error (not a system note, unlike
        # /attach's own "could not confirm"): the sid the USER TYPED is
        # available here without touching ctx.session (it's `rest`,
        # client-side input). This branch is reached only when the
        # pre-validation above could not rule `rest` out (list unsupported,
        # or a race dropped the session between the two calls), so the
        # message stays honest about not knowing why.
        switched = await ctx.transport.request_session_switch(rest)
        if switched:
            await reply(ctx, f"switching to session {rest!r}")
        else:
            await reply_error(ctx, f"could not confirm the switch to session {rest!r}")
        return

    if sub == "list":
        # #5099: connection locus (see _session_locus above) -- MUST NOT
        # read ctx.session, which is None here. The registry-level read
        # this used to do directly (reg.session_ids/reg.attached_sid) now
        # goes through the same typed op `switch`'s pre-validation above
        # uses — one seam, one source of truth for both consumers.
        #
        # Disclosed simplification: the pre-#5099 reply named the attached
        # AGENT ("sessions for 'default':"); no ClientTransport op reads
        # that at connection locus today (AgUiTransport's own attached
        # agent is scoped into the connection URL, not exposed generically
        # on the base seam), and inventing one is out of this PR's scope
        # (lead-coder ruling — see request_session_list's own docstring
        # for the #4996/#5093-family reasoning already applied tonight).
        # Dropped rather than faked.
        sessions = await ctx.transport.request_session_list()
        if not sessions:
            await reply(ctx, "no sessions loaded")
            return
        lines = [
            f"  {'*' if entry['attached'] else ' '} {entry['sid']}"
            for entry in sessions
        ]
        await reply(ctx, "sessions:\n" + "\n".join(lines))
        return

    reg = ctx.session._registry
    if reg is None:
        await reply_error(ctx, "/session needs a multi-agent registry session")
        return
    name = reg.attached_name
    if name is None:
        await reply_error(ctx, "no agent attached")
        return

    if sub == "new":
        # #3562: the child is born under the ATTACHED agent, and the layer that decides
        # its capability envelope is keyed by (agent, sid) — so the INVOKING session's
        # #2103-S1a narrowing has to be carried explicitly or the child is born wider
        # than whoever asked for it. The name-keyed layers (the agent's permissions
        # declaration, topology capability_profile bindings, the #2081 _delegate floor)
        # ride along for free; the #2285 /visibility toggle and the #1827-S4b ephemeral
        # untrusted-context narrowing are deliberately not carried, same as the three
        # sibling spawn sites (see test_3546's module docstring, layers 3 and 4).
        #
        # ★ WHY, since the answer changed under this code: an OWNER POLICY decision —
        # if an operator has switched a capability off for their session and then opens
        # a new one from it, that capability stays off. Operator intent persists across
        # the spawn. The gap was originally argued as a containment one (#3561 had
        # measured that model output reached this site, so an un-narrowed child was an
        # escape); #3595 step 1 ruled that reachability a defect and closed it, which
        # retires the containment argument WITHOUT touching this one — the policy claim
        # never depended on who could reach the command.
        #
        # The composition rule is the siblings' rule applied UNIFORMLY (deny ∪, allow ∩,
        # an absent allow = ⊤) — this site imposes nothing of its own, so the child term
        # is None and the invoker's mapping stands. There is deliberately NO branch for
        # the case where the invoking session's agent differs from the attached one:
        # ``name`` is ``reg.attached_name``, so on the operator path the caller IS the
        # attach target and the identities coincide. A branch would be a lenient special
        # case for exactly the caller a uniform restrict-only rule exists to bound.
        parent_narrowing = reg.per_session_narrowing(
            ctx.session.agent_name, ctx.session.session_id,
        )
        inherited = compose_narrowing_mappings(parent_narrowing, None)
        try:
            # #2708 P3-item3: /session new opens a real attachable conversation session — the user
            # /session switches to focus + drain it; self-binding to the factory default is reviewed-NA.
            _routing = ReviewedNA("interfaces/slash/session.py::session_cmd")
            sid = reg.spawn_session(
                name,
                presentation_consumer=_routing.presentation_consumer,
                intervention_bridge=_routing.intervention_bridge,
                narrowing=inherited,
            )
        except ValueError as exc:  # dup id (spawn_session guards)
            await reply_error(ctx, str(exc))
            return
        lines = [f"opened session {sid!r} — /session switch {sid} to focus it"]
        if inherited:
            lines.extend(_inherited_restriction_lines(reg, name, sid))
        await reply(ctx, "\n".join(lines))
        return

    await reply_error(ctx, _USAGE)
