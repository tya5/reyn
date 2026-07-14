"""Tier 2: CompactionEngine.compact() must NOT wipe the token-estimate cache.

THE BUG: `compact()` used to unconditionally `_token_cache.clear()` at its start
("fresh estimates each compaction"). The cache key is `(model, hash(text))` and
`use_chars4_estimate` is a fixed per-session config (never toggled mid-session)
— text is immutable and the tokenizer/fallback choice never changes underneath
an existing entry, so a cached count is valid for the process's lifetime. The
blanket clear forced the NEXT `build_history()` call (which re-estimates
tokens across the WHOLE raw history every turn to check the elide trigger) to
recompute from a cold cache — synchronous, on the event loop, for the entire
conversation. On a long chat this froze the inline CUI's event loop for real,
user-visible seconds, and recurred every time compaction fired again as the
conversation kept growing.

THE FIX: the clear was removed — the cache is now naturally incremental
(warm entries survive compaction; only genuinely new text is a miss).

Asserted via the PUBLIC-surface proxy for "was this a cache hit": count real
underlying tokenizer invocations (`litellm.token_counter`), not the private
`_token_cache` dict (Tier 4 — no private-state assertions).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.services.compaction.engine import (
    CompactionEngine,
    HistoryChunkToCompact,
    estimate_tokens,
)


def _resp(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _counting_token_counter(counts: dict):
    """Real callable (not a mock) standing in for litellm.token_counter, so a
    cache hit vs miss is observable by call count instead of reaching into the
    private `_token_cache` dict."""
    def _counter(*, model: str, text: str) -> int:
        counts[text] = counts.get(text, 0) + 1
        return max(1, len(text) // 4)
    return _counter


def test_compact_does_not_clear_the_token_cache(monkeypatch) -> None:
    """Tier 2: a token estimate cached BEFORE compact() runs is served from
    cache (the underlying tokenizer is NOT invoked a second time) AFTER
    compact() returns — the load-bearing fix. A regression to
    `_token_cache.clear()` at the top of `compact()` would make this fail
    (the tokenizer would be called again for the same text)."""
    model = "test-model-cache-a"
    warm_text = "this text was estimated before any compaction ran"
    counts: dict = {}
    monkeypatch.setattr("litellm.token_counter", _counting_token_counter(counts))

    # Warm the cache for text UNRELATED to this compaction's own input
    # (simulates the rest of a long conversation's already-estimated turns).
    warm_value = estimate_tokens(warm_text, model, use_chars4=False)
    assert counts.get(warm_text) == 1  # one real tokenizer call so far

    async def _capture(**kwargs):
        return _resp(json.dumps({
            "topic_arc": "arc", "new_turn_seqs": [1],
            "decisions": [], "pending": [],
            "session_user_facts": [], "artifacts_referenced": [],
        }))

    monkeypatch.setattr("litellm.acompletion", _capture)

    engine = CompactionEngine(
        model=model,
        events=EventLog(),
        cfg=CompactionConfig(use_chars4_estimate=False),
    )
    chunk = HistoryChunkToCompact(
        previous_summary=None,
        new_turns=[{"role": "user", "text": "hi", "seq": 1}],
        section_token_caps={},
    )
    asyncio.run(engine.compact(chunk))

    # Re-estimating the SAME text after compact() must be a cache HIT: same
    # value, and the tokenizer is NOT invoked again (still exactly 1 call).
    assert estimate_tokens(warm_text, model, use_chars4=False) == warm_value
    assert counts.get(warm_text) == 1, (
        f"tokenizer was invoked {counts.get(warm_text)} times for unchanged "
        "text — compact() re-cleared the cache (regression)"
    )


def test_repeated_estimate_of_unchanged_text_is_a_cache_hit_across_compactions(monkeypatch) -> None:
    """Tier 2: end-to-end incrementality — estimating the SAME text across TWO
    separate compact() calls invokes the real tokenizer only ONCE. This is the
    actual UX property the fix restores: repeated history turns are priced
    once, not re-priced every compaction cycle."""
    model = "test-model-cache-b"
    text = "a stable historical turn that never changes"
    counts: dict = {}
    monkeypatch.setattr("litellm.token_counter", _counting_token_counter(counts))

    first = estimate_tokens(text, model, use_chars4=False)

    async def _capture(**kwargs):
        return _resp(json.dumps({
            "topic_arc": "arc", "new_turn_seqs": [1],
            "decisions": [], "pending": [],
            "session_user_facts": [], "artifacts_referenced": [],
        }))

    monkeypatch.setattr("litellm.acompletion", _capture)
    engine = CompactionEngine(
        model=model, events=EventLog(),
        cfg=CompactionConfig(use_chars4_estimate=False),
    )
    chunk = HistoryChunkToCompact(
        previous_summary=None, new_turns=[{"role": "user", "text": "t1", "seq": 1}],
        section_token_caps={},
    )
    asyncio.run(engine.compact(chunk))
    asyncio.run(engine.compact(chunk))  # a SECOND compaction cycle

    second = estimate_tokens(text, model, use_chars4=False)
    assert second == first
    assert counts.get(text) == 1  # never re-tokenized across either compaction
