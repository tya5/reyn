"""Tier 2: #3288 ③d — AG-UI generic multi-CONTENT streaming.

③b (#3305's sibling PR) put a streamed LLM reply's per-chunk deltas on a
SEPARATE audit-event channel (``agent_delta``), forwarded on the wire as an
opaque ``CUSTOM`` event — a generic AG-UI client could not render it as
streaming text at all; it saw one whole-text ``TEXT_MESSAGE_CONTENT`` only,
after the fact. ③d upgrades the wire encoding so a generic client streams
natively: ``TEXT_MESSAGE_START`` at the first delta, one real
``TEXT_MESSAGE_CONTENT`` per delta, ``TEXT_MESSAGE_END`` at completion — with
NO second full-text CONTENT re-sent at the end (a client that accumulated the
deltas would render the body twice).

Design rulings this file witnesses (issue #3288 ③d comment thread):

1. A streamed message's completion maps to END only (never a duplicate
   full-text CONTENT).
2. Reconstruction authority is the completion's full text, EXCLUSIVELY — the
   deltas are non-persistent derived notifications; the terminal END's
   ``_reyn`` block carries the actual full text, never a delta.
3. The late-joiner window is closed: a connection's ``TextStreamTracker`` is
   scoped to what THAT connection personally observed, so a connection that
   witnessed zero deltas for a chain gets the unchanged whole-message triplet
   (full text on CONTENT) instead of a bare END — no per-client "which deltas
   did you receive" bookkeeping.
4. Local ≡ remote: the reyn client's reconstruction (via ``decode_event``) is
   the same set of Frames whether frames arrive via ``InProcessTransport`` or
   over the AG-UI wire.

Real instances only — the real codec (``protocol.py``), a real ``AgUiEmitter``
over real SSE text, a real ``InProcessTransport`` / ``AgUiTransport`` pair; no
mocks (per testing.md).
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.events import Event, EventLog
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.protocol import (
    TEXT_MESSAGE_CONTENT,
    TEXT_MESSAGE_END,
    TEXT_MESSAGE_START,
    AgUiEvent,
    TextStreamTracker,
    decode_event,
    encode_frame_wire,
    encode_frame_wire_streaming,
    parse_sse_blocks,
)
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.runtime.outbox import OutboxMessage

_CHAIN = "chain-3288d-1"
_PIECES = ["hello ", "streamed ", "world"]
_FULL = "hello streamed world"


def _delta_frames(chain_id: str = _CHAIN, pieces=_PIECES) -> "list[EventFrame]":
    return [
        EventFrame(Event(type="agent_delta", data={"text": p, "chain_id": chain_id}))
        for p in pieces
    ]


def _completion_frame(chain_id: str = _CHAIN, text: str = _FULL) -> DisplayFrame:
    return DisplayFrame(OutboxMessage(kind="agent", text=text, meta={"chain_id": chain_id}))


def _encode_script(frames, tracker: "TextStreamTracker | None" = None) -> "list[AgUiEvent]":
    tracker = tracker if tracker is not None else TextStreamTracker()
    out: list[AgUiEvent] = []
    for f in frames:
        out.extend(encode_frame_wire_streaming(f, tracker))
    return out


async def _frame_source(frames):
    for f in frames:
        yield f


async def _wire_events(frames) -> "list[AgUiEvent]":
    """Route ``frames`` through a REAL ``AgUiEmitter`` and parse the real SSE
    text back into events — exercises the actual production call site
    (``emitter.py``'s ``encode_frame_wire_streaming(frame, self._text_stream)``),
    not a re-implementation of it."""
    emitter = AgUiEmitter(_frame_source(frames), lambda: None)
    sse = "".join([chunk async for chunk in emitter.stream()])
    events = parse_sse_blocks(sse.split("\n"))
    # Drop the reconnect-snapshot preamble (MESSAGES_SNAPSHOT/STATE_SNAPSHOT)
    # and any STATE_DELTA the emitter interleaves — irrelevant to this gate.
    return [e for e in events if e.type not in ("MESSAGES_SNAPSHOT", "STATE_SNAPSHOT", "STATE_DELTA")]


# ---------------------------------------------------------------------------
# 1. No double body on a generic client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streamed_reply_is_start_n_content_end_no_full_text_content() -> None:
    """Tier 2: a streamed reply yields START + N CONTENT + END through the
    REAL AgUiEmitter — and no CONTENT ever carries the accumulated full text
    (only the per-chunk piece)."""
    events = await _wire_events([*_delta_frames(), _completion_frame()])

    assert [e.type for e in events] == [
        TEXT_MESSAGE_START,
        TEXT_MESSAGE_CONTENT,
        TEXT_MESSAGE_CONTENT,
        TEXT_MESSAGE_CONTENT,
        TEXT_MESSAGE_END,
    ]
    contents = [e for e in events if e.type == TEXT_MESSAGE_CONTENT]
    assert [c.data["delta"] for c in contents] == _PIECES
    assert _FULL not in [c.data["delta"] for c in contents], (
        "the full accumulated text must never appear as a CONTENT delta — "
        "a client that rendered the deltas live would double-render the body"
    )
    end = events[-1]
    assert "delta" not in end.data, "END must never carry a text delta"
    # one shared messageId across the whole sequence (spec correlation)
    assert len({e.data.get("messageId") for e in events}) == 1


def test_falling_back_to_the_plain_triplet_would_double_the_body() -> None:
    """Tier 2: non-vacuity (RED without the ③d gate) — the pre-③d
    ``encode_frame_wire`` applied to the SAME completion frame emits a CONTENT
    carrying the FULL text — exactly the double-render a client that already
    rendered the deltas live would suffer, if the emitter fell back to this
    instead of routing the completion through the tracker-gated
    ``encode_frame_wire_streaming``. Demonstrates why gate 1 is load-bearing."""
    plain = encode_frame_wire(_completion_frame())
    content = next(e for e in plain if e.type == TEXT_MESSAGE_CONTENT)
    assert content.data["delta"] == _FULL, (
        "this is the double-render encode_frame_wire_streaming's END-only "
        "mapping exists specifically to avoid"
    )


# ---------------------------------------------------------------------------
# 2. Non-streamed path unchanged
# ---------------------------------------------------------------------------


def test_non_streamed_completion_gets_the_unchanged_whole_message_triplet() -> None:
    """Tier 2: a completion whose chain never streamed on this tracker (no
    prior ``agent_delta`` observed) falls straight through to the SAME
    sequence ``encode_frame_wire`` would produce — byte-identical to
    pre-③d."""
    tracker = TextStreamTracker()
    streamed_path = encode_frame_wire_streaming(_completion_frame(), tracker)
    plain_path = encode_frame_wire(_completion_frame())

    assert [e.type for e in streamed_path] == [e.type for e in plain_path]
    content = next(e for e in streamed_path if e.type == TEXT_MESSAGE_CONTENT)
    assert content.data["delta"] == _FULL
    assert "_reyn" in content.data
    for e in streamed_path:
        if e.type != TEXT_MESSAGE_CONTENT:
            assert "_reyn" not in e.data


@pytest.mark.asyncio
async def test_non_streamed_reply_through_real_emitter_unchanged() -> None:
    """Tier 2: end-to-end through the real AgUiEmitter, a whole (never
    streamed) reply is exactly the pre-③d triplet."""
    events = await _wire_events([_completion_frame()])
    assert [e.type for e in events] == [
        TEXT_MESSAGE_START,
        TEXT_MESSAGE_CONTENT,
        TEXT_MESSAGE_END,
    ]
    content = events[1]
    assert content.data["delta"] == _FULL
    assert "_reyn" not in events[0].data and "_reyn" not in events[2].data


# ---------------------------------------------------------------------------
# 3. ★Late-joiner: a client that connects mid-stream eventually obtains the
#    full text (via the terminal completion's own reconstruction authority).
# ---------------------------------------------------------------------------


def _decoded_agent_display_texts(events) -> "list[str]":
    out = []
    for e in events:
        decoded = decode_event(e.type, e.data)
        if isinstance(decoded, DisplayFrame) and decoded.message.kind == "agent":
            out.append(decoded.message.text)
    return out


def test_late_joiner_that_saw_some_deltas_still_reconstructs_full_text() -> None:
    """Tier 2: ★late-joiner — a connection whose tracker only observed the
    LAST delta (it attached mid-stream, missing START + earlier deltas)
    still, eventually, reconstructs the completion's FULL text — off the
    terminal END's own ``_reyn``, never off the deltas it did see."""
    tracker = TextStreamTracker()
    late_joiner_view = list(_delta_frames(pieces=_PIECES[-1:]))  # missed the rest
    late_joiner_view.append(_completion_frame())

    events = _encode_script(late_joiner_view, tracker)
    texts = _decoded_agent_display_texts(events)
    assert texts == [_FULL], "the late-joiner must obtain the FULL persisted text, not a partial"


def test_late_joiner_that_saw_zero_deltas_also_reconstructs_full_text() -> None:
    """Tier 2: ★late-joiner, the other corner — a connection that witnessed
    NO delta at all for this chain (attached right as the turn finished)
    falls through to the plain whole-message triplet and STILL gets the full
    text — via gate 2's fallback, not via any delta."""
    tracker = TextStreamTracker()
    events = _encode_script([_completion_frame()], tracker)
    assert _decoded_agent_display_texts(events) == [_FULL]


def test_late_joiner_strip_without_end_reyn_loses_the_body() -> None:
    """Tier 2: non-vacuity (RED without the fix) — a LOCAL broken
    reimplementation that reproduces every other part of ③d's streaming
    encode but omits the completion's ``_reyn`` from the END event — i.e. the
    late-joiner-closing mechanism this PR adds — leaves a mid-stream joiner
    with NO reconstructable full text anywhere in the sequence it received.
    Proves the ``_reyn``-on-END step (not just END existing) is load-bearing."""

    def _broken_encode(frame, tracker: TextStreamTracker):
        if isinstance(frame, EventFrame) and frame.event.type == "agent_delta":
            return encode_frame_wire_streaming(frame, tracker)  # deltas unaffected
        if isinstance(frame, DisplayFrame) and frame.message.kind == "agent":
            chain_id = str((frame.message.meta or {}).get("chain_id") or "")
            message_id = tracker.end_stream(chain_id)
            if message_id is not None:
                # BUG: no _reyn attached — the exact step ③d's fix supplies.
                return [AgUiEvent(type=TEXT_MESSAGE_END, data={"messageId": message_id})]
        return encode_frame_wire(frame)

    tracker = TextStreamTracker()
    late_joiner_view = list(_delta_frames(pieces=_PIECES[-1:]))
    late_joiner_view.append(_completion_frame())

    out: list[AgUiEvent] = []
    for f in late_joiner_view:
        out.extend(_broken_encode(f, tracker))

    assert _decoded_agent_display_texts(out) == [], (
        "the broken (no-_reyn-on-END) encoder should leave the late-joiner "
        "with a missing body — proves the real fix's _reyn-on-END is load-bearing"
    )


# ---------------------------------------------------------------------------
# 4. local ≡ remote parity
# ---------------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self) -> None:
        self.repl_outbox: "asyncio.Queue" = asyncio.Queue()
        self.audit_events = EventLog()
        self._cb = None

    def bind_focus_listeners(self, *, on_audit_event=None, intervention_channel=None) -> None:
        self._cb = on_audit_event
        if on_audit_event is not None:
            self.audit_events.add_subscriber(on_audit_event)

    def unbind_focus_listeners(self) -> None:
        if self._cb is not None:
            self.audit_events.remove_subscriber(self._cb)
            self._cb = None

    def attached_session(self):
        return None


def _norm(frames) -> "list[tuple]":
    # ``__end__`` is a control sentinel with an ASYMMETRIC transport
    # disposition BY DESIGN, unrelated to ③d: InProcessTransport forwards it
    # (the local frame queue's own terminator), while the AG-UI emitter
    # consumes-not-forwards it (``CONTROL_FILTER_KINDS`` — the server just
    # stops streaming; the client's SSE loop ends on stream close instead).
    # Excluded here so the comparison is about the ③d streaming content, not
    # this pre-existing, already-covered disposition
    # (``tests/interfaces/test_agui_control_filter.py``).
    out: list[tuple] = []
    for f in frames:
        if isinstance(f, EventFrame):
            out.append(("event", f.event.type, dict(f.event.data)))
        elif isinstance(f, DisplayFrame) and f.message.kind != "__end__":
            out.append(("display", f.message.kind, f.message.text))
    return out


async def _collect(gen) -> list:
    return [f async for f in gen]


async def _via_in_process(script) -> list:
    # #4511: forward each script EventFrame's own Event straight to
    # subscribers, rather than re-emitting it through fake.audit_events
    # (a real EventLog). Re-emitting used to be a harmless passthrough
    # (this EventLog has no agent_id/run_id configured, so nothing got
    # added) -- but EventLog.emit now ALWAYS stamps audit_seq/emitter
    # (#4496 PR-1), which only this path re-emits through, diverging from
    # _via_agui's _frame_source(script) below (which consumes the SAME
    # script's Event objects untouched, never routing through .emit() at
    # all). Both helpers must treat the script's Event objects as
    # already-final so this test's own comparison stays about transport
    # reconstruction, not EventLog's stamping (which has its own coverage,
    # test_4496_pr1_audit_seq.py) -- matches production, where both
    # transports subscribe to ONE session EventLog.emit() call and see
    # identical data either way.
    fake = _FakeRegistry()
    transport = InProcessTransport(fake, intervention_channel="tui")
    transport.start()
    try:
        for f in script:
            if isinstance(f, EventFrame):
                for sub in fake.audit_events.subscribers:
                    sub(f.event)
            else:
                fake.repl_outbox.put_nowait(f.message)
        fake.repl_outbox.put_nowait(OutboxMessage(kind="__end__", text=""))
        return await asyncio.wait_for(_collect(transport.frames()), timeout=2.0)
    finally:
        transport.close()


async def _sse_lines(text):
    for line in text.split("\n"):
        yield line


async def _noop_send(_payload):
    return None


async def _via_agui(script) -> list:
    full = [*script, DisplayFrame(OutboxMessage(kind="__end__", text=""))]
    emitter = AgUiEmitter(_frame_source(full), lambda: None)
    sse = "".join([chunk async for chunk in emitter.stream()])
    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    return await asyncio.wait_for(_collect(transport.frames()), timeout=2.0)


@pytest.mark.asyncio
async def test_local_and_remote_reconstruct_the_identical_streamed_sequence() -> None:
    """Tier 2: ★local ≡ remote — the SAME script (N ``agent_delta`` audit-events
    + the terminal ``kind="agent"`` completion) drives ``InProcessTransport``
    and the AG-UI wire (real ``AgUiEmitter`` → real SSE → real
    ``AgUiTransport``); the reyn client's reconstructed Frame sequence is
    IDENTICAL (same events, same order, same text) across both transports."""
    script = [*_delta_frames(), _completion_frame()]

    ip_frames = await _via_in_process(script)
    ag_frames = await _via_agui(script)

    assert _norm(ip_frames) == _norm(ag_frames)
    # non-trivial: the deltas actually made it across, not just the final text
    assert len([f for f in ag_frames if isinstance(f, EventFrame)]) == len(_PIECES)


@pytest.mark.asyncio
async def test_local_remote_parity_check_is_not_vacuous() -> None:
    """Tier 2: non-vacuity — the comparison in the parity test above actually
    discriminates — dropping one delta from the AG-UI-side script (simulating
    a forwarding regression on the wire side only) makes the two
    reconstructions MISMATCH, proving the assertion is not tautologically
    true regardless of what either side produces."""
    full_script = [*_delta_frames(), _completion_frame()]
    missing_one_delta = [*_delta_frames(pieces=_PIECES[1:]), _completion_frame()]

    ip_frames = await _via_in_process(full_script)
    ag_frames = await _via_agui(missing_one_delta)

    assert _norm(ip_frames) != _norm(ag_frames), (
        "the parity check must be sensitive to a dropped delta — otherwise "
        "it would pass even when local and remote actually diverged"
    )


# ---------------------------------------------------------------------------
# 5. Spec validity — no bare *_CONTENT without its START/END bracket
# ---------------------------------------------------------------------------


def _check_bracket_invariant(events) -> None:
    started: "set[str]" = set()
    for e in events:
        mid = e.data.get("messageId")
        if e.type == TEXT_MESSAGE_START:
            assert mid not in started, f"duplicate START for messageId={mid}"
            started.add(mid)
        elif e.type == TEXT_MESSAGE_CONTENT:
            assert mid in started, f"bare CONTENT with no preceding START: messageId={mid}"
        elif e.type == TEXT_MESSAGE_END:
            assert mid in started, f"END with no preceding START: messageId={mid}"
            started.discard(mid)


@pytest.mark.asyncio
async def test_streamed_sequence_satisfies_the_start_content_end_bracket() -> None:
    """Tier 2: every CONTENT/END produced for a streamed reply has a preceding
    START for the SAME messageId — a strict generic client never drops a bare
    CONTENT."""
    events = await _wire_events([*_delta_frames(), _completion_frame()])
    _check_bracket_invariant(events)


def test_mid_stream_cancel_closes_the_dangling_stream() -> None:
    """Tier 2: a mid-stream cancel (``turn_cancelled``, never reaching a
    ``kind="agent"`` completion — router_loop.py's cancel path emits
    ``kind="system"`` instead) still closes the open stream with a
    defensive END, so the bracket invariant holds even on this edge."""
    tracker = TextStreamTracker()
    script = [
        *_delta_frames(),
        EventFrame(Event(type="turn_cancelled", data={"chain_id": _CHAIN})),
    ]
    events = _encode_script(script, tracker)
    _check_bracket_invariant(events)
    assert TEXT_MESSAGE_END in [e.type for e in events], (
        "the dangling stream must be closed with an END even though the "
        "cancel path never produces a kind=\"agent\" completion"
    )


def test_bracket_checker_is_not_vacuous() -> None:
    """Tier 2: non-vacuity — the checker above actually rejects a bare CONTENT
    with no START — proving it is a real assertion, not a no-op."""
    bare = [AgUiEvent(type=TEXT_MESSAGE_CONTENT, data={"messageId": "orphan"})]
    with pytest.raises(AssertionError):
        _check_bracket_invariant(bare)
