"""Tier 2: OS invariant — #4206 T1 ②bounding axis, the ``model`` key.

``recorded_acompletion`` (the #1190 cost-observability chokepoint every
`litellm.acompletion` call passes through, AST-guard enforced) additionally
enforces a caller-declared ``model_class`` against an optional
``model_class_ceiling`` — restrict-only, reject-not-clamp, the same shape as
#3903①'s ``SandboxPolicy.max_timeout_seconds``. A call whose class exceeds the
ceiling is REJECTED before ``litellm.acompletion`` is ever invoked; a call
that opts out (``model_class=None``) is untouched by this axis entirely.

Real instances + a scripted ``litellm.acompletion`` (a plain async callable,
not a mock) throughout, per the repo testing policy.
"""
from __future__ import annotations

import asyncio

import litellm
import pytest

from reyn.llm.llm import recorded_acompletion
from reyn.llm.model_resolver import (
    STANDARD_CLASSES,
    ModelClassExceedsCeilingError,
    ModelResolver,
    model_class_exceeds_ceiling,
)

# ---------------------------------------------------------------------------
# model_class_exceeds_ceiling — pure predicate
# ---------------------------------------------------------------------------


def test_model_class_exceeds_ceiling_orders_by_standard_classes() -> None:
    """Tier 1: contract — the predicate orders strictly by STANDARD_CLASSES'
    own declared order (light < standard < strong), for every pair."""
    assert list(STANDARD_CLASSES) == ["light", "standard", "strong"]
    results = {
        (cls, ceiling): model_class_exceeds_ceiling(cls, ceiling)
        for cls in STANDARD_CLASSES
        for ceiling in STANDARD_CLASSES
    }
    # Only a class STRICTLY more expensive than the ceiling violates.
    assert results[("light", "light")] is False
    assert results[("light", "standard")] is False
    assert results[("light", "strong")] is False
    assert results[("standard", "light")] is True
    assert results[("standard", "standard")] is False
    assert results[("standard", "strong")] is False
    assert results[("strong", "light")] is True
    assert results[("strong", "standard")] is True
    assert results[("strong", "strong")] is False


def test_model_class_exceeds_ceiling_none_ceiling_never_violates() -> None:
    """Tier 1: contract — ceiling=None (unbounded, the compat default) is
    never a violation, regardless of class_name."""
    for cls in (*STANDARD_CLASSES, "custom-class", "openai/gpt-4o"):
        assert model_class_exceeds_ceiling(cls, None) is False


def test_model_class_exceeds_ceiling_unknown_names_not_comparable() -> None:
    """Tier 1: contract — a class_name or ceiling outside the 3 standard tiers
    (a raw provider/model string, or a project-declared custom class) is not
    comparable on this axis — never a violation, since there is nothing to
    order it against."""
    assert model_class_exceeds_ceiling("openai/gpt-4o", "light") is False
    assert model_class_exceeds_ceiling("strong", "custom-tier") is False
    assert model_class_exceeds_ceiling("custom-a", "custom-b") is False


# ---------------------------------------------------------------------------
# recorded_acompletion enforcement — falsify + accept-side
# ---------------------------------------------------------------------------


def _resp():
    from types import SimpleNamespace
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )


def test_recorded_acompletion_rejects_over_ceiling_without_calling_litellm(monkeypatch) -> None:
    """Tier 2: falsify direction — a call whose model_class exceeds the
    configured ceiling is rejected BEFORE litellm.acompletion is ever
    invoked (no partial call, no charge). Spies on litellm.acompletion so a
    reject that nonetheless slipped through and called it would fail this
    assertion, not just the exception-type assertion."""
    called = {"n": 0}

    async def _spy(model, messages, **kw):  # noqa: ANN001, ANN003
        called["n"] += 1
        return _resp()
    monkeypatch.setattr(litellm, "acompletion", _spy)

    with pytest.raises(ModelClassExceedsCeilingError) as excinfo:
        asyncio.run(recorded_acompletion(
            model="anthropic/claude-opus-5", messages=[{"role": "user", "content": "hi"}],
            purpose="main", model_class="strong", model_class_ceiling="light",
            recorder=None,
        ))
    assert called["n"] == 0, "litellm.acompletion must never be invoked on a rejected call"
    assert excinfo.value.requested == "strong"
    assert excinfo.value.ceiling == "light"


