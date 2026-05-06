"""Tier 1+2: ModelResolver extends / deep-merge / cycle-detection tests (PR-MODEL-SPEC-EXTENDS).

Pinned invariants (Tier 1 — contract):
  - str form with ``/`` (literal) is treated as literal model string
  - str form without ``/`` (shorthand) resolves via namespace
  - dict form with ``extends`` resolves base and deep-merges override
  - multi-level extends chains resolve to the correct final ModelSpec
  - deep merge: nested dict sibling fields are carried from base
  - cycle detection raises ValueError for direct and indirect cycles
  - self-cycle raises ValueError
  - unknown extends target raises ValueError at construction time
  - unknown str shorthand raises ValueError at construction time
  - missing ``model`` field after extends resolution raises ValueError

Pinned invariants (Tier 2 — OS invariant):
  - built-in pre-load: ModelResolver() with empty user mapping resolves built-ins
  - user override: user entry wins over built-in with the same name
  - backward compat: ``/``-containing str form unchanged

Reference: PR-MODEL-SPEC-EXTENDS Task 3 (Tier 1 + Tier 2).
"""
from __future__ import annotations

import pytest

from reyn.llm.model_resolver import ModelResolver, ModelSpec


# ---------------------------------------------------------------------------
# Helper: resolver with built-ins disabled for isolation
# ---------------------------------------------------------------------------

def make_resolver(mapping: dict, *, with_builtin: bool = False) -> ModelResolver:
    """Create a ModelResolver with optional built-in catalog."""
    if with_builtin:
        return ModelResolver(mapping)
    # Disable built-ins so tests are self-contained.
    return ModelResolver(mapping, builtin={})


# ---------------------------------------------------------------------------
# Tier 1: str form — backward compat (``/``-containing literal)
# ---------------------------------------------------------------------------


def test_str_form_with_slash_is_literal():
    """Tier 1: str value containing '/' is treated as literal LiteLLM model string."""
    r = make_resolver({"light": "openai/gemini-2.5-flash-lite"})
    spec = r.resolve("light")
    assert spec.model == "openai/gemini-2.5-flash-lite"
    assert spec.kwargs == {}


def test_str_form_with_slash_is_not_class_ref():
    """Tier 1: str value with '/' is NOT a class ref even if it matches a key name."""
    r = make_resolver({"my/model": "openai/gpt-4o"})
    # The key "my/model" is in the namespace but the value "openai/gpt-4o" is literal.
    spec = r.resolve("my/model")
    assert spec.model == "openai/gpt-4o"


# ---------------------------------------------------------------------------
# Tier 1: str form shorthand (no ``/`` — class reference)
# ---------------------------------------------------------------------------


def test_str_shorthand_resolves_via_namespace():
    """Tier 1: str value without '/' is a class reference shorthand."""
    r = make_resolver({
        "base": {"model": "anthropic/claude-3-7-sonnet"},
        "alias": "base",  # shorthand for {extends: base}
    })
    spec = r.resolve("alias")
    assert spec.model == "anthropic/claude-3-7-sonnet"


def test_str_shorthand_unknown_raises_value_error():
    """Tier 1: str value without '/' referencing unknown name raises ValueError."""
    with pytest.raises(ValueError, match="nonexistent"):
        make_resolver({"light": "nonexistent"})


# ---------------------------------------------------------------------------
# Tier 1: dict form with ``extends`` — single level
# ---------------------------------------------------------------------------


def test_extends_single_level_inherits_model():
    """Tier 1: dict form with extends inherits model from base."""
    r = make_resolver({
        "base": {"model": "anthropic/claude-3-7-sonnet"},
        "derived": {"extends": "base"},
    })
    spec = r.resolve("derived")
    assert spec.model == "anthropic/claude-3-7-sonnet"


def test_extends_single_level_inherits_kwargs():
    """Tier 1: dict form with extends inherits kwargs from base."""
    r = make_resolver({
        "base": {
            "model": "anthropic/claude-3-7-sonnet",
            "max_completion_tokens": 8192,
        },
        "derived": {"extends": "base"},
    })
    spec = r.resolve("derived")
    assert spec.kwargs["max_completion_tokens"] == 8192


def test_extends_single_level_override_simple_field():
    """Tier 1: dict form with extends overrides a simple field from base."""
    r = make_resolver({
        "base": {
            "model": "anthropic/claude-3-7-sonnet",
            "max_completion_tokens": 8192,
        },
        "derived": {
            "extends": "base",
            "max_completion_tokens": 4000,
        },
    })
    spec = r.resolve("derived")
    assert spec.model == "anthropic/claude-3-7-sonnet"
    assert spec.kwargs["max_completion_tokens"] == 4000


# ---------------------------------------------------------------------------
# Tier 1: deep merge — nested dict sibling field carry
# ---------------------------------------------------------------------------


