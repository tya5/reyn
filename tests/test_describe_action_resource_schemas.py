"""Tier 2: describe_action's per-resource-schema history, and its #3026 end state.

Originally a regression test for the gap discovered while landing #119:
``describe_action`` delegated to the registry's target
``ToolDefinition.parameters``, which for resource-category actions
(``skill__X``, ``agent.peer__X``, ``mcp.tool__X.Y``, ``mcp.server__X``,
``rag_corpus__X``) was the generic dispatcher's args shape — not the
resource's actual input schema. #879 (mcp.server/mcp.tool) and the Phase-1
``agent.peer`` collapse already removed most of those special cases by
removing the resource categories themselves (verb targets need no override
because the target IS the action). ``rag_corpus__X`` was the last
survivor of the per-resource-schema override (``_resource_input_schema`` /
``_resource_description``, D2-full).

**#3026 deletes the override seam entirely**, alongside ``rag_corpus`` (the
last resource category): ``_describe_one`` now always returns the routing
TARGET's own ``description`` + ``parameters``, with no per-category
special-casing. This file now covers three things instead of the original
per-resource matrix:

  - The collapsed name (``rag_corpus__X``) no longer resolves at all —
    ``split_qualified_name`` rejects the category before any schema lookup.
  - Its operation-category replacement (``rag_operation__semantic_search``)
    describes with its own real schema, ``sources`` included (no currying).
  - The surviving AUTHOR-TIME resource name (``mcp__<server>__<tool>``,
    resolved via ``universal_dispatch._RESOURCE_RULES`` — kept alive only
    for names a human/DSL writes by hand, never enumerated) still describes
    as its dispatcher target (``mcp_call_tool``), independent of
    router_state, preserving the §D11 metadata-envelope coverage.

These tests use real ``ToolContext`` + ``RouterCallerState`` with a stub
``host`` / ``mcp_servers`` payload — no mocks of the dispatch internals.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import _handle_describe_action


class _FakeEvents:
    def emit(self, *args, **kwargs) -> None:
        pass


class _FakeHost:
    """Minimal host: ``list_available_skills`` returns an enriched catalogue
    (= D2-full shape with ``input_schema`` + ``input_wrapped`` per entry)."""

    def __init__(self, skills):
        self._skills = skills

    def list_available_skills(self):
        return list(self._skills)


def _make_ctx(skills=None, mcp_servers=None):
    rs = RouterCallerState(
        host=_FakeHost(skills or []),
        mcp_servers=mcp_servers,
    )
    return ToolContext(
        events=_FakeEvents(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=rs,
    )


def _describe(qualified_name: str, ctx: ToolContext) -> dict:
    return asyncio.run(_handle_describe_action(
        {"action_name": qualified_name}, ctx,
    ))


# Phase 1 multi_agent collapse (2026-05-25): agent.peer__X resource shape
# removed.  multi_agent__delegate is the operation-shape replacement and
# exposes the full delegate_to_agent schema (= ``to`` + ``request``) via
# the standard operation-category describe path; no curried-field surface.


# ── rag_corpus__X (#3026: collapsed; see replacement below) ──────────────


def test_rag_corpus_describe_drops_curried_sources_field():
    """Tier 1: ``rag_corpus__X`` no longer resolves at all (#3026);
    ``rag_operation__semantic_search`` — the operation-category verb that
    replaced it — describes with its full real schema, ``sources`` INCLUDED.

    This test used to pin the per-resource override (``_resource_input_schema``)
    that made ``rag_corpus__X`` describe as ``recall``'s schema minus the
    curried ``sources`` field, papering over the fact that the target was a
    generic dispatcher. #3026 deleted that override seam along with the
    ``rag_corpus`` category: there is no longer a resource action whose
    target is generic, so the special-casing this test pinned has nothing
    left to guard. ``sources`` is no longer curried away because there is no
    per-corpus qualified name to curry it FROM — the model must pass
    ``sources`` explicitly, using names learned from
    ``rag_operation__list_sources`` (see test_universal_handlers.py). The
    collapsed name itself resolving to an explicit §D12 error (not a
    schema) is the other half of the same invariant, pinned below.
    """
    ctx = _make_ctx()

    # The collapsed resource name no longer resolves — #3026 removed
    # ``rag_corpus`` from CATEGORIES, so describe_action returns the
    # §D12 error-with-suggestions envelope, not a schema.
    gone = _describe("rag_corpus__my_docs", ctx)
    assert "error" in gone
    assert "input_schema" not in gone

    # Its replacement — the operation-category verb — describes with its
    # OWN full schema (no curried-field override; #3026 deleted that seam).
    out = _describe("rag_operation__semantic_search", ctx)
    schema = out["input_schema"]
    assert "sources" in schema["properties"]
    assert "sources" in (schema.get("required") or [])
    assert "query" in schema["properties"]


# Issue #879: mcp.server__X / mcp.tool__X.Y resource-invoke describe
# paths were removed when the MCP surface collapsed to verb actions.
# Per-tool input schemas now travel through mcp__list_tools (= entries
# carry the tool description) + the existing describe_mcp_tool surface
# for richer per-tool detail.


# ── operation categories pass through unchanged ─────────────────────────


def test_operation_category_describe_returns_target_parameters():
    """Tier 1: Operation categories (``web__fetch``, …) are NOT remapped — their
    target IS the resource so ``target.parameters`` is correct."""
    ctx = _make_ctx()
    out = _describe("web__fetch", ctx)
    schema = out["input_schema"]
    # web_fetch ToolDefinition declares url + max_length.
    assert "url" in schema["properties"]
    assert schema["required"] == ["url"]


# ── empty router_state fallback ─────────────────────────────────────────


def test_no_router_state_falls_back_for_resource_categories():
    """Tier 1: describe_action of a surviving AUTHOR-TIME name
    (``mcp__<server>__<tool>``) works without router_state, describing as
    its routing target — no crash, no per-session state dependency.

    This test used to pin a per-resource-schema "fallback when router_state
    is absent" behavior for ``rag_corpus__X``. #3026 removed ``rag_corpus``
    from CATEGORIES outright (not just its router_state-aware schema path):
    ``split_qualified_name`` now raises before any router_state lookup could
    happen, with OR without router_state, so there is no fallback left to
    test for that name (see the sibling assertion in
    ``test_rag_corpus_describe_drops_curried_sources_field``).

    The surviving generalization of "describe without router_state must not
    crash" is ``universal_dispatch._RESOURCE_RULES`` — kept alive
    specifically for author-time names a human or agent DSL writes by hand
    (module docstring: ``tool: mcp__echo__ping`` in a pipeline step).
    ``resolve_describe_action`` never takes ``ctx``/router_state at all, so
    this resolves identically regardless of session state — describing as
    ``mcp_call_tool``, the dispatcher target, since the per-tool schema
    override for MCP resource names was removed in #879, not #3026.
    """
    ctx = ToolContext(
        events=_FakeEvents(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=None,
    )
    out = _describe("mcp__echo__ping", ctx)
    assert "input_schema" in out
    schema = out["input_schema"]
    props = schema.get("properties") or {}
    assert "tool" in props
    assert out["metadata"]["target_tool_name"] == "mcp_call_tool"


@pytest.mark.parametrize("qn", [
    # #3026: ``rag_corpus__my_docs`` no longer resolves (removed from
    # CATEGORIES; see test_rag_corpus_describe_drops_curried_sources_field
    # for that half). ``mcp__echo__ping`` is the surviving author-time
    # resource name (module docstring: still resolves via
    # ``_RESOURCE_RULES`` for pipeline-DSL / hand-typed use) that keeps this
    # envelope coverage alive for a name whose target is a generic
    # dispatcher rather than an operation-category verb.
    "mcp__echo__ping",
])
def test_metadata_envelope_preserved(qn: str):
    """Tier 1: All cases preserve the §D11 metadata envelope (qualified_name +
    description + metadata.{target_tool_name, category, purity}); only
    input_schema is enriched."""
    ctx = _make_ctx()
    out = _describe(qn, ctx)
    assert out["qualified_name"] == qn
    assert "description" in out
    assert "input_schema" in out
    meta = out.get("metadata") or {}
    assert "target_tool_name" in meta
    assert "category" in meta
    assert "purity" in meta
