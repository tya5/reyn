"""Tier 2: #4403 — ``RouterHistoryBuffer``'s elide-check total is computed
incrementally, not by re-estimating every turn's token cost on every call.

Measured (real litellm tokenizer, the config default
``use_chars4_estimate=False``): 5.13ms/turn -> 559s at 108,896 turns,
because ``build_history()`` re-summed ALL turns EVERY call to check "total
<= effective_trigger". ``_TOKEN_CACHE_MAXSIZE=8192``'s per-(model,text)
cache could not help: the loop visits turns in the SAME order every call,
so an 8192-entry FIFO always evicts the early turns before a 100k-turn
pass reaches them again next call — 100% miss.

Correctness is proven by comparing the incremental total (observed via the
real ``elide_evaluated`` audit event, the SAME public observation point
``test_2957_prb_elide_advisor_token_unification.py`` already established —
never the private ``_cached_elide_*`` fields) against a from-scratch
canonical recompute, across the real invalidation triggers: growth (the
common case), and shrink/reorder (simulating a rewind, #4387's own concern
for this ``history_fn``).

Cost is proven by counting real ``estimate_tokens_for_any_turn`` calls via
a counting wrapper (mirrors ``test_compaction_token_cache_incremental.py``'s
own ``_counting_token_counter`` technique for this exact module) — the
SECOND call over a grown history must cost proportionally to the NEW
turns only, not the whole conversation again.

Real ``RouterHistoryBuffer`` + real ``ChatMessage`` throughout — no fakes.
"""
from __future__ import annotations

import pytest

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer

_MODEL = "gpt-3.5-turbo"


def _turns(n: int, *, start: int = 1) -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content=f"turn {i} " + ("x" * 40), seq=i)
        for i in range(start, start + n)
    ]


def _make_buf(history: list[ChatMessage], events: EventLog) -> RouterHistoryBuffer:
    return RouterHistoryBuffer(
        history_fn=lambda: history,
        compaction=CompactionConfig(use_chars4_estimate=True),
        compaction_controller=None,
        model_fn=lambda: _MODEL,
        events=events,
        media_store=None,
        router_host=None,
        universal_wrappers_enabled=False,  # #4552 PR-3
        non_interactive=True,
    )


async def _latest_elide_total(events: EventLog, collected: list) -> int:
    """#4961 C: dispatch moved off of ``emit()``'s synchronous caller onto
    a queue-consumer task — yields once (``await asyncio.sleep(0)``) so
    the consumer has actually run and ``collected`` reflects every emit
    that happened before this call, in order (FIFO queue), before reading
    it."""
    await events.drain()
    matches = [e for e in collected if e.type == "elide_evaluated"]
    assert matches, "build_history() must have emitted elide_evaluated"
    return matches[-1].data["total"]


def _subscribed(events: EventLog) -> list:
    collected: list = []
    events.add_subscriber(lambda e: collected.append(e))
    return collected


@pytest.mark.asyncio
async def test_incremental_total_matches_a_from_scratch_recompute_after_growth() -> None:
    """Tier 2: append new turns (the common case — a real conversation
    extending), call build_history() again — the incrementally-derived
    total must equal what a FRESH buffer (no cache) computes from scratch
    over the exact same (now-longer) history."""
    history = _turns(50)
    events = EventLog()
    collected = _subscribed(events)
    buf = _make_buf(history, events)
    buf.build_history()  # first call: populates the cache

    history.extend(_turns(30, start=51))  # grow — mutates the SAME list history_fn reads
    buf.build_history()  # second call: must extend incrementally
    incremental_total = await _latest_elide_total(events, collected)

    fresh_events = EventLog()
    fresh_collected = _subscribed(fresh_events)
    fresh_buf = _make_buf(list(history), fresh_events)  # independent buffer, no cache
    fresh_buf.build_history()
    canonical_total = await _latest_elide_total(fresh_events, fresh_collected)

    assert incremental_total == canonical_total


@pytest.mark.asyncio
async def test_incremental_total_recomputes_correctly_after_a_shrink() -> None:
    """Tier 2: #4387's own concern for this history_fn — a rewind can make
    the ``turns`` list SHORTER than what was cached. The cache must detect
    this and recompute from scratch, not silently keep serving a total
    that includes turns no longer present."""
    history = _turns(50)
    events = EventLog()
    collected = _subscribed(events)
    buf = _make_buf(history, events)
    buf.build_history()

    history[:] = _turns(10)  # shrink — simulates a rewind cutting most turns
    buf.build_history()
    incremental_total = await _latest_elide_total(events, collected)

    fresh_events = EventLog()
    fresh_collected = _subscribed(fresh_events)
    fresh_buf = _make_buf(list(history), fresh_events)
    fresh_buf.build_history()
    canonical_total = await _latest_elide_total(fresh_events, fresh_collected)

    assert incremental_total == canonical_total


@pytest.mark.asyncio
async def test_incremental_total_recomputes_when_the_prefix_reorders_at_the_same_length() -> None:
    """Tier 2: #4387's harder rewind case — a branch-switch can produce a
    SAME-LENGTH but DIFFERENT list (the boundary seq no longer matches what
    was cached). Length alone cannot catch this; the seq check must."""
    history = _turns(20)
    events = EventLog()
    collected = _subscribed(events)
    buf = _make_buf(history, events)
    buf.build_history()

    # Same length, but the entry at the cached boundary position now has a
    # DIFFERENT seq (as if turn 20 belonged to an abandoned branch and a
    # different turn 20 from another branch is now active).
    history[:] = _turns(19) + [ChatMessage(role="user", content="alt turn 20", seq=999)]
    buf.build_history()
    incremental_total = await _latest_elide_total(events, collected)

    fresh_events = EventLog()
    fresh_collected = _subscribed(fresh_events)
    fresh_buf = _make_buf(list(history), fresh_events)
    fresh_buf.build_history()
    canonical_total = await _latest_elide_total(fresh_events, fresh_collected)

    assert incremental_total == canonical_total


def test_second_call_over_a_grown_history_estimates_only_the_new_turns(monkeypatch) -> None:
    """Tier 2: the actual cost claim — counts real
    estimate_tokens_for_any_turn calls via a counting wrapper (mirrors
    test_compaction_token_cache_incremental.py's own technique for this
    module). The second call, after 30 new turns were appended to an
    existing 50, must cost ~30 calls, not ~80."""
    import reyn.services.compaction.engine as engine_module

    real_fn = engine_module.estimate_tokens_for_any_turn
    call_count = {"n": 0}

    def _counting(wt, model, *, use_chars4):
        call_count["n"] += 1
        return real_fn(wt, model, use_chars4=use_chars4)

    history = _turns(50)
    events = EventLog()
    buf = _make_buf(history, events)
    buf.build_history()  # first call — cold cache, cost not counted (wrapper not installed yet)

    monkeypatch.setattr(engine_module, "estimate_tokens_for_any_turn", _counting)
    history.extend(_turns(30, start=51))
    buf.build_history()  # second call — must only estimate the 30 NEW turns

    assert call_count["n"] == 30, (
        f"expected exactly 30 new-turn estimates (incremental), got {call_count['n']} "
        "(80 would mean the cache re-estimated the whole history again)"
    )
