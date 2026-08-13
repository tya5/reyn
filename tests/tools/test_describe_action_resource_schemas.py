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
    is ITSELF now retired (FP-0066 P1b: the ``rag_operation`` category and
    the layer-1 agent tool it routed to are gone) — so this name ALSO no
    longer resolves, for the same reason as ``rag_corpus__X``.
  - **#3429**: the last AUTHOR-TIME resource name (``mcp__<server>__<tool>``,
    kept resolvable for names a human/DSL wrote by hand, never enumerated)
    is gone too — it was the qualified spelling in operator-facing clothes.
    Describing it now returns the §D12 unknown-action envelope, and the
    §D11 metadata-envelope coverage moves to ``mcp_call_tool``, the name
    that actually reaches the tool.

These tests use real ``ToolContext`` + ``RouterCallerState`` with a stub
``host`` / ``mcp_servers`` payload — no mocks of the dispatch internals.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.events import EventLog
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import _handle_describe_action


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
        events=EventLog(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=rs,
    )


def _describe(action_name: str, ctx: ToolContext) -> dict:
    return asyncio.run(_handle_describe_action(
        {"action_name": action_name}, ctx,
    ))


# Phase 1 multi_agent collapse (2026-05-25): agent.peer__X resource shape
# removed.  delegate_to_agent is the operation-shape replacement and
# exposes the full delegate_to_agent schema (= ``to`` + ``request``) via
# the standard operation-category describe path; no curried-field surface.


# ── rag_corpus__X (#3026: collapsed; see replacement below) ──────────────


def test_rag_corpus_and_retired_rag_operation_neither_resolve():
    """Tier 1: ``rag_corpus__X`` (#3026-collapsed) and
    ``rag_operation__semantic_search`` (FP-0066 P1b-retired, was #3026's
    operation-category replacement for ``rag_corpus``) both fail to resolve —
    neither ``rag_corpus`` nor ``rag_operation`` is a category any more.

    This test used to pin the per-resource override (``_resource_input_schema``)
    that made ``rag_corpus__X`` describe as ``recall``'s schema minus the
    curried ``sources`` field, papering over the fact that the target was a
    generic dispatcher. #3026 deleted that override seam along with the
    ``rag_corpus`` category. FP-0066 P1b then retired the operation-category
    replacement itself (the ``rag_operation`` category + the layer-1 agent
    tools it routed to), so both qualified names now resolve to the same
    §D12 error-with-suggestions envelope, not a schema.
    """
    ctx = _make_ctx()

    gone = _describe("rag_corpus__my_docs", ctx)
    assert "error" in gone
    assert "input_schema" not in gone

    also_gone = _describe("rag_operation__semantic_search", ctx)
    assert "error" in also_gone
    assert "input_schema" not in also_gone


# Issue #879: mcp.server__X / mcp.tool__X.Y resource-invoke describe
# paths were removed when the MCP surface collapsed to verb actions.
# Per-tool input schemas now travel through list_mcp_tools (= entries
# carry the tool description) + the existing describe_mcp_tool surface
# for richer per-tool detail.


# ── operation categories pass through unchanged ─────────────────────────


def test_operation_category_describe_returns_target_parameters():
    """Tier 1: Operation categories (``web_fetch``, …) are NOT remapped — their
    target IS the resource so ``target.parameters`` is correct."""
    ctx = _make_ctx()
    out = _describe("web_fetch", ctx)
    schema = out["input_schema"]
    # web_fetch ToolDefinition declares url only (#3580 ③ removed max_length).
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
    crash" is ``mcp_call_tool`` — the verb an MCP tool is reached THROUGH,
    whose ``{tool, tool_args}`` envelope is the same whatever the session
    knows. Membership is a static table lookup that never consults
    ``ctx``/router_state, so this describes identically regardless of session
    state. (#3429 removed the ``mcp__<server>__<tool>`` author-time name that
    used to stand in for this case.)
    """
    ctx = ToolContext(
        events=EventLog(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=None,
    )
    out = _describe("mcp_call_tool", ctx)
    assert "input_schema" in out
    schema = out["input_schema"]
    props = schema.get("properties") or {}
    assert "tool" in props
    assert out["action_name"] == "mcp_call_tool"


@pytest.mark.parametrize("qn", [
    # #3026 removed ``rag_corpus__my_docs``; #3429 removed the last
    # author-time resource name (``mcp__echo__ping``). ``mcp_call_tool`` is
    # what an MCP tool is reached through, so it carries this envelope
    # coverage for a name whose schema is a generic dispatch envelope rather
    # than an operation verb's own arguments.
    "mcp_call_tool",
])
def test_metadata_envelope_preserved(qn: str):
    """Tier 1: the §D11 metadata envelope (action_name + description +
    metadata.{category, purity}) is preserved.

    #3429 dropped ``metadata.target_tool_name`` from that envelope: it named
    the tool the QUALIFIED spelling resolved to, and with one name there is
    nothing for it to differ from."""
    ctx = _make_ctx()
    out = _describe(qn, ctx)
    assert out["action_name"] == qn
    assert "description" in out
    assert "input_schema" in out
    meta = out.get("metadata") or {}
    assert "target_tool_name" not in meta
    assert "category" in meta
    assert "purity" in meta
