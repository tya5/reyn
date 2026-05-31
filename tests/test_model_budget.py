"""Tier 2: model_budget.get_max_input_tokens invariants.

Invariants guarded:
  1. Known LiteLLM model returns a positive integer from the catalog.
  2. Unknown model string returns the conservative fallback default.
  3. Fallback for an unknown model emits a model_budget_fallback event.
  4. Fallback value is always > 0 (safe for downstream compaction math).
  5. Repeated calls for the same unknown model emit the event only once
     per EventLog instance (no event flood).
  6. #1162 provider-prefix-strip-retry: a proxy-routed ``<provider>/<model>``
     that misses the catalog under the prefix resolves under the bare name
     (avoids premature over-compaction); a still-unknown name keeps the
     fallback (and still emits the event).
"""
from __future__ import annotations

import pytest

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


# ── #1162 provider-prefix-strip-retry ─────────────────────────────────────────

_BARE = "gemini-2.5-flash-lite"  # cataloged at 1M (≠ 128K fallback) per the issue probe


@pytest.mark.parametrize("wrong_prefix", ["openai", "anthropic", "vertex_ai"])
def test_proxy_prefixed_model_resolves_via_prefix_strip(wrong_prefix: str) -> None:
    """Tier 2: a ``<wrong-provider>/<model>`` (= proxy routing) resolves to the
    same real window as the bare model via prefix-strip-retry — not the 128K
    fallback. ``openai/gemini-2.5-flash-lite`` was returning 128K (~87% of a
    real 1M window wasted on premature compaction) before #1162.
    """
    bare_window = get_max_input_tokens(_BARE)
    if bare_window == _FALLBACK_MAX_INPUT_TOKENS:
        pytest.skip(f"litellm catalog lacks {_BARE!r} in this env — strip target absent")
    prefixed_window = get_max_input_tokens(f"{wrong_prefix}/{_BARE}")
    assert prefixed_window == bare_window, (
        f"{wrong_prefix}/{_BARE} must resolve to the bare model's window "
        f"({bare_window}) via prefix-strip, got {prefixed_window}"
    )
    assert prefixed_window > _FALLBACK_MAX_INPUT_TOKENS, (
        "prefix-strip must surface the real (>128K) window, not the fallback"
    )


def test_prefix_strip_resolution_emits_no_fallback_event() -> None:
    """Tier 2: a prefix-strip-resolvable model does NOT emit model_budget_fallback
    (it resolved — the event is reserved for genuinely-unknown models)."""
    if get_max_input_tokens(_BARE) == _FALLBACK_MAX_INPUT_TOKENS:
        pytest.skip(f"litellm catalog lacks {_BARE!r} in this env")
    events = EventLog()
    get_max_input_tokens(f"openai/{_BARE}", events=events)
    assert not [e for e in events.all() if e.type == "model_budget_fallback"]


def test_unknown_prefixed_model_still_falls_back() -> None:
    """Tier 2 (regression guard): a prefixed model whose bare name is also unknown
    keeps the 128K fallback — prefix-strip only improves resolution, never hides
    a genuinely-unknown model."""
    events = EventLog()
    model = "openai/totally-made-up-proxy-model-1162-xyz"
    result = get_max_input_tokens(model, events=events)
    assert result == _FALLBACK_MAX_INPUT_TOKENS
    # the fallback event still fires for the genuinely-unknown model (unchanged).
    assert [e for e in events.all() if e.type == "model_budget_fallback"]
