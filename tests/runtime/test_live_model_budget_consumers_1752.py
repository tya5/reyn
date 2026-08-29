"""Tier 2: per-turn budget consumers read the model live via ``model_fn`` (#1752).

ContextBudgetAdvisor used to cache ``model=self.model`` at construction, so a
``/model`` override (which can change the context window) left it budgeting
against the construction-time model. #1752 threads a live ``model_fn`` (the
session resolves the active class → litellm string); the consumer reads it
on every budget/count instead of caching.

#5367: this file used to also carry a ``RouterHistoryBuffer`` witness
(``build_history()`` trimming against the live model) — removed in the same
PR that retired ``build_history``'s elide computation (owner ruling, see
``RouterHistoryBuffer.build_history``'s own docstring): that method no
longer reads the model for anything, so there is nothing left of #1752's
claim to witness there. #1752's own claim survives independently via the
consumer below, which is unaffected by #5367.

This is a consumer-contract unit test: a mutable ``model_fn`` is flipped and
the change is observed through the consumer's PUBLIC budget surface
(``compaction_controller=None`` so the window comes straight from the model,
with no compaction-engine confound). Real instances, no mocks.

Falsification (per the file's convention): the test documents the assertion
that would fail under the pre-#1752 construction-cache.
"""
from __future__ import annotations

from reyn.config import CompactionConfig
from reyn.llm.litellm_bootstrap import ensure_litellm_ready
from reyn.llm.model_budget import get_max_input_tokens
from reyn.runtime.services.context_budget_advisor import ContextBudgetAdvisor

# Pre-existing flakiness (unrelated to #5367, fixed opportunistically while
# already editing this file): run in isolation, litellm's own background
# warm-up hasn't finished by the time this module's assertions run, so
# get_max_input_tokens falls back to the SAME conservative 128,000 for both
# "gpt-4o" and "gpt-4" — the `after != base` assertion needs the real,
# per-model windows. Forces the deterministic blocking warm-up first, same
# reasoning as test_5509_model_media_capability.py's own module-level call.
ensure_litellm_ready()


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
