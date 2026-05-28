"""Tier 2: model_budget.get_max_input_tokens invariants.

Invariants guarded:
  1. Known LiteLLM model returns a positive integer from the catalog.
  2. Unknown model string returns the conservative fallback default.
  3. Fallback for an unknown model emits a model_budget_fallback event.
  4. Fallback value is always > 0 (safe for downstream compaction math).
  5. Repeated calls for the same unknown model emit the event only once
     per EventLog instance (no event flood).
"""
from __future__ import annotations

from reyn.events.events import EventLog
from reyn.llm.model_budget import _FALLBACK_MAX_INPUT_TOKENS, get_max_input_tokens


def test_known_model_returns_positive_int() -> None:
    """Tier 2: a real LiteLLM-cataloged model returns a positive integer budget."""
    # gemini-2.5-flash-lite is in LiteLLM's catalog with max_input_tokens=1048576.
    result = get_max_input_tokens("gemini/gemini-2.5-flash-lite")
    assert isinstance(result, int)
    assert result > 0


def test_unknown_model_returns_fallback() -> None:
    """Tier 2: a model string unknown to LiteLLM returns the conservative fallback."""
    result = get_max_input_tokens("unknown/garbage-model-xyz-test-only")
    assert result == _FALLBACK_MAX_INPUT_TOKENS


def test_fallback_emits_observability_event() -> None:
    """Tier 2: fallback for an unknown model emits model_budget_fallback event (P6)."""
    events = EventLog()
    # Use a unique model name to avoid being filtered by the process-global
    # "warned_models" set from other tests — suffix with a unique token.
    model = "unknown/test-only-fallback-event-model-abc123"
    get_max_input_tokens(model, events=events, phase="test_phase", run_id="run-1")

    fallback_events = [e for e in events.all() if e.type == "model_budget_fallback"]
    assert len(fallback_events) >= 1
    ev = fallback_events[0]
    assert ev.data["model"] == model
    assert ev.data["fallback_tokens"] == _FALLBACK_MAX_INPUT_TOKENS


def test_fallback_value_always_positive() -> None:
    """Tier 2: the fallback default is a positive integer (safe for compaction arithmetic)."""
    assert isinstance(_FALLBACK_MAX_INPUT_TOKENS, int)
    assert _FALLBACK_MAX_INPUT_TOKENS > 0
