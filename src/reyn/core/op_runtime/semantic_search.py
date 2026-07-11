"""semantic_search macro op handler — per-source-model embed query -> iterate
index_query -> merge top-K (FP-0057 Phase 2a; renamed from `recall` — clean
break, fixes the observed `recall`/`search_actions`/`memory` naming
collision).

**Multi-model correctness (co-vet #1, CRITICAL)**: `semantic_search` takes
MULTIPLE sources, and different sources may be indexed under DIFFERENT
embedding models. Embedding the query ONCE with a single model then querying
every source is a silent-mismatch bug — a source indexed under a different
model gets compared in the WRONG vector space, producing garbage/incorrect
ranking with no error signal.

The fix: each source's embedding model is AUTO-ADOPTED from its recorded
index (`SourceManifest.embedding_model`, falling back to the SQLite
backend's `stat().embedding_model`) — never caller-supplied per source.
Sources are grouped by DISTINCT resolved model; the query is embedded ONCE
per distinct model (not once per source, not once total), and each source is
queried with its matching model's vector.

**Merge strategy — never compare cross-model scores.** Cosine similarity
scores from different embedding spaces are not commensurable (a 0.8 in model
X's space and a 0.8 in model Y's space mean different things — they are not
even guaranteed to share a value range). So:
  - WITHIN a model group (one or more sources sharing the same resolved
    model), chunks are merged and sorted by score — safe, same space, exact
    mirror of the pre-fix single-model recall behaviour.
  - ACROSS groups (genuinely mixed-model multi-source calls), each group's
    already-ranked top-K is combined via an order-preserving ROUND-ROBIN
    interleave (group order = `op.sources` order of first appearance),
    capped at `op.top_k` total. Raw score magnitudes are NEVER compared
    across groups — only each group's own internal (same-space) ranking is
    used. This is chosen over rejecting mixed-model calls outright because
    a per-source top-K union stays useful (every source contributes its
    genuinely-best matches) without fabricating a cross-space ranking.

Single-source and same-model multi-source calls are BYTE-IDENTICAL in
behaviour to the pre-rename `recall` (exactly one model group, same merge).

**Query-embed goes through the shared `embed` op (redaction-egress seam).**
The per-distinct-model query embed is dispatched via `execute_op(EmbedIROp(
...))`, NOT provider-direct — so the query (which IS an egress to the
external embedding API) passes through the Phase 1 `embed` op's PRE-embed
`redact_secrets` scan + `embed_secret_redacted` audit-event, symmetric with
`index_update`'s ingestion embed (co-vet fix, architect ruling (a)): the
ingestion path and the query path share ONE redaction-gated embed mechanism,
no provider-direct bypass. Multi-model correctness (#1) is unchanged — the
embed op is still called once per distinct model; only the call mechanism
changed.

ADR-0033 §2.1: macro op.
"""
from __future__ import annotations

from reyn.data.index import SqliteIndexBackend
from reyn.data.index.source_manifest import get_source_manifest
from reyn.schemas.models import EmbedIROp, IndexQueryIROp, SemanticSearchIROp

from . import execute_op, register
from .context import OpContext


async def _resolve_source_model(
    source: str, workspace_root, fallback_model: str,
) -> str:
    """AUTO-ADOPT the embedding model for *source* (co-vet #1) — never
    caller-supplied. Preference order: `SourceManifest.embedding_model`
    (the durable per-source record) -> `SqliteIndexBackend.stat()`'s
    recorded model (populated straight from index writes even when no
    manifest entry exists, e.g. in tests that seed the backend directly) ->
    `fallback_model` (an empty/unindexed source, where `index_query` falls
    back to enumeration anyway — the model choice there is moot)."""
    manifest = get_source_manifest(workspace_root)
    entry = await manifest.get(source)
    if entry is not None and entry.embedding_model:
        return entry.embedding_model
    backend = SqliteIndexBackend(workspace_root=workspace_root)
    stat = await backend.stat(source)
    if stat.get("embedding_model"):
        return stat["embedding_model"]
    return fallback_model