def test_extends_deep_merge_carries_sibling_fields():
    """Tier 1: deep merge carries sibling fields from base's nested dict."""
    r = make_resolver({
        "base": {
            "model": "anthropic/claude-3-7-sonnet",
            "extra_body": {
                "thinking": {"type": "enabled", "budget_tokens": 8000},
            },
        },
        "derived": {
            "extends": "base",
            "extra_body": {
                "thinking": {"budget_tokens": 4000},  # override only budget_tokens
            },
        },
    })
    spec = r.resolve("derived")
    thinking = spec.kwargs["extra_body"]["thinking"]
    # budget_tokens overridden
    assert thinking["budget_tokens"] == 4000
    # type carried from base (sibling field preserved)
    assert thinking["type"] == "enabled"


def test_extends_deep_merge_adds_override_only_field():
    """Tier 1: deep merge adds fields present only in override."""
    r = make_resolver({
        "base": {"model": "openai/gpt-4o"},
        "derived": {
            "extends": "base",
            "extra_body": {"foo": "bar"},
        },
    })
    spec = r.resolve("derived")
    assert spec.kwargs["extra_body"]["foo"] == "bar"


def test_extends_deep_merge_list_replaced_not_merged():
    """Tier 1: deep merge replaces lists (not merged element-by-element)."""
    r = make_resolver({
        "base": {"model": "openai/gpt-4o", "stop": ["<end>"]},
        "derived": {"extends": "base", "stop": ["STOP", "END"]},
    })
    spec = r.resolve("derived")
    assert spec.kwargs["stop"] == ["STOP", "END"]


def test_extends_deep_merge_scalar_replaced():
    """Tier 1: deep merge replaces scalar with override value."""
    r = make_resolver({
        "base": {"model": "openai/gpt-4o", "temperature": 0.7},
        "derived": {"extends": "base", "temperature": 0.0},
    })
    spec = r.resolve("derived")
    assert spec.kwargs["temperature"] == 0.0


# ---------------------------------------------------------------------------
# Tier 1: multi-level extends
# ---------------------------------------------------------------------------


def test_extends_multi_level_two_hops():
    """Tier 1: multi-level extends (A extends B, B extends builtin-like C) resolves fully."""
    r = make_resolver({
        "base": {
            "model": "anthropic/claude-3-7-sonnet",
            "max_completion_tokens": 16000,
            "extra_body": {
                "thinking": {"type": "enabled", "budget_tokens": 8000},
            },
        },
        "mid": {
            "extends": "base",
            "extra_body": {"thinking": {"budget_tokens": 4000}},
        },
        "top": {
            "extends": "mid",
            "extra_body": {"thinking": {"budget_tokens": 16000}},
            "max_completion_tokens": 32000,
        },
    })
    spec = r.resolve("top")
    assert spec.model == "anthropic/claude-3-7-sonnet"
    assert spec.kwargs["max_completion_tokens"] == 32000
    thinking = spec.kwargs["extra_body"]["thinking"]
    assert thinking["budget_tokens"] == 16000
    # type was set in base, must be carried through mid -> top
    assert thinking["type"] == "enabled"


def test_extends_multi_level_base_only_field_carried():
    """Tier 1: field set in base and not overridden at any level is carried to top."""
    r = make_resolver({
        "base": {"model": "openai/gpt-4o", "seed": 42},
        "mid": {"extends": "base"},
        "top": {"extends": "mid"},
    })
    spec = r.resolve("top")
    assert spec.kwargs["seed"] == 42


# ---------------------------------------------------------------------------
# Tier 1: extends with built-in catalog (real built-ins)
# ---------------------------------------------------------------------------


def test_extends_builtin_claude_sonnet_thinking():
    """Tier 1: dict form extends claude-sonnet-thinking built-in, overrides budget_tokens."""
    r = ModelResolver({
        "reasoning-light": {
            "extends": "claude-sonnet-thinking",
            "extra_body": {"thinking": {"budget_tokens": 4000}},
        }
    })
    spec = r.resolve("reasoning-light")
    assert spec.model == "anthropic/claude-3-7-sonnet"
    thinking = spec.kwargs["extra_body"]["thinking"]
    assert thinking["budget_tokens"] == 4000
    assert thinking["type"] == "enabled"  # carried from builtin


def test_str_shorthand_with_builtin():
    """Tier 1: str shorthand without '/' resolves against built-in catalog."""
    r = ModelResolver({"standard": "claude-sonnet-thinking"})
    spec = r.resolve("standard")
    assert spec.model == "anthropic/claude-3-7-sonnet"


# ---------------------------------------------------------------------------
# Tier 2: built-in pre-load (OS invariant)
# ---------------------------------------------------------------------------


def test_builtin_preload_no_user_mapping():
    """Tier 2: ModelResolver() with empty user mapping resolves built-in entries."""
    r = ModelResolver({})
    spec = r.resolve("claude-sonnet-thinking")
    assert spec.model == "anthropic/claude-3-7-sonnet"
    assert spec.kwargs["extra_body"]["thinking"]["type"] == "enabled"


