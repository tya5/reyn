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

- **The mapping is a table, derived, not hand-listed at the call site.** The
  ``kind``/``type`` → AG-UI-event-type tables (:data:`_DISPLAY_KIND_EVENT` /
  :data:`_EVENT_TYPE_EVENT`) are the single source the completeness gate reads;
  an unmapped kind falls back to :data:`CUSTOM` and still round-trips (lossless
  by construction), so a new renderer kind can never silently vanish on the wire.

STATE (the status read-model) and MESSAGES snapshots ride the same SSE stream but
are not ``Frame``\\s — they decode to :class:`StateUpdate` / :class:`MessagesSnapshot`
so the client demuxes render frames from the status side-channel.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from reyn.core.events.events import Event
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame, Frame
from reyn.runtime.outbox import OutboxMessage

# ── AG-UI event type names (hand-rolled; the SDK is not a dependency) ─────────
TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
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
    "error": RUN_ERROR,
    "intervention": CUSTOM,
    "trace": CUSTOM,
    "presentation": CUSTOM,
}

# EventFrame ``Event.type`` → AG-UI event type. turn_* → RUN_*; tool_* →
# TOOL_CALL_*; ``user_answered_intervention`` is reyn-private → CUSTOM. Every
# entry here is one of the eight ``renderer_chat_events()`` the transport
# forwards; the completeness gate binds the two independently.
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
    """A decoded MESSAGES_SNAPSHOT — the reconnect display backlog (A4)."""

    frames: list = field(default_factory=list)


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


def _display_event_type(kind: str) -> str:
    return _DISPLAY_KIND_EVENT.get(kind, CUSTOM)


def _event_event_type(etype: str) -> str:
    return _EVENT_TYPE_EVENT.get(etype, CUSTOM)


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
        std = {"role": "assistant" if kind == "agent" else "status", "delta": text}
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
    if ag_type in (TOOL_CALL_START, TOOL_CALL_END):
        std = {"toolName": edata.get("tool"), "step": edata.get("tool")}
    elif ag_type in (RUN_STARTED, RUN_FINISHED):
        std = {"phase": etype}
    else:  # CUSTOM — user_answered_intervention et al.
        std = {"name": f"reyn.event.{etype}", "value": edata}
    return AgUiEvent(type=ag_type, data={**std, _REYN: reyn})


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


def encode_messages_snapshot(frames: "list[Frame]") -> AgUiEvent:
    """MESSAGES_SNAPSHOT — the display backlog replayed on connect (A4)."""
    payload = [_encode_display(f).data[_REYN] for f in frames if isinstance(f, DisplayFrame)]
    return AgUiEvent(
        type=MESSAGES_SNAPSHOT,
        data={"messages": payload, _REYN: {"frame": "messages", "messages": payload}},
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
        return DisplayFrame(
            OutboxMessage(
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
        frames = [
            DisplayFrame(
                OutboxMessage(
                    kind=m.get("kind", ""),
                    text=m.get("text", ""),
                    meta=dict(m.get("meta") or {}),
                )
            )
            for m in (reyn.get("messages") or [])
        ]
        return MessagesSnapshot(frames=frames)
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
    "TEXT_MESSAGE_CONTENT",
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
    "encode_frame",
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
