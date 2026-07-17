"""Tier 2: #3026 resource-category collapse invariants.

Pins the two halves the #3026 PR establishes for the universal catalog:

1. **Payload invariant** — ``catalog_entries(ctx)`` (the flat action list the
   enumerate-all scheme sends to the LLM as ``tools=``) does not scale with
   operator-accumulated data. The number and identity of entries at 0, 10,
   and 50 memories / rag corpora / MCP tools / registered pipelines must be
   IDENTICAL. This is an INDEPENDENCE assertion (same name-set regardless of
   ``n``), not a count pin — the whole point of the PR is that no fixed
   number is being pinned, growth simply has zero effect on the payload.

2. **Enumeration-vs-resolution split** — author-time qualified names
   (``pipeline__<name>``, ``mcp__<server>__<tool>``) are NOT present in the
   enumerated ``catalog_entries`` output (they are resource entries, and
   #3026 removed per-resource enumeration), while
   ``universal_dispatch.resolve_invoke_action`` still RESOLVES them to their
   target tool with the resource id curried into the args. Resolution and
   enumeration are different concerns; this test pins that they stay split.
"""
from __future__ import annotations

from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import catalog_entries
from reyn.tools.universal_dispatch import resolve_invoke_action


class _FakePipelineRegistry:
    """Minimal stand-in for the real PipelineRegistry — plain class, no mocks."""

    def __init__(self, n: int) -> None:
        self._n = n

    def entries(self) -> tuple[tuple[str, str], ...]:
        return tuple((f"pipe{i}", f"pipeline {i}") for i in range(self._n))


def _ctx(n: int) -> ToolContext:
    """Build a ToolContext whose operator-accumulated data scales with ``n``."""
    rs = RouterCallerState(
        list_memory_fn=lambda _p: [
            {"name": f"mem{i}", "description": f"m{i}"} for i in range(n)
        ],
        available_rag_sources=[
            {"name": f"corpus{i}", "description": f"c{i}"} for i in range(n)
        ],
        pipeline_registry=_FakePipelineRegistry(n),
        mcp_servers=[
            {
                "name": "srv",
                "description": "s",
                "tools": [
                    {
                        "name": f"tool{i}",
                        "description": f"t{i}",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                    for i in range(n)
                ],
            }
        ],
    )
    return ToolContext(
        events=None,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=rs,
    )


def test_catalog_entries_payload_independent_of_operator_data_volume() -> None:
    """Tier 2: catalog_entries() size + name-set is invariant to accumulated data.

    This is an INDEPENDENCE assertion, not a count pin: no number is
    hardcoded anywhere in this test. Growing memories / rag corpora / MCP
    tools / registered pipelines from 0 to 10 to 50 must not change which
    (or how many) qualified action names the LLM is shown — the resource
    volume is invisible to the enumerated catalog by construction. Equal
    name-SETS (not just equal counts) is the strictly stronger check.
    """
    names_at_0 = {entry["name"] for entry in catalog_entries(_ctx(0))}
    names_at_10 = {entry["name"] for entry in catalog_entries(_ctx(10))}
    names_at_50 = {entry["name"] for entry in catalog_entries(_ctx(50))}

    assert names_at_0 == names_at_10 == names_at_50
    assert len(names_at_0) == len(names_at_10) == len(names_at_50)


def test_catalog_entries_never_contains_per_resource_dynamic_names() -> None:
    """Tier 2: no per-memory/per-corpus/per-mcp-tool/per-pipeline entry leaks in.

    Complements the independence check above with a positive-shape
    assertion: even at n=50, none of the qualified names in the enumerated
    catalog matches the per-resource dynamic shape (``mcp__srv__toolN``,
    ``pipeline__pipeN``, a bare memory/corpus name). Those names are
    resource entries, and #3026's whole change is that resources are never
    enumerated — only fixed verbs are.
    """
    names = {entry["name"] for entry in catalog_entries(_ctx(50))}

    for i in range(50):
        assert f"mcp__srv__tool{i}" not in names
        assert f"pipeline__pipe{i}" not in names
        assert f"memory_entry__mem{i}" not in names
        assert f"rag_corpus__corpus{i}" not in names


def test_pipeline_dunder_name_resolves_despite_not_being_enumerated() -> None:
    """Tier 2: pipeline__<name> still RESOLVES though it is never enumerated.

    Author-time names (taught in docs/guide/for-users/write-a-pipeline.md,
    and used by a pipeline DSL ``tool:`` step) must keep routing to
    ``run_pipeline`` with the pipeline name curried into ``target_args``,
    even though the previous test proves this exact name is absent from
    the enumerated catalog. This is the enumeration-vs-resolution split
    the PR establishes: resolving a caller-supplied name costs zero tools,
    so it is kept working, while enumerating one per pipeline is removed.
    """
    resolved = resolve_invoke_action("pipeline__greet", {"input": {"name": "Reyn"}})

    assert resolved.target_tool_name == "run_pipeline"
    assert resolved.target_args["name"] == "greet"
    assert resolved.target_args["input"] == {"name": "Reyn"}


def test_mcp_dunder_tool_name_resolves_despite_not_being_enumerated() -> None:
    """Tier 2: mcp__<server>__<tool> still RESOLVES though never enumerated.

    Mirrors the pipeline case for MCP: a pipeline DSL ``tool:`` step may
    name an MCP tool directly (``tool: mcp__echo__ping``), so resolution
    must keep working via ``_RESOURCE_RULES`` even though per-tool names
    are no longer part of the enumerated catalog (proven above).
    """
    resolved = resolve_invoke_action("mcp__echo__ping", {"message": "hi"})

    assert resolved.target_tool_name == "mcp_call_tool"
    assert resolved.target_args["tool"] == "echo__ping"
    assert resolved.target_args["tool_args"] == {"message": "hi"}