def test_recorded_acompletion_allows_at_or_under_ceiling(monkeypatch) -> None:
    """Tier 2: accept-side — a call whose class is AT or under the ceiling
    proceeds normally (this axis must not false-positive)."""
    called = {"n": 0}

    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        called["n"] += 1
        return _resp()
    monkeypatch.setattr(litellm, "acompletion", _fake)

    for cls in ("light", "standard"):
        resp = asyncio.run(recorded_acompletion(
            model="openai/gpt-4o-mini", messages=[{"role": "user", "content": "hi"}],
            purpose="main", model_class=cls, model_class_ceiling="standard",
            recorder=None,
        ))
        assert resp.choices[0].message.content == "ok"
    assert called["n"] == 2


def test_recorded_acompletion_model_class_none_bypasses_ceiling(monkeypatch) -> None:
    """Tier 2: accept-side — model_class=None (a caller that declared itself
    OUT of the axis, e.g. compaction #3785) is never enforced, even against a
    configured ceiling that its literal model string would otherwise exceed
    were it class-based."""
    called = {"n": 0}

    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        called["n"] += 1
        return _resp()
    monkeypatch.setattr(litellm, "acompletion", _fake)

    resp = asyncio.run(recorded_acompletion(
        model="anthropic/claude-opus-5", messages=[{"role": "user", "content": "hi"}],
        purpose="compaction", model_class=None, model_class_ceiling="light",
        recorder=None,
    ))
    assert resp.choices[0].message.content == "ok"
    assert called["n"] == 1


def test_recorded_acompletion_model_class_required_no_default() -> None:
    """Tier 1: contract — model_class has NO default (#4271 precedent): a
    caller that forgets to pass it gets a TypeError at the call site, not a
    silently-unenforced bound."""
    with pytest.raises(TypeError):
        recorded_acompletion(  # type: ignore[call-arg]
            model="m", messages=[{"role": "user", "content": "hi"}], purpose="main",
        )


# ---------------------------------------------------------------------------
# ModelResolver.class_ceiling() — a pure read, resolution/display unaffected
# ---------------------------------------------------------------------------


def test_resolver_class_ceiling_read_does_not_affect_resolution() -> None:
    """Tier 2: accept-side, direction ② — a configured ceiling is a value
    ``class_ceiling()`` exposes for a CALLER to compare; it does not itself
    gate ``resolve()`` / ``class_for_purpose()``. A display-only read (e.g.
    Session.active_model_class(), the model picker) must keep working
    unchanged even when the active/declared class is above the ceiling —
    only the actual LLM call (recorded_acompletion) is blocked."""
    resolver = ModelResolver(
        {"strong": "anthropic/claude-opus-5"},
        default_class="strong",
        model_max_class="light",  # ceiling BELOW the configured default
    )
    assert resolver.class_ceiling() == "light"
    # resolution/display still resolves the (over-ceiling) default normally —
    # this axis never touches resolve()/class_for_purpose().
    assert resolver.class_for_purpose("router") == "strong"
    assert resolver.resolve("strong").model == "anthropic/claude-opus-5"


def test_resolver_class_ceiling_defaults_to_none() -> None:
    """Tier 1: contract — a resolver built without model_max_class (every
    pre-#4206 construction site, and any caller that doesn't pass it) is
    unbounded, byte-identical to before this field existed."""
    resolver = ModelResolver({})
    assert resolver.class_ceiling() is None
