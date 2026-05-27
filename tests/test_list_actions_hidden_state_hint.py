"""Tier 2: FP-0043 Component C.1 — list_actions hidden-state hint contract.

Pins the self-service onboarding bridge that surfaces install / config
instructions to the LLM when ``search_actions`` is gated out of ``tools=``:

  1. Pure-test context (``router_state=None``) MUST NOT receive the hint.
     This guarantees existing fixtures + LLMReplay tests stay byte-stable.

  2. Production-context with no embedding class configured (= operator
     default) gets the hint. This is the fresh-user discovery path.

  3. Production-context with an embedding class set but the index not
     yet ready (= mid-build, or missing extras) ALSO gets the hint.
     False-positives during the brief build window are acceptable per
     FP-0043 §Component C.1 design.

  4. Production-context with a fully-ready index does NOT get the hint
     (= the LLM already sees search_actions in tools= via the §D14
     visibility gate).

  5. Hint content references the canonical install + config paths so a
     future docs / extras-name change surfaces here.

No mocks. Uses real RouterCallerState + a real fake EmbeddingProvider /
ActionEmbeddingIndex pair injected via constructor DI (= pattern
established in #929 / #931 reviews).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import LIST_ACTIONS


class _NullEvents:
    subscribers: list[Any] = []

    def emit(self, *_args: Any, **_kwargs: Any) -> None:
        pass


def _ctx(rs: RouterCallerState | None = None) -> ToolContext:
    return ToolContext(
        events=_NullEvents(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=rs,
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _ReadyIndex:
    """Real fake ActionEmbeddingIndex stand-in claiming readiness."""

    def is_ready(self) -> bool:
        return True


class _NotReadyIndex:
    """Real fake ActionEmbeddingIndex stand-in claiming non-readiness."""

    def is_ready(self) -> bool:
        return False


class _FakeProvider:
    """Real fake EmbeddingProvider — methods exist but are never invoked."""

    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        return {"vectors": [], "model": model, "total_tokens": 0}

    def estimate_tokens(self, _t: list[str]) -> int:
        return 0

    def get_dimension(self, _m: str) -> int:
        return 384


# ── 1. Pure-test context: no hint ───────────────────────────────────────────


def test_pure_test_context_does_not_receive_hint() -> None:
    """Tier 2: ``router_state=None`` (= unit-test / standalone caller) suppresses
    the hint so existing fixtures + LLMReplay tests stay byte-stable.
    """
    result = _run(LIST_ACTIONS.handler({"category": ["file"]}, _ctx(None)))
    assert "hint" not in result


# ── 2. Fresh-user production context: hint fires ────────────────────────────


def test_no_embedding_class_configured_fires_hint() -> None:
    """Tier 2: fresh production session with no embedding class fires the hint.

    This is the canonical onboarding scenario: operator hasn't enabled
    semantic search, the LLM sees only ``list_actions`` in tools=, the
    hint tells the LLM how to enable the rest.
    """
    rs = RouterCallerState(
        action_embedding_index=None,
        embedding_provider=None,
        embedding_model_class=None,
    )
    result = _run(LIST_ACTIONS.handler({"category": ["file"]}, _ctx(rs)))
    assert "hint" in result, (
        "hint must fire when embedding_class is unset in a production session"
    )


def test_class_set_but_index_not_ready_fires_hint() -> None:
    """Tier 2: class set but index still building (or missing extras) → hint.

    Catches both the brief background-build window AND the
    "configured-but-extras-not-installed" case. Both are situations
    where the LLM should surface the install path; the hint is
    informational, not blocking.
    """
    rs = RouterCallerState(
        action_embedding_index=_NotReadyIndex(),
        embedding_provider=_FakeProvider(),
        embedding_model_class="local-mini",
    )
    result = _run(LIST_ACTIONS.handler({"category": ["file"]}, _ctx(rs)))
    assert "hint" in result


def test_class_set_but_index_object_missing_fires_hint() -> None:
    """Tier 2: embedding_model_class set but action_embedding_index is None.

    Edge case: operator set the config but RouterLoop hasn't constructed
    the index yet (= very early in session boot). Hint fires; the LLM
    relays "install local-embed" which is harmless extra info.
    """
    rs = RouterCallerState(
        action_embedding_index=None,
        embedding_provider=_FakeProvider(),
        embedding_model_class="local-mini",
    )
    result = _run(LIST_ACTIONS.handler({"category": ["file"]}, _ctx(rs)))
    assert "hint" in result


# ── 3. Ready production context: no hint ────────────────────────────────────


def test_ready_index_suppresses_hint() -> None:
    """Tier 2: when the §D14 gate would expose search_actions, no hint.

    The LLM already sees search_actions in tools=; surfacing the
    install hint to the user would be confusing noise.
    """
    rs = RouterCallerState(
        action_embedding_index=_ReadyIndex(),
        embedding_provider=_FakeProvider(),
        embedding_model_class="local-mini",
    )
    result = _run(LIST_ACTIONS.handler({"category": ["file"]}, _ctx(rs)))
    assert "hint" not in result


# ── 4. Hint content invariants ──────────────────────────────────────────────


def test_hint_references_local_embed_extras() -> None:
    """Tier 2: hint string mentions the canonical extras name.

    Pinned so a future rename of the extras (= ``local-embed`` → ?)
    surfaces here rather than silently breaking the onboarding path.
    """
    rs = RouterCallerState()
    result = _run(LIST_ACTIONS.handler({"category": ["file"]}, _ctx(rs)))
    hint = result["hint"]
    assert "reyn[local-embed]" in hint


def test_hint_references_openai_config_path() -> None:
    """Tier 2: hint string mentions the OpenAI-via-reyn.yaml alternative.

    Operator with credentials but unwilling to install heavy extras
    can take the OpenAI path; the hint must document both options.
    """
    rs = RouterCallerState()
    result = _run(LIST_ACTIONS.handler({"category": ["file"]}, _ctx(rs)))
    hint = result["hint"]
    assert "OPENAI_API_KEY" in hint
    assert "embedding_class" in hint


def test_hint_references_search_actions_by_name() -> None:
    """Tier 2: hint identifies search_actions as the unlocked capability.

    The LLM needs to understand WHAT the install enables, not just HOW.
    """
    rs = RouterCallerState()
    result = _run(LIST_ACTIONS.handler({"category": ["file"]}, _ctx(rs)))
    hint = result["hint"]
    assert "search_actions" in hint


# ── 5. Existing shape preserved ─────────────────────────────────────────────


def test_items_and_total_shape_unchanged_when_hint_present() -> None:
    """Tier 2: adding ``hint`` MUST NOT mutate the ``items`` / ``total`` shape.

    Downstream callers depend on the §D11 envelope; the hint is an
    additive field, not a replacement.
    """
    rs = RouterCallerState()
    result = _run(LIST_ACTIONS.handler({"category": ["file"]}, _ctx(rs)))
    assert isinstance(result["items"], list)
    assert isinstance(result["total"], int)
    assert result["total"] == len(result["items"])
    assert "hint" in result  # added on top, not replacing


def test_items_count_when_hint_absent_matches_when_present() -> None:
    """Tier 2: identical category filter yields identical items regardless
    of whether the hint fired.

    The hint is presentational metadata only; it has zero impact on
    the underlying catalog enumeration.
    """
    rs_ready = RouterCallerState(
        action_embedding_index=_ReadyIndex(),
        embedding_provider=_FakeProvider(),
        embedding_model_class="local-mini",
    )
    rs_unready = RouterCallerState()
    r_ready = _run(LIST_ACTIONS.handler({"category": ["file"]}, _ctx(rs_ready)))
    r_unready = _run(LIST_ACTIONS.handler({"category": ["file"]}, _ctx(rs_unready)))
    assert "hint" not in r_ready
    assert "hint" in r_unready
    # Same items in the same order, regardless of hint state.
    assert (
        [it["qualified_name"] for it in r_ready["items"]]
        == [it["qualified_name"] for it in r_unready["items"]]
    )
