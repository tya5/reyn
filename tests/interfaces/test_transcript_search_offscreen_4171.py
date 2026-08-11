"""#4171 — transcript text search (``*``/``n``/``N``) must find matches above
the rendered window, not just what has already been scrolled to.

Upstream (textual-flowview <0.17.0) read search rows through the RENDER path:
an entry that had never scrolled into view had no ``Presentation`` yet, so
search saw the placeholder text instead of the entry's real content and
silently missed it — with no indication the result was partial. 0.17.0 adds
``FlowView(search_text=...)``, which lets flowview read an entry's real text
directly, without rendering it, so the whole model is searched. reyn's own
wiring (``search_text=lambda msg: msg.text`` in
``interfaces/inline/textual_chat/app.py``) is what this test pins — not
flowview's own search algorithm (a third party's promise, not reyn's to
test), but that reyn's real :class:`TextualChatApp`, built with real
:class:`OutboxMessage` entries, actually finds a message that was never
rendered.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.runtime.outbox import OutboxMessage
from tests._support.textual_chat_test_helpers import QueueTransport

# Enough filler entries that, with a small terminal and the transcript's
# default sticky-bottom anchor, the FIRST entry (the needle) is nowhere near
# the initially-rendered band — it has never been presented at mount.
_FILLER_COUNT = 300


@pytest.mark.asyncio
async def test_search_finds_a_match_that_has_never_been_rendered() -> None:
    """Tier 2: reyn's real FlowView wiring (search_text=lambda msg: msg.text)
    finds a real OutboxMessage's text even when that entry was never scrolled
    to — the exact invariant #4171's flowview 0.17.0 bump exists to restore."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await transport.push(
            OutboxMessage(kind="agent", text="the quokka guards the archive")
        )
        for i in range(_FILLER_COUNT):
            await transport.push(OutboxMessage(kind="agent", text=f"filler line {i}"))
        await pilot.pause()

        flow = app._flow
        # Sanity: the needle entry is genuinely off-screen at mount — the
        # viewport is scrolled to the bottom (sticky-bottom anchor) and only
        # shows entries from near the end of the (now 301-entry) transcript.
        assert flow.row_count > _FILLER_COUNT

        found = await flow.search("quokka", forward=True)
        assert found, (
            "search for text that exists ONLY in the first (never-rendered) "
            "entry returned no match — the placeholder-text regression #4171 "
            "fixed (flowview 0.17.0 search_text=) is back"
        )

        # Negative control: a string that exists nowhere in the model must
        # still report not-found — proves the positive assertion above isn't
        # a vacuous "search always returns True".
        not_found = await flow.search("no-such-word-anywhere", forward=True)
        assert not not_found
