"""Tier 2: ChatLifecycleForwarder routes ``hot_list_updated`` events (#192).

The :class:`~reyn.tools.action_usage_tracker.ActionUsageTracker` emits a
``hot_list_updated`` event whenever the compacted ranking's qualified-name
order changes. This file pins the forwarder-side contract:

  - ``hot_list_updated`` event → ``OutboxMessage(kind="hot_list_updated")``
    with the full ranking carried in ``meta["ranking"]``.
  - Empty / missing ranking → outbox message with ``ranking=[]``
    (= subscribers treat this as a reset signal).

Tracker-side callback semantics (= when the order actually changes,
freq+last_ts payload shape, exception swallowing) live in
``tests/test_action_usage_tracker.py``. This file is forwarder-only.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from reyn.runtime.lifecycle_forwarder import ChatLifecycleForwarder
from reyn.schemas.models import Event


def _drain(q: asyncio.Queue) -> list[Any]:
    items: list[Any] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def test_lifecycle_forwarder_forwards_hot_list_updated() -> None:
    """Tier 2: on_hot_list_updated → OutboxMessage(kind=hot_list_updated)."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="hot_list_updated",
        data={"ranking": [
            {"qualified_name": "file__read", "freq": 5, "last_ts": time.time()},
            {"qualified_name": "web__search", "freq": 2, "last_ts": time.time()},
        ]},
    ))
    (msg,) = _drain(q)
    assert msg.kind == "hot_list_updated"
    assert msg.text == ""  # data signal, not display
    ranking = msg.meta["ranking"]
    assert [r["qualified_name"] for r in ranking] == ["file__read", "web__search"]
    assert ranking[0]["freq"] == 5


def test_lifecycle_forwarder_empty_ranking_ok() -> None:
    """Tier 2: empty / missing ranking still emits with [] (= signals reset)."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="hot_list_updated", data={}))
    (msg,) = _drain(q)
    assert msg.meta["ranking"] == []
