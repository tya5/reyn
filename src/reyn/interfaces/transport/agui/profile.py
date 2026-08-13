"""reyn AG-UI extension profile — the ``reyn.*`` namespace registry.

The AG-UI standard surface renders the interoperable core (text / tool / run /
error / state) for any generic client. Beyond that core, reyn names its own
vocabulary under a **reyn-owned namespace** in two ways:

- as the ``name`` of a ``CUSTOM`` event (chrome with no standard analog — trace
  lines, the ``present`` render model, the intervention-answer axis); and
- as the ``toolName`` of the HITL intervention **frontend-tool**
  ``TOOL_CALL_START`` (a standard event, not a ``CUSTOM`` one).

This module is the single registry that formalizes that namespace into a
documented, tested **extension profile**: every ``reyn.*`` name reyn emits, its
value schema, and what it means.

Three namespaces:

- ``reyn.display.<kind>`` — a reyn display frame with no standard AG-UI analog.
  A ``CUSTOM`` ``name``; ``value`` is ``{"text": <the display line text>}``.
  (``presentation`` also carries its render-node model on the ``_reyn`` block's
  ``meta.nodes``, inert on the wire.) Closed member set (below).
- ``reyn.event.<etype>`` — a reyn audit-event (working-indicator axis) with no
  standard AG-UI analog. A ``CUSTOM`` ``name``; ``value`` is the event's data
  object. Closed member set (below).
- ``reyn.intervention.<kind>`` — the HITL frontend-tool ``toolName``. ``<kind>``
  is the intervention kind (``ask_user`` / ``permission.*`` / …), caller-supplied,
  so this is an **open namespace** profiled at the prefix level with a fixed value
  schema (:data:`OPEN_NAMESPACES`), not a closed member set.

**Non-circular completeness gate.** ``tests/interfaces/test_agui_profile_completeness.py``
enumerates the reyn-mapped vocabulary *from the source vocabulary* — display kinds
+ ``forwarded_frame_kinds`` (encoded through the codec, ``CUSTOM`` names collected)
AND the intervention frontend-tool encoder's real ``toolName`` — and asserts each
emitted ``reyn.*`` name is profiled (a closed-member entry or an open-namespace
prefix). An unprofiled name is RED — doc-drift is designed out, the same discipline
as the P1/P2 completeness gates. The gate reads the codec's output, never this
registry, so it is not comparing the profile to itself.
"""
from __future__ import annotations

from dataclasses import dataclass

# Namespace prefixes (the ``name`` before the terminal ``.<kind>`` / ``.<etype>``).
DISPLAY_NS = "reyn.display"
EVENT_NS = "reyn.event"
INTERVENTION_NS = "reyn.intervention"

# Human-readable namespace summaries — enumerated in the profile doc section.
NAMESPACES: dict[str, str] = {
    DISPLAY_NS: "a reyn display frame with no standard AG-UI analog (CUSTOM name; value: {text})",
    EVENT_NS: "a reyn audit-event with no standard AG-UI analog (CUSTOM name; value: the event data object)",
    INTERVENTION_NS: "the HITL intervention frontend-tool toolName (open namespace; args: {prompt, detail, choices, suggestions})",
}

# Open namespaces profiled at the PREFIX level (the terminal segment is
# caller-supplied, so there is no closed member set). Maps the ``<prefix>.`` a
# name must start with → its fixed value schema. ``reyn.intervention.`` is the
# HITL frontend-tool ``toolName`` (``reyn.intervention.<kind>``); the trailing dot
# is part of the key so a bare ``reyn.intervention`` does not spuriously match.
OPEN_NAMESPACES: dict[str, str] = {
    f"{INTERVENTION_NS}.": "args: {prompt, detail, choices, suggestions}; toolCallId = intervention id",
}


@dataclass(frozen=True)
class CustomName:
    """One profiled ``reyn.*`` Custom name: its namespace + ``value`` schema."""

    name: str
    namespace: str
    value_schema: str
    summary: str


def _entries(*entries: CustomName) -> "dict[str, CustomName]":
    return {e.name: e for e in entries}


