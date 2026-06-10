"""Tier 2: ModelResolver OS invariant tests (PR-MODEL-SPEC + PR-MODEL-SPEC-EXTENDS).

Pinned invariants:
  - resolve(name) returns ModelSpec for known classes (API change pin)
  - str-form and dict-form values both produce correct ModelSpec
  - backward compat: str form -> ModelSpec(kwargs={})
  - dict form: extra_body / temperature / etc. carried in ModelSpec.kwargs
  - is_known_class behaves identically for str-form and dict-form values
  - unknown name passthrough: ModelSpec(model=name, kwargs={})
  - ReynConfig.models accepts dict-form values (config layer check)
  - [EXTENDS] built-in pre-load: claude-sonnet-thinking resolvable with empty user mapping
  - [EXTENDS] user override: user-declared entry wins over same-named built-in
  - [EXTENDS] backward compat: existing ``/``-containing str form unchanged

Reference: PR-MODEL-SPEC Task 2 (Tier 2) + PR-MODEL-SPEC-EXTENDS Task 3 (Tier 2).
"""
from __future__ import annotations

from reyn.llm.model_resolver import ModelResolver, ModelSpec

# ---------------------------------------------------------------------------
# resolve() returns ModelSpec — API change pin
# ---------------------------------------------------------------------------


def test_resolve_known_class_returns_model_spec():
    """Tier 2: resolve(known_class) returns ModelSpec instance."""
    r = ModelResolver({"light": "openai/model-a"})
    spec = r.resolve("light")
    assert isinstance(spec, ModelSpec)


def test_resolve_unknown_name_returns_model_spec_passthrough():
    """Tier 2: resolve(unknown) returns ModelSpec with model=name, kwargs={}."""
    r = ModelResolver({"standard": "openai/model-b"})
    spec = r.resolve("openai/gpt-4o")
    assert isinstance(spec, ModelSpec)
    assert spec.model == "openai/gpt-4o"
    assert spec.kwargs == {}


# ---------------------------------------------------------------------------
# Backward compat: str form
# ---------------------------------------------------------------------------


def test_resolve_str_form_model_string():
    """Tier 2: str-form mapping -> ModelSpec.model matches configured string."""
    r = ModelResolver({"standard": "openai/gemini-2.5-flash-lite"})
    spec = r.resolve("standard")
    assert spec.model == "openai/gemini-2.5-flash-lite"
    assert spec.kwargs == {}


def test_resolve_str_form_empty_kwargs():
    """Tier 2: str-form mapping -> ModelSpec.kwargs is empty (no extra params)."""
    r = ModelResolver({"light": "openai/gemini-2.5-flash-lite"})
    spec = r.resolve("light")
    assert spec.kwargs == {}


def test_resolve_str_form_multiple_classes():
    """Tier 2: multiple str-form classes resolve independently."""
    r = ModelResolver({
        "light": "openai/gemini-2.5-flash-lite",
        "standard": "openai/gpt-4o",
        "strong": "anthropic/claude-3-5-sonnet",
    })
    assert r.resolve("light").model == "openai/gemini-2.5-flash-lite"
    assert r.resolve("standard").model == "openai/gpt-4o"
    assert r.resolve("strong").model == "anthropic/claude-3-5-sonnet"


# ---------------------------------------------------------------------------
# dict form: extra_body / temperature / arbitrary kwargs carried
# ---------------------------------------------------------------------------


def test_resolve_dict_form_temperature_carried():
    """Tier 2: dict-form with temperature -> ModelSpec.kwargs has temperature."""
    r = ModelResolver({"standard": {"model": "openai/gpt-4o", "temperature": 0.7}})
    spec = r.resolve("standard")
    assert spec.model == "openai/gpt-4o"
    assert spec.kwargs["temperature"] == 0.7


def test_resolve_dict_form_extra_body_carried():
    """Tier 2: dict-form with extra_body -> ModelSpec.kwargs has extra_body."""
    thinking = {"type": "enabled", "budget_tokens": 16000}
    r = ModelResolver({
        "strong": {
            "model": "anthropic/claude-3-7-sonnet",
            "extra_body": {"thinking": thinking},
            "max_tokens": 16000,
            "temperature": 0.0,
        }
    })
    spec = r.resolve("strong")
    assert spec.model == "anthropic/claude-3-7-sonnet"
    assert spec.kwargs["extra_body"] == {"thinking": thinking}
    assert spec.kwargs["max_tokens"] == 16000
    assert spec.kwargs["temperature"] == 0.0
    assert "model" not in spec.kwargs


def test_resolve_dict_form_model_key_not_in_kwargs():
    """Tier 2: 'model' key from dict is not duplicated in kwargs."""
    r = ModelResolver({"light": {"model": "openai/model-a", "top_p": 0.9}})
    spec = r.resolve("light")
    assert "model" not in spec.kwargs
    assert spec.kwargs == {"top_p": 0.9}


# ---------------------------------------------------------------------------
# is_known_class identical for str-form and dict-form
# ---------------------------------------------------------------------------


def test_is_known_class_str_form():
    """Tier 2: is_known_class True for str-form class, False for unknown."""
    r = ModelResolver({"light": "openai/model-a"})
    assert r.is_known_class("light") is True
    assert r.is_known_class("strong") is False


