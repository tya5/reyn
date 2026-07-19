"""RAG corpus discovery verb — ``rag_operation__list_sources`` (#3026).

The discovery half of the RAG surface. ``semantic_search`` has always been able
to search a corpus, but nothing told the model which corpora exist: the
``sources`` argument is a closed set of operator-chosen names, and a name the
model cannot see is a corpus it cannot search.

**Why this tool is new even though the gap is old.** Until #3026 the naming
surface was the ``rag_corpus__<name>`` catalog category — one flat action per
indexed corpus, so the operator's corpora landed directly in the LLM's
``tools=`` payload and the payload grew with them. #3026 collapses that
category (the payload must not scale with what the operator accumulated), which
removes the only surface that named a corpus. Discovery therefore has to come
back as a VERB whose result carries the names, exactly one tool regardless of
corpus count — the same shape #2971 used for ``skill_management__list`` and
#879 used for ``mcp__list_tools``.

**Why not the system prompt.** ``semantic_search``'s ``sources`` description
once pointed at an "Indexed sources" SP section, and ``build_system_prompt``
used to accept an ``indexed_sources_section`` argument — but that section had
not been rendered since B23-PRE-1, so the argument was accepted and discarded
while the router still paid a per-turn ``SourceManifest.format_for_prompt()``
to build it (#3025 removed both the parameter and that prefetch). Reviving it
would put a per-corpus list in every turn's prompt: the same operator-scaling
cost this PR is removing, just moved from ``tools=`` into the SP. A verb is
paid for only when the model actually asks.

Read-only, and no permission gate: it returns the operator's own corpus
declarations from the snapshot the router already built for this session —
strictly less than an ``index_update`` caller supplied in the first place.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import discovery as _discovery_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_LIST_RAG_SOURCES_DESCRIPTION = _discovery_descriptions.list_rag_sources.text

# No parameters: the result is already scoped to the corpora indexed for this
# session. A ``backend`` / name filter would be premature — the whole point is
# that the model does not yet know what exists, so it has nothing to filter by.
_LIST_RAG_SOURCES_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {},
}


async def _handle_list_rag_sources(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Return the indexed RAG corpora: ``{sources: [{name, description,
    backend, chunk_count}, ...]}``.

    Reads ``ctx.router_state.available_rag_sources`` — the snapshot RouterLoop
    builds once per turn from ``SourceManifest.get_all()`` — rather than
    re-reading the manifest, so this tool costs nothing beyond what the session
    already paid and cannot disagree with the rest of the catalog about which
    corpora exist.

    ``None`` (= router supplied no snapshot: narrow test hosts, plan-step
    hosts) is treated identically to empty, matching how the catalog
    enumeration has always degraded. An empty list is a truthful answer —
    "nothing is indexed yet" — not an error.

    ``name`` is the load-bearing field: it is what the caller passes back as
    ``semantic_search(sources=[...])``. ``chunk_count`` / ``backend`` ride along
    so the model can tell a populated corpus from an empty one before spending
    a search on it.
    """
    rs = getattr(ctx, "router_state", None)
    entries = getattr(rs, "available_rag_sources", None) or []

    sources = [
        {
            "name": e.get("name", ""),
            "description": e.get("description", "") or "",
            "backend": e.get("backend"),
            "chunk_count": e.get("chunk_count"),
        }
        for e in entries
        if isinstance(e, Mapping) and e.get("name")
    ]
    return {"sources": sources}


from reyn.core.offload.canonical import list_rag_sources_to_canonical  # noqa: E402

LIST_RAG_SOURCES = ToolDefinition(
    canonical=list_rag_sources_to_canonical,
    name="list_rag_sources",
    description=_LIST_RAG_SOURCES_DESCRIPTION,
    parameters=_LIST_RAG_SOURCES_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_list_rag_sources,
    category="discovery",
    purity="read_only",
    # A corpus description is not necessarily the operator's own text: the
    # ``description`` field is a parameter of the agent-callable ``index_update``
    # tool, so an agent (or anything steering one) can author it, and this tool
    # re-surfaces it on every later call. Same fencing rationale as ``list_memory``
    # ("user/agent-written") and ``skill_list`` (re-surfaced install-time text) —
    # threat-scanning at write time does not cover a scan-rule update afterwards.
    returns_external_content=True,
    doc_ref="docs/concepts/tools-integrations/universal-catalog.md",
)

__all__ = ["LIST_RAG_SOURCES"]
