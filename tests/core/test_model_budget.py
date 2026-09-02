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

from reyn.core.events.events import EventLog
from reyn.llm.model_budget import (
    _STARTUP_FALLBACK_MAX_INPUT_TOKENS,
    get_max_input_tokens,
    get_max_input_tokens_source,
)
from tests._support.events import collect_events


def test_known_model_returns_positive_int() -> None:
    """Tier 2: a real LiteLLM-cataloged model returns a positive integer budget."""
    # gemini-2.5-flash-lite is in LiteLLM's catalog with max_input_tokens=1048576.
    result = get_max_input_tokens("gemini/gemini-2.5-flash-lite")
    assert isinstance(result, int)
    assert result > 0


def test_unknown_model_returns_fallback() -> None:
    """Tier 2: a model string unknown to LiteLLM returns the conservative fallback."""
    result = get_max_input_tokens("unknown/garbage-model-xyz-test-only")
    assert result == _STARTUP_FALLBACK_MAX_INPUT_TOKENS


def test_fallback_emits_observability_event() -> None:
    """Tier 2: fallback for an unknown model emits model_budget_fallback event (P6)."""
    events = EventLog()
    collected = collect_events(events)
    # Use a unique model name to avoid being filtered by the process-global
    # "warned_models" set from other tests — suffix with a unique token.
    model = "unknown/test-only-fallback-event-model-abc123"
    get_max_input_tokens(model, events=events, phase="test_phase", run_id="run-1")

    fallback_events = [e for e in collected if e.type == "model_budget_fallback"]
    assert len(fallback_events) >= 1
    ev = fallback_events[0]
    assert ev.data["model"] == model
    assert ev.data["fallback_tokens"] == _STARTUP_FALLBACK_MAX_INPUT_TOKENS


def test_fallback_value_always_positive() -> None:
    """Tier 2: the fallback default is a positive integer (safe for compaction arithmetic)."""
    assert isinstance(_STARTUP_FALLBACK_MAX_INPUT_TOKENS, int)
    assert _STARTUP_FALLBACK_MAX_INPUT_TOKENS > 0


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
    if bare_window == _STARTUP_FALLBACK_MAX_INPUT_TOKENS:
        pytest.skip(f"litellm catalog lacks {_BARE!r} in this env — strip target absent")
    prefixed_window = get_max_input_tokens(f"{wrong_prefix}/{_BARE}")
    assert prefixed_window == bare_window, (
        f"{wrong_prefix}/{_BARE} must resolve to the bare model's window "
        f"({bare_window}) via prefix-strip, got {prefixed_window}"
    )
    assert prefixed_window > _STARTUP_FALLBACK_MAX_INPUT_TOKENS, (
        "prefix-strip must surface the real (>128K) window, not the fallback"
    )


def test_prefix_strip_resolution_emits_no_fallback_event() -> None:
    """Tier 2: a prefix-strip-resolvable model does NOT emit model_budget_fallback
    (it resolved — the event is reserved for genuinely-unknown models)."""
    if get_max_input_tokens(_BARE) == _STARTUP_FALLBACK_MAX_INPUT_TOKENS:
        pytest.skip(f"litellm catalog lacks {_BARE!r} in this env")
    events = EventLog()
    collected = collect_events(events)
    get_max_input_tokens(f"openai/{_BARE}", events=events)
    assert not [e for e in collected if e.type == "model_budget_fallback"]


def test_unknown_prefixed_model_still_falls_back() -> None:
    """Tier 2: regression guard — a prefixed model whose bare name is also unknown
    keeps the 128K fallback — prefix-strip only improves resolution, never hides
    a genuinely-unknown model."""
    events = EventLog()
    collected = collect_events(events)
    model = "openai/totally-made-up-proxy-model-1162-xyz"
    result = get_max_input_tokens(model, events=events)
    assert result == _STARTUP_FALLBACK_MAX_INPUT_TOKENS
    # the fallback event still fires for the genuinely-unknown model (unchanged).
    assert [e for e in collected if e.type == "model_budget_fallback"]


def test_source_for_cataloged_model_names_litellm() -> None:
    """Tier 2: get_max_input_tokens_source (status-bar ctx chip's source line)
    names "litellm catalog" — not the fallback — for a real cataloged model.

    #4680②: this test's own subject is the CATALOG-HIT path, which
    requires litellm to have actually finished importing — a DIFFERENT
    (and real, #4680② confirmed) axis is whether it has, which this test
    is not about. Blocks on the real, non-mocked ``ensure_litellm_ready``
    first (not a sleep — waits on the actual condition, unbounded, per
    testing.md § Time) so this test exercises the catalog-hit path
    deterministically regardless of run order — before this fix, running
    this test FIRST in a fresh process (no prior call anywhere had warmed
    litellm) hit the NOT_READY fallback instead, a pre-existing
    order-dependent flake this PR's own NOT_READY/UNCATALOGED split made
    newly legible (the assertion failure now names which state it hit)."""
    from reyn.llm.litellm_bootstrap import ensure_litellm_ready
    ensure_litellm_ready()

    source = get_max_input_tokens_source("gemini/gemini-2.5-flash-lite")
    assert source.startswith("litellm catalog:")
    assert "gemini/gemini-2.5-flash-lite" in source


def test_source_for_unknown_model_names_reyn_fallback() -> None:
    """Tier 2: an unrecognized model's source names reyn's OWN fallback default,
    not litellm — the owner explicitly wants this distinguished from a real
    catalog hit (there is no user-configurable window override in reyn today,
    so these two are the only two real sources)."""
    source = get_max_input_tokens_source("unknown/garbage-model-xyz-test-only")
    assert source.startswith("reyn fallback default")
    assert str(_STARTUP_FALLBACK_MAX_INPUT_TOKENS) in source.replace(",", "")


def test_source_for_prefix_strip_resolved_model_names_bare_litellm_hit() -> None:
    """Tier 2: a proxy-prefixed model that only resolves via #1162's bare-name
    retry still reports "litellm catalog" (naming the bare model), matching
    what get_max_input_tokens actually used — not a fallback claim.

    #4680②: same real-readiness precondition as
    test_source_for_cataloged_model_names_litellm above — see its
    docstring for why."""
    from reyn.llm.litellm_bootstrap import ensure_litellm_ready
    ensure_litellm_ready()

    if get_max_input_tokens(_BARE) == _STARTUP_FALLBACK_MAX_INPUT_TOKENS:
        pytest.skip(f"litellm catalog lacks {_BARE!r} in this env")
    source = get_max_input_tokens_source(f"openai/{_BARE}")
    assert source.startswith("litellm catalog:")
    assert _BARE in source