def test_is_known_class_dict_form_same_as_str_form():
    """Tier 2: is_known_class behaves identically for dict-form values."""
    r_str = ModelResolver({"light": "openai/model-a", "standard": "openai/model-b"})
    r_dict = ModelResolver({
        "light": {"model": "openai/model-a"},
        "standard": {"model": "openai/model-b"},
    })
    for name in ("light", "standard", "strong", "gpt-4o"):
        assert r_str.is_known_class(name) == r_dict.is_known_class(name), (
            f"is_known_class({name!r}) differs between str-form and dict-form mapping"
        )


def test_is_known_class_mixed_mapping():
    """Tier 2: mapping can mix str-form and dict-form values."""
    r = ModelResolver({
        "light": "openai/gemini-2.5-flash-lite",
        "strong": {"model": "anthropic/claude-3-7-sonnet", "temperature": 0.0},
    })
    assert r.is_known_class("light") is True
    assert r.is_known_class("strong") is True
    assert r.is_known_class("standard") is False


# ---------------------------------------------------------------------------
# Config layer: ReynConfig.models accepts dict-form values
# ---------------------------------------------------------------------------


def test_reyn_config_models_accepts_dict_form():
    """Tier 2: ReynConfig.models field allows dict-form values through config layer."""
    from reyn.config import ReynConfig
    cfg = ReynConfig(models={
        "light": "openai/gemini-2.5-flash-lite",
        "strong": {
            "model": "anthropic/claude-3-7-sonnet",
            "temperature": 0.0,
            "extra_body": {"thinking": {"type": "enabled", "budget_tokens": 8000}},
        },
    })
    r = ModelResolver(cfg.models)
    light_spec = r.resolve("light")
    assert light_spec.model == "openai/gemini-2.5-flash-lite"
    assert light_spec.kwargs == {}

    strong_spec = r.resolve("strong")
    assert strong_spec.model == "anthropic/claude-3-7-sonnet"
    assert strong_spec.kwargs["temperature"] == 0.0
    assert strong_spec.kwargs["extra_body"]["thinking"]["type"] == "enabled"


# ---------------------------------------------------------------------------
# PR-MODEL-SPEC-EXTENDS: built-in pre-load + user override (Tier 2)
# ---------------------------------------------------------------------------


def test_extends_builtin_preload_empty_user_mapping():
    """Tier 2: [EXTENDS] empty user mapping -> built-in claude-sonnet-thinking resolvable."""
    r = ModelResolver({})
    spec = r.resolve("claude-sonnet-thinking")
    assert isinstance(spec, ModelSpec)
    assert "anthropic" in spec.model
    assert spec.kwargs.get("extra_body", {}).get("thinking", {}).get("type") == "enabled"


def test_extends_user_override_wins_over_builtin():
    """Tier 2: [EXTENDS] user-declared entry with same name as built-in takes precedence."""
    r = ModelResolver({"claude-sonnet": {"model": "openai/gpt-4o"}})
    spec = r.resolve("claude-sonnet")
    assert spec.model == "openai/gpt-4o"


def test_extends_backward_compat_slash_str_with_builtin_loaded():
    """Tier 2: [EXTENDS] existing '/' str form resolves as literal even with built-ins loaded."""
    r = ModelResolver({
        "light": "openai/gemini-2.5-flash-lite",
        "standard": "openai/gpt-4o",
    })
    assert r.resolve("light").model == "openai/gemini-2.5-flash-lite"
    assert r.resolve("light").kwargs == {}
    assert r.resolve("standard").model == "openai/gpt-4o"


# ── #1454 PR-B: resolve_class_or_fallback (the closed-world class gate) ──────


def test_resolve_class_or_fallback_known_class_is_honoured():
    """Tier 2: #1454 — a requested value that IS a known class is returned."""
    r = ModelResolver({"strong": "openai/gpt-4o"}, builtin={})
    assert r.resolve_class_or_fallback("strong", "standard", where="t") == "strong"


def test_resolve_class_or_fallback_unknown_falls_back():
    """Tier 2: #1454 — a non-class value (e.g. an LLM-injected literal model
    string) is rejected and the trusted fallback is returned (closed-world:
    op/skill-supplied class-typed fields never pass through as a name)."""
    r = ModelResolver({"standard": "openai/gpt-4o"}, builtin={})
    assert r.resolve_class_or_fallback(
        "gpt-3.5-turbo", "standard", where="t",
    ) == "standard"
    # even a provider-prefixed literal is rejected here — class position only
    assert r.resolve_class_or_fallback(
        "openai/gpt-4o", "standard", where="t",
    ) == "standard"


def test_resolve_class_or_fallback_none_requested_uses_fallback():
    """Tier 2: #1454 — no requested class → the fallback is used as-is."""
    r = ModelResolver({"standard": "openai/gpt-4o"}, builtin={})
    assert r.resolve_class_or_fallback(None, "standard", where="t") == "standard"


def test_resolve_class_or_fallback_none_everywhere_defaults_standard():
    """Tier 2: #1454 — requested and fallback both absent → 'standard'."""
    r = ModelResolver({}, builtin={})
    assert r.resolve_class_or_fallback(None, None, where="t") == "standard"


def test_bare_model_name_warns_prefixed_does_not(caplog):
    """Tier 2: #1454 PR-B — a name position (models[*].model) lacking a '/'
    provider prefix warns at load (degraded-but-allowed); a prefixed name is
    silent. The class/name unified rule's name-position leg."""
    import logging

    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_resolver"):
        ModelResolver({"bare": {"model": "gpt-4o-mini"}}, builtin={})
    assert any("no provider prefix" in r.message for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_resolver"):
        ModelResolver({"ok": {"model": "openai/gpt-4o"}}, builtin={})
    assert not any("no provider prefix" in r.message for r in caplog.records)
