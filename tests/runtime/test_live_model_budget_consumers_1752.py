"""Tier 2: per-turn budget consumers read the model live via ``model_fn`` (#1752).

ContextBudgetAdvisor + RouterHistoryBuffer used to cache ``model=self.model`` at
construction, so a ``/model`` override (which can change the context window) left
them budgeting against the construction-time model. #1752 threads a live
``model_fn`` (the session resolves the active class → litellm string); the
consumers read it on every budget/count instead of caching.

These are consumer-contract unit tests: a mutable ``model_fn`` is flipped and the
change is observed through each consumer's PUBLIC budget surface
(``compaction_controller=None`` so the window comes straight from the model, with
no compaction-engine confound). Real instances, no mocks.

Falsification (per the file's convention): each test documents the assertion that
would fail under the pre-#1752 construction-cache.
"""
from __future__ import annotations

from reyn.config import CompactionConfig
from reyn.llm.model_budget import get_max_input_tokens
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.context_budget_advisor import ContextBudgetAdvisor
from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer


def test_context_budget_advisor_reads_model_fn_live():
    """Tier 2: ContextBudgetAdvisor.context_window_status() reflects model_fn live.

    Falsification: pre-#1752 the advisor cached the model at __init__, so after
    flipping model_fn the effective_trigger would stay the gpt-4o window and the
    ``after != base`` assertion would fail.
    """
    current = {"m": "openai/gpt-4o"}  # 128K window
    advisor = ContextBudgetAdvisor(
        compaction=CompactionConfig(),
        compaction_controller=None,  # → effective_trigger straight from the model window
        media_store=None,
        model_fn=lambda: current["m"],
        events=None,
        history_fn=lambda: [],
    )

    base = advisor.context_window_status()["effective_trigger"]
    assert base == get_max_input_tokens("openai/gpt-4o")

    current["m"] = "openai/gpt-4"  # 8K window — live switch
    after = advisor.context_window_status()["effective_trigger"]
    assert after == get_max_input_tokens("openai/gpt-4")
    assert after != base  # budgeting tracks the active model, not the cached one


def test_router_history_buffer_reads_model_fn_live():
    """Tier 2: RouterHistoryBuffer.build_history() trims against the live model.

    An oversized history fits a 128K window (no elide) but exceeds an 8K window
    (elide to head+tail). Flipping model_fn from gpt-4o → gpt-4 must change the
    sliced output.

    Falsification: pre-#1752 the buffer cached the model at __init__, so both
    build_history() calls would use the gpt-4o window and produce identical
    (un-elided) output — the ``len(trimmed) < len(full)`` assertion would fail.
    """
    big = "word " * 5000  # ~25k chars ≈ ~6k tokens per turn
    history = [
        ChatMessage(
            role=("user" if i % 2 == 0 else "assistant"),
            content=big,
            ts="t",
        )
        for i in range(8)
    ]
    current = {"m": "openai/gpt-4o"}  # 128K window — all 8 turns fit
    buf = RouterHistoryBuffer(
        history_fn=lambda: history,
        compaction=CompactionConfig(),
        compaction_controller=None,  # → window straight from the model
        model_fn=lambda: current["m"],
        events=None,
        media_store=None,
        router_host=None,
        action_retrieval=None,
        non_interactive=False,
    )

    full = buf.build_history()  # 128K: full raw conversation, no elide

    current["m"] = "openai/gpt-4"  # 8K window — live switch → elide
    trimmed = buf.build_history()
    # Fewer messages survive the smaller live window → the buffer tracks the
    # active model, not the construction-time one.
    assert len(trimmed) < len(full)
