"""index_update op handler — incremental/delta-reconcile ingestion (FP-0057
Phase 2a).

NO full-rebuild mode. A from-scratch rebuild is ``index_drop`` ->
``index_update`` on an empty source. Reconciles the caller-supplied
``chunks`` against the source's CURRENT index (``existing_hashes_by_path``,
content-addressed by ``content_hash`` within each ``source_path``):

  - **add**    — new ``content_hash``, new ``source_path`` -> embed + insert.
  - **update**  — new ``content_hash``, ``source_path`` already indexed
                  (content changed) -> embed + insert; the path's stale
                  hash(es) are removed in the same pass.
  - **remove**  — an indexed hash whose ``source_path`` IS among this call's
                  chunks but whose hash is NOT -> deleted. Scoped to the
                  ``source_path``s THIS call supplies chunks for (a path
                  never mentioned is left untouched).
  - **skip**    — ``content_hash`` already indexed -> no-op (no re-embed).

Reuses the Phase 1 ``embed`` op for the actual embedding call (dispatched via
``execute_op`` — same primitive, no duplicated embed logic) and the existing
``SqliteIndexBackend`` / ``SourceManifest`` (ADR-0033 Phase 1 / FP-0057 Phase
0) for storage — the resume-key pattern (dedup BEFORE embedding = the cost
save) mirrors the retired ``reyn.api.safe.embed_index.embed_and_index``
(FP-0057 Phase 2b clean-break: safe-mode python steps now call
``reyn.api.safe.index_update`` instead, a thin dispatch onto THIS op), plus
the update/remove reconciliation legs the old append-only safe-mode entry
never had.

**Source-model-bound**: the source's embedding model is recorded on first
ingestion (``SourceManifest.embedding_model`` / the SQLite backend's
``stat().embedding_model``) and reused on every subsequent call for that
source — a source is one embedding space; ``op.embedding_model`` is a
fallback used only when the source has no recorded model yet.

**Cost (co-vet #4)**: ``EmbeddingProvider.estimate_tokens`` is consulted on
the to-embed batch (add+update chunks, AFTER the pre-embed dedup skip) and
compared against ``embedding.cost_warn_threshold`` (``reyn.yaml``). Exceeding
it does not block the op (index_update is not a destructive/ask-gated op) —
it emits an ``index_update_cost_warning`` audit-event (P6) and the returned
envelope carries a ``cost_warning`` field, so a large ingestion surfaces its
cost instead of embedding silently. This closes the previously-dead-code gap
where ``cost_warn_threshold`` was parsed from config but no caller ever read
it.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from reyn.data.embedding import get_provider
from reyn.data.index import SqliteIndexBackend
from reyn.data.index.backend import (
    ChunkRecord,
    cache_dir_for_source,
    sources_manifest_path,
)
from reyn.data.index.source_manifest import SourceEntry, get_source_manifest
from reyn.schemas.models import EmbedIROp, IndexUpdateIROp

from . import execute_op, register
from .context import OpContext, sandbox_policy_from_ctx


def _resolve_embedding_config() -> dict:
    """Mirrors embed/semantic_search's config resolution (env override + reyn.yaml)."""
    try:
        from reyn.config import load_config
        return load_config().embedding or {}
    except Exception:
        return {}


