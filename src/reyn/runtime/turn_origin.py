"""``TurnOrigin`` — the closed vocabulary of INBOX message kinds.

One value answers one question: **who authored the text (or the trigger) that
this turn runs on.** ``Session._run_turn_body`` dispatches on it, and
``Session._handle_user_message`` acts on ``CLIENT_INPUT`` specifically by handing
a ``/``-prefixed line to slash dispatch — so a member is a *claim about
provenance* that the OS then trusts, not a routing hint a producer may pick for
convenience. A producer that cannot truthfully claim an existing member adds one
rather than borrowing the closest.

**Why it is a type and not a ``str``.** Provenance is enumerable, so it is
enumerated. A free ``str`` made the census unbounded: the same claim was spelled
``kind="user"``, ``_put_inbox("user", …)``, ``inbox_kind="user"``, and could be
spelled via a variable or a dict literal, so every attempt to answer "who claims
to be a client input?" answered instead "who matches my pattern?" — the question
was decided by the search, and five separate censuses on #3595 each missed a
different form. With a member there is exactly one spelling, and the question
becomes "who references this symbol", which an AST walk answers completely
(``tests/runtime/test_3595_client_input_provenance_gate.py``).

**Why no member is named ``USER``.** ``kind`` was one word used by two unrelated
vocabularies: this one (inbox, provenance) and the outbox DISPLAY kinds
(``runtime/outbox.py`` — how to render a frame, validated at construction). Their
intersection was exactly one word, ``"user"``, and that word was the whole of the
collision. Naming the inbox member ``CLIENT_INPUT`` makes the intersection of the
two *symbol* sets empty, so reading ``TurnOrigin.CLIENT_INPUT`` at a call site
tells you which namespace you are in without knowing the surrounding code. The
outbox keeps its own vocabulary in its own module and is deliberately untouched:
``OutboxMessage(kind="user", …)`` is a display frame and always was.

**Why the wire value is still ``"user"``.** The member NAME is a source-level
identifier; the VALUE is an on-disk and on-the-wire format. It is written into
the WAL / snapshot inbox (``SnapshotJournal.append_inbox``), read back verbatim
by ``Session.restore_state``, and published as the ``kind`` field of the
``turn_started`` / ``turn_settled`` audit events, which ``.reyn/events`` has
consumers outside reyn for. Changing it would either break restore of a snapshot
written by an older build — a recovery regression, and recovery is a
cross-cutting-band member — or require a legacy-value mapping this change has no
independent reason to add. The renaming that mattered was making the *producers*
that falsely claimed this kind say what they are (#3595 steps 1 and 1b, already
landed); by ``docs/reference/runtime/events.md``'s own wording, ``kind == "user"``
on the audit surface is now TRUE rather than approximate.

``StrEnum`` is what keeps that separation cheap: a member IS its wire string, so
a kind restored from a snapshot as a plain ``str`` still compares equal to the
member, JSON-serialises unchanged, and no consumer needs a conversion step.
Passing a bare ``str`` where a ``TurnOrigin`` is annotated is still a static type
error (``str`` is not a subtype of ``TurnOrigin``), which is the migration
insurance: a producer this arc missed surfaces under a type checker instead of at
runtime.

⚠️ ``StrEnum``, not the ``(str, Enum)`` spelling this repo uses elsewhere
(``tools.transport.Transport``, ``llm.pricing.UsageSource``, …). The two differ
in exactly one place, and it is a place this value reaches: ``(str, Enum)``
inherits ``Enum.__str__``, so ``f"{kind}"`` renders ``"TurnOrigin.CLIENT_INPUT"``
while ``json.dumps`` renders ``"user"``. This kind IS formatted — the live
progress-notification label is ``f"turn: {data.get('kind')}"``
(``core/events/progress_lifecycle.format_progress_message``), reached with the
in-memory payload, so a member that stringifies to its own repr would change an
operator-visible line. ``StrEnum`` restores ``str.__str__``; the interpolated
form and the serialised form are then the same string, which is the property the
"wire value is unchanged" argument above actually rests on.
"""
from __future__ import annotations

from enum import StrEnum


