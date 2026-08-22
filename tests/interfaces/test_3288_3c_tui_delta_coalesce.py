"""#3288 ③c — the TUI L7 consumer that coalesces streaming "agent_delta"
audit-events into one FlowView entry per reply
(:meth:`~reyn.interfaces.inline.textual_chat.app.TextualChatApp._handle_agent_delta_event`).

This is the LAST phase of the token-streaming arc (③a core streaming, ③b
audit-event emit, ③d AG-UI generic multi-CONTENT — all merged). Nothing
consumed "agent_delta" in the TUI before this phase; ``tests/
test_agent_delta_no_visible_garbage_3288.py`` (③b, re-pointed by this same
PR) covers the core "N deltas -> exactly one coalesced entry" property with
its own arrival witness. This file covers the two gates specific to ③c that
are NOT redundant with that file:

- **★mid-stream-join**: a connection that only starts receiving PARTIAL
  content partway through a reply (it missed the earlier deltas — the ③d
  co-vet's explicit hand-off to ③c: the server side of this was proven in
  ③d, "not double-rendering that case depends on your coalesce") must still
  end with exactly ONE entry, finalized to the completion's full text —
  never a duplicate row from the completion arriving on top of a
  differently-seeded partial entry.
- **stream ≡ whole equivalence at the render level** (the load-bearing gate
  of the whole #3288 arc): streaming ON (deltas + completion) and streaming
  OFF (completion only, no deltas at all — the ``recorded_acompletion``
  non-streaming path) must produce IDENTICAL final rendered content for the
  same reply, through the SAME app code.

Each gate below asserts at a MID-STREAM cross-section (before the terminal
completion arrives) in addition to the post-completion state — asserting
only the post-completion state would be vacuous, since the completion frame
alone creates a one-entry, full-text result regardless of whether any delta
coalescing ever happened (a correction raised mid-PR after an earlier draft
of this gate only asserted post-completion).

Real ``TextualChatApp`` + a real minimal ``ClientTransport`` (mirrors
``tests/interfaces/test_agent_delta_no_visible_garbage_3288.py``'s ``QueueTransport``
idiom) throughout — no ``unittest.mock``. No widget tree / layout change was
made in this phase (the consumer reuses the existing ``kind="agent"`` render
path via ``FlowModel.append`` / ``Entry.set_item``), so no new geometry gate
is required per this arc's established rule (a geometry gate is for
widget-tree/layout changes; this PR adds none).
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.app import _STREAM_REPAINT_MIN_INTERVAL
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class _DrivenClock:
    """The app's own ``clock`` injection point, driven instead of slept through
    (the idiom ``tests/interfaces/test_stream_spinner_3530.py`` uses for the blink).

    ★Load-bearing since #3570: a streamed reply's entry is repainted at most
    once per ``_STREAM_REPAINT_MIN_INTERVAL`` on THIS clock, so "push two deltas
    and read the row" is only a statement about the render if the test says when
    the budget window has passed. Left on the real clock, the assertion below
    passes or fails according to how long ``pilot.pause()`` happens to take on
    the machine — measured 9/20 failures at ~25 ms per pause and 0/20 at ~35 ms,
    which is a coin-flip CI gate, not a gate (the #3473 flake class). The
    accumulated TEXT is never affected either way: it is appended
    unconditionally, and only the ``set_item`` is budgeted."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def past_the_repaint_budget(self) -> None:
        """Move beyond the #3570 repaint window, so the NEXT delta repaints."""
        self.advance(_STREAM_REPAINT_MIN_INTERVAL * 2)


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` fed one frame at a time from a
    queue (mirrors ``tests/interfaces/test_agent_delta_no_visible_garbage_3288.py``'s
    helper of the same name)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

    async def push_display(self, msg: OutboxMessage) -> None:
        await self._queue.put(DisplayFrame(msg))

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str) -> None:  # pragma: no cover
        pass

    async def answer_intervention_text(self, text: str) -> bool:  # pragma: no cover
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:  # pragma: no cover
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _agent_delta(*, chain_id: str, text: str) -> Event:
    return Event(type="agent_delta", data={"text": text, "chain_id": chain_id})


@pytest.mark.asyncio
async def test_mid_stream_join_finalizes_to_one_entry_full_text() -> None:
    """Tier 2: ★a client that joins mid-stream (misses the earlier deltas —
    it only ever sees the TAIL of the partial content) must end with exactly
    ONE entry, finalized to the completion's full text — never a second row
    from the completion landing on top of the partial-seeded entry.

    Mid-stream cross-section FIRST (not vacuous): after the one tail delta
    this "late joiner" DOES receive, there must already be exactly one entry
    whose content is that delta's own (incomplete, non-full) text — proving
    the entry existed and was PARTIAL before the completion ever arrived,
    not fabricated by the completion alone.

    Strip-falsify (recorded in the PR body): reverting
    ``_handle_agent_delta_event`` to a no-op makes the mid-stream
    cross-section fail (no entry created by the tail delta) — the same
    strip as the sibling ③b/③c file's, applied to the late-join framing
    specifically requested by the ③d co-vet hand-off.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        before = len(app.query_one(FlowView).entries)

        # This client attaches mid-stream: the only delta it ever receives is
        # the tail of the reply — a strict, non-trivial substring of the
        # eventual full text, never the whole thing.
        await transport.push_event(
            _agent_delta(chain_id="late-join", text="...and that's the summary.")
        )
        await pilot.pause()

        entries = app.query_one(FlowView).entries
        assert len(entries) == before + 1, (
            "the late-joiner's one tail delta must create exactly one entry — "
            f"count {before} -> {len(entries)}"
        )
        assert entries[-1].item.text == "...and that's the summary.", (
            "the mid-stream cross-section must show the PARTIAL (tail-only) "
            f"text, got {entries[-1].item.text!r} — proves the entry is "
            "seeded by the delta, not fabricated later by the completion"
        )

        full_text = (
            "Here is the full reply this late-joining client never saw the "
            "start of...and that's the summary."
        )
        await transport.push_display(
            OutboxMessage(kind="agent", text=full_text, meta={"chain_id": "late-join"})
        )
        await pilot.pause()

        entries = app.query_one(FlowView).entries
        assert len(entries) == before + 1, (
            "the completion for a mid-stream-joined chain_id must NOT append "
            f"a second entry — count changed to {len(entries)} (double-render)"
        )
        assert entries[-1].item.text == full_text, (
            "the late joiner must end up with the AUTHORITATIVE full text "
            f"(never the partial tail it happened to see), got "
            f"{entries[-1].item.text!r}"
        )


@pytest.mark.asyncio
async def test_stream_on_off_render_equivalence() -> None:
    """Tier 2: ★load-bearing, whole #3288 arc — streaming ON (N deltas then
    the completion) and streaming OFF (the completion alone, exactly what a
    non-streaming-capable provider produces) must render IDENTICAL final
    content through the SAME ``TextualChatApp`` consumer code — streaming is
    a rendering OPTIMIZATION, never a semantic change, at the render layer
    just as ③a proved it at the LLM-result layer.

    Mid-stream cross-section on the streaming-ON app (not vacuous): before
    its completion arrives, the streaming app must show exactly one entry
    with PARTIAL (not yet full) text — proving the deltas actually drove the
    entry rather than the comparison only exercising the (identical either
    way) completion-only path.
    """
    reply_text = "The answer is 42, computed across three steps."

    # Streaming OFF: exactly what a non-capability provider (or ③a's
    # capability gate declining to stream) produces — one completion frame,
    # no deltas at all.
    off_transport = QueueTransport()
    off_app = TextualChatApp(transport=off_transport)
    async with off_app.run_test(size=(100, 30)) as off_pilot:
        await off_pilot.pause()
        await off_transport.push_display(
            OutboxMessage(kind="agent", text=reply_text, meta={"chain_id": "off-chain"})
        )
        await off_pilot.pause()
        off_entries = off_app.query_one(FlowView).entries
        off_final_text = off_entries[-1].item.text
        off_final_count = len(off_entries)

    # Streaming ON: the SAME reply, chunked into deltas, then the SAME
    # completion text.
    on_transport = QueueTransport()
    on_clock = _DrivenClock()
    on_app = TextualChatApp(transport=on_transport, clock=on_clock)
    async with on_app.run_test(size=(100, 30)) as on_pilot:
        await on_pilot.pause()
        before = len(on_app.query_one(FlowView).entries)
        chunks = ["The answer is 42, ", "computed across ", "three steps."]
        for chunk in chunks[:-1]:
            # #3570: each chunk must be past the repaint budget for the
            # mid-stream cross-section below to be a statement about the RENDER
            # (the accumulated text is unconditional either way).
            on_clock.past_the_repaint_budget()
            await on_transport.push_event(_agent_delta(chain_id="on-chain", text=chunk))
            await on_pilot.pause()

        mid_entries = on_app.query_one(FlowView).entries
        assert len(mid_entries) == before + 1, (
            "mid-stream: the ON app must already have exactly one entry "
            f"before its completion arrives — count {before} -> "
            f"{len(mid_entries)}"
        )
        assert mid_entries[-1].item.text == "".join(chunks[:-1]), (
            "mid-stream: the ON app's entry must hold the PARTIAL "
            f"(not-yet-full) coalesced text, got {mid_entries[-1].item.text!r}"
        )
        assert mid_entries[-1].item.text != reply_text, (
            "mid-stream cross-section is vacuous if the partial text already "
            "equals the full text — the chunking must be non-trivial"
        )

        on_clock.past_the_repaint_budget()
        await on_transport.push_event(_agent_delta(chain_id="on-chain", text=chunks[-1]))
        await on_pilot.pause()
        await on_transport.push_display(
            OutboxMessage(kind="agent", text=reply_text, meta={"chain_id": "on-chain"})
        )
        await on_pilot.pause()

        on_entries = on_app.query_one(FlowView).entries
        on_final_text = on_entries[-1].item.text
        on_final_count = len(on_entries) - before

    assert on_final_count == off_final_count == 1, (
        "both streaming ON and OFF must settle to exactly one entry for the "
        f"reply — on={on_final_count} off={off_final_count}"
    )
    assert on_final_text == off_final_text == reply_text, (
        "streaming ON and OFF must render IDENTICAL final content for the "
        f"same reply — on={on_final_text!r} off={off_final_text!r}"
    )
