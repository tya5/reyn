"""recall macro op handler — embed query → iterate index_query → merge top-K.

Dispatches sub-ops (embed + index_query) via the OS execute_op path so
events are emitted per sub-op (P6 audit trail, same pattern as iterate.py
which also calls sub-op handlers via execute_op).

ADR-0033 §2.1: macro op, OpPurity.external.
"""
from __future__ import annotations

from typing import Literal

from reyn.schemas.models import EmbedIROp, IndexQueryIROp, RecallIROp

from . import execute_op, register
from .context import OpContext


async def handle(
    op: RecallIROp,
    ctx: OpContext,
    caller: Literal["preprocessor", "control_ir"],
) -> dict:
    """Execute a recall macro op (ADR-0033 §2.1).

    Steps:
      1. Embed the query text via embed sub-op (Form A inline).
      2. For each source, run index_query sub-op.
      3. Merge all top-K chunks globally by score, return top_k.

    Returns:
      {chunks: list[ChunkRecord], mode: "semantic" | "fallback" | "mixed"}
    """
    if not op.sources:
        return {"chunks": [], "mode": "fallback"}

    # ── 1. Embed query ────────────────────────────────────────────────────────
    embed_op = EmbedIROp(
        kind="embed",
        texts=[op.query],
        model=op.embedding_model,
    )
    embed_result = await execute_op(embed_op, ctx, caller=caller)

    if embed_result.get("status") in ("error", "denied", "skipped"):
        # Embed failed — fallback to empty result
        ctx.events.emit(
            "recall_embed_failed",
            query=op.query,
            error=embed_result.get("error", "unknown"),
        )
        return {"chunks": [], "mode": "fallback"}

    vectors = embed_result.get("vectors", [])
    if not vectors:
        return {"chunks": [], "mode": "fallback"}

    query_vec: list[float] = vectors[0]

    # ── 2. Iterate sources via index_query ────────────────────────────────────
    per_source: list[dict] = []
    for source in op.sources:
        query_op = IndexQueryIROp(
            kind="index_query",
            source=source,
            query_vector=query_vec,
            top_k=op.top_k,
            filters=op.filters,
        )
        result = await execute_op(query_op, ctx, caller=caller)

        if result.get("status") in ("error", "denied", "skipped"):
            # Source query failed — treat as fallback for this source
            per_source.append({"chunks": [], "mode": "fallback"})
        else:
            per_source.append(result)

    # ── 3. Merge top-K globally by score ─────────────────────────────────────
    all_chunks: list[dict] = []
    semantic_count = 0
    fallback_count = 0

    for r in per_source:
        all_chunks.extend(r.get("chunks", []))
        if r.get("mode") == "semantic":
            semantic_count += 1
        else:
            fallback_count += 1

    # Sort by score descending; chunks without score treated as 0
    all_chunks.sort(key=lambda c: c.get("score") or 0.0, reverse=True)
    top_chunks = all_chunks[: op.top_k]

    # ── Strip raw vector field before returning to caller (B18-S5-1) ─────────
    # ChunkRecord carries a `vector: list[float]` field that backends use for
    # similarity ranking. Once top-K is selected, the vector is no longer
    # useful to downstream consumers (the LLM, postprocessors) but is large
    # (~6KB / 1536-dim float-as-string serialised JSON, ~40KB per call at
    # top_k=5). Leaving it in the returned envelope quietly inflates router
    # context on every recall invocation. Strip it here; backends keep the
    # vector internally for re-ranking but it never crosses the op boundary.
    stripped_chunks = [
        {k: v for k, v in c.items() if k != "vector"} for c in top_chunks
    ]

    # Determine overall mode
    n = len(per_source)
    if semantic_count == n:
        mode = "semantic"
    elif fallback_count == n:
        mode = "fallback"
    else:
        mode = "mixed"

    return {"chunks": stripped_chunks, "mode": mode}


register("recall", handle)