class TurnOrigin(StrEnum):
    """Who authored the inbox message that triggers a turn.

    See the module docstring for why this is a closed type, why no member is
    named ``USER``, and why the wire values are unchanged.
    """

    #: A human typed this line at a first-party client: the TUI composer / plain
    #: CUI (``Session.submit_user_text``), ``reyn run-once``'s stdin, or a
    #: dogfood scenario standing in for an operator.
    #:
    #: ★ The ONLY kind whose text reaches slash dispatch — ``/reset`` in a
    #: message of any other kind is text, not a command. That is what makes this
    #: member a trust decision rather than a label, and why every site in
    #: ``src/`` that names it is enumerated with its reason in
    #: ``tests/runtime/test_3595_client_input_provenance_gate.py``. Four producers that
    #: were not that human claimed it until #3595 steps 1 / 1b: a pipeline agent
    #: step's prompt (model-authored), a chat webhook, an MCP / A2A peer, and a
    #: cron fire.
    CLIENT_INPUT = "user"

    #: A sub-agent request pulled from another session (inter-agent messaging).
    AGENT_REQUEST = "agent_request"

    #: A sub-agent's reply arriving back at the originating session.
    AGENT_RESPONSE = "agent_response"

    #: An async pipeline driver's terminal result (IS-2), posted as a fresh turn
    #: — the ``AGENT_RESPONSE`` mirror, but chainless, because the launch
    #: returned immediately.
    PIPELINE_RESULT = "pipeline_result"

    #: A pipeline ``agent`` step's prompt (#3595 step 1): text a MODEL will read
    #: as an ephemeral worker's one turn, never an operator's typed line. Under
    #: the old ``CLIENT_INPUT`` claim every registered slash command was
    #: executable from model output; under its own member the prompt reaches the
    #: turn body directly and none of them are.
    AGENT_STEP = "agent_step"

    #: Text that arrived over an EXTERNAL transport (#3595 step 1b): a chat
    #: webhook (``gateway.api.push_to_agent`` — Slack / LINE / any
    #: ``reyn.webhooks`` plugin) or an out-of-process request handler
    #: (``mcp.server.send_to_agent_impl``, reached by the MCP ``send_to_agent``
    #: tool and by the A2A JSON-RPC router). Under the old ``CLIENT_INPUT`` claim
    #: a Slack message reading ``/reset`` executed the command, so anyone able to
    #: post to the webhook could run any registered slash command.
    #:
    #: **Why ONE member for two transports.** What the member has to answer is
    #: who authored the text, for the purpose of deciding whether the OS may act
    #: on its FORM — and a webhook peer and an MCP/A2A peer answer identically: a
    #: counterparty outside this process, never the operator. Every in-tree
    #: consumer (turn dispatch, ``_stamp_execution_context``, the
    #: hook-driven-turn valve, ``queued_user_messages``) branches identically on
    #: the two. A consumer that needs the transport ITSELF already has a strictly
    #: better source on the envelope — ``sender`` (``"slack:U456"``) or
    #: ``reply_to`` (``McpRef`` / ``ExternalRef``) — which names the individual
    #: peer, not just its transport. A distinction no consumer branches on, and
    #: that a richer field already carries, is a label rather than a union
    #: member.
    EXTERNAL_MESSAGE = "external_message"

    #: A fired message-based cron job's text (#3595 step 1b). Under the old
    #: ``CLIENT_INPUT`` claim a job whose message began with ``/`` executed an
    #: operator command in an UNATTENDED session with no client to show the
    #: result to.
    #:
    #: **Why cron does not share** ``EXTERNAL_MESSAGE``. The member's job is to
    #: say who authored the text for the purpose of deciding whether the OS may
    #: act on its form, and cron answers differently from those two: a cron
    #: message is OPERATOR-authored, in ``.reyn/`` job config, under the same
    #: file-permission trust as the rest of the workspace — not a counterparty's
    #: chat line. It is still not ``CLIENT_INPUT``, because it was authored as
    #: the AGENT'S PROMPT and delivered to a session with no client attached, not
    #: typed at a composer. That is a distinction a trust decision (e.g. #3501's
    #: untrusted-content narrowing) would branch on, and no envelope field can
    #: carry it: ``sender`` (``"cron:<job>"`` vs ``"slack:U456"``) is a free-form
    #: attribution string the pushing plugin supplies, and a discriminator a
    #: trust decision reads must not come from the side being classified.
    CRON = "cron"

    #: A wake=true lifecycle-hook push delivered as a turn trigger (#1800 slice
    #: 5b): a system-role ``[hook:name]`` message plus one router turn
    #: (self-continuation). ``Session.run_one_iteration``'s loop valve counts
    #: consecutive turns of this member.
    HOOK = "hook"

    #: The empty-text pump that starts an ATTACHED pipeline run
    #: (``session_api.run_pipeline_attached``). Nobody authored it: its text is
    #: ``""`` and its only job is to hand the driver-session's executor one
    #: iteration. It claimed ``CLIENT_INPUT`` until #3595 S2, which was a
    #: statement about which member the dispatch table would run a turn for, not
    #: about who wrote the message — the same conflation, in the one shape the
    #: slash defect could not expose, because empty text never starts with ``/``.
    PIPELINE_NUDGE = "pipeline_nudge"

    #: A peer SESSION's text — proposal 0067 P5 (#3978): ``send_to_session``
    #: (fire-and-forget delivery) and P4's ``run_prompt(collect="attached")``
    #: (synchronous collection) both land here. Architect's ruling
    #: (#3978): this member's FIRST criterion is "who authored it", not "who
    #: dispatches on it" — both producers answer identically (a peer session's
    #: text, addressed to a specific (agent, session) pair, #2130's primitive),
    #: so one member serves both, the same shape ``EXTERNAL_MESSAGE`` already
    #: uses for its two transports. The fire-and-forget/collect-a-reply
    #: distinction rides on ``reply_to``/``collect`` in the payload, not on
    #: this member — a distinction a richer field already carries is a label,
    #: not a union member (this module's own discriminator, applied to
    #: ``EXTERNAL_MESSAGE`` and ``CRON`` above).
    #:
    #: Not named ``SESSION_MESSAGE``: that name would misrepresent
    #: ``run_prompt``'s synchronous-collection use as "message" — ADR-0040 D1's
    #: three-word vocabulary reserves "message" for delivery-only.
    PEER_SESSION = "peer_session"


