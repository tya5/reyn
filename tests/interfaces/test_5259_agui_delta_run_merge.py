"""Tier 2: consecutive agent_delta frames leave one AG-UI connection as one.

The producer emits one ``agent_delta`` per provider chunk — a count the
provider chose, not one reyn defined. When a connection cannot keep up, those
frames sit in its queue and were previously encoded and written one at a time.
This merges whatever is ALREADY waiting; it never waits to see whether more
arrives, so an idle queue behaves exactly as it did before.

The run boundary is ``chain_id`` + ``round_index``, the two fields
``RouterLoop._emit_agent_delta`` stamps so a consumer never re-derives it from
arrival order.
"""
from __future__ import annotations

import asyncio

from reyn.interfaces.transport.agui.endpoint import _SessionFrameSource
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


def _delta(text: str, *, chain_id: str = "c1", round_index: int = 0) -> EventFrame:
    return EventFrame(
        Event(
            type="agent_delta",
            data={"text": text, "chain_id": chain_id, "round_index": round_index},
        )
    )


_END = DisplayFrame(OutboxMessage(kind="__end__", text=""))


def _source_with(frames: list) -> _SessionFrameSource:
    """A source whose queue already holds ``frames``, then the stream terminator.

    ``_SessionFrameSource.__init__`` binds to a live session; this reaches the
    drain through ``__new__`` plus the one attribute it reads, because the
    behaviour under test is the queue-draining rule and nothing else on the
    object participates in it. Standing up a real session to exercise a pure
    queue rule would make the test depend on everything a session needs.

    The terminator is appended so :func:`_drain` ends on the generator's own
    ``return`` — a CONDITION the code reaches, never a wait for one. Without it
    the generator would block on an empty queue and the only way out would be a
    timeout, which is a duration the assertion would then depend on.
    """
    source = _SessionFrameSource.__new__(_SessionFrameSource)
    source._q = asyncio.Queue()
    for f in frames:
        source._q.put_nowait(f)
    source._q.put_nowait(_END)
    return source


def _drain(source: _SessionFrameSource) -> list:
    """Every frame ``frames()`` yields, excluding the appended terminator."""
    out: list = []

    async def _run() -> None:
        async for frame in source.frames():
            out.append(frame)

    asyncio.run(_run())
    assert out and out[-1] is _END, "the generator must end on the terminator"
    return out[:-1]


def test_a_waiting_run_of_deltas_leaves_as_one_frame() -> None:
    """Tier 2: three queued deltas of one run reach the wire as one frame."""
    source = _source_with([_delta("a"), _delta("b"), _delta("c")])

    out = _drain(source)

    assert [f.event.data["text"] for f in out] == ["abc"]


def test_a_single_delta_is_unchanged() -> None:
    """Tier 2: falsification pair — with nothing waiting, the frame that comes
    out is the frame that went in. Without this, a merge that always rebuilt
    the event (dropping fields, renaming text) would still pass the test above.
    """
    only = _delta("solo")
    source = _source_with([only])

    out = _drain(source)

    assert out == [only]


def test_a_different_round_index_ends_the_run() -> None:
    """Tier 2: the boundary between what the model said before a tool call and
    after reading its result is not merged away.
    """
    source = _source_with([
        _delta("before", round_index=0),
        _delta("after", round_index=1),
    ])

    out = _drain(source)

    assert [f.event.data["text"] for f in out] == ["before", "after"]


def test_a_different_chain_id_ends_the_run() -> None:
    """Tier 2: two turns' deltas never merge into one."""
    source = _source_with([_delta("t1", chain_id="c1"), _delta("t2", chain_id="c2")])

    out = _drain(source)

    assert [f.event.data["text"] for f in out] == ["t1", "t2"]


def test_a_display_frame_ends_the_run_and_is_not_lost() -> None:
    """Tier 2: the frame that ends a run was taken off the queue to find the
    boundary — it must still reach the wire, in its own position.
    """
    display = DisplayFrame(OutboxMessage(kind="agent", text="done"))
    source = _source_with([_delta("a"), _delta("b"), display])

    out = _drain(source)

    assert [f.event.data["text"] for f in out[:1]] == ["ab"]
    assert out[1:] == [display]


def test_a_non_delta_event_ends_the_run() -> None:
    """Tier 2: only agent_delta merges — another event kind is left alone."""
    other = EventFrame(Event(type="turn_started", data={"chain_id": "c1"}))
    source = _source_with([_delta("a"), other])

    out = _drain(source)

    assert out[0].event.data["text"] == "a"
    assert out[1] is other


def test_the_merged_frame_keeps_the_runs_identity_fields() -> None:
    """Tier 2: merging replaces the text and nothing else — a consumer that
    routes on chain_id/round_index still finds them.
    """
    source = _source_with([
        _delta("x", chain_id="cX", round_index=7),
        _delta("y", chain_id="cX", round_index=7),
    ])

    out = _drain(source)

    assert out[0].event.data["chain_id"] == "cX"
    assert out[0].event.data["round_index"] == 7
    assert out[0].event.data["text"] == "xy"


def test_merging_does_not_mutate_the_frames_it_merged() -> None:
    """Tier 2: the same Event object is fanned out to every connection's queue,
    so building the merged frame must copy. If this failed, one connection's
    merge would rewrite what a second connection had not yet read.
    """
    first, second = _delta("a"), _delta("b")
    source = _source_with([first, second])

    _drain(source)

    assert first.event.data["text"] == "a"
    assert second.event.data["text"] == "b"


def test_an_end_sentinel_that_ends_a_run_still_terminates() -> None:
    """Tier 2: the terminator is honoured when it is the frame that ENDED a
    run — i.e. when it was pulled off the queue by the drain rather than read
    on its own turn. :func:`_drain` asserts the generator returned, so a
    terminator swallowed by the merge shows up here as a hang-free failure
    rather than a silently truncated stream.
    """
    source = _source_with([_delta("a"), _delta("b")])

    out = _drain(source)

    assert [f.event.data["text"] for f in out] == ["ab"]
