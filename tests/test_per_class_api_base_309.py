"""Tier 2: #309 — per-model-class api_base / provider routing (multi-provider).

A model class can target its own endpoint/provider while other classes use the
global default — e.g. router=light on a Gemini proxy + skill=capable on
Anthropic-direct, simultaneously. Mirrors EmbeddingClassSpec.api_base; provider
is added for the direct-vs-proxy opt-out. NO per-class api_key (no literal
secret in config — litellm resolves the key from the standard provider env).

No mocks: real ModelSpec / routing_for_spec, and a real async fake for
litellm.acompletion that captures the kwargs it received.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import litellm
import pytest

from reyn.llm.llm import recorded_acompletion, routing_for_spec
from reyn.llm.model_resolver import ModelSpec

# ── routing_for_spec resolution ──────────────────────────────────────────────


def test_routing_api_base_is_proxy_route(monkeypatch) -> None:
    """Tier 2: api_base set → proxy route (custom_llm_provider openai default)."""
    monkeypatch.setenv("OPENAI_API_KEY", "k-proxy")
    r = routing_for_spec(ModelSpec(model="openai/x", api_base="https://proxy.local"))
    assert r == {
        "api_base": "https://proxy.local",
        "custom_llm_provider": "openai",
        "api_key": "k-proxy",
    }


def test_routing_api_base_with_explicit_provider() -> None:
    """Tier 2: provider overrides the default custom_llm_provider on a proxy."""
    r = routing_for_spec(ModelSpec(model="x", api_base="https://h", provider="vertex_ai"))
    assert r["api_base"] == "https://h"
    assert r["custom_llm_provider"] == "vertex_ai"


def test_routing_provider_only_is_direct() -> None:
    """Tier 2: provider w/o api_base → DIRECT (no api_base; litellm env key)."""
    r = routing_for_spec(ModelSpec(model="anthropic/claude", provider="anthropic"))
    assert r == {"custom_llm_provider": "anthropic"}
    assert "api_base" not in r  # direct → no endpoint override


def test_routing_none_inherits_global() -> None:
    """Tier 2: no per-class routing → None → caller falls back to proxy_kwargs."""
    assert routing_for_spec(ModelSpec(model="openai/x")) is None
    assert routing_for_spec(None) is None


# ── ModelSpec.from_config: routing fields are explicit, not kwargs ───────────


def test_from_config_extracts_routing_fields() -> None:
    """Tier 2: api_base/provider become explicit fields, NOT litellm kwargs."""
    spec = ModelSpec.from_config({
        "model": "anthropic/claude",
        "provider": "anthropic",
        "api_base": "https://h",
        "temperature": 0.5,
    })
    assert spec.api_base == "https://h"
    assert spec.provider == "anthropic"
    assert spec.kwargs == {"temperature": 0.5}  # routing fields not leaked to kwargs


def test_from_config_backward_compat_no_routing() -> None:
    """Tier 2: configs without routing → fields None, kwargs unchanged."""
    s_str = ModelSpec.from_config("openai/gpt-4o")
    assert s_str.api_base is None and s_str.provider is None
    s_dict = ModelSpec.from_config({"model": "openai/gpt-4o", "max_tokens": 100})
    assert s_dict.api_base is None and s_dict.provider is None
    assert s_dict.kwargs == {"max_tokens": 100}


# ── integration: per-class routing wins over the global proxy ────────────────


def _capture_acompletion(box: dict):
    async def _fn(**kwargs):
        box.update(kwargs)
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1), choices=[],
        )
    return _fn


def test_per_class_routing_overrides_global_proxy(monkeypatch) -> None:
    """Tier 2: with a GLOBAL proxy set, a per-class routing still wins — the call
    hits the per-class api_base, not the global one."""
    monkeypatch.setenv("LITELLM_API_BASE", "https://GLOBAL.proxy")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    box: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _capture_acompletion(box))

    asyncio.run(recorded_acompletion(
        model="gemini/x", messages=[{"role": "user", "content": "hi"}],
        purpose="main", recorder=None,
        routing={"api_base": "https://PERCLASS.endpoint", "custom_llm_provider": "openai", "api_key": "k"},
    ))
    assert box["api_base"] == "https://PERCLASS.endpoint"  # per-class, not GLOBAL


def test_global_proxy_used_when_no_per_class_routing(monkeypatch) -> None:
    """Tier 2: backward-compat — no routing → the global proxy_kwargs applies."""
    monkeypatch.setenv("LITELLM_API_BASE", "https://GLOBAL.proxy")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    box: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _capture_acompletion(box))

    asyncio.run(recorded_acompletion(
        model="gemini/x", messages=[{"role": "user", "content": "hi"}],
        purpose="main", recorder=None,  # routing defaults None → global
    ))
    assert box["api_base"] == "https://GLOBAL.proxy"
