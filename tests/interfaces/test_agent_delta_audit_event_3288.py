"""Tier 2: #3288 ③b — the "agent_delta" audit-event, end-to-end through a real
Session + RouterLoop.

The owner's ratified decision (issue #3288 comment thread) replaces the
original ADR L4 wording ("add ``agent_delta`` to the ``OutboxMessage`` closed
vocabulary") with an audit-event route: a partial rides ``host.events`` (the
SAME audit-event channel ``user_submitted`` / ``router_represent_round``
already use), never ``OutboxMessage``. This file witnesses the two invariants
that decision must preserve:

1. **L9 whole-persist** — history receives the completed full text EXACTLY
   ONCE; a streamed turn's per-chunk deltas never append to history.
2. **stream≡whole surface parity** — the terminal outbox message a client
   sees is byte-identical to the non-streaming shape (kind="agent", the FULL
   text, exactly once) regardless of how many deltas preceded it.

``call_llm_tools`` is patched at ``reyn.runtime.router_loop.call_llm_tools``
with a real async callable (never a mock, per testing.md) that invokes the
``on_content_delta`` keyword callback RouterLoop wires in
(``RouterLoop._emit_agent_delta``) before returning the final
``LLMToolCallResult`` — this exercises the REAL production wiring
(``router_loop.py``'s call site → ``RouterHostAdapter.events`` →
``Session._audit_events``), not a re-implementation of it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from reyn.core.events.events import Event
from reyn.interfaces.transport.agui.profile import is_profiled
from reyn.interfaces.transport.agui.protocol import CUSTOM, encode_frame
from reyn.interfaces.transport.frames import EventFrame, forwarded_frame_kinds
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

_EMPTY_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)
_FULL_TEXT = "hello streamed world"
_PIECES = ["hello ", "streamed ", "world"]


class _EventSink:
    """A real (non-mock) audit-event subscriber — a plain callback collector
    (mirrors ``tests/interfaces/test_user_submitted_render_3300.py``'s ``_EventSink``)."""

    def __init__(self) -> None:
        self.events: list = []

    def __call__(self, event) -> None:
        self.events.append(event)


def _make_session(tmp_path: Path) -> Session:
    return make_session(agent_name="test_agent")


def _drain_outbox(session: Session) -> list:
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    return msgs


def _run(coro):
    return asyncio.run(coro)


def test_agent_delta_is_forwarded_and_profiled_on_the_wire() -> None:
    """Tier 1: "agent_delta" is in the transport's forward-set
    (``forwarded_frame_kinds()`` — both ``InProcessTransport`` and the AG-UI
    endpoint filter against this, so absence here means it never reaches
    EITHER client) AND its encoded CUSTOM name is a profiled ``reyn.event.*``
    entry (``tests/interfaces/test_agui_profile_completeness.py`` enforces this
    generically for every forwarded etype; this is the etype-specific pin the
    ③b PR is responsible for)."""
    assert "agent_delta" in forwarded_frame_kinds()

    ev = encode_frame(EventFrame(Event(type="agent_delta", data={"text": "x"})))
    assert ev.type == CUSTOM
    assert ev.data["name"] == "reyn.event.agent_delta"
    assert is_profiled(ev.data["name"])


def test_agent_delta_events_fire_and_history_stays_whole_persist(tmp_path, monkeypatch) -> None:
    """Tier 2: streamed deltas surface as "agent_delta" audit-events, in order,
    while the outbox + history stay whole-persist (exactly once, full text) —
    the L9 invariant ③b must not disturb."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    sink = _EventSink()
    session.subscribe_audit_events(sink)

    async def fake_llm(*args, **kwargs):
        on_delta = kwargs.get("on_content_delta")
        assert on_delta is not None, (
            "RouterLoop must thread on_content_delta into call_llm_tools "
            "for this to be a real streaming-wiring witness"
        )
        # #5261: on_content_delta is now called per MERGED batch, carrying
        # ①raw_chunk_count and ②first/last arrival. This fake stand-in
        # drives call sites directly (it doesn't exercise the real
        # ``_stream_and_reconstruct`` merge machinery — that's covered in
        # ``tests/llm/test_llm_streaming_delta_emission_3288.py``), so each
        # piece here stands in as its OWN unmerged batch of 1 raw chunk —
        # the same "no field ⇒ 1" fact ``backend.py``'s summing fix relies
        # on for a caller that predates #5261's merging.
        for piece in _PIECES:
            now = datetime.now().astimezone()
            on_delta(piece, raw_chunk_count=1, first_arrival=now, last_arrival=now)
        return LLMToolCallResult(
            content=_FULL_TEXT, tool_calls=[], finish_reason="stop", usage=_EMPTY_USAGE,
        )

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", fake_llm)

    _run(session._handle_inbox_text("hi", chain_id="chain-delta-1"))

    # (1) one "agent_delta" audit-event per piece, in order, carrying the raw
    # per-chunk text — the non-vacuity witness that streaming actually ran
    # (not just that on_content_delta was accepted and ignored).
    delta_events = [e for e in sink.events if e.type == "agent_delta"]
    assert [e.data.get("text") for e in delta_events] == _PIECES
    assert all(e.data.get("chain_id") == "chain-delta-1" for e in delta_events)
    # #5261: architect's mandatory condition — every "agent_delta" event
    # carries the raw chunk count and arrival window it stands in for, all
    # the way out to the audit-event, not just inside RouterLoop.
    assert all(e.data.get("raw_chunk_count") == 1 for e in delta_events)
    assert all(e.data.get("first_arrival") and e.data.get("last_arrival") for e in delta_events)

    # (2) the outbox NEVER carries a partial — exactly one kind="agent"
    # message, the FULL text. An "agent_delta" outbox entry never appears
    # (it structurally cannot: OutboxMessage.__post_init__ would raise on an
    # unregistered kind — the closed vocabulary is a second, independent
    # backstop against this regression).
    msgs = _drain_outbox(session)
    agent_msgs = [m for m in msgs if m.kind == "agent"]
    assert [m.text for m in agent_msgs] == [_FULL_TEXT]
    assert not [m for m in msgs if m.kind == "agent_delta"]

    # (3) L9 whole-persist: history receives the FULL text EXACTLY ONCE —
    # never once per delta (3 pieces streamed, 1 history append).
    assistant_turns = [m for m in session.history if m.role == "assistant"]
    assert [m.content for m in assistant_turns] == [_FULL_TEXT]


def test_non_streaming_turn_emits_no_agent_delta_events(tmp_path, monkeypatch) -> None:
    """Tier 2: non-vacuity witness — a turn whose ``call_llm_tools`` stand-in
    never invokes ``on_content_delta`` (the non-capable / non-streaming case)
    produces ZERO "agent_delta" audit-events, proving the previous test's
    events came specifically from the callback firing, not from generic
    per-turn event emission."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    sink = _EventSink()
    session.subscribe_audit_events(sink)

    async def fake_llm(*args, **kwargs):
        return LLMToolCallResult(
            content="no streaming here", tool_calls=[], finish_reason="stop",
            usage=_EMPTY_USAGE,
        )

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", fake_llm)

    _run(session._handle_inbox_text("hi", chain_id="chain-nodelta"))

    assert not [e for e in sink.events if e.type == "agent_delta"]
    agent_msgs = [m for m in _drain_outbox(session) if m.kind == "agent"]
    assert [m.text for m in agent_msgs] == ["no streaming here"]