# The concrete, emitted Custom names. Keyed by the exact ``name`` the codec puts
# on the wire; the completeness gate binds this set to the codec's emitted
# vocabulary (a new Custom-mapped kind/etype with no entry here fails CI).
CUSTOM_PROFILE: dict[str, CustomName] = _entries(
    # ── reyn.display.<kind> — display frames with no standard AG-UI analog ──
    CustomName(
        "reyn.display.intervention", DISPLAY_NS, "{text: str}",
        "an intervention prompt is displayed (the reyn client draws it natively; the answer round-trip rides the reyn.intervention.* frontend-tool)",
    ),
    CustomName(
        "reyn.display.presentation", DISPLAY_NS, "{text: str}",
        "a present op's text; the render-node model rides the _reyn block's meta.nodes, inert on the wire",
    ),
    CustomName(
        "reyn.display.user", DISPLAY_NS, "{text: str}",
        "a user-authored line echoed live to the scrollback (backlog user turns ride the standard messages array)",
    ),
    CustomName(
        "reyn.display.system", DISPLAY_NS, "{text: str}",
        "a reyn chrome line — a persisted lifecycle/status marker (compaction / budget / cost-warn) or the operator's 'answered:' echo; reyn-private, no standard AG-UI analog",
    ),
    CustomName(
        "reyn.display.__copy_last_reply__", DISPLAY_NS, "{text: str}",
        "the /copy sentinel — FORWARDED (not a local-control filter): the CLIENT consumes it over the transport stream and does a real client-side clipboard copy (stream_client._handle_copy_sentinel), so filtering it would make remote /copy a silent no-op",
    ),
    CustomName(
        "reyn.display.__rewind_list__", DISPLAY_NS, "{text: str}",
        "the /rewind sentinel — FORWARDED (not a local-control filter): the CLIENT consumes it over the transport stream and renders the rewind region picker, so filtering it would make remote /rewind a silent no-op",
    ),
    CustomName(
        "reyn.display.trace", DISPLAY_NS, "{text: str}",
        "a nested detail / trace line (dim, transient) — the generic trace kind rendered nested with the tool-call trace subtypes",
    ),
    CustomName(
        "reyn.display.tool_call_started", DISPLAY_NS, "{text: str}",
        "a tool-call start trace line",
    ),
    CustomName(
        "reyn.display.tool_call_completed", DISPLAY_NS, "{text: str}",
        "a tool-call completion trace line",
    ),
    CustomName(
        "reyn.display.tool_call_failed", DISPLAY_NS, "{text: str}",
        "a tool-call failure trace line",
    ),
    # ── reyn.event.<etype> — audit-events with no standard AG-UI analog ──
    CustomName(
        "reyn.event.user_answered_intervention", EVENT_NS, "the event data object",
        "the user answered an intervention (working-indicator axis)",
    ),
    CustomName(
        "reyn.event.user_submitted", EVENT_NS, "the event data object",
        "a user turn was submitted (#3300 P1 C) — carries the RAW text + "
        "chain_id + msg_id + seq + meta; each surface's event→display handler "
        "renders the echo and neutralizes at that render boundary "
        "(replaces the earlier kind=\"user\" outbox-echo write); msg_id/seq "
        "are the #3300 P2a sent-queue correlation id + order-race-gate token",
    ),
    CustomName(
        "reyn.event.intervention_answer_submitted", EVENT_NS, "the event data object",
        "an intervention answer was resolved (#3300, event-ifying the last "
        "outbox kind=\"user\" broadcast site — InterventionHandler."
        "deliver_answer_to) — carries the RAW display text (the raw answer, "
        "or the matched choice's label) + intervention_id + attribution meta; "
        "each surface's event→display handler renders the echo and "
        "neutralizes at that render boundary, following the user_submitted "
        "precedent exactly",
    ),
    CustomName(
        "reyn.event.inbox_cancel", EVENT_NS, "the event data object",
        "an UNDISPATCHED queued user message was cancelled by id (#3300 P3 "
        "Y-server) — carries msg_id + seq; the server-authoritative sent-queue "
        "removal signal, exclusive with turn_started for the same msg_id "
        "(never a client-local cancel-success response)",
    ),
    CustomName(
        "reyn.event.session_attached", EVENT_NS, "the event data object",
        "a session/agent switch just happened (#3310 N1) — carries "
        "{agent, session_id}, the identity a client keys its display/reset "
        "on. Emitted at the registry attach seam (`AgentRegistry.attach`/"
        "`attach_session`), put directly on `repl_outbox` as a stream "
        "BARRIER (no session's own audit-events, since that stream is the "
        "thing being swapped) — forwarded ahead of any consumer (N2, a "
        "separate PR, adds the client-side reset)",
    ),
    CustomName(
        "reyn.event.agent_delta", EVENT_NS, "the event data object",
        "one streamed LLM content-delta chunk (#3288 ③b) — carries the raw "
        "delta text, chain_id, and round_index (which LLM round of the turn "
        "produced it, #3656: a turn that calls a tool emits more than one "
        "assistant message, and the rounds are told apart by this); a "
        "NON-PERSISTENT, purely additive notification "
        "(the owner-ratified L4 audit-event route, replacing the earlier "
        "OutboxMessage-kind ADR wording) — the single source of truth stays "
        "the completed full-text kind=\"agent\" OutboxMessage emitted exactly "
        "once at turn end (L9 whole-persist is unaffected). A surface with no "
        "handler for this event consumes-but-drops it (no draw), unlike an "
        "unknown DISPLAY kind; ③c adds the textual_chat coalescing consumer",
    ),
    CustomName(
        "reyn.event.session_halted", EVENT_NS, "the event data object",
        "the session fail-stopped on a persistent durability failure (#2259 "
        "PR-3) — carries reason (e.g. \"durability_failure\"); emitted at "
        "most once (Session._fail_stop_if_durability_dead / "
        "run_one_iteration, guarded on halted_reason is None) the FIRST "
        "time the fail-stop latches, on either the accept-edge raise or the "
        "process-edge halt (#2280). Purely observability — the halt itself "
        "is already enforced synchronously by the DurabilityHaltError raise "
        "regardless of this event; a surface with no handler consumes-but-"
        "drops it, like agent_delta above. Also rides STATE_SNAPSHOT/"
        "STATE_DELTA as halted_reason (agui/state.py) for a remote client's "
        "status panel, independently of this event frame",
    ),
)


def profiled_names() -> "frozenset[str]":
    """The set of ``reyn.*`` Custom names the extension profile documents."""
    return frozenset(CUSTOM_PROFILE)


def is_profiled(name: str) -> bool:
    """True iff *name* is profiled — a closed :data:`CUSTOM_PROFILE` member OR a
    name under a profiled :data:`OPEN_NAMESPACES` prefix (the intervention
    frontend-tool). An unprofiled name is skipped by a reyn client, so ``False``
    is the generic ignore-unknown case, not fatal."""
    if name in CUSTOM_PROFILE:
        return True
    return any(name.startswith(prefix) for prefix in OPEN_NAMESPACES)


__all__ = [
    "DISPLAY_NS",
    "EVENT_NS",
    "INTERVENTION_NS",
    "NAMESPACES",
    "OPEN_NAMESPACES",
    "CustomName",
    "CUSTOM_PROFILE",
    "profiled_names",
    "is_profiled",
]
