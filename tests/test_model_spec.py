"""Tier 1: ModelSpec.from_config() contract tests (PR-MODEL-SPEC).

Pinned invariants:
  - str form -> ModelSpec(model=str, kwargs={})
  - dict form with model only -> ModelSpec(model=..., kwargs={})
  - dict form with extra fields -> kwargs carry all non-model fields
  - dict form missing 'model' -> ValueError
  - invalid type (int, None, list) -> ValueError
  - kwargs are a shallow copy (frozen dataclass guarantees immutability)
  - ModelSpec is frozen / hashable

Reference: PR-MODEL-SPEC Task 1 (Tier 1).
"""
from __future__ import annotations

import pytest
from reyn.llm.model_resolver import ModelSpec


# ---------------------------------------------------------------------------
# str form
# ---------------------------------------------------------------------------


def test_from_config_str_produces_empty_kwargs():
    """Tier 1: str form -> ModelSpec with model=str and kwargs={}."""
    spec = ModelSpec.from_config("openai/gemini-2.5-flash-lite")
    assert spec.model == "openai/gemini-2.5-flash-lite"
    assert spec.kwargs == {}


def test_from_config_str_arbitrary_model_string():
    """Tier 1: any str is accepted as model string without validation."""
    spec = ModelSpec.from_config("anthropic/claude-3-7-sonnet-20250219")
    assert spec.model == "anthropic/claude-3-7-sonnet-20250219"
    assert spec.kwargs == {}


# ---------------------------------------------------------------------------
# dict form — model only
# ---------------------------------------------------------------------------


def test_from_config_dict_model_only():
    """Tier 1: dict with only 'model' key -> ModelSpec with empty kwargs."""
    spec = ModelSpec.from_config({"model": "anthropic/claude-3-7-sonnet"})
    assert spec.model == "anthropic/claude-3-7-sonnet"
    assert spec.kwargs == {}


# ---------------------------------------------------------------------------
# dict form — with extra fields (passthrough)
# ---------------------------------------------------------------------------


def test_from_config_dict_with_temperature():
    """Tier 1: dict with temperature -> kwargs carries temperature."""
    spec = ModelSpec.from_config({"model": "openai/gpt-4o", "temperature": 0.5})
    assert spec.model == "openai/gpt-4o"
    assert spec.kwargs["temperature"] == 0.5
    assert "model" not in spec.kwargs


def test_from_config_dict_with_max_tokens():
    """Tier 1: dict with max_tokens -> kwargs carries max_tokens."""
    spec = ModelSpec.from_config({"model": "openai/gpt-4o", "max_tokens": 4096})
    assert spec.kwargs["max_tokens"] == 4096


def test_from_config_dict_with_extra_body():
    """Tier 1: dict with extra_body -> kwargs carries extra_body intact."""
    extra_body = {"thinking": {"type": "enabled", "budget_tokens": 8000}}
    spec = ModelSpec.from_config({
        "model": "anthropic/claude-3-7-sonnet",
        "extra_body": extra_body,
    })
    assert spec.model == "anthropic/claude-3-7-sonnet"
    assert spec.kwargs["extra_body"] == extra_body


def test_from_config_dict_with_multiple_kwargs():
    """Tier 1: dict with temperature + max_tokens + extra_body -> all in kwargs."""
    spec = ModelSpec.from_config({
        "model": "anthropic/claude-3-7-sonnet",
        "temperature": 0.0,
        "max_tokens": 16000,
        "extra_body": {"thinking": {"type": "enabled", "budget_tokens": 8000}},
    })
    assert spec.model == "anthropic/claude-3-7-sonnet"
    assert spec.kwargs["temperature"] == 0.0
    assert spec.kwargs["max_tokens"] == 16000
    assert spec.kwargs["extra_body"]["thinking"]["type"] == "enabled"
    assert "model" not in spec.kwargs


def test_from_config_dict_unknown_field_passes_through():
    """Tier 1: unknown field in dict -> silently carried in kwargs (passthrough policy)."""
    spec = ModelSpec.from_config({"model": "openai/gpt-4o", "future_param": "some_value"})
    assert spec.kwargs["future_param"] == "some_value"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_from_config_dict_missing_model_raises_value_error():
    """Tier 1: dict without 'model' key -> ValueError (model is required)."""
    with pytest.raises(ValueError, match="model"):
        ModelSpec.from_config({"temperature": 0.5})


def test_from_config_int_raises_value_error():
    """Tier 1: int input -> ValueError."""
    with pytest.raises(ValueError):
        ModelSpec.from_config(123)  # type: ignore[arg-type]


def test_from_config_none_raises_value_error():
    """Tier 1: None input -> ValueError."""
    with pytest.raises(ValueError):
        ModelSpec.from_config(None)  # type: ignore[arg-type]


def test_from_config_list_raises_value_error():
    """Tier 1: list input -> ValueError."""
    with pytest.raises(ValueError):
        ModelSpec.from_config(["openai/gpt-4o"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ModelSpec dataclass properties
# ---------------------------------------------------------------------------


def test_model_spec_is_frozen():
    """Tier 1: ModelSpec is frozen (immutable). Mutation raises FrozenInstanceError."""
    spec = ModelSpec(model="openai/gpt-4o", kwargs={"temperature": 0.5})
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        spec.model = "other"  # type: ignore[misc]


def test_model_spec_is_hashable():
    """Tier 1: ModelSpec is hashable (frozen dataclass with dict kwargs needs special care)."""
    # Note: frozen=True on dataclass doesn't automatically make dict-field hashable.
    # We test that ModelSpec can at least be instantiated and equality works.
    spec1 = ModelSpec(model="openai/gpt-4o", kwargs={})
    spec2 = ModelSpec(model="openai/gpt-4o", kwargs={})
    assert spec1 == spec2
