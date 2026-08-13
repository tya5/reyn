"""``search_knowledge`` — the ``knowledge`` category surface (FP-0066 P3c,
#3247 "P3 設計 firm" §2/§3/§5/§6, the final P3 piece that lights up
knowledge-RAG for the LLM).

**Where this sits in the P3 arc**: P3-helper unified the search-emit wrap
(``coordinator.emit_wrapped_semantic_search``); P3a/P3b built the FOUR
knowledge sources and their ingest producers (``knowledge_ingest.py`` —
``KNOWLEDGE_MEMORY_SOURCE_ID`` / ``KNOWLEDGE_SKILL_SOURCE_ID`` /
``KNOWLEDGE_REPO_DOC_SOURCE_ID`` / ``KNOWLEDGE_REPO_SRC_SOURCE_ID``); this
module is the first LLM-reachable READ surface over those four sources —
without it the P3a/b ingest work was write-only (indexed but unqueryable).

**§5 contract**: ``search_knowledge(query) -> {items: [{kind, id, title,
description}, ...], total}``. ``id`` is KIND-NATIVE (a skill name / a
memory doc path / a repo file path) — never an abstract handle — so the
caller activates a hit via the KIND-ROUTED verb (skill -> ``load_skill``,
memory -> ``read_memory_body``, repo_doc/repo_src -> ``reyn_repo_read``),
never a unified "load" verb (the firm's explicit anti-pattern: unifying
across kinds smells like the retired ``file.read`` skill special-case).

**★ §G1 chunk->entity aggregation**: every knowledge source is indexed at
CHUNK granularity (``knowledge_ingest.py``'s v1 shape happens to embed one
chunk per entity today, but the Coordinator/backend contract is
chunk-level, and a future code-aware ``repo_src`` chunker — firm §12 —
will split one file into several chunks). A raw per-source query can
therefore return MULTIPLE rows for the same entity; ``search_knowledge``
must present ENTITY-level results. ``_aggregate_entities`` groups the
merged chunk-level hits by ``(kind, id)``, keeps the MAX score per entity
(the highest-scoring chunk best represents why the entity matched), and
orders the output by descending score — see its docstring for why this is
the one place that guarantee is enforced, not per-source.

**Query fan-out**: for each of the four sources, ``search_knowledge``
awaits ``IndexCoordinator.search_await(source_id)`` (steady-state cheap
no-op; heals a dirty/never-built source when this process happens to own
its build strategy — see ``coordinator.search_await``'s own degrade
notes for the cross-process case) THEN queries it, via the shared
``emit_wrapped_semantic_search`` helper (P3-helper, #3247 firm §6) so the
``semantic_search_started``/``_complete`` audit-event pair and the
completeness discipline are identical to ``search_actions`` — this is the
helper's THIRD call site (the firm explicitly names this as the reason
the helper had to exist before this module could land, to avoid a third
duplicate of the wrap).

**#1822 = _EXTERNAL** (firm §2): unlike ``load_skill`` (activation,
_NOT_EXTERNAL), ``search_knowledge``'s role is DISCOVERY — it re-surfaces
operator/user-authored skill/memory/repo text without activating it, the
same role class as ``skill_list`` (_EXTERNAL). ``returns_external_content
=True`` below; ``tests/tools/test_returns_external_content_flagset_1822.py``
pins the classification.

**visibility (firm §6)**: the ``knowledge`` category (and therefore
``search_knowledge`` / this tool's registry entry, reachable via
``invoke_action``) is enumerated only when ``embedding.enabled:
true`` — ``universal_catalog._enumerate_category``'s ``"knowledge"``
branch calls the SAME ``is_search_available`` predicate ``search_actions``
already uses (shared helper, not a duplicated embedding-config re-check —
mirrors how the pre-existing ``"exec"`` branch shares ``is_exec_available``
with ``visible_categories``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Mapping

from reyn.core.offload.canonical import search_knowledge_to_canonical
from reyn.data.index.knowledge_ingest import (
    KNOWLEDGE_MEMORY_SOURCE_ID,
    KNOWLEDGE_REPO_DOC_SOURCE_ID,
    KNOWLEDGE_REPO_SRC_SOURCE_ID,
    KNOWLEDGE_SKILL_SOURCE_ID,
)
from reyn.tools.descriptions import discovery as _discovery_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

if TYPE_CHECKING:
    from reyn.core.op_runtime.context import OpContext
    from reyn.data.index.backend import ChunkRecord, IndexBackend

__all__ = [
    "SEARCH_KNOWLEDGE",
]


# ── source -> kind mapping (the four P3a/b knowledge sources) ─────────────

_KNOWLEDGE_SOURCES: Final[tuple[tuple[str, str], ...]] = (
    (KNOWLEDGE_MEMORY_SOURCE_ID, "memory"),
    (KNOWLEDGE_SKILL_SOURCE_ID, "skill"),
    (KNOWLEDGE_REPO_DOC_SOURCE_ID, "repo_doc"),
    (KNOWLEDGE_REPO_SRC_SOURCE_ID, "repo_src"),
)

_MAX_DESCRIPTION_CHARS: Final[int] = 240


def _truncate(text: "str | None") -> str:
    """Trim a chunk's raw embedded text for the LLM-facing ``description``
    field — mirrors ``universal_catalog._truncate_short_description``'s cap
    (kept as a small local copy rather than importing that private helper
    across modules — same rationale as ``knowledge_ingest.py``'s
    ``_strip_frontmatter`` "port, don't import-up" note: this module is a
    sibling of ``universal_catalog``, not a dependent of its private
    surface)."""
    if not text:
        return ""
    stripped = text.strip()
    if len(stripped) <= _MAX_DESCRIPTION_CHARS:
        return stripped
    return stripped[: _MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"


def _chunk_to_raw_result(kind: str, rec: "ChunkRecord") -> dict[str, Any]:
    """Map one queried ``ChunkRecord`` to a CHUNK-level result row.

    ``id`` is kind-native (§5): the skill's registered name (from
    ``extra["name"]`` — ``knowledge_ingest._skill_to_chunk_record``), or
    the doc-shaped ``source_path`` every other kind's chunk metadata
    already carries (memory: ``"{layer}/{slug}.md"``; repo_doc/repo_src:
    the repo-relative path). ``title`` is a shorter human label distinct
    from ``id`` where the metadata gives one (memory's bare slug); it
    falls back to ``id`` otherwise. This is CHUNK-level output —
    ``_aggregate_entities`` collapses same-``(kind, id)`` rows before this
    reaches the caller.
    """
    metadata = rec["metadata"]
    extra = metadata.get("extra") or {}
    source_path = str(metadata.get("source_path") or "")
    if kind == "skill":
        entity_id = str(extra.get("name") or source_path)
        title = entity_id
    elif kind == "memory":
        entity_id = source_path
        title = str(extra.get("slug") or source_path)
    else:  # repo_doc / repo_src
        entity_id = source_path
        title = source_path
    return {
        "kind": kind,
        "id": entity_id,
        "title": title,
        "description": _truncate(rec.get("text")),
        "score": float(rec.get("score") or 0.0),
    }


def _aggregate_entities(raw_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """§G1 chunk->entity aggregation.

    Groups chunk-level result rows by ``(kind, id)`` — the entity identity
    — keeping only the MAX-score row per entity (the chunk that best
    represents why the entity matched), then orders the deduplicated
    entities by descending score. An entity hit by N chunks across N
    ``top_k`` slots therefore contributes exactly ONE row to the final
    output, at its best score — the "chunk-level index, entity-level
    result" contract the firm's §5 calls out explicitly. Dropped fields
    (``score`` on the OUTPUT) stay internal — the public §5 shape is
    ``{kind, id, title, description}`` only (score is ranking-only, not
    part of the contract); the caller strips it after this call.
    """
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in raw_results:
        key = (row["kind"], row["id"])
        current = best.get(key)
        if current is None or row["score"] > current["score"]:
            best[key] = row
    return sorted(best.values(), key=lambda row: row["score"], reverse=True)


# ── read-only query adapter (emit_wrapped_semantic_search's ``index`` arg) ──


class _KnowledgeSourceIndex:
    """Minimal read-only index adapter for ONE knowledge source, shaped to
    satisfy ``coordinator.emit_wrapped_semantic_search``'s ``index.query(text,
    ctx, model_class, top_k=...)`` contract — the same shape
    ``ActionEmbeddingIndex.query`` provides for the action-catalog call
    site, but with no build/readiness bookkeeping of its own: ingest
    (build) is owned entirely by the P3a/b producers
    (``knowledge_ingest.py``) via the ``IndexCoordinator``; this adapter
    only ever READS the backend that producer already wrote to.
    """

    def __init__(self, source_id: str, kind: str, backend: "IndexBackend") -> None:
        self._source_id = source_id
        self._kind = kind
        self._backend = backend

    async def query(
        self, query_text: str, ctx: "OpContext", model_class: str, top_k: int = 10,
    ) -> list[dict[str, Any]]:
        if not query_text or not query_text.strip() or top_k <= 0:
            return []
        from reyn.core.op_runtime import execute_op
        from reyn.schemas.models import EmbedIROp

        result = await execute_op(
            EmbedIROp(kind="embed", texts=[query_text], embedding_model=model_class), ctx,
        )
        if result.get("status") == "error":
            return []
        vectors = list(result.get("vectors", []))
        if not vectors:
            return []
        records = await self._backend.query(self._source_id, vectors[0], top_k, filters={})
        return [_chunk_to_raw_result(self._kind, rec) for rec in records]


# ── ToolDefinition ──────────────────────────────────────────────────────────

_SEARCH_KNOWLEDGE_DESCRIPTION = _discovery_descriptions.search_knowledge.text

_SEARCH_KNOWLEDGE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": _discovery_descriptions.PARAMS["search_knowledge"]["query"].text,
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "default": 10,
            "description": _discovery_descriptions.PARAMS["search_knowledge"]["limit"].text,
        },
    },
    "required": ["query"],
}


async def _handle_search_knowledge(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """search_knowledge handler — semantic search across the 4 knowledge
    sources, merged + entity-aggregated (§G1).

    Graceful degradation (mirrors ``_handle_search_actions``): missing
    ``query`` -> §D12-style error envelope; ``router_state``/embedding/
    workspace/op-context absent -> empty result, never a crash.
    """
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {
            "error": "missing required argument 'query'",
            "reason": (
                "search_knowledge requires a non-empty string `query` "
                "describing the knowledge you're looking for."
            ),
            "hint": (
                "Call search_knowledge(query='...') with a natural-language "
                "description of the skill / memory / repo content you need."
            ),
        }

    rs = ctx.router_state
    if rs is None:
        return {"items": [], "total": 0}

    provider = rs.embedding_provider
    model_class = rs.embedding_model_class
    if provider is None or not model_class:
        return {"items": [], "total": 0}

    op_ctx_factory = rs.op_context_factory
    if op_ctx_factory is None:
        return {"items": [], "total": 0}
    op_ctx = op_ctx_factory()

    if ctx.workspace is None:
        return {"items": [], "total": 0}
    workspace_root = ctx.workspace.base_dir

    limit = args.get("limit", 10)
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 10

    from reyn.data.index import get_backend
    from reyn.data.index.coordinator import (
        emit_wrapped_semantic_search,
        get_index_coordinator,
    )

    backend = get_backend("sqlite", workspace_root=workspace_root)
    coordinator = get_index_coordinator(workspace_root)
    events = ctx.events

    raw_results: list[dict[str, Any]] = []
    for source_id, kind in _KNOWLEDGE_SOURCES:
        index = _KnowledgeSourceIndex(source_id, kind, backend)
        chunk_hits = await emit_wrapped_semantic_search(
            events=events,
            coordinator=coordinator,
            source_id=source_id,
            index=index,
            query=query,
            op_ctx=op_ctx,
            model_class=model_class,
            top_k=limit,
        )
        raw_results.extend(chunk_hits)

    entities = _aggregate_entities(raw_results)[:limit]
    items = [
        {"kind": e["kind"], "id": e["id"], "title": e["title"], "description": e["description"]}
        for e in entities
    ]
    return {"items": items, "total": len(items)}


SEARCH_KNOWLEDGE = ToolDefinition(
    canonical=search_knowledge_to_canonical,
    name="search_knowledge",
    router_dispatched=True,
    description=_SEARCH_KNOWLEDGE_DESCRIPTION,
    parameters=_SEARCH_KNOWLEDGE_PARAMETERS,
    gates=ToolGates(router="allow"),
    handler=_handle_search_knowledge,
    category="discovery",
    purity="read_only",
    # FP-0066 P3c (#3247 firm §2): discovery role, re-surfaces operator/user-
    # authored skill/memory/repo text without activating it — same role
    # class as skill_list (_EXTERNAL), the symmetric OPPOSITE of load_skill
    # (_NOT_EXTERNAL, activation). See tests/test_returns_external_content_
    # flagset_1822.py.
    returns_external_content=True,
)
