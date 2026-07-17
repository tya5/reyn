"""Tier 2: #2939 — building the router view must materialise its history producer
ONCE per call, not once per internal consumer.

``RouterHistoryBuffer._history_fn`` is not a cheap accessor. In production it is
``Session._active_branch_history``: a rewind-aware view recomputed over the whole
conversation on every invocation. ``build_history`` fetched it, then
``_latest_summary`` silently fetched it AGAIN (and a third time on the elide
path, which calls ``_latest_summary`` a second time) — so the single most
expensive step on the turn's hot path ran 2-3x per turn. #2940 measured that
producer at ~99.7% of a context-dropdown open, making the multiplier a direct
2-3x on the user-visible cost.

The invariant is the call count, not a duration: how long the producer takes is
environment-dependent, but "each ``build_history`` materialises the view exactly
once" is a property of the code's shape and is what stops a re-multiplication
from being reintroduced by a future caller that reaches for ``self._history_fn()``
instead of threading the list it already has.

Real seam: the real ``RouterHistoryBuffer`` and the real ``CompactionConfig`` /
budget resolution, with a real counting callable as the producer (not a Mock).
Both branches of ``build_history`` are covered — the non-elide path and the
elide path, which is where the third call used to appear.
"""
from __future__ import annotations

import pytest

from reyn.config.chat import CompactionConfig
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer


def _buffer(history: list, model: str):
    """Real RouterHistoryBuffer over a counting producer; returns (buf, counter)."""
    calls = {"n": 0}

    def counting_history_fn() -> list:
        calls["n"] += 1
        return history

    buf = RouterHistoryBuffer(
        history_fn=counting_history_fn,
        compaction=CompactionConfig(),
        compaction_controller=None,  # → window resolved straight from the model
        model_fn=lambda: model,
        events=None,
        media_store=None,
        router_host=None,
        action_retrieval=None,
        non_interactive=False,
    )
    return buf, calls


def _conversation(n: int, filler: int = 8) -> list:
    out: list = []
    for i in range(n):
        out.append(ChatMessage(role="user", content=f"q{i} " + "word " * filler))
        out.append(ChatMessage(role="assistant", content=f"a{i} " + "word " * filler))
    return out


def test_build_history_materialises_the_producer_once_without_elide():
    """Tier 2: a conversation that fits the window costs exactly one producer
    call — the pre-#2939 shape re-fetched it for the summary lookup."""
    buf, calls = _buffer(_conversation(10), "openai/gpt-4o")

    wire = buf.build_history()

    assert wire, "sanity: the router view is non-empty"
    assert calls["n"] == 1, (
        f"build_history must materialise its history producer once (got "
        f"{calls['n']} calls) — the producer is a recomputed whole-conversation "
        "view, so each extra call is a full extra derivation on the turn hot path"
    )


def test_build_history_materialises_the_producer_once_on_the_elide_path():
    """Tier 2: the elide path (history over the trigger, summary bridge inserted)
    also costs exactly one producer call — this branch consults the summary a
    SECOND time, and used to re-derive the whole view for it."""
    history = _conversation(200, filler=60)
    history.insert(0, ChatMessage(role="summary", content="earlier conversation"))
    # gpt-4's 8K window against a large conversation → over the trigger → elide.
    buf, calls = _buffer(history, "openai/gpt-4")

    wire = buf.build_history()

    assert any(
        "[summary of earlier conversation]" in m.get("content", "")
        for m in wire
        if isinstance(m.get("content"), str)
    ), "test premise: this must actually take the elide path (summary bridge present)"
    assert calls["n"] == 1, (
        f"the elide path must materialise its history producer once (got "
        f"{calls['n']} calls) — its extra summary lookup is where the third "
        "whole-view derivation used to come from"
    )


def test_decompose_history_for_retry_materialises_the_producer_once():
    """Tier 2: the retry decomposition shares build_history's producer contract —
    it fetches the view, then looks up the summary, and must not re-derive."""
    history = _conversation(20)
    history.insert(0, ChatMessage(role="summary", content="earlier conversation"))
    buf, calls = _buffer(history, "openai/gpt-4o")

    head, _raw_middle, _tail, _summary = buf.decompose_history_for_retry()

    assert head, "sanity: the decomposition is non-empty"
    assert calls["n"] == 1, (
        f"decompose_history_for_retry must materialise its history producer once "
        f"(got {calls['n']} calls)"
    )


@pytest.mark.parametrize("model", ["openai/gpt-4o", "openai/gpt-4"])
def test_producer_call_count_does_not_grow_with_conversation_size(model):
    """Tier 2: the producer call count is a property of build_history's shape, not
    of the conversation — a 20x-larger history costs the same number of calls.
    Pins the invariant against re-multiplication on either window branch."""
    _buf_small, small = _buffer(_conversation(10), model)
    _buf_small.build_history()

    _buf_large, large = _buffer(_conversation(200, filler=60), model)
    _buf_large.build_history()

    assert small["n"] == large["n"], (
        f"producer calls must not depend on conversation size (small: {small['n']}, "
        f"20x-larger: {large['n']})"
    )
