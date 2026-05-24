"""Tier 2: describe_action returns per-resource input_schema (FP-0034 D2-full).

Regression for the gap discovered while landing #119: ``describe_action``
delegates to the registry's target ``ToolDefinition.parameters``, which for
resource-category actions (``skill__X``, ``agent.peer__X``,
``mcp.tool__X.Y``, ``mcp.server__X``, ``rag.corpus__X``) is the generic
dispatcher's args shape — not the resource's actual input schema. The
weak-model probe path is: hot-list direct alias unavailable for this skill
→ LLM calls ``describe_action(skill__X)`` to learn args → gets
``{name, input}`` (= ``invoke_skill`` dispatcher schema) → has no path
forward.

The fix special-cases resource categories in ``_handle_describe_action``:

  - ``skill__X``           : the skill's input artifact schema (= the
                             ``schema:`` block of ``<input_artifact>.yaml``)
                             via ``ctx.router_state.host.list_available_skills``.
  - ``agent.peer__X``      : ``delegate_to_agent`` parameters minus ``to``.
  - ``mcp.server__X``      : empty object (``list_mcp_tools`` curries server).
  - ``rag.corpus__X``      : ``recall`` parameters minus ``sources``.
  - ``mcp.tool__X.Y``      : the MCP tool's declared ``inputSchema`` via
                             ``ctx.router_state.mcp_servers``.

Operation categories (``web__fetch``, ``file__read``, …) continue to fall
through to the target's parameters (which IS the resource for operations).

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


# ── skill__X ────────────────────────────────────────────────────────────


def test_skill_describe_returns_skill_input_schema():
    """Tier 1: ``describe_action(skill__X)`` returns the SKILL's input fields, not
    invoke_skill's ``{name, input}`` envelope."""
    ctx = _make_ctx(skills=[
        {
            "name": "index_docs",
            "description": "Index docs",
            "input_artifact": "index_docs_input",
            "input_fields": ["source", "path", "description"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "path": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["source", "path", "description"],
            },
            "input_wrapped": True,
        },
    ])
    out = _describe("skill__index_docs", ctx)
    schema = out["input_schema"]
    # Real skill fields, not the dispatcher's {name, input}
    assert set(schema["properties"].keys()) == {"source", "path", "description"}
    assert set(schema["required"]) == {"source", "path", "description"}
    # The dispatcher's keys must NOT be present
    assert "name" not in schema["properties"]
    assert "input" not in schema["properties"]


def test_skill_without_input_schema_falls_back_to_dispatcher():
    """Tier 1: When the catalogue entry has no ``input_schema`` (= caller didn't
    enrich), describe_action falls back to ``invoke_skill``'s parameters
    so the LLM at least sees the dispatcher contract."""
    ctx = _make_ctx(skills=[
        {"name": "legacy_skill", "description": "old", "input_fields": []},
    ])
    out = _describe("skill__legacy_skill", ctx)
    schema = out["input_schema"]
    # Dispatcher exposes ``name`` (curried) + ``input`` — caller sees the
    # generic envelope, recoverable but suboptimal.
    assert "input" in schema.get("properties", {}) or "name" in schema.get(
        "properties", {}
    )


# ── agent.peer__X ───────────────────────────────────────────────────────


def test_agent_peer_describe_drops_curried_to_field():
    """Tier 1: ``describe_action(agent.peer__X)`` exposes delegate_to_agent's
    parameters MINUS ``to`` (the routing rule curries ``to=<name>``)."""
    ctx = _make_ctx()
    out = _describe("agent.peer__alice", ctx)
    schema = out["input_schema"]
    assert "to" not in schema.get("properties", {})
    assert "to" not in (schema.get("required") or [])
    # delegate_to_agent always takes ``request`` from the caller.
    assert "request" in schema["properties"]


# ── mcp.server__X ───────────────────────────────────────────────────────


# ── rag.corpus__X ───────────────────────────────────────────────────────


def test_rag_corpus_describe_drops_curried_sources_field():
    """Tier 1: ``rag.corpus__X`` resolves to ``recall(sources=[X], …)`` — expose
    recall's parameters MINUS ``sources``."""
    ctx = _make_ctx()
    out = _describe("rag.corpus__my_docs", ctx)
    schema = out["input_schema"]
    assert "sources" not in schema.get("properties", {})
    assert "sources" not in (schema.get("required") or [])
    # ``query`` is the canonical user-facing field.
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
    """Tier 1: Phase-side / test sites with no router_state get the dispatcher's
    schema as a fallback — better than crashing."""
    ctx = ToolContext(
        events=_FakeEvents(),
        permission_resolver=None,
        workspace=None,
        caller_kind="phase",
        router_state=None,
    )
    out = _describe("skill__index_docs", ctx)
    # Fallback shape — dispatcher's parameters, not crash.
    assert "input_schema" in out
    schema = out["input_schema"]
    # invoke_skill dispatcher carries an ``input`` or ``name`` field.
    props = schema.get("properties") or {}
    assert "input" in props or "name" in props


@pytest.mark.parametrize("qn", [
    "skill__index_docs",
    "agent.peer__alice",
    "rag.corpus__my_docs",
])
def test_metadata_envelope_preserved(qn: str):
    """Tier 1: All cases preserve the §D11 metadata envelope (qualified_name +
    description + metadata.{target_tool_name, category, purity}); only
    input_schema is enriched."""
    ctx = _make_ctx(skills=[{
        "name": "index_docs",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }])
    out = _describe(qn, ctx)
    assert out["qualified_name"] == qn
    assert "description" in out
    assert "input_schema" in out
    meta = out.get("metadata") or {}
    assert "target_tool_name" in meta
    assert "category" in meta
    assert "purity" in meta
