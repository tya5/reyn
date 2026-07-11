"""Tier 2: ChatLifecycleForwarder does NOT emit ``hot_list_updated`` messages.

``on_hot_list_updated`` is not implemented in ChatLifecycleForwarder.
A ``hot_list_updated`` event on the chat_events bus produces no outbox
message (no live consumer: no display surface renders this kind).

The underlying tracker event still fires on ``_chat_events``; only the
outbox-forwarding path is absent.
"""
from __future__ import annotations

import asyncio
import time

from reyn.runtime.lifecycle_forwarder import ChatLifecycleForwarder
from reyn.schemas.models import Event


def test_hot_list_updated_does_not_reach_outbox() -> None:
    """Tier 2: hot_list_updated event produces no outbox message.

    on_hot_list_updated is not present on ChatLifecycleForwarder — the
    event dispatch finds no handler and the outbox stays empty. If the
    handler is silently re-added this test goes RED, catching a dead-emit
    revival before it can leak as a bare-text line through _output_loop.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="hot_list_updated",
        data={"ranking": [
            {"qualified_name": "file__read", "freq": 5, "last_ts": time.time()},
            {"qualified_name": "web__search", "freq": 2, "last_ts": time.time()},
        ]},
    ))
    assert q.empty()


def test_hot_list_updated_empty_ranking_does_not_reach_outbox() -> None:
    """Tier 2: empty ranking hot_list_updated event also produces no outbox message."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="hot_list_updated", data={}))
    assert q.empty()
