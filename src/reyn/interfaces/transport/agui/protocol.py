"""AG-UI wire protocol — the reyn ``Frame`` ⇄ AG-UI-event codec (ADR-0039 P2).

P1 unified the CUI onto a :class:`~reyn.interfaces.transport.client_transport.ClientTransport`
seam whose vocabulary is the tagged :class:`~reyn.interfaces.transport.frames.Frame`
(``DisplayFrame`` / ``EventFrame``). P2 adds a SECOND transport over the wire; this
module is its **codec** — the one place a ``Frame`` is turned into an AG-UI event
and back, so the remote renderer stays byte-identical to the local one.

Design (the load-bearing invariants this module carries):

- **One frame ⇄ one AG-UI event.** :func:`encode_frame` maps each ``Frame`` to a
  single :class:`AgUiEvent` (``type`` + ``data``); :func:`decode_event` inverts
  it. A 1:1 mapping keeps the decode unambiguous and the round-trip lossless for
  the renderer-relevant fields (display bytes + WaitingOn sequence).

- **Standard envelope, reyn-private richness (D6).** Every encoded event carries
  BOTH a standard AG-UI field shape (so a generic AG-UI client renders the core:
  text / tool / run / error / state) AND a reyn-private ``_reyn`` reconstruction
  block. :func:`decode_event` reconstructs the exact ``Frame`` from ``_reyn``; a
  foreign event with no ``_reyn`` block decodes to ``None`` (ignore-unknown /
  graceful degrade — the generic-client contract).

- **Re-decided invariant (#3288 ③d): "1 frame ⇄ 1 ``_reyn``-bearing event"
  now holds ONLY for a non-streamed whole message.** ③b introduced
  ``agent_delta`` audit-events (token-level LLM deltas); ③d gives them a REAL
  multi-CONTENT wire lifecycle via :func:`encode_frame_wire_streaming` (used
  by :class:`~reyn.interfaces.transport.agui.emitter.AgUiEmitter` in place of
  :func:`encode_frame_wire`). For a message that streamed: N ``agent_delta``
  EventFrames each become their OWN ``TEXT_MESSAGE_CONTENT``, each carrying
  its OWN ``_reyn`` (so a reyn client reconstructs the exact same per-chunk
  ``agent_delta`` EventFrame it would get in-process); the terminal
  ``kind="agent"`` completion then maps to ``TEXT_MESSAGE_END`` ONLY — never
  a second CONTENT re-sending the full text (a client that accumulated the
  deltas would render the body twice) — but that END carries its OWN
  ``_reyn``, holding the completion's FULL text. So a streamed message has
  N+1 ``_reyn``-bearing wire events, not 1. The reyn client's
  **reconstruction AUTHORITY is always the LAST one** (the END's full
  text) — NEVER a concatenation of the N deltas, which are non-persistent,
  derived, live-only narration; the restore/reconnect (``MESSAGES_SNAPSHOT``)
  path never reads a delta, only the persisted completion. A message that
  never streamed (no capability, or the connection never observed a delta
  for it) is untouched: the plain :func:`encode_frame_wire` triplet, CONTENT
  carries ``_reyn``, START/END are scaffold — byte-identical to pre-③d.

- **The mapping is a table, derived, not hand-listed at the call site.** The
  ``kind``/``type`` → AG-UI-event-type tables (:data:`_DISPLAY_KIND_EVENT` /
  :data:`_EVENT_TYPE_EVENT`) are the single source the completeness gate reads;
  an unmapped kind falls back to :data:`CUSTOM` and still round-trips (lossless
  by construction), so a new renderer kind can never silently vanish on the wire.

STATE (the status read-model) and MESSAGES snapshots ride the same SSE stream but
are not ``Frame``\\s — they decode to :class:`StateUpdate` / :class:`MessagesSnapshot`
so the client demuxes render frames from the status side-channel.

**Generic-client conformance (ADR-0039 P4).** The standard surface is widened so a
stock AG-UI client with zero reyn knowledge renders a functional chat:

- **Text triplet.** A bare ``TEXT_MESSAGE_CONTENT`` is invalid per the AG-UI spec,
  which mandates ``TEXT_MESSAGE_START`` → one-or-more ``TEXT_MESSAGE_CONTENT`` →
  ``TEXT_MESSAGE_END``, correlated by ``messageId``. :func:`encode_frame_wire`
  expands a whole text message into that triplet with a shared generated id
  (reyn's outbox has no stable message id). Only the CONTENT event carries the
  ``_reyn`` reconstruction block — START/END are generic scaffold the reyn client
  decodes to ``None`` — so the invariant stays **1 frame ⇄ 1 ``_reyn``-bearing
  event** and reyn bit-identity is unchanged, for a message that never streamed.
  **A message that DID stream (#3288 ③d)** rides
  :func:`encode_frame_wire_streaming` instead: a real per-chunk
  ``TEXT_MESSAGE_CONTENT`` for every ``agent_delta``, bracketed by ONE
  ``TEXT_MESSAGE_START`` (at the first delta) and ONE ``TEXT_MESSAGE_END`` (at
  completion, carrying the completion's ``_reyn`` — see the module docstring's
  re-decided invariant above) — never a second full-text CONTENT.
- **Standard ``messages`` array.** ``MESSAGES_SNAPSHOT`` carries a standard
  ``[{role, content}]`` array of **conversation turns only** (``agent`` →
  ``assistant``, ``user`` → ``user``); reyn chrome (status / error / present /
  intervention / trace) replays to the reyn client via ``_reyn`` and is NOT in the
  standard array.
- **Standard tool status.** ``TOOL_CALL_END`` carries a standard ``status``
  (``"ok"`` / ``"error"``, derived from the frame etype) so a generic client sees
  a tool failure; the reyn client still exact-recovers from ``_reyn``.

The ``reyn.*`` Custom namespace is a documented, tested extension profile — see
:mod:`reyn.interfaces.transport.agui.profile`.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from reyn.core.events.events import Event
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame, Frame
from reyn.runtime.outbox import OutboxMessage

# ── AG-UI event type names (hand-rolled; the SDK is not a dependency) ─────────
TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
TEXT_MESSAGE_END = "TEXT_MESSAGE_END"
# AG-UI Reasoning message lifecycle (ADR-0039 P6a). The canonical Reasoning
# category has seven events; reyn is whole-message (no token streaming), so it
# maps the content-bearing inner triplet — ``REASONING_MESSAGE_START`` →
# ``REASONING_MESSAGE_CONTENT`` → ``REASONING_MESSAGE_END``, correlated by
# ``messageId`` with ``role: "reasoning"`` — the minimal valid sequence a generic
# AG-UI client (CopilotKit) renders as a reasoning message. The outer
# ``REASONING_START`` / ``REASONING_END`` context wrapper and the streaming
# ``REASONING_MESSAGE_CHUNK`` / ``REASONING_ENCRYPTED_VALUE`` variants are not
# emitted (no streaming, no encrypted CoT).
REASONING_MESSAGE_START = "REASONING_MESSAGE_START"
REASONING_MESSAGE_CONTENT = "REASONING_MESSAGE_CONTENT"
REASONING_MESSAGE_END = "REASONING_MESSAGE_END"
RUN_STARTED = "RUN_STARTED"
RUN_FINISHED = "RUN_FINISHED"
RUN_ERROR = "RUN_ERROR"
TOOL_CALL_START = "TOOL_CALL_START"
TOOL_CALL_END = "TOOL_CALL_END"
TOOL_CALL_RESULT = "TOOL_CALL_RESULT"
STATE_SNAPSHOT = "STATE_SNAPSHOT"
STATE_DELTA = "STATE_DELTA"
MESSAGES_SNAPSHOT = "MESSAGES_SNAPSHOT"
CUSTOM = "CUSTOM"

# Namespaced reconstruction key. Its presence is the "this is a reyn frame"
# marker a generic client ignores and the reyn client reconstructs from.
_REYN = "_reyn"

# Control sentinels the AG-UI emitter does NOT forward on the wire
# (:class:`~reyn.interfaces.transport.agui.emitter.AgUiEmitter` consults this set).
# An EXPLICIT per-entry allowlist — deliberately NOT the negation of any
# forward-set (negating a partial/legacy forward-set would wrongly filter
# renderable display kinds like ``presentation`` / ``reasoning`` / ``system`` /
# ``user`` that happen to be absent from it). Each entry is a standalone decision:
#
# - ``__end__`` — the stream terminator; the emitter returns on it (the
#   client's loop also ends on stream close).
#
# #4534 retired both of this set's former sentinel entries — PR-2's
# ``__attach_request__`` and PR-2b's ``__session_switch_request__``. Neither
# ``/attach`` nor ``/session switch`` posts an ``OutboxMessage`` anymore;
# both go through ``ClientTransport.request_attach`` /
# ``request_session_switch`` (typed operations), and
# ``endpoint._SessionFrameSource``'s switch-follow now subscribes to
# ``registry.add_attach_listener`` directly rather than consuming a sentinel
# off this wire's source stream — see that class's own docstring.
#
# NOT here (deliberately forwarded — profiled CUSTOM display names):
# - ``__copy_last_reply__`` / ``__rewind_list__`` are consumed by the CLIENT over
#   the transport stream — ``/copy`` does a real client-side clipboard copy
#   (``_copy_sentinel.handle_copy_sentinel``) and ``/rewind`` renders a client-side
#   picker. In the thin-client model transport IS the AG-UI wire, so filtering them
#   would make remote ``/copy`` / ``/rewind`` silent no-ops. They MUST reach the
#   wire (``_reyn``-lossless; generic clients ignore-unknown safely).
CONTROL_FILTER_KINDS: "frozenset[str]" = frozenset({
    "__end__",
    # #4482 PR-3: /open <ref> — local-only by construction (launching an OS
    # app on the machine the client is running on); mirrors outbox.py's
    # CONTROL_KINDS entry, kept in sync with it deliberately (see that
    # module's own comment for why it's control-filtered, not profiled).
    "__open_artifact__",
})

# Reserved frontend-tool namespace for the HITL round-trip (ADR-0039 P3, D6/R4).
# An intervention rides the wire in TWO representations: the P2 ``DisplayFrame``
# (kind ``intervention`` → the reyn client's NATIVE prompt UI) AND — added here —
# a companion ``TOOL_CALL_START`` **frontend-tool** whose ``toolName`` is
# ``reyn.intervention.<kind>``. The namespace lets a generic AG-UI client tell a
# "you must answer this" frontend-tool from an ordinary (passive) tool event, and
# the ``toolCallId`` (= the intervention id, a space distinct from chat tool-call
# ids) is the answer-correlation anchor a client echoes back verbatim in a
# ``TOOL_CALL_RESULT`` (R1: answer BY-ID, never answer-oldest). The reyn client
# uses the frontend-tool ONLY for that correlation — it draws the prompt from the
# DisplayFrame, so there is no double-render (R4-ii).
_INTERVENTION_TOOL_PREFIX = "reyn.intervention."

# DisplayFrame ``OutboxMessage.kind`` → AG-UI event type. Kinds not listed here
# (control sentinels like ``__end__`` / ``__copy_last_reply__``, and any future
# renderer kind) fall back to CUSTOM in :func:`_display_event_type` — they still
# round-trip losslessly via ``_reyn``; the table only selects the generic-client
# surface. ``presentation`` rides CUSTOM = present-on-wire (G2).
_DISPLAY_KIND_EVENT: dict[str, str] = {
    "agent": TEXT_MESSAGE_CONTENT,
    "status": TEXT_MESSAGE_CONTENT,
    "reasoning": REASONING_MESSAGE_CONTENT,
    "error": RUN_ERROR,
    "intervention": CUSTOM,
    "trace": CUSTOM,
    "presentation": CUSTOM,
}

# EventFrame ``Event.type`` → AG-UI event type. turn_* → RUN_*; tool_* →
# TOOL_CALL_*; ``user_answered_intervention`` / ``user_submitted`` (#3300 P1 C)
# are reyn-private → CUSTOM (the ``_event_event_type`` default, not listed
# below). Every entry in ``forwarded_frame_kinds()`` — not just the ones
# explicitly mapped here — round-trips losslessly via ``_reyn``; the
# completeness gate binds the transport's forward-set to the renderer's
# vocabulary independently of this table's non-CUSTOM selections.
_EVENT_TYPE_EVENT: dict[str, str] = {
    "turn_started": RUN_STARTED,
    "turn_settled": RUN_FINISHED,
    "turn_completed": RUN_FINISHED,
    "turn_cancelled": RUN_FINISHED,
    "user_answered_intervention": CUSTOM,
    "tool_called": TOOL_CALL_START,
    "tool_returned": TOOL_CALL_END,
    "tool_failed": TOOL_CALL_END,
}


@dataclass(frozen=True)
class AgUiEvent:
    """One AG-UI wire event: an SSE ``event:`` type plus its ``data:`` object."""

    type: str
    data: dict


@dataclass(frozen=True)
class StateUpdate:
    """A decoded STATE_* event — the status read-model side-channel, not a frame.

    Exactly one of ``snapshot`` (full, on connect) / ``delta`` (changed keys) is
    set. Consumed by the client's remote-status view, never by the renderer.
    """

    snapshot: "dict | None" = None
    delta: "dict | None" = None


@dataclass(frozen=True)
class MessagesSnapshot:
    """A decoded MESSAGES_SNAPSHOT — the reconnect display backlog (A4).

    #5139 C: ``has_more``/``next_cursor`` carry the server's own page
    boundary — see :class:`~reyn.interfaces.transport.frames.BacklogBatch`'s
    docstring for what each means; this is the wire-decoded form of the
    same two fields, read straight through by
    :meth:`~reyn.interfaces.transport.agui.client.AgUiTransport._consume_block`."""

    frames: list = field(default_factory=list)
    has_more: bool = False
    next_cursor: "str | None" = None


@dataclass(frozen=True)
class InterventionTool:
    """A decoded intervention frontend-tool ``TOOL_CALL_START`` (P3, R4).

    NOT a render frame — the reyn client uses it only to track which
    intervention is pending an answer (``intervention_id`` = the ``toolCallId``
    answer-correlation anchor), so a subsequent operator line is delivered to
    the RIGHT intervention by id (R1), never the head-of-queue. Display is driven
    by the P2 ``DisplayFrame``, so this is never rendered (no double-draw).
    """

    intervention_id: str
    kind: str = ""


@dataclass(frozen=True)
class InterventionToolResult:
    """A decoded terminal ``TOOL_CALL_RESULT`` for a pending intervention (P3).

    Emitted server→client when an intervention leaves the pending set (answered
    or fail-close DENY), so a client's pending frontend-tool does not dangle.
    ``status`` is ``"answered"`` or ``"denied"``.
    """

    intervention_id: str
    status: str = "answered"


# Display kinds that are conversation TURNS (vs reyn chrome) — the only kinds
# that go into the standard MESSAGES_SNAPSHOT ``[{role, content}]`` array. Their
# standard AG-UI ``role``: agent → assistant, user → user.
_CONVERSATION_KINDS: dict[str, str] = {"agent": "assistant", "user": "user"}


def _display_event_type(kind: str) -> str:
    return _DISPLAY_KIND_EVENT.get(kind, CUSTOM)


def _event_event_type(etype: str) -> str:
    return _EVENT_TYPE_EVENT.get(etype, CUSTOM)


def _new_message_id() -> str:
    """A per-message id for the text triplet (reyn's outbox has no stable id)."""
    return uuid.uuid4().hex


# ── encode: Frame → AgUiEvent ────────────────────────────────────────────────

def encode_frame(frame: Frame) -> AgUiEvent:
    """Encode one ``Frame`` to a single AG-UI event (standard fields + ``_reyn``)."""
    if isinstance(frame, DisplayFrame):
        return _encode_display(frame)
    if isinstance(frame, EventFrame):
        return _encode_event(frame)
    raise TypeError(f"not a Frame: {type(frame).__name__}")


def _encode_display(frame: DisplayFrame) -> AgUiEvent:
    msg = frame.message
    kind, text, meta = msg.kind, msg.text, dict(msg.meta or {})
    ag_type = _display_event_type(kind)
    reyn = {"frame": "display", "kind": kind, "text": text, "meta": meta}
    # Standard AG-UI surface a generic client renders (best-effort).
    if ag_type is TEXT_MESSAGE_CONTENT:
        # ``messageId`` correlates the START/CONTENT/END triplet
        # (:func:`encode_frame_wire`); ``delta`` is the whole message text.
        std = {
            "messageId": _new_message_id(),
            "role": "assistant" if kind == "agent" else "status",
            "delta": text,
        }
    elif ag_type is REASONING_MESSAGE_CONTENT:
        # Whole-frame reasoning (P6a). ``messageId`` correlates the reasoning
        # triplet (:func:`encode_frame_wire`); ``role`` is the spec-mandated
        # ``"reasoning"``; ``delta`` is the whole reasoning text.
        std = {
            "messageId": _new_message_id(),
            "role": "reasoning",
            "delta": text,
        }
    elif ag_type is RUN_ERROR:
        std = {"message": text}
    else:  # CUSTOM — reyn-private (presentation / trace / intervention / control)
        std = {"name": f"reyn.display.{kind}", "value": {"text": text}}
    return AgUiEvent(type=ag_type, data={**std, _REYN: reyn})


def _encode_event(frame: EventFrame) -> AgUiEvent:
    ev = frame.event
    etype = getattr(ev, "type", None) or ""
    edata = dict(getattr(ev, "data", {}) or {})
    ag_type = _event_event_type(etype)
    reyn = {"frame": "event", "type": etype, "data": edata}
    if ag_type is TOOL_CALL_START:
        std = {"toolName": edata.get("tool"), "step": edata.get("tool")}
    elif ag_type is TOOL_CALL_END:
        # Standard failure field: a generic client sees the failure; the reyn
        # client still exact-recovers the etype from ``_reyn``.
        std = {
            "toolName": edata.get("tool"),
            "step": edata.get("tool"),
            "status": "error" if etype == "tool_failed" else "ok",
        }
    elif ag_type in (RUN_STARTED, RUN_FINISHED):
        # This "phase" is the AG-UI wire protocol's own standard field for a
        # RUN_STARTED/RUN_FINISHED event — it carries the run-lifecycle type
        # string itself (``etype``, e.g. "run_started"), a live, populated
        # value read by AG-UI clients. Same name, unrelated meaning to
        # ``core.events.event_schema.RETIRED_PHASE_FIELD`` (reyn's own
        # audit-event ``phase`` field, permanently the empty string since
        # #2696 — the phase engine that once populated it is gone). Neither
        # can be renamed: this one is the AG-UI spec's own vocabulary; that
        # one is a persisted, replay-compatible audit-event schema field. Two
        # different "phase"s that happen to sit in the same codebase — do not
        # assume one explains the other.
        std = {"phase": etype}
    else:  # CUSTOM — user_answered_intervention et al.
        std = {"name": f"reyn.event.{etype}", "value": edata}
    return AgUiEvent(type=ag_type, data={**std, _REYN: reyn})


# Content events that require the canonical START → CONTENT → END lifecycle
# scaffold. A bare ``*_CONTENT`` is invalid per the AG-UI spec (a strict generic
# client drops it), so :func:`encode_frame_wire` brackets each with its
# ``(START, END)`` pair correlated by a shared ``messageId``. Text (P4) and
# reasoning (P6a) share the exact same discipline.
_CONTENT_TRIPLET: dict[str, "tuple[str, str]"] = {
    TEXT_MESSAGE_CONTENT: (TEXT_MESSAGE_START, TEXT_MESSAGE_END),
    REASONING_MESSAGE_CONTENT: (REASONING_MESSAGE_START, REASONING_MESSAGE_END),
}


def encode_frame_wire(frame: Frame) -> "list[AgUiEvent]":
    """Encode one ``Frame`` to its full AG-UI **wire sequence** (P4/P6a).

    Most frames are a single event. A whole text message (P4) or a whole
    reasoning message (P6a) expands to the canonical AG-UI lifecycle triplet —
    ``*_MESSAGE_START`` → ``*_MESSAGE_CONTENT`` → ``*_MESSAGE_END``, correlated by
    a shared ``messageId`` — because a bare ``*_MESSAGE_CONTENT`` is invalid per
    the spec (a strict generic client drops it). Only the CONTENT event carries
    the ``_reyn`` reconstruction block; START/END are generic scaffold that
    :func:`decode_event` returns ``None`` for, so the invariant stays **1 frame ⇄
    1 ``_reyn``-bearing event** and the reyn client's render is unchanged.
    """
    event = encode_frame(frame)
    triplet = _CONTENT_TRIPLET.get(event.type)
    if triplet is None:
        return [event]
    start_type, end_type = triplet
    message_id = event.data.get("messageId")
    start = AgUiEvent(
        type=start_type,
        data={"messageId": message_id, "role": event.data.get("role")},
    )
    end = AgUiEvent(type=end_type, data={"messageId": message_id})
    return [start, event, end]


# Turn-lifecycle etypes that terminate a chain's stream even when it never
# reaches a ``kind="agent"`` completion (e.g. a mid-stream cancel emits
# ``kind="system"`` "turn interrupted" instead, router_loop.py:2422-2427).
# Defensive-only: closes a dangling stream (START+CONTENT sent, no END yet) so
# a strict generic client never sees a message left permanently open — the
# "Spec validity" gate applies to this edge just as much as the happy path.
_TURN_TERMINAL_ETYPES: "frozenset[str]" = frozenset(
    {"turn_settled", "turn_completed", "turn_cancelled"}
)


class TextStreamTracker:
    """Per-connection state correlating a streamed reply's ``agent_delta``
    audit-events with its terminal ``kind="agent"`` completion (#3288 ③d),
    keyed by ``chain_id`` — the turn-correlation id both carry
    (``RouterLoop._emit_agent_delta`` stamps it on every delta;
    ``RouterLoop.run``'s terminal ``put_outbox(meta={"chain_id": ...})`` stamps
    the SAME id on the completion, ``router_loop.py``).

    Owned one-per-connection by :class:`~reyn.interfaces.transport.agui.emitter.AgUiEmitter`
    — so it reflects exactly what THIS connection personally observed. A
    connection that witnesses at least one delta for a chain_id maps that
    chain's completion to END-only; a connection that never witnessed a delta
    for it (never connected, or connected after the whole turn already
    finished) gets the unchanged plain whole-message triplet instead — which
    is how the late-joiner window closes without any additional per-client
    "which deltas did you receive" bookkeeping (explicitly rejected, issue
    #3288 ③d design thread): whichever branch fires, the reyn client ends up
    calling the SAME renderer entry point with the SAME persisted full text.
    """

    def __init__(self) -> None:
        self._message_id_by_chain: "dict[str, str]" = {}

    def begin_delta(self, chain_id: str) -> "tuple[str, bool]":
        """Registers one delta for ``chain_id``; returns ``(messageId,
        is_first_delta_for_this_chain)``."""
        is_first = chain_id not in self._message_id_by_chain
        message_id = self._message_id_by_chain.setdefault(chain_id, _new_message_id())
        return message_id, is_first

    def end_stream(self, chain_id: str) -> "str | None":
        """Pops and returns the streamed ``messageId`` for ``chain_id`` — or
        ``None`` when this connection never observed a delta for it (a plain
        whole-message reply, or a chain this connection joined too late to
        see any delta of)."""
        return self._message_id_by_chain.pop(chain_id, None)


def encode_frame_wire_streaming(
    frame: Frame, tracker: TextStreamTracker
) -> "list[AgUiEvent]":
    """Encode one ``Frame`` to its AG-UI wire sequence WITH generic
    multi-CONTENT text streaming (#3288 ③d — see the module docstring's
    re-decided invariant for the full contract).

    - An ``agent_delta`` ``EventFrame`` (③b) becomes a real
      ``TEXT_MESSAGE_CONTENT``: the FIRST one observed for a ``chain_id`` (on
      THIS connection, via ``tracker``) is preceded by a ``TEXT_MESSAGE_START``
      so a strict generic client accepts it (ADR-0039 P4); every delta CONTENT
      carries its own ``_reyn`` block.
    - A ``kind="agent"`` completion ``DisplayFrame`` whose ``chain_id`` has an
      active stream on ``tracker`` maps to ``TEXT_MESSAGE_END`` ONLY — never a
      second CONTENT re-sending the full text. The END carries the
      completion's OWN ``_reyn`` (the full text) — the sole reconstruction
      authority for a streamed message; this is what closes the late-joiner
      window (a connection that saw only some/no deltas for this chain still
      gets the full text off THIS event, or off the plain fallback below).
    - A ``kind="agent"`` completion whose ``chain_id`` was never streamed on
      THIS connection (``tracker.end_stream`` returns ``None``) falls straight
      through to the unchanged :func:`encode_frame_wire` triplet — byte-identical
      to pre-③d.
    - A turn-terminal event (``turn_settled``/``turn_completed``/``turn_cancelled``)
      that arrives while a stream is still active for its ``chain_id`` (a
      mid-stream cancel never reaches a ``kind="agent"`` completion) closes the
      dangling stream defensively with a bare ``TEXT_MESSAGE_END`` (no ``_reyn``
      — no full text exists to attach) ahead of that event's own encoding, so a
      strict generic client never sees a message left permanently open.
    - Every other frame is unaffected: :func:`encode_frame_wire` unchanged.
    """
    if isinstance(frame, EventFrame) and getattr(frame.event, "type", "") == "agent_delta":
        edata = dict(getattr(frame.event, "data", {}) or {})
        chain_id = str(edata.get("chain_id") or "")
        text = str(edata.get("text") or "")
        message_id, is_first = tracker.begin_delta(chain_id)
        events: "list[AgUiEvent]" = []
        if is_first:
            events.append(
                AgUiEvent(
                    type=TEXT_MESSAGE_START,
                    data={"messageId": message_id, "role": "assistant"},
                )
            )
        reyn = {"frame": "event", "type": "agent_delta", "data": edata}
        events.append(
            AgUiEvent(
                type=TEXT_MESSAGE_CONTENT,
                data={
                    "messageId": message_id,
                    "role": "assistant",
                    "delta": text,
                    _REYN: reyn,
                },
            )
        )
        return events
    if isinstance(frame, DisplayFrame) and frame.message.kind == "agent":
        chain_id = str((frame.message.meta or {}).get("chain_id") or "")
        message_id = tracker.end_stream(chain_id)
        if message_id is not None:
            msg = frame.message
            reyn = {
                "frame": "display",
                "kind": msg.kind,
                "text": msg.text,
                "meta": dict(msg.meta or {}),
            }
            return [
                AgUiEvent(
                    type=TEXT_MESSAGE_END,
                    data={"messageId": message_id, _REYN: reyn},
                )
            ]
        return encode_frame_wire(frame)
    if isinstance(frame, EventFrame) and getattr(frame.event, "type", "") in _TURN_TERMINAL_ETYPES:
        edata = dict(getattr(frame.event, "data", {}) or {})
        chain_id = str(edata.get("chain_id") or "")
        message_id = tracker.end_stream(chain_id)
        events = encode_frame_wire(frame)
        if message_id is not None:
            events = [
                AgUiEvent(type=TEXT_MESSAGE_END, data={"messageId": message_id}),
                *events,
            ]
        return events
    return encode_frame_wire(frame)


def encode_state_snapshot(snapshot: dict) -> AgUiEvent:
    """STATE_SNAPSHOT — the full status read-model, emitted on connect (A4)."""
    return AgUiEvent(
        type=STATE_SNAPSHOT,
        data={"snapshot": dict(snapshot), _REYN: {"frame": "state", "snapshot": dict(snapshot)}},
    )


def encode_state_delta(delta: dict) -> AgUiEvent:
    """STATE_DELTA — the changed status-read-model keys since the last emit."""
    return AgUiEvent(
        type=STATE_DELTA,
        data={"delta": dict(delta), _REYN: {"frame": "state_delta", "delta": dict(delta)}},
    )


def encode_messages_snapshot(
    frames: "list[Frame]", *, has_more: bool = False, next_cursor: "str | None" = None,
) -> AgUiEvent:
    """MESSAGES_SNAPSHOT — the display backlog replayed on connect (A4), a
    switch re-fire (#3310 N3), or a client-driven older-page pull (#5139 C —
    same encoder, same decoder, same :class:`MessagesSnapshot` shape; the
    3 call sites differ only in which slice of history they pass in).

    The standard ``messages`` field is a ``[{role, content}]`` array of
    **conversation turns only** (P4) — the shape a generic AG-UI client expects;
    reyn chrome (status / error / present / intervention / trace) is NOT a
    conversation turn and is excluded. The reyn client rebuilds the FULL backlog
    (all display frames) from the ``_reyn`` block, so its scrollback is unchanged.

    ``has_more``/``next_cursor`` (#5139 C) ride alongside inside the
    ``_reyn`` block — see :class:`~reyn.interfaces.transport.frames.BacklogBatch`'s
    docstring for what each means.
    """
    reyn_payload = [
        _encode_display(f).data[_REYN] for f in frames if isinstance(f, DisplayFrame)
    ]
    standard = [
        {"role": _CONVERSATION_KINDS[f.message.kind], "content": f.message.text}
        for f in frames
        if isinstance(f, DisplayFrame) and f.message.kind in _CONVERSATION_KINDS
    ]
    return AgUiEvent(
        type=MESSAGES_SNAPSHOT,
        data={
            "messages": standard,
            _REYN: {
                "frame": "messages",
                "messages": reyn_payload,
                "has_more": has_more,
                "next_cursor": next_cursor,
            },
        },
    )


def intervention_tool_name(kind: str) -> str:
    """The reserved frontend-tool ``toolName`` for an intervention of ``kind``."""
    return f"{_INTERVENTION_TOOL_PREFIX}{kind or 'ask_user'}"


def encode_intervention_tool_start(iv_meta: dict) -> AgUiEvent:
    """Encode an intervention as a HITL frontend-tool ``TOOL_CALL_START`` (P3, R4).

    ``iv_meta`` is the announce ``OutboxMessage.meta`` (``_iv_meta`` shape):
    ``intervention_id`` / ``intervention_kind`` / ``prompt`` / optional
    ``detail`` / ``choices`` / ``suggestions``. The ``args`` carry the
    prompt/choices a generic client renders; ``_reyn`` reconstructs the typed
    :class:`InterventionTool` for the reyn client's answer-correlation.
    """
    iv_id = str(iv_meta.get("intervention_id") or "")
    kind = str(iv_meta.get("intervention_kind") or "")
    args = {
        "prompt": iv_meta.get("prompt", ""),
        "detail": iv_meta.get("detail", ""),
        "choices": iv_meta.get("choices", []),
        "suggestions": iv_meta.get("suggestions", []),
    }
    std = {
        "toolCallId": iv_id,
        "toolName": intervention_tool_name(kind),
        "args": args,
    }
    reyn = {"frame": "intervention_tool", "intervention_id": iv_id, "kind": kind}
    return AgUiEvent(type=TOOL_CALL_START, data={**std, _REYN: reyn})


def encode_intervention_tool_result(intervention_id: str, status: str = "answered") -> AgUiEvent:
    """Encode the terminal ``TOOL_CALL_RESULT`` for a resolved intervention (P3).

    ``status`` is ``"answered"`` (operator answered) or ``"denied"`` (fail-close
    typed DENY). Sent so a client's pending frontend-tool never dangles.
    """
    iv_id = str(intervention_id or "")
    std = {"toolCallId": iv_id, "result": {"status": status}}
    reyn = {"frame": "intervention_tool_result", "intervention_id": iv_id, "status": status}
    return AgUiEvent(type=TOOL_CALL_RESULT, data={**std, _REYN: reyn})


# ── decode: AgUiEvent → Frame | StateUpdate | MessagesSnapshot | None ─────────

def decode_event(
    ag_type: str, data: dict
) -> "Frame | StateUpdate | MessagesSnapshot | InterventionTool | InterventionToolResult | None":
    """Invert :func:`encode_frame` (and the STATE / MESSAGES encoders).

    Reconstructs the exact reyn object from the ``_reyn`` block. An event with no
    ``_reyn`` block (a generic/foreign AG-UI event) decodes to ``None`` — the
    ignore-unknown / graceful-degrade contract (D6).
    """
    reyn = data.get(_REYN) if isinstance(data, dict) else None
    if not isinstance(reyn, dict):
        return None
    frame_tag = reyn.get("frame")
    if frame_tag == "display":
        # UNTRUSTED wire value → from_wire (lenient): an unknown remote kind must
        # ignore-unknown / graceful-degrade, never fail-close on __post_init__.
        return DisplayFrame(
            OutboxMessage.from_wire(
                kind=reyn.get("kind", ""),
                text=reyn.get("text", ""),
                meta=dict(reyn.get("meta") or {}),
            )
        )
    if frame_tag == "event":
        return EventFrame(Event(type=reyn.get("type", ""), data=dict(reyn.get("data") or {})))
    if frame_tag == "state":
        return StateUpdate(snapshot=dict(reyn.get("snapshot") or {}))
    if frame_tag == "state_delta":
        return StateUpdate(delta=dict(reyn.get("delta") or {}))
    if frame_tag == "messages":
        # UNTRUSTED wire values (reconnect backlog) → from_wire (lenient), same
        # ignore-unknown contract as the single-display decode above.
        frames = [
            DisplayFrame(
                OutboxMessage.from_wire(
                    kind=m.get("kind", ""),
                    text=m.get("text", ""),
                    meta=dict(m.get("meta") or {}),
                )
            )
            for m in (reyn.get("messages") or [])
        ]
        return MessagesSnapshot(
            frames=frames,
            has_more=bool(reyn.get("has_more", False)),
            next_cursor=reyn.get("next_cursor"),
        )
    if frame_tag == "intervention_tool":
        return InterventionTool(
            intervention_id=str(reyn.get("intervention_id") or ""),
            kind=str(reyn.get("kind") or ""),
        )
    if frame_tag == "intervention_tool_result":
        return InterventionToolResult(
            intervention_id=str(reyn.get("intervention_id") or ""),
            status=str(reyn.get("status") or "answered"),
        )
    return None


# ── SSE framing ──────────────────────────────────────────────────────────────

def to_sse(event: AgUiEvent) -> str:
    """Serialize an AG-UI event to an SSE block: ``event: T\\ndata: {json}\\n\\n``."""
    return f"event: {event.type}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


def parse_sse_blocks(lines: "list[str]") -> "list[AgUiEvent]":
    """Parse SSE text (already split to lines) into AG-UI events.

    Accumulates ``event:`` / ``data:`` fields until a blank separator line and
    emits one :class:`AgUiEvent` per block. Malformed JSON in a ``data:`` line
    is skipped (a robust wire never lets one bad block kill the stream).
    """
    events: list[AgUiEvent] = []
    ev_type: str | None = None
    data_buf: list[str] = []

    def _flush() -> None:
        nonlocal ev_type, data_buf
        if ev_type is not None and data_buf:
            try:
                payload = json.loads("\n".join(data_buf))
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                events.append(AgUiEvent(type=ev_type, data=payload))
        ev_type, data_buf = None, []

    for raw in lines:
        line = raw.rstrip("\n")
        if line == "":
            _flush()
            continue
        if line.startswith("event:"):
            ev_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_buf.append(line[len("data:"):].strip())
    _flush()
    return events


__all__ = [
    "AgUiEvent",
    "StateUpdate",
    "MessagesSnapshot",
    "InterventionTool",
    "InterventionToolResult",
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_END",
    "REASONING_MESSAGE_START",
    "REASONING_MESSAGE_CONTENT",
    "REASONING_MESSAGE_END",
    "RUN_STARTED",
    "RUN_FINISHED",
    "RUN_ERROR",
    "TOOL_CALL_START",
    "TOOL_CALL_END",
    "TOOL_CALL_RESULT",
    "STATE_SNAPSHOT",
    "STATE_DELTA",
    "MESSAGES_SNAPSHOT",
    "CUSTOM",
    "CONTROL_FILTER_KINDS",
    "encode_frame",
    "encode_frame_wire",
    "TextStreamTracker",
    "encode_frame_wire_streaming",
    "encode_state_snapshot",
    "encode_state_delta",
    "encode_messages_snapshot",
    "encode_intervention_tool_start",
    "encode_intervention_tool_result",
    "intervention_tool_name",
    "decode_event",
    "to_sse",
    "parse_sse_blocks",
]