#: #5677 (architect co-vet, lead-coder's own correction of #3595's
#: original slash-dispatch predicate): which kinds may STEER a turn
#: that is already running — ``InboxArbiter.peek_mid_turn_injection``'s
#: own eligibility set. This is a SEPARATE question from
#: ``CLIENT_INPUT``'s "may this text reach slash dispatch" (that
#: predicate is a trust decision about a text's FORM; this one is about
#: whether a producer may interrupt an in-flight tool loop at all) —
#: #3595 answered the first question by closing ``CLIENT_INPUT`` to
#: non-human producers; #5677 found that ``inbox_arbiter.py`` had
#: reused that SAME closed predicate to answer the second, unrelated
#: question, which meant widening injection eligibility could only be
#: done by reopening #3595's own gate. Declared here, member-by-member,
#: so the set has exactly one home and the reason for each member's
#: inclusion (or exclusion) is next to the member it is about, not
#: reconstructed from a PR description later.
#:
#: A member here does not get a free pass on the WIRE: injected content
#: renders under its OWN ``kind`` (see ``session.py``'s
#: ``_render_mid_turn_injection``), never silently as ``role="user"``
#: (the #3595 defect class — a non-human producer's text made
#: indistinguishable from the operator's own — reproduced ONE LAYER
#: DOWN, on the mid-turn wire, if this set had been widened without
#: also widening the rendering).
MID_TURN_INJECTABLE: "frozenset[TurnOrigin]" = frozenset({
    # The operator's own typed line — the founding case #3792 built the
    # whole mid-turn-injection feature for (a human steering a running
    # tool loop). Renders unchanged, role="user".
    TurnOrigin.CLIENT_INPUT,
    # A sub-agent request pulled from another session (#5677's own
    # motivation, verbatim lead-coder incident: sending a peer a
    # corrected instruction only reached it at the NEXT turn boundary,
    # so the wrong first step already ran). Origin is inside the trust
    # boundary (the operator's own workspace spawned the peer session,
    # #2103/#3556 narrowing already applies) — unlike EXTERNAL_MESSAGE,
    # this is not a remote third party steering the turn. Architect's
    # own recommendation (co-vet on #5677); EXTERNAL_MESSAGE stays OUT
    # pending an explicit owner ruling (the one open question #5677
    # itself named — architect and lead-coder's recommendation agrees
    # on excluding it, so this PR does not pre-empt that decision by
    # including it and then needing to walk it back).
    TurnOrigin.AGENT_REQUEST,
})


__all__ = ["MID_TURN_INJECTABLE", "TurnOrigin"]
