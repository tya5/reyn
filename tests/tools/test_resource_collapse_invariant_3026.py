"""Tier 2: #3026 resource-category collapse invariants.

Pins the two halves the #3026 PR establishes for the universal catalog:

1. **Payload invariant** — ``catalog_entries(ctx)`` (the flat action list the
   enumerate-all scheme sends to the LLM as ``tools=``) does not scale with
   operator-accumulated data. The number and identity of entries at 0, 10,
   and 50 memories / rag corpora / MCP tools / registered pipelines must be
   IDENTICAL. This is an INDEPENDENCE assertion (same name-set regardless of
   ``n``), not a count pin — the whole point of the PR is that no fixed
   number is being pinned, growth simply has zero effect on the payload.

2. **A resource is an ARGUMENT, never a name** — no per-resource entry
   (``pipeline__<name>``, ``mcp__<server>__<tool>``, a memory slug, a corpus
   name) appears in the enumerated ``catalog_entries`` output.

   #3026 left half of this open: those names were not enumerated but still
   RESOLVED, as an "author-time" spelling for a human writing a pipeline DSL
   step. #3429 closed it — they were the qualified spelling in operator-facing
   clothes, and the same coin-flip every name-keyed subsystem had to call. The
   resource id now rides as an ordinary argument on the reachable verb
   (``run_pipeline{name}``, ``mcp_call_tool{tool, tool_args}``), which is what
   the enumerated verbs already did. Both halves are asserted below.
"""
from __future__ import annotations

from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import catalog_entries
from reyn.tools.universal_dispatch import is_known_action


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


def test_pipeline_dunder_name_is_not_an_action() -> None:
    """Tier 2: #3429 — ``pipeline__<name>`` is not a name the OS answers to.

    #3026 kept it resolvable as an author-time form (the user guide taught
    ``pipeline__greet``, and a pipeline DSL ``tool:`` step could carry it) on the
    reasoning that resolving a name someone already typed costs zero payload.
    That reasoning survives the payload lens and fails the naming one: it was a
    SECOND name for ``run_pipeline``, so every subsystem keyed on a tool name had
    to decide whether to handle it. The capability is unchanged —
    ``run_pipeline{name: "greet"}`` is the same call the curried form made."""
    assert not is_known_action("pipeline__greet")
    assert is_known_action("run_pipeline")


def test_mcp_dunder_tool_name_is_not_an_action() -> None:
    """Tier 2: #3429 — ``mcp__<server>__<tool>`` is not a name the OS answers to.

    Mirrors the pipeline case. The MCP tool identifier itself
    (``echo__ping``) is unchanged and still carries a ``__`` — it belongs to the
    MCP server's namespace, not reyn's, and reaches the tool as
    ``mcp_call_tool``'s ``tool`` ARGUMENT rather than as a reyn tool name."""
    assert not is_known_action("mcp__echo__ping")
    assert is_known_action("mcp_call_tool")
