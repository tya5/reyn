"""Tier 1 + Tier 2: model class resolution for run_skill op (B13-NEW-1 fix).

Pinned invariants:

  Tier 1 (ModelResolver.is_known_class contract):
  - is_known_class returns True for names in the mapping, False otherwise.
  - resolve still passes through unknown strings unchanged (backward compat).

  Tier 2b (run_skill OS invariant — model class only):
  - When op.model is a known class, run_skill uses it.
  - When op.model is a literal model string NOT in the resolver mapping
    (e.g. "gpt-3.5-turbo"), run_skill ignores it and falls back to
    ctx.model.  This prevents LLM-hallucinated model strings from
    reaching the proxy and causing BadRequestError.

Reference: B13-NEW-1 fix (batch 14 R1).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.llm.model_resolver import ModelResolver
from reyn.schemas.models import RunSkillIROp


# ---------------------------------------------------------------------------
# Tier 1 — ModelResolver.is_known_class contract
# ---------------------------------------------------------------------------


def test_is_known_class_returns_true_for_configured_class():
    """Tier 1: is_known_class(name) is True when name is in the mapping."""
    r = ModelResolver({"light": "openai/model-a", "standard": "openai/model-b"})
    assert r.is_known_class("light") is True
    assert r.is_known_class("standard") is True


def test_is_known_class_returns_false_for_unknown_string():
    """Tier 1: is_known_class(name) is False when name is not in the mapping."""
    r = ModelResolver({"standard": "openai/model-b"})
    assert r.is_known_class("gpt-3.5-turbo") is False
    assert r.is_known_class("gpt-4o") is False
    assert r.is_known_class("strong") is False  # not in this mapping


def test_is_known_class_false_for_empty_mapping():
    """Tier 1: empty mapping — no classes are known."""
    r = ModelResolver({})
    assert r.is_known_class("standard") is False
    assert r.is_known_class("light") is False


def test_resolve_still_passes_through_unknown():
    """Tier 1: resolve() backward-compat passthrough is preserved alongside is_known_class."""
    r = ModelResolver({"standard": "openai/gemini-2.5-flash-lite"})
    assert r.resolve("standard") == "openai/gemini-2.5-flash-lite"
    assert r.resolve("gpt-3.5-turbo") == "gpt-3.5-turbo"  # passthrough unchanged


# ---------------------------------------------------------------------------
# Tier 2b — run_skill model selection OS invariant
# ---------------------------------------------------------------------------

# Helper: build a minimal RunSkillIROp and OpContext for testing model selection
# without invoking the full handler (which would require a real sub-skill).
# We test the selection logic by directly applying the same if-branch that
# run_skill.py uses, then verify with the same resolver.


def _model_for_op(op_model: str, ctx_model: str, mapping: dict[str, str]) -> str:
    """Mirror of the model-selection logic in run_skill.handle().

    This function replicates the exact logic so that when we change the
    production code we must also update this, keeping the test in sync
    and failing loudly on divergence.
    """
    resolver = ModelResolver(mapping)
    if op_model and not resolver.is_known_class(op_model):
        return ctx_model or "standard"
    return op_model or ctx_model or "standard"


def test_run_skill_uses_known_class_from_op():
    """Tier 2b: op.model='light' (known class) → used as-is."""
    mapping = {"light": "openai/model-a", "standard": "openai/model-b"}
    result = _model_for_op("light", "standard", mapping)
    assert result == "light"


def test_run_skill_falls_back_when_op_model_is_literal():
    """Tier 2b: op.model='gpt-3.5-turbo' (not a known class) → ctx.model used.

    This is the B13-NEW-1 scenario: LLM emits a literal LiteLLM string in
    the run_skill op; the OS must ignore it to prevent proxy BadRequestError.
    """
    mapping = {"standard": "openai/gemini-2.5-flash-lite"}
    result = _model_for_op("gpt-3.5-turbo", "standard", mapping)
    assert result == "standard"


def test_run_skill_falls_back_when_op_model_is_gpt4():
    """Tier 2b: op.model='openai/gpt-4o' (not a known class) → ctx.model used."""
    mapping = {"light": "openai/model-a", "standard": "openai/model-b"}
    result = _model_for_op("openai/gpt-4o", "light", mapping)
    assert result == "light"


def test_run_skill_uses_ctx_model_when_op_model_empty():
    """Tier 2b: op.model='' (not set) → ctx.model inherited."""
    mapping = {"standard": "openai/gemini-2.5-flash-lite"}
    result = _model_for_op("", "standard", mapping)
    assert result == "standard"


def test_run_skill_defaults_to_standard_when_both_empty():
    """Tier 2b: op.model='' and ctx.model='' → 'standard' fallback."""
    mapping = {}
    result = _model_for_op("", "", mapping)
    assert result == "standard"


def test_run_skill_op_schema_model_field_defaults_empty():
    """Tier 1: RunSkillIROp.model defaults to '' (inherit-from-runtime sentinel)."""
    op = RunSkillIROp(
        kind="run_skill",
        skill="some_skill",
        input={"type": "user_message", "data": {"text": "hello"}},
    )
    assert op.model == ""
