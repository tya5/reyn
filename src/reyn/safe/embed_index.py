"""Safe-mode provider-direct embed+index for stdlib RAG skills (#1303 Stage I).

Folds the old ``embed`` + ``index_write`` run-op pair into a single
``reyn.safe.*`` surface a safe-mode python step (the swappable chunker) can
import through the AST allowlist. The chunker streams its chunks straight into
:func:`embed_and_index` — no intermediate ``<cwd>/artifacts/*.jsonl`` file —
which embeds them provider-direct and writes the vectors to the per-source
SQLite index. This is the doc-index analogue of the action search index
(``ActionEmbeddingIndex``), which has always called ``provider.embed`` directly.

Design (ADR-0033 op-separation reasons preserved internally)
-----------------------------------------------------------
- **resume = DB-as-checkpoint**: :meth:`SqliteIndexBackend.existing_hashes`
  is consulted *before* embedding, so a re-run / crash-resume skips
  already-indexed ``content_hash`` values without paying to re-embed them
  (the cost save; ``INSERT OR IGNORE`` alone dedups only *after* the vector
  is computed). On crash only the in-flight batch is re-embedded.
- **streaming**: chunks are consumed from an iterator and flushed per embed
  batch, so a bulk index holds only one batch of vectors in memory.
- **cost**: cost control stays Phase-1 ``cost_preflight`` (the LLM decides
  before the postprocessor runs) — byte-identical to today's behaviour. A
  dedicated ``permissions.embed`` capability axis is intentionally *not*
  introduced here (it does not exist today); it is a separate owner-gated
  feature (#1303 Q1).
- **SourceManifest upsert**: the per-source manifest entry is refreshed after
  the write so the per-turn router system prompt reflects the latest chunk
  count / model (mirrors the old ``index_write`` op).

Permission / config context
---------------------------
Mirrors :mod:`reyn.safe.file` / :mod:`reyn.safe.http`: the parent wires the
workspace root + embedding config via :func:`_set_context` before the user
step runs (the python harness does this). Writes land only under
``<workspace_root>/.reyn/index/`` (the default write zone), so the safe-mode
step needs no out-of-zone declaration.

Internal layering
-----------------
This module is reyn-package internal code (= not subject to the safe-mode AST
validator). It freely imports the embedding provider / index backend; the
validator only rejects *user-code* imports outside the allowlist, and
``reyn.safe.*`` is admitted.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from reyn.embedding import get_provider
from reyn.index import SqliteIndexBackend
from reyn.index.backend import ChunkRecord
from reyn.index.source_manifest import SourceEntry, get_source_manifest

# ── Internal state ─────────────────────────────────────────────────────────
#
# Set once at python-harness start-up via :func:`_set_context` (mirrors
# ``reyn.safe.file`` / ``reyn.safe.http``'s module-globals contract).

_workspace_root: Path | None = None
_embedding_config: dict = {}
_provider_name: str = "litellm"
_context_initialised: bool = False


def _set_context(
    *,
    workspace_root: str | Path,
    embedding_config: dict | None = None,
    provider_name: str = "litellm",
) -> None:
    """Wire the workspace root + embedding config into this module.

    Called by the python harness before the user step runs. Tests that
    exercise the API directly call this to establish a controlled context.
    Idempotent — overwrites the previous context.
    """
    global _workspace_root, _embedding_config, _provider_name, _context_initialised
    _workspace_root = Path(workspace_root)
    _embedding_config = dict(embedding_config or {})
    _provider_name = provider_name
    _context_initialised = True


def _require_context() -> Path:
    if not _context_initialised or _workspace_root is None:
        raise RuntimeError(
            "reyn.safe.embed_index: context not initialised. The parent "
            "process must call _set_context(workspace_root=...) before the "
            "safe-mode step runs (the python harness wires this)."
        )
    return _workspace_root


# ── Core ───────────────────────────────────────────────────────────────────


async def embed_and_index_async(
    chunks: Iterable[dict],
    source: str,
    model: str,
    mode: str = "append",
    *,
    description: str | None = None,
    path: str | None = None,
    text_field: str = "text",
    batch_size: int = 100,
) -> dict:
    """Embed ``chunks`` provider-direct and write the vectors to the source index.

    ``chunks`` is any iterator of chunk dicts (``{text, metadata: {content_hash,
    ...}}``) — the chunker yields these. Streams per embed batch; skips chunks
    whose ``content_hash`` is already indexed (append) before embedding.

    Returns ``{embedded, skipped_embed, written, skipped_write}``.
    """
    workspace_root = _require_context()
    provider = get_provider(_provider_name, config=_embedding_config)
    backend = SqliteIndexBackend(workspace_root=workspace_root)

    # Resume key: hashes already indexed (skip BEFORE embedding = cost save).
    # Append resumes against the existing DB; replace rebuilds from scratch, so
    # clear once up-front and let the per-batch writes below all append (a
    # single replace-write can't interleave the async embed inside its sync
    # transaction while preserving streaming).
    seen: set[str] = set()
    if mode == "append":
        seen = await backend.existing_hashes(source)
    elif mode == "replace":
        await backend.write(source, [], "replace")

    embedded = 0
    skipped_embed = 0
    written = 0
    skipped_write = 0

    async def _flush(batch: list[dict]) -> None:
        nonlocal embedded, written, skipped_write
        if not batch:
            return
        texts = [c.get(text_field, "") for c in batch]
        result = await provider.embed(texts, model)
        vectors = result["vectors"]
        if len(vectors) != len(batch):
            # Refuse a partial result rather than write a corrupt half-batch
            # (mirrors ActionEmbeddingIndex). The DB checkpoint means a retry
            # re-embeds only this batch.
            raise RuntimeError(
                f"embed provider returned {len(vectors)} vectors for "
                f"{len(batch)} chunks; refusing partial write"
            )
        records: list[ChunkRecord] = []
        for chunk, vector in zip(batch, vectors):
            meta = dict(chunk.get("metadata", {}))
            meta["embedding_model"] = result["model"]
            records.append(
                ChunkRecord(
                    text=chunk.get(text_field, ""),
                    vector=list(vector),
                    metadata=meta,
                    score=None,
                )
            )
        wr = await backend.write(source, records, "append")
        written += wr["written"]
        skipped_write += wr["skipped"]
        embedded += len(batch)

    batch: list[dict] = []
    for chunk in chunks:
        chash = (chunk.get("metadata") or {}).get("content_hash", "")
        if chash and chash in seen:
            skipped_embed += 1
            continue
        if chash:
            seen.add(chash)  # also dedups repeats within this run
        batch.append(chunk)
        if len(batch) >= batch_size:
            await _flush(batch)
            batch = []
    await _flush(batch)

    # Refresh the SourceManifest entry (per-turn system-prompt rebuild reflects
    # latest chunk count / model). Mirrors the old index_write op's resolution:
    # caller-provided description/path win, else keep the existing entry's, else
    # a placeholder.
    manifest = get_source_manifest(workspace_root)
    stat = await backend.stat(source)
    existing_entry = await manifest.get(source)
    if description is not None and description != "":
        desc = description
    elif existing_entry is not None:
        desc = existing_entry.description
    else:
        desc = f"Index of source {source!r}"
    if path is not None and path != "":
        pth = path
    elif existing_entry is not None:
        pth = existing_entry.path
    else:
        pth = "(unknown)"
    await manifest.upsert(
        SourceEntry(
            name=source,
            description=desc,
            path=pth,
            backend="sqlite",
            last_indexed=datetime.now(timezone.utc).isoformat(),
            chunk_count=stat["chunk_count"],
            embedding_model=stat["embedding_model"],
        )
    )

    return {
        "embedded": embedded,
        "skipped_embed": skipped_embed,
        "written": written,
        "skipped_write": skipped_write,
    }


def embed_and_index(
    chunks: Iterable[dict],
    source: str,
    model: str,
    mode: str = "append",
    *,
    description: str | None = None,
    path: str | None = None,
    text_field: str = "text",
    batch_size: int = 100,
) -> dict:
    """Synchronous entry point for safe-mode python steps.

    The chunker runs synchronously in the harness subprocess (no running event
    loop), so wrap the async core via :func:`asyncio.run` (#1303 Q4). Returns
    the same envelope as :func:`embed_and_index_async`.
    """
    return asyncio.run(
        embed_and_index_async(
            chunks,
            source,
            model,
            mode,
            description=description,
            path=path,
            text_field=text_field,
            batch_size=batch_size,
        )
    )


__all__ = ["embed_and_index", "embed_and_index_async", "_set_context"]
