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

THE FIX: the unconditional clear was removed. But the clear had ALSO been the
codebase's ONLY size bound on this cache (no other prune/maxsize existed
anywhere) — naively removing it would trade the freeze for an unbounded
memory leak proportional to total-distinct-turns-ever, worsening in the exact
same "long session" scenario. The cache is now LRU-bounded
(`_TOKEN_CACHE_MAXSIZE`) instead: warm entries survive compaction (the perf
fix) AND the cache never exceeds its bound (the memory fix).

Asserted via PUBLIC-surface proxies, not the private `_token_cache` dict
(Tier 4 forbids private-state assertions): "was this a cache hit" is observed
by counting real underlying tokenizer invocations (`litellm.token_counter`);
"is the cache bounded" is observed via `token_cache_size()`.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.services.compaction import engine as engine_mod
from reyn.services.compaction.engine import (
    CompactionEngine,
    HistoryChunkToCompact,
    estimate_tokens,
    token_cache_size,
)


@pytest.fixture(autouse=True)
def _clean_token_cache():
    """Tier 2 hygiene: `_token_cache` is a module-global shared across every
    test in the process — clear it before each test so exact size/eviction
    assertions aren't polluted by entries other tests warmed. Setup/teardown,
    not an assertion (Tier 4 forbids asserting on private state, not touching
    it for isolation — the same pattern as this session's `_clean_task_polls`
    for `_TASK_POLLS`)."""
    engine_mod._token_cache.clear()
    yield
    engine_mod._token_cache.clear()


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


def test_cache_never_exceeds_its_bound(monkeypatch) -> None:
    """Tier 2: falsifying memory-leak guard. Shrink the bound to 5 and
    push 20 distinct texts through the cache — `token_cache_size()` must never
    exceed 5. A regression to an unbounded dict (the naive "just remove the
    clear" fix) would let this grow to 20 and fail.

    Verified locally: with LRU eviction (`_token_cache_put`) stubbed to a
    no-op bound-check, this genuinely goes RED — the bound is not a lucky
    accident of test ordering.
    """
    monkeypatch.setattr(engine_mod, "_TOKEN_CACHE_MAXSIZE", 5)
    counts: dict = {}
    monkeypatch.setattr("litellm.token_counter", _counting_token_counter(counts))

    for i in range(20):
        estimate_tokens(f"distinct historical turn number {i}", "test-model-bound", use_chars4=False)
        assert token_cache_size() <= 5, (
            f"cache grew to {token_cache_size()} entries after {i + 1} distinct "
            "texts — the LRU bound is not being enforced (memory-leak regression)"
        )

    assert token_cache_size() == 5  # settled at exactly the bound, not below it
    expected_texts = {f"distinct historical turn number {i}" for i in range(20)}
    assert set(counts) == expected_texts  # all 20 were really tokenized (no false hits/misses)


def test_recently_used_entry_survives_eviction_pressure(monkeypatch) -> None:
    """Tier 2: LRU (recency), not FIFO (insertion order) — re-touching an old
    entry before the bound is exceeded keeps it alive past newer entries that
    are never touched again. Proves eviction targets the LEAST-recently-used
    entry, not simply the oldest-inserted one."""
    monkeypatch.setattr(engine_mod, "_TOKEN_CACHE_MAXSIZE", 3)
    counts: dict = {}
    monkeypatch.setattr("litellm.token_counter", _counting_token_counter(counts))
    model = "test-model-lru-order"

    estimate_tokens("A", model, use_chars4=False)
    estimate_tokens("B", model, use_chars4=False)
    estimate_tokens("C", model, use_chars4=False)
    # Bound is 3, all present. Re-touch "A" — it becomes MRU, "B" becomes LRU.
    estimate_tokens("A", model, use_chars4=False)
    assert counts.get("A") == 1  # the re-touch was itself a cache hit

    # Insert a 4th distinct text — must evict "B" (the LRU), not "A" or "C".
    estimate_tokens("D", model, use_chars4=False)
    assert token_cache_size() == 3

    estimate_tokens("A", model, use_chars4=False)
    assert counts.get("A") == 1  # still a hit — "A" survived eviction
    estimate_tokens("B", model, use_chars4=False)
    assert counts.get("B") == 2  # "B" was evicted — re-tokenized (a miss)


def test_cache_is_derived_not_a_recovery_source(monkeypatch) -> None:
    """Tier 2: crash-recovery witness. `_token_cache` is IN-MEMORY-ONLY,
    DERIVED from history text — not a WAL/snapshot-backed recovery source, so
    the #1983 truncate-falsify gate (CLAUDE.md's recovery-feature PR rule)
    does not apply to it. This proves the derivation property directly: an
    entry lost from a cold/empty cache (the crash scenario — the process
    restarts with a fresh, empty `_token_cache`) recomputes to the IDENTICAL
    value from the same text, with no external state needed beyond the text
    itself (which `history.jsonl` already durably holds independently of this
    cache)."""
    counts: dict = {}
    monkeypatch.setattr("litellm.token_counter", _counting_token_counter(counts))
    model = "test-model-crash-repopulate"
    text = "a historical turn that must survive a cold restart"

    before_crash = estimate_tokens(text, model, use_chars4=False)
    assert token_cache_size() == 1

    # Simulate a process restart: the cache starts fresh (nothing persisted to
    # carry over — this IS the recovery path, not a special-cased one).
    engine_mod._token_cache.clear()
    assert token_cache_size() == 0

    after_crash = estimate_tokens(text, model, use_chars4=False)
    assert after_crash == before_crash  # recomputed from the SAME durable text
    assert counts.get(text) == 2  # a genuine re-tokenization, not a stale hit
