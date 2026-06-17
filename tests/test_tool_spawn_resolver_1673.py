"""Tier 2: #1673 — tool-spawned skill runs get a real resolver + config-following
model class (latent literal-model-to-litellm bug + #1672 CAT-3 completion).

The 5 hand-building tool handlers built their OpContext with `resolver=None` +
the literal `model="standard"`, and `ToolContext` carried no resolver. With
`resolver=None` the spawned run degrades to `ModelResolver({})`, where
`resolve("standard").model == "standard"` — a LITERAL model name that litellm
rejects with `BadRequestError` on the spawned run's first LLM call (the same
"standard"-literal class #1172 fixed for compaction but missed for tool-spawns).

The fix threads the config-aware resolver through `ToolContext` to the spawned
OpContext, and resolves the model via `class_for_purpose("tool")` (follows
`model_class_by_purpose` / config.model). This pins:
  - the bug shape (resolver-None → literal "standard"), and
  - the fix (real resolver → a real provider/model string, NOT the literal).

No mocks: a real `ModelResolver`, a real `ToolContext`, and a real async capture
function (a counting/inspecting wrapper monkeypatched onto the run_skill handler —
the permitted pattern, not unittest.mock).
"""
from __future__ import annotations

import asyncio

from reyn.llm.model_resolver import ModelResolver, resolve_purpose_class
from reyn.tools.invoke_skill import _handle as invoke_skill_handle
from reyn.tools.types import ToolContext


class _MinimalEvents:
    subscribers: list = []


def _ctx_with_resolver(resolver) -> ToolContext:
    """A ToolContext on the FALLBACK path (router_state=None → invoke_skill builds
    the minimal OpContext) carrying a real config-aware resolver."""
    return ToolContext(
        events=_MinimalEvents(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=None,
        resolver=resolver,
    )


# ── the bug shape (what the fix prevents) ───────────────────────────────────────


def test_bug_shape_resolver_none_yields_literal_standard() -> None:
    """Tier 2: #1673 — the pre-fix degradation: a resolver=None OpContext resolves
    the "standard" class to the LITERAL "standard" (which litellm rejects with
    BadRequestError). Documents the bug the threading prevents."""
    degraded = ModelResolver({})  # what resolver=None becomes in the spawned runtime
    assert degraded.resolve("standard").model == "standard", (
        "the literal 'standard' is what reached litellm pre-fix"
    )


# ── the fix: invoke_skill threads the real resolver + a resolvable model ─────────


def test_invoke_skill_threads_real_resolver_and_resolvable_model(monkeypatch) -> None:
    """Tier 2: #1673 — invoke_skill's fallback builds the spawned OpContext with the
    THREADED config-aware resolver and a model that resolves to a real provider/model
    string (NOT the literal "standard" → no BadRequestError)."""
    captured: dict = {}

    async def _capture_handle(*, op, ctx, caller):  # real async fn, not a Mock
        captured["model"] = ctx.model
        captured["resolver"] = ctx.resolver
        return {"status": "ok"}

    # Replace the run_skill handler invoke_skill delegates to (lazy import target).
    monkeypatch.setattr("reyn.core.op_runtime.run_skill.handle", _capture_handle)

    resolver = ModelResolver(
        {"standard": "openai/gpt-4o"}, default_class="standard",
    )
    ctx = _ctx_with_resolver(resolver)
    asyncio.run(invoke_skill_handle({"name": "demo", "input": {"text": "hi"}}, ctx))

    # The spawned OpContext carries the REAL resolver (not None).
    assert captured["resolver"] is resolver
    # And its model resolves to a real provider/model string — NOT the literal.
    resolved = resolver.resolve(captured["model"]).model
    assert resolved == "openai/gpt-4o", f"expected a real model, got {resolved!r}"
    assert captured["model"] != "standard" or resolved != "standard"


def test_invoke_skill_follows_tool_purpose_class(monkeypatch) -> None:
    """Tier 2: #1673 / #1672 CAT-3 — a `model_class_by_purpose: {tool: ...}` override
    is honoured for the spawned run (it follows the "tool" purpose class, not a
    hardcoded tier)."""
    captured: dict = {}

    async def _capture_handle(*, op, ctx, caller):
        captured["model"] = ctx.model
        return {"status": "ok"}

    monkeypatch.setattr("reyn.core.op_runtime.run_skill.handle", _capture_handle)

    resolver = ModelResolver(
        {"standard": "openai/gpt-4o", "strong": "anthropic/claude-3-7-sonnet"},
        default_class="standard",
        purpose_classes={"tool": "strong"},
    )
    ctx = _ctx_with_resolver(resolver)
    asyncio.run(invoke_skill_handle({"name": "demo", "input": {"text": "hi"}}, ctx))

    # tool purpose → "strong" class → the strong model.
    assert resolver.resolve(captured["model"]).model == "anthropic/claude-3-7-sonnet"


def test_tool_purpose_class_helper_resolvable_not_literal() -> None:
    """Tier 2: #1673 — resolve_purpose_class(None, <real resolver>, "tool") yields a
    class the resolver maps to a real provider/model (the boundary the tool sites
    now use), vs the no-resolver path which can only fall back to "standard"."""
    resolver = ModelResolver({"standard": "gemini/gemini-2.5-flash-lite"}, default_class="standard")
    cls = resolve_purpose_class(None, resolver, "tool")
    assert resolver.resolve(cls).model == "gemini/gemini-2.5-flash-lite"
    # No resolver → "standard" (the literal that would need a resolver to be safe).
    assert resolve_purpose_class(None, None, "tool") == "standard"
