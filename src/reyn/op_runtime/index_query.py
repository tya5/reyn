"""index_query op handler — semantic search over a single source (ADR-0033 Phase 1).

Inline-only I/O (top-K is small, ~30KB).

When query_vector is None: fallback enumerate (ADR-0033 §2.1 — returns empty
list for phase 1, mode="fallback").

UX gap fix E: SQLite read errors wrapped with actionable hint message.
"""
from __future__ import annotations

import sqlite3
from typing import Literal

from reyn.index import SqliteIndexBackend
from reyn.schemas.models import IndexQueryIROp

from . import register
from .context import OpContext

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class IndexCorruptionError(Exception):
    """Raised when SQLite read fails — UX gap fix E hint included in message."""


# ---------------------------------------------------------------------------
# Fallback enumerate
# ---------------------------------------------------------------------------

async def _fallback_enumerate(op: IndexQueryIROp, ctx: OpContext) -> dict:
    """Phase 1 fallback: return empty list with mode='fallback'.

    Phase 2 will read source file glob, return chunks up to fallback_size_cap tokens.
    """
    return {"chunks": [], "mode": "fallback"}


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

async def handle(
    op: IndexQueryIROp,
    ctx: OpContext,
    caller: Literal["preprocessor", "control_ir"],
) -> dict:
    """Execute an index_query op (ADR-0033 §2.1).

    Returns:
      {chunks: list[ChunkRecord], mode: "semantic" | "fallback"}
    """
    if op.query_vector is None:
        return await _fallback_enumerate(op, ctx)

    workspace_root = ctx.workspace.base_dir
    backend = SqliteIndexBackend(workspace_root=workspace_root)

    try:
        chunks = await backend.query(
            op.source,
            op.query_vector,
            op.top_k,
            op.filters,
        )
    except sqlite3.DatabaseError as exc:
        raise IndexCorruptionError(
            f"Source '{op.source}' index appears corrupted: {exc}. "
            f"Run: reyn source rm {op.source} && reyn run index_docs "
            f"--source {op.source} ... to re-index."
        ) from exc

    mode = "semantic" if chunks else "fallback"
    return {"chunks": [dict(c) for c in chunks], "mode": mode}


register("index_query", handle)
