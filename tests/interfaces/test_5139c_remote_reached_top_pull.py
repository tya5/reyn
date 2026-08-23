"""Tier 2: #5139 C — a REMOTE session's ``FlowView.ReachedTop`` pulls the
next-older backlog page over the wire instead of being a permanent no-op
(the pre-#5139 C shape: ``RemoteReadModel.load_older_conversation_history``
always returns 0, and local's own on-disk page-in path
(:meth:`TextualChatApp._extend_older_frames_from_disk`) has nothing to
extend for a remote session, so ``on_flow_view_reached_top`` silently did
nothing past the initial bounded backlog).

Root cause this closes (architect ruling, issuecomment-5383993909): a
bounded initial/switch backlog (:data:`~reyn.interfaces.transport.frames.
HYDRATE_PAGE_FRAMES`) is correct for the wire-send-amount half of #5139,
but WITHOUT a continuation path it silently drops every turn older than
that one page for a remote client — a real feature loss local restore
does not have (:meth:`TextualChatApp.on_flow_view_reached_top`'s own
local page-in). This file covers the CLIENT-DRIVEN pull half: the older
page arrives as a second :class:`~reyn.interfaces.transport.frames.
BacklogBatch` (``is_older_page=True``) through the SAME ``frames()``
stream the initial one did, and is PREPENDED (never appended).

Real ``TextualChatApp`` + a real, minimal ``ClientTransport`` (built on
``ClientTransportStub``, queue-backed so :meth:`request_older_backlog`
can genuinely push a second item onto the SAME stream
``_pump_frames`` is still reading from) — no mocks. ``FlowView.ReachedTop``
is fired by REAL scrolling (``flow.scroll_to_top()``), mirroring
``test_4387_tui_paging_extends_from_disk.py``'s own established pattern,
not a direct handler call.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import BacklogBatch, DisplayFrame, Frame
from reyn.runtime.outbox import OutboxMessage

_APP_DEFAULT_AGENT = "default"
_APP_DEFAULT_SID = "main"


class _PagingTransport(ClientTransportStub):
    """A real, minimal ``ClientTransport`` whose :meth:`frames` is a
    genuinely LIVE queue (mirrors ``AgUiTransport``'s own architecture) so
    :meth:`request_older_backlog` can push a SECOND item onto the exact
    same stream :meth:`frames` is still being read from — never a
    separate/second channel."""

    def __init__(
        self,
        initial_batch: "BacklogBatch",
        *,
        older_batch: "BacklogBatch | None" = None,
    ) -> None:
        self._queue: "asyncio.Queue[Frame | BacklogBatch]" = asyncio.Queue()
        self._queue.put_nowait(initial_batch)
        self._older_batch = older_batch
        self.pull_calls: "list[str]" = []

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[Frame | BacklogBatch]":
        while True:
            yield await self._queue.get()

    async def request_older_backlog(self, before_root_id: str) -> None:
        self.pull_calls.append(before_root_id)
        if self._older_batch is not None:
            self._queue.put_nowait(self._older_batch)

    async def submit_user_text(self, text: str) -> str:
        return ""

    async def answer_intervention_text(self, text: str, **_kw) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str, **_kw) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        pass

    async def cancel_inflight(self) -> str:  # pragma: no cover - trivial
        return ""

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _batch(text: str, *, has_more: bool, next_cursor: "str | None", is_older_page: bool = False) -> "BacklogBatch":
    return BacklogBatch(
        agent=_APP_DEFAULT_AGENT,
        sid=_APP_DEFAULT_SID,
        frames=[DisplayFrame(OutboxMessage(kind="agent", text=text))],
        has_more=has_more,
        next_cursor=next_cursor,
        is_older_page=is_older_page,
    )


@pytest.mark.asyncio
async def test_reached_top_pulls_the_next_older_page_and_prepends_it() -> None:
    """Tier 2: acceptance② — reaching the top pulls the NEXT-older page
    (the cursor the INITIAL batch itself carried) and PREPENDS it, never
    appends (the entry must land ABOVE the initial batch's own entry, not
    below it — an append would put "older" content after "newer",
    silently inverting scrollback order)."""
    initial = _batch("newest turn", has_more=True, next_cursor="turn-c1")
    older = _batch("older turn", has_more=False, next_cursor=None, is_older_page=True)
    transport = _PagingTransport(initial, older_batch=older)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        flow = app.query_one(FlowView)
        for _ in range(4):
            flow.scroll_to_top()
            await pilot.pause()
            await pilot.pause()
            if transport.pull_calls:
                break

        assert transport.pull_calls == ["turn-c1"], (
            f"expected exactly one pull for the initial batch's own cursor, "
            f"got {transport.pull_calls!r}"
        )
        await pilot.pause()
        await pilot.pause()
        texts = [e.item.text for e in app.conversation.entries]

    assert "older turn" in texts and "newest turn" in texts, texts
    assert texts.index("older turn") < texts.index("newest turn"), (
        f"the older page must PREPEND (render ABOVE the initial batch's "
        f"own entry), never append — got order {texts!r}"
    )


@pytest.mark.asyncio
async def test_has_more_false_means_reaching_top_pulls_nothing() -> None:
    """Tier 2: acceptance④ — ``has_more=False`` (the true start already
    reached, or nothing ever received) must be a hard "no more", never a
    request for a page that does not exist — even with a (malformed/
    lingering) non-``None`` cursor still present, so this isolates the
    ``has_more`` guard itself rather than piggy-backing on
    ``next_cursor is None`` alone (production never pairs the two this
    way, but the guard's OWN authority is what this test is for). Strip-
    falsifier: replacing the ``self._remote_backlog_has_more`` guard in
    ``on_flow_view_reached_top`` with a bare ``True`` turns this red — a
    pull fires despite ``has_more=False`` (verified locally)."""
    initial = _batch("only turn", has_more=False, next_cursor="stale-cursor-should-be-ignored")
    transport = _PagingTransport(initial)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        flow = app.query_one(FlowView)
        for _ in range(4):
            flow.scroll_to_top()
            await pilot.pause()
            await pilot.pause()

    assert transport.pull_calls == [], (
        f"has_more=False must never trigger a pull — got {transport.pull_calls!r}"
    )
