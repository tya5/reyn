"""index_query op handler — semantic search over a single source (ADR-0033 Phase 1).

Inline-only I/O (top-K is small, ~30KB).

When query_vector is None: fallback enumerate (ADR-0033 §2.1 — returns empty
list for phase 1, mode="fallback").

UX gap fix E: SQLite read errors wrapped with actionable hint message.
"""
from __future__ import annotations

import sqlite3
from typing import Literal

from reyn.data.index import SqliteIndexBackend
from reyn.schemas.models import IndexQueryIROp

from . import register
from .context import OpContext, sandbox_policy_from_ctx

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

    # B48-NF-W2-S7 fix (2026-05-22): ctx.workspace may be None when the
    # caller (= recall tool or similar router-side path) propagates a
    # workspace-less ToolContext. Raise a clear ValueError instead of the
    # opaque ``AttributeError: 'NoneType' object has no attribute 'base_dir'``
    # so the failure is actionable to the LLM and to operators reading the
    # control_ir_failed event. Observed B48 W2-S7 (= chained_find_then_index)
    # 4x consecutive failures with the AttributeError noise.
    if ctx.workspace is None:
        raise ValueError(
            "index_query: op_runtime context has no workspace. Index ops "
            "require a workspace to locate the SQLite backend; pass an "
            "OpContext with a populated `workspace` field. This typically "
            "indicates the calling tool (e.g. recall, drop_source) was "
            "invoked from a router-side path without a workspace."
        )

    workspace_root = ctx.workspace.base_dir

    # #1199 S3.4 Part1: route the index FS-op through the permission gate (the
    # SQLite I/O itself stays host-direct — random-access/lock can't go on the
    # read_file abstraction — so we gate the DB path BEFORE the backend opens
    # it, with the phase sandbox_policy ∩, same shape as S3.1c-2). Closes the
    # hole where a sandbox read_paths cap could not constrain index reads.
    if ctx.permission_resolver is not None:
        db_path = workspace_root / ".reyn" / "index" / op.source / "index.db"
        await ctx.permission_resolver.require_file_read(
            ctx.permission_decl, str(db_path), ctx.skill_name,
            sandbox_policy=sandbox_policy_from_ctx(ctx),
        )

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
