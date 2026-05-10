"""index_write op handler — write chunks to IndexBackend (ADR-0033 Phase 1).

Two input forms:
  Form A (inline): op.chunks is set — iterate directly.
  Form B (artifact reference): op.input_artifact is set — stream from JSONL.

After write, upserts the SourceManifest entry so per-turn system prompt
rebuild reflects the latest chunk count and model.

No permission gate: index_write is a workspace-internal write (P5 —
.reyn/ default zone), and ADR-0033 does not require a prompt for writes.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Iterable, Iterator, Literal

from reyn.index import SqliteIndexBackend
from reyn.index.backend import ChunkRecord
from reyn.index.source_manifest import SourceEntry, get_source_manifest
from reyn.schemas.models import IndexWriteIROp

from . import register
from .context import OpContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stream_jsonl(path: Path) -> Iterator[ChunkRecord]:
    """Yield ChunkRecord dicts from a JSONL file (Form B)."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                yield ChunkRecord(
                    text=rec.get("text", ""),
                    vector=rec.get("vector", []),
                    metadata=rec.get("metadata", {}),
                    score=None,
                )
            except (json.JSONDecodeError, KeyError):
                continue


def _chunks_to_records(chunks: list[dict]) -> Generator[ChunkRecord, None, None]:
    """Yield ChunkRecords from inline chunk dicts (Form A)."""
    for c in chunks:
        yield ChunkRecord(
            text=c.get("text", ""),
            vector=c.get("vector", []),
            metadata=c.get("metadata", {}),
            score=None,
        )


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

async def handle(
    op: IndexWriteIROp,
    ctx: OpContext,
    caller: Literal["preprocessor", "control_ir"],
) -> dict:
    """Execute an index_write op (ADR-0033 §2.1).

    Returns:
      {written: int, skipped: int}
    """
    # Input validation
    if op.chunks is not None and op.input_artifact is not None:
        raise ValueError("IndexWriteIROp: only one of chunks / input_artifact may be set")
    if op.chunks is None and op.input_artifact is None:
        raise ValueError("IndexWriteIROp: one of chunks / input_artifact must be set")

    workspace_root = ctx.workspace.base_dir
    backend = SqliteIndexBackend(workspace_root=workspace_root)

    # Build the record stream
    if op.chunks is not None:
        records: Iterable[ChunkRecord] = list(_chunks_to_records(op.chunks))
    else:
        input_path = workspace_root / op.input_artifact  # type: ignore[operator]
        if not input_path.exists():
            raise FileNotFoundError(
                f"index_write op: input_artifact not found: {op.input_artifact!r}"
            )
        records = _stream_jsonl(input_path)

    result = await backend.write(op.source, records, mode=op.mode)

    # Update SourceManifest (= per-turn system prompt rebuild reflects latest state)
    manifest = get_source_manifest(workspace_root)
    stat = await backend.stat(op.source)

    # SourceManifest description / path resolution (B21-S0-1 fix):
    # - Prefer caller-provided op.description / op.path when present (= the
    #   index_docs skill's first write of a new source carries these from
    #   the user's input artifact)
    # - Fall back to the existing entry's value on subsequent writes (= an
    #   incremental append should not clobber the original description)
    # - Only use the placeholder when both caller and existing entry are
    #   silent. Without this fix the router system prompt's "Indexed
    #   sources" section displayed only the placeholder, which deprived
    #   the LLM of any signal to pick recall over file_read.
    existing = await manifest.get(op.source)
    if op.description is not None and op.description != "":
        description = op.description
    elif existing is not None:
        description = existing.description
    else:
        description = f"Index of source '{op.source}'"

    if op.path is not None and op.path != "":
        path = op.path
    elif existing is not None:
        path = existing.path
    else:
        path = "(unknown)"

    entry = SourceEntry(
        name=op.source,
        description=description,
        path=path,
        backend="sqlite",
        last_indexed=datetime.now(timezone.utc).isoformat(),
        chunk_count=stat["chunk_count"],
        embedding_model=stat["embedding_model"],
    )
    await manifest.upsert(entry)

    return {"written": result["written"], "skipped": result["skipped"]}


register("index_write", handle)