async def handle(
    op: SemanticSearchIROp,
    ctx: OpContext,
) -> dict:
    """Execute a semantic_search macro op (ADR-0033 §2.1 / FP-0057 Phase 2a).

    Steps:
      1. Resolve each source's embedding model (auto-adopt, per co-vet #1).
      2. Group sources by distinct model; embed the query ONCE per group.
      3. For each source, run index_query with its group's query vector.
      4. Merge WITHIN each group by score; combine ACROSS groups by
         order-preserving round-robin (never comparing cross-model scores).

    Returns:
      {chunks: list[ChunkRecord], mode: "semantic" | "fallback" | "mixed"}
    """
    if not op.sources:
        return {"kind": "semantic_search", "chunks": [], "mode": "fallback"}

    workspace_root = ctx.workspace.base_dir if ctx.workspace is not None else None

    # ── 1. Resolve each source's model + group sources by distinct model ───
    source_models: dict[str, str] = {}
    for source in op.sources:
        if workspace_root is None:
            source_models[source] = op.embedding_model
        else:
            source_models[source] = await _resolve_source_model(
                source, workspace_root, op.embedding_model,
            )

    # Group order = first-appearance order in op.sources (deterministic,
    # order-preserving — used for the cross-group round-robin below).
    model_groups: dict[str, list[str]] = {}
    for source in op.sources:
        model_groups.setdefault(source_models[source], []).append(source)

    # ── 2. Embed the query ONCE per distinct model ──────────────────────────
    # Via the shared `embed` op (execute_op) — NOT provider-direct — so the
    # query, which IS an egress to the external embedding API, passes through
    # the Phase 1 embed op's PRE-embed redaction-egress seam (`redact_secrets`
    # + `embed_secret_redacted` audit-event). Symmetric with index_update's
    # ingestion embed (co-vet fix, architect ruling (a)): both the ingestion
    # path and the query path share ONE redaction-gated embed mechanism, no
    # provider-direct bypass. Multi-model correctness (#1) is UNCHANGED — the
    # embed op is still called once per DISTINCT model, each source queried
    # with its matching model's vector; only the call mechanism changed from
    # provider-direct to execute_op(EmbedIROp).
    query_vectors: dict[str, list[float]] = {}
    for model in model_groups:
        embed_result = await execute_op(
            EmbedIROp(kind="embed", texts=[op.query], embedding_model=model), ctx,
        )
        if embed_result.get("status") == "error":
            ctx.events.emit(
                "semantic_search_embed_failed",
                query=op.query,
                model=model,
                error=embed_result.get("error", ""),
            )
            continue
        vectors = embed_result.get("vectors", [])
        if vectors:
            query_vectors[model] = vectors[0]

    if not query_vectors:
        return {"kind": "semantic_search", "chunks": [], "mode": "fallback"}

    # ── 3. Iterate sources via index_query, WITHIN their model's vector ────
    per_source: list[dict] = []
    for source in op.sources:
        model = source_models[source]
        query_vec = query_vectors.get(model)
        query_op = IndexQueryIROp(
            kind="index_query",
            source=source,
            query_vector=query_vec,
            top_k=op.top_k,
            filters=op.filters,
        )
        result = await execute_op(query_op, ctx)

        if result.get("status") in ("error", "denied", "skipped"):
            per_source.append({"chunks": [], "mode": "fallback", "_model": model})
        else:
            result = dict(result)
            result["_model"] = model
            per_source.append(result)

    # ── 4. Merge WITHIN each model group by score; combine ACROSS groups by
    #        round-robin (never compare cross-model score magnitudes) ───────
    per_model_ranked: dict[str, list[dict]] = {}
    semantic_count = 0
    fallback_count = 0
    for r in per_source:
        model = r["_model"]
        chunks = r.get("chunks", [])
        per_model_ranked.setdefault(model, []).extend(chunks)
        if r.get("mode") == "semantic":
            semantic_count += 1
        else:
            fallback_count += 1

    for model, chunks in per_model_ranked.items():
        chunks.sort(key=lambda c: c.get("score") or 0.0, reverse=True)
        per_model_ranked[model] = chunks[: op.top_k]

    if len(per_model_ranked) <= 1:
        # Single model group (the common case, and the exact pre-rename
        # `recall` behaviour): the "combine" step is a no-op — just the one
        # group's already-capped top_k.
        top_chunks = next(iter(per_model_ranked.values()), [])
    else:
        # Multiple distinct models: round-robin interleave the groups'
        # per-model top-K lists (group order = model_groups insertion
        # order = first-appearance order in op.sources) — order-preserving,
        # NEVER a cross-model score comparison.
        ordered_groups = [per_model_ranked[m] for m in model_groups if m in per_model_ranked]
        top_chunks = []
        idx = 0
        while len(top_chunks) < op.top_k and any(idx < len(g) for g in ordered_groups):
            for g in ordered_groups:
                if idx < len(g):
                    top_chunks.append(g[idx])
                    if len(top_chunks) >= op.top_k:
                        break
            idx += 1

    # ── Strip raw vector field before returning to caller (B18-S5-1) ─────────
    stripped_chunks = [
        {k: v for k, v in c.items() if k != "vector"} for c in top_chunks
    ]

    n = len(per_source)
    if semantic_count == n:
        mode = "semantic"
    elif fallback_count == n:
        mode = "fallback"
    else:
        mode = "mixed"

    # #2425 案B: ``kind`` drives the canonical mapper (chunks → structured attachment).
    return {"kind": "semantic_search", "chunks": stripped_chunks, "mode": mode}


from reyn.core.offload.canonical import chunks_to_canonical  # noqa: E402

register("semantic_search", handle, canonical=chunks_to_canonical)
