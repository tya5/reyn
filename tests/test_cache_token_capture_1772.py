"""Tier 2: #1772 — prompt-cache token capture in usage + cost events.

`_extract_usage` must pull the prompt-cache metrics litellm surfaces so the
cost tab can show cache hits. Cross-provider field placement (verified
ref-exact against real router fixtures):

  - Anthropic: ``usage.cache_read_input_tokens`` (+ ``cache_creation_input_tokens``)
  - OpenAI:    ``usage.prompt_tokens_details.cached_tokens``
  - litellm normalizes Anthropic's read to BOTH the top-level field and the
    nested OpenAI-style field; Gemini exposes no cache-creation metric.

Tests use real ``litellm.types.utils.Usage`` objects (not mocks) so they pin
the actual provider contract, plus the ``TokenUsage`` public surface.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import litellm
import pytest
from litellm.types.utils import PromptTokensDetailsWrapper, Usage

from reyn.core.events.events import EventLog, set_llm_request_event_log
from reyn.llm.llm import _extract_cache_tokens, _extract_usage, recorded_acompletion
from reyn.llm.pricing import TokenUsage


def test_extract_anthropic_style_read_and_creation() -> None:
    """Tier 2: Anthropic top-level cache_read + cache_creation are captured."""
    u = Usage(
        prompt_tokens=2885,
        completion_tokens=17,
        cache_read_input_tokens=2719,
        cache_creation_input_tokens=500,
    )
    assert _extract_cache_tokens(u) == (2719, 500)


def test_extract_openai_style_nested_cached_only() -> None:
    """Tier 2: OpenAI nested cached_tokens is captured when no top-level field."""
    u = Usage(
        prompt_tokens=900,
        completion_tokens=10,
        prompt_tokens_details=PromptTokensDetailsWrapper(cached_tokens=800),
    )
    assert _extract_cache_tokens(u) == (800, 0)


def test_extract_no_cache_is_zero() -> None:
    """Tier 2: a response with no cache metrics yields (0, 0)."""
    u = Usage(prompt_tokens=100, completion_tokens=5)
    assert _extract_cache_tokens(u) == (0, 0)


def test_extract_usage_carries_cache_fields() -> None:
    """Tier 2: _extract_usage threads the cache metrics onto TokenUsage."""
    u = Usage(prompt_tokens=2885, completion_tokens=17, cache_read_input_tokens=2719)
    tu = _extract_usage(SimpleNamespace(usage=u))
    assert tu is not None
    assert tu.prompt_tokens == 2885
    assert tu.completion_tokens == 17
    assert tu.cached_tokens == 2719
    assert tu.cache_creation_tokens == 0


def test_token_usage_roundtrip_and_total_excludes_cached() -> None:
    """Tier 2: cached is a SUBSET of prompt (not in total) + survives round-trip."""
    tu = TokenUsage(
        prompt_tokens=2885,
        completion_tokens=17,
        cached_tokens=2719,
        cache_creation_tokens=500,
    )
    # cached_tokens are part of prompt_tokens, not additive to the total.
    assert tu.total_tokens == 2885 + 17
    d = tu.to_dict()
    assert d["cached_tokens"] == 2719
    assert d["cache_creation_tokens"] == 500
    rt = TokenUsage.from_dict(d)
    assert rt == tu


def test_token_usage_add_accumulates_cache() -> None:
    """Tier 2: __add__ / __iadd__ accumulate the cache metrics."""
    a = TokenUsage(prompt_tokens=10, completion_tokens=1, cached_tokens=8, cache_creation_tokens=2)
    b = TokenUsage(prompt_tokens=20, completion_tokens=2, cached_tokens=15, cache_creation_tokens=0)
    assert (a + b).cached_tokens == 23
    assert (a + b).cache_creation_tokens == 2
    a += b
    assert a.cached_tokens == 23
    assert a.cache_creation_tokens == 2


def test_from_dict_backward_compat_missing_cache_fields() -> None:
    """Tier 2: older usage dicts (no cache fields) read as 0 (no crash)."""
    rt = TokenUsage.from_dict({"prompt_tokens": 50, "completion_tokens": 5})
    assert rt.cached_tokens == 0
    assert rt.cache_creation_tokens == 0


@pytest.fixture
def _reset_event_log():
    yield
    set_llm_request_event_log(None)


def test_cost_event_carries_flat_cache_tokens(monkeypatch, _reset_event_log) -> None:
    """Tier 2: the emitted llm_response_received carries FLAT cached_tokens.

    cost_tab reads flat fields off llm_response_received (not the nested usage
    dict), so the cache metric must ride the event as a flat field. No mocks:
    real recorded_acompletion + a real async fake for litellm.acompletion
    returning a cache-bearing litellm Usage + a real EventLog.
    """
    async def _resp(**_kwargs):
        return SimpleNamespace(
            usage=Usage(
                prompt_tokens=3000,
                completion_tokens=20,
                cache_read_input_tokens=2719,
            ),
            choices=[],
        )

    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.setattr(litellm, "acompletion", _resp)
    log = EventLog()
    set_llm_request_event_log(log)

    asyncio.run(recorded_acompletion(
        model="openai/gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        purpose="main",
        recorder=None,
        emit_cost_events=True,
    ))

    resp = next(e for e in log.all() if e.type == "llm_response_received")
    assert resp.data["prompt_tokens"] == 3000
    assert resp.data["cached_tokens"] == 2719
    assert resp.data["cache_creation_tokens"] == 0
