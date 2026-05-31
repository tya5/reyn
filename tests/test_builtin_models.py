"""Tier 1: BUILTIN_MODELS catalog contract tests (PR-MODEL-SPEC-EXTENDS).

Pinned invariants:
  - Catalog names match the expected sealed set (via set equality)
  - Every entry has a ``model`` field that is a non-empty str
  - Every ``model`` field starts with a provider prefix (contains ``/``)
  - ``claude-sonnet-thinking`` has extra_body.thinking.{type, budget_tokens}
  - ``gemini-2.0-flash`` has extra_body.thinking_config.thinking_budget == 0
  - Every entry is parseable by ``ModelSpec.from_config`` (i.e. valid form)

Reference: PR-MODEL-SPEC-EXTENDS Task 1 (Tier 1).
"""
from __future__ import annotations

import pytest

from reyn.llm.builtin_models import BUILTIN_MODELS
from reyn.llm.model_resolver import ModelSpec

# ---------------------------------------------------------------------------
# Catalog structure
# ---------------------------------------------------------------------------

EXPECTED_NAMES = {
    "claude-sonnet",
    "claude-sonnet-thinking",
    "claude-haiku",
    "gpt-4o-mini",
    "gpt-4o",
    "gemini-flash-lite",
    "gemini-pro",
    "gemini-3.1-flash-preview",
    "gemini-2.0-flash",
}


def test_builtin_models_names_match_expected():
    """Tier 1: BUILTIN_MODELS keys match the expected catalog names."""
    assert set(BUILTIN_MODELS.keys()) == EXPECTED_NAMES


# ---------------------------------------------------------------------------
# Every entry is a valid dict with a ``model`` field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED_NAMES))
def test_builtin_entry_is_dict(name):
    """Tier 1: every BUILTIN_MODELS entry is a dict."""
    assert isinstance(BUILTIN_MODELS[name], dict), (
        f"BUILTIN_MODELS[{name!r}] should be a dict, got {type(BUILTIN_MODELS[name])}"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_NAMES))
def test_builtin_entry_has_model_field(name):
    """Tier 1: every BUILTIN_MODELS entry has a non-empty 'model' field."""
    entry = BUILTIN_MODELS[name]
    assert "model" in entry, f"BUILTIN_MODELS[{name!r}] missing 'model' key"
    assert isinstance(entry["model"], str) and entry["model"], (
        f"BUILTIN_MODELS[{name!r}]['model'] must be a non-empty str"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_NAMES))
def test_builtin_entry_model_contains_provider_prefix(name):
    """Tier 1: every 'model' value contains '/' (= LiteLLM provider prefix)."""
    model_str = BUILTIN_MODELS[name]["model"]
    assert "/" in model_str, (
        f"BUILTIN_MODELS[{name!r}]['model'] = {model_str!r} must contain '/'"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_NAMES))
def test_builtin_entry_parseable_by_model_spec(name):
    """Tier 1: every BUILTIN_MODELS entry is parseable by ModelSpec.from_config."""
    spec = ModelSpec.from_config(BUILTIN_MODELS[name])
    assert isinstance(spec, ModelSpec)
    assert spec.model  # non-empty


# ---------------------------------------------------------------------------
# Provider-specific structure checks
# ---------------------------------------------------------------------------


def test_claude_sonnet_thinking_extra_body_structure():
    """Tier 1: claude-sonnet-thinking has extra_body.thinking with type and budget_tokens."""
    entry = BUILTIN_MODELS["claude-sonnet-thinking"]
    eb = entry.get("extra_body", {})
    thinking = eb.get("thinking", {})
    assert thinking.get("type") == "enabled", (
        "claude-sonnet-thinking extra_body.thinking.type must be 'enabled'"
    )
    assert isinstance(thinking.get("budget_tokens"), int), (
        "claude-sonnet-thinking extra_body.thinking.budget_tokens must be int"
    )
    assert thinking["budget_tokens"] > 0


def test_claude_sonnet_thinking_uses_max_completion_tokens():
    """Tier 1: claude-sonnet-thinking uses max_completion_tokens (not max_tokens)."""
    entry = BUILTIN_MODELS["claude-sonnet-thinking"]
    assert "max_completion_tokens" in entry, (
        "claude-sonnet-thinking should use max_completion_tokens for hard cost control"
    )


def test_gemini_2_0_flash_thinking_config_disables_thinking():
    """Tier 1: gemini-2.0-flash has extra_body.thinking_config.thinking_budget == 0."""
    entry = BUILTIN_MODELS["gemini-2.0-flash"]
    tc = entry.get("extra_body", {}).get("thinking_config", {})
    assert tc.get("thinking_budget") == 0, (
        "gemini-2.0-flash extra_body.thinking_config.thinking_budget must be 0 "
        "(disables thinking for cost reduction)"
    )


def test_anthropic_entries_use_max_completion_tokens():
    """Tier 1: Anthropic built-ins use max_completion_tokens for hard cost control."""
    for name in ("claude-sonnet", "claude-sonnet-thinking", "claude-haiku"):
        entry = BUILTIN_MODELS[name]
        assert "max_completion_tokens" in entry, (
            f"{name} should declare max_completion_tokens"
        )