def test_builtin_preload_all_8_are_resolvable():
    """Tier 2: all 8 built-in entries are resolvable from ModelResolver({})."""
    r = ModelResolver({})
    expected_names = [
        "claude-sonnet",
        "claude-sonnet-thinking",
        "claude-haiku",
        "gpt-4o-mini",
        "gpt-4o",
        "gemini-flash-lite",
        "gemini-3.1-flash-preview",
        "gemini-2.0-flash",
    ]
    for name in expected_names:
        spec = r.resolve(name)
        assert isinstance(spec, ModelSpec), f"resolve({name!r}) should return ModelSpec"
        assert spec.model  # non-empty LiteLLM model string


def test_builtin_is_known_class():
    """Tier 2: built-in entries appear in is_known_class even with empty user mapping."""
    r = ModelResolver({})
    assert r.is_known_class("claude-sonnet") is True
    assert r.is_known_class("nonexistent-class") is False


# ---------------------------------------------------------------------------
# Tier 2: user override of built-in (OS invariant — user always wins)
# ---------------------------------------------------------------------------


def test_user_override_replaces_builtin():
    """Tier 2: user-declared entry with same name as built-in takes precedence."""
    r = ModelResolver({
        "claude-sonnet": {
            "model": "openai/gpt-4o",  # intentional override to different provider
        }
    })
    spec = r.resolve("claude-sonnet")
    assert spec.model == "openai/gpt-4o"


def test_user_override_partial_builtin_name_unchanged():
    """Tier 2: user override of one built-in does not affect other built-ins."""
    r = ModelResolver({"claude-sonnet": {"model": "openai/gpt-4o"}})
    spec = r.resolve("claude-haiku")
    assert spec.model == "anthropic/claude-3-5-haiku"


# ---------------------------------------------------------------------------
# Tier 1: Error cases — startup fail-fast
# ---------------------------------------------------------------------------


def test_cycle_direct_raises_value_error():
    """Tier 1: A extends B, B extends A -> ValueError (cycle)."""
    with pytest.raises(ValueError, match="circular"):
        make_resolver({
            "a": {"extends": "b"},
            "b": {"extends": "a"},
        })


def test_cycle_self_raises_value_error():
    """Tier 1: A extends A -> ValueError (self-cycle)."""
    with pytest.raises(ValueError, match="circular"):
        make_resolver({"a": {"extends": "a"}})


def test_cycle_indirect_three_node_raises_value_error():
    """Tier 1: A extends B, B extends C, C extends A -> ValueError."""
    with pytest.raises(ValueError, match="circular"):
        make_resolver({
            "a": {"extends": "b"},
            "b": {"extends": "c"},
            "c": {"extends": "a"},
        })


def test_unknown_extends_target_raises_value_error():
    """Tier 1: extends referencing a name not in namespace raises ValueError."""
    with pytest.raises(ValueError, match="nonexistent"):
        make_resolver({"derived": {"extends": "nonexistent"}})


def test_unknown_str_shorthand_raises_value_error():
    """Tier 1: str shorthand referencing unknown name raises ValueError at construction."""
    with pytest.raises(ValueError, match="nonexistent"):
        make_resolver({"light": "nonexistent"})


def test_missing_model_field_after_extends_raises_value_error():
    """Tier 1: if base has no model and extends chain cannot supply one, ValueError."""
    # This case arises if someone bypasses from_config and puts an invalid entry.
    # We simulate by passing a malformed dict (no model) directly as builtin.
    with pytest.raises(ValueError):
        ModelResolver(
            {"derived": {"extends": "empty-base"}},
            builtin={"empty-base": {"max_completion_tokens": 4096}},  # no 'model'
        )


# ---------------------------------------------------------------------------
# Tier 2: Backward compat — existing str/dict forms unchanged
# ---------------------------------------------------------------------------


def test_backward_compat_str_form_slash():
    """Tier 2: existing str form with '/' continues to work as literal (backward compat)."""
    r = make_resolver({
        "light": "openai/gemini-2.5-flash-lite",
        "standard": "openai/gpt-4o",
        "strong": "anthropic/claude-3-5-sonnet-20241022",
    })
    assert r.resolve("light").model == "openai/gemini-2.5-flash-lite"
    assert r.resolve("standard").model == "openai/gpt-4o"
    assert r.resolve("strong").model == "anthropic/claude-3-5-sonnet-20241022"


def test_backward_compat_dict_form_no_extends():
    """Tier 2: existing dict form without extends continues to work (backward compat)."""
    r = make_resolver({
        "strong": {
            "model": "anthropic/claude-3-7-sonnet",
            "temperature": 0.0,
            "extra_body": {"thinking": {"type": "enabled", "budget_tokens": 8000}},
        }
    })
    spec = r.resolve("strong")
    assert spec.model == "anthropic/claude-3-7-sonnet"
    assert spec.kwargs["temperature"] == 0.0
    assert spec.kwargs["extra_body"]["thinking"]["type"] == "enabled"


def test_backward_compat_unknown_name_passthrough():
    """Tier 2: unknown name (raw LiteLLM string) passes through unchanged."""
    r = make_resolver({"light": "openai/gemini-2.5-flash-lite"})
    spec = r.resolve("openai/gpt-4o")
    assert spec.model == "openai/gpt-4o"
    assert spec.kwargs == {}