async def handle(op: IndexUpdateIROp, ctx: OpContext) -> dict:
    """Execute an index_update op (FP-0057 Phase 2a). See module docstring
    for the add/update/remove/skip reconciliation contract.

    Returns: ``{"kind": "index_update", "source": str, "added": int,
    "updated": int, "removed": int, "skipped": int, "chunk_count": int,
    "embedding_model": str, "cost_warning": dict | None}``.
    """
    if ctx.workspace is None:
        raise ValueError(
            "index_update: op_runtime context has no workspace. Index ops "
            "require a workspace to locate the SQLite backend; pass an "
            "OpContext with a populated `workspace` field."
        )

    workspace_root = ctx.workspace.base_dir

    # #2856 Part B: resolve the sandbox cap ONCE, unconditionally (not just
    # inside the `permission_resolver is not None` branch below) — it is
    # forwarded into the backend/manifest regardless of whether a
    # permission_resolver is present, so the safe-mode path (resolver=None,
    # e.g. `reyn.api.safe.index_update`) gets the SAME real-write-site
    # self-gate as the LLM-tool path, closing the asymmetry the wrapper's
    # retired pre-flight used to paper over.
    sandbox_policy = sandbox_policy_from_ctx(ctx)
    sandbox_write_paths = sandbox_policy.write_paths if sandbox_policy is not None else None

    # Permission gate — own-write (not destructive), same shape as
    # index_query's read gate / index_drop's write gate: declares
    # file.write authority over this source's own index + the sources.yaml
    # manifest, so a sandbox write_paths cap constrains it. Default-ALLOW
    # posture (no ask-gate) comes from the ToolGates on the `index_update`
    # ToolDefinition, not from this call.
    if ctx.permission_resolver is not None:
        # Derive the DB path via the SAME `cache_dir_for_source` helper
        # `SqliteIndexBackend._db_path` uses for the actual write, so the gate
        # checks exactly the path the backend writes (guaranteed-equal by
        # construction — not two hand-agreeing hardcoded formulas).
        db_path = cache_dir_for_source(workspace_root, op.source) / "index.db"
        await ctx.permission_resolver.require_file_write(
            ctx.permission_decl, str(db_path), ctx.actor,
            sandbox_policy=sandbox_policy,
        )
        sources_yaml = sources_manifest_path(workspace_root)
        await ctx.permission_resolver.require_file_write(
            ctx.permission_decl, str(sources_yaml), ctx.actor,
            sandbox_policy=sandbox_policy,
        )

    # #2856 Part B: forward the cap into the backend — its own self-gate on
    # `db_file` (sqlite.py `write`/`delete`) now fires at the REAL write site
    # for every caller/surface, not just the LLM-tool path's pre-embed
    # require_file_write above.
    backend = SqliteIndexBackend(
        workspace_root=workspace_root, sandbox_write_paths=sandbox_write_paths,
    )
    manifest = get_source_manifest(workspace_root)

    # ── Source-model-bound: resolve the model to embed with ────────────────
    existing_entry = await manifest.get(op.source)
    existing_stat = await backend.stat(op.source)
    model = (
        (existing_entry.embedding_model if existing_entry else None)
        or existing_stat.get("embedding_model")
        or op.embedding_model
    )

    # ── Reconcile against the current index (content-addressed, per path) ──
    existing_by_path = await backend.existing_hashes_by_path(op.source)
    existing_hashes_flat: set[str] = set().union(*existing_by_path.values()) if existing_by_path else set()

    incoming_paths: set[str] = set()
    incoming_hashes: set[str] = set()
    to_embed: list[dict] = []
    skipped = 0

    for chunk in op.chunks:
        meta = dict(chunk.get("metadata") or {})
        content_hash = meta.get("content_hash", "")
        source_path = meta.get("source_path", "")
        if source_path:
            incoming_paths.add(source_path)
        if content_hash:
            incoming_hashes.add(content_hash)
        if content_hash and content_hash in existing_hashes_flat:
            skipped += 1
            continue
        to_embed.append(chunk)

    added = sum(
        1 for c in to_embed
        if (c.get("metadata") or {}).get("source_path", "") not in existing_by_path
    )
    updated = len(to_embed) - added

    # ── Cost surfacing (co-vet #4) — BEFORE embedding, on the actual
    #    to-embed batch (post pre-embed-dedup skip) ─────────────────────────
    cost_warning: dict | None = None
    provider = get_provider(
        os.environ.get("REYN_EMBEDDING_PROVIDER", "litellm"),
        config=_resolve_embedding_config(),
    )
    if to_embed:
        texts_for_estimate = [c.get("text", "") for c in to_embed]
        try:
            estimated_tokens = provider.estimate_tokens(texts_for_estimate)
        except Exception:
            estimated_tokens = 0
        try:
            from reyn.config import load_config
            threshold = load_config().embedding.cost_warn_threshold
        except Exception:
            threshold = 10000
        if len(to_embed) > threshold:
            cost_warning = {
                "chunk_count": len(to_embed),
                "estimated_tokens": estimated_tokens,
                "threshold": threshold,
            }
            ctx.events.emit(
                "index_update_cost_warning",
                source=op.source,
                chunk_count=len(to_embed),
                estimated_tokens=estimated_tokens,
                threshold=threshold,
            )

    # ── Embed (via the shared `embed` op — no duplicated embed logic) ──────
    if to_embed:
        embed_op = EmbedIROp(kind="embed", texts=[c.get("text", "") for c in to_embed], embedding_model=model)
        embed_result = await execute_op(embed_op, ctx)
        if embed_result.get("status") == "error":
            return embed_result
        vectors = embed_result.get("vectors", [])
        if len(vectors) != len(to_embed):
            raise RuntimeError(
                f"embed op returned {len(vectors)} vectors for {len(to_embed)} "
                f"chunks; refusing partial index_update write"
            )
        records: list[ChunkRecord] = []
        resolved_model = embed_result.get("model", model)
        for chunk, vector in zip(to_embed, vectors):
            meta = dict(chunk.get("metadata") or {})
            meta["embedding_model"] = resolved_model
            records.append(ChunkRecord(
                text=chunk.get("text", ""),
                vector=list(vector),
                metadata=meta,
                score=None,
            ))
        await backend.write(op.source, records, "append")
        model = resolved_model

    # ── Remove: existing hashes whose path is re-supplied but whose hash
    #    is no longer present in this call's chunks ─────────────────────────
    stale_hashes: list[str] = []
    for source_path in incoming_paths:
        for h in existing_by_path.get(source_path, set()):
            if h not in incoming_hashes:
                stale_hashes.append(h)
    removed = 0
    if stale_hashes:
        removed = await backend.delete(op.source, stale_hashes)

    # ── Refresh the SourceManifest entry ────────────────────────────────────
    stat = await backend.stat(op.source)
    if op.description is not None and op.description != "":
        desc = op.description
    elif existing_entry is not None:
        desc = existing_entry.description
    else:
        desc = f"Index of source {op.source!r}"
    if op.path is not None and op.path != "":
        pth = op.path
    elif existing_entry is not None:
        pth = existing_entry.path
    else:
        pth = "(unknown)"
    await manifest.upsert(
        SourceEntry(
            name=op.source,
            description=desc,
            path=pth,
            backend="sqlite",
            last_indexed=datetime.now(timezone.utc).isoformat(),
            chunk_count=stat["chunk_count"],
            embedding_model=stat["embedding_model"] or model,
        ),
        sandbox_write_paths=sandbox_write_paths,
    )

    # P6 audit trail
    ctx.events.emit(
        "index_updated",
        source=op.source,
        added=added,
        updated=updated,
        removed=removed,
        skipped=skipped,
    )

    return {
        "kind": "index_update",
        "source": op.source,
        "added": added,
        "updated": updated,
        "removed": removed,
        "skipped": skipped,
        "chunk_count": stat["chunk_count"],
        "embedding_model": stat["embedding_model"] or model,
        "cost_warning": cost_warning,
    }


from reyn.core.offload.canonical import index_update_to_canonical  # noqa: E402

register("index_update", handle, canonical=index_update_to_canonical)
