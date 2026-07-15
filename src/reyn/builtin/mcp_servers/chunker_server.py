"""Builtin chunking MCP server -- wraps ``chonkie`` (FP-0063 P2).

P1 (the OSS selection spike) confirmed no chunking-ONLY MCP server exists
-- every OSS candidate bundles chunking into a specific vector store,
which would violate FP-0057 C2 (the user store is EXTERNAL / reyn hosts
none of it). This thin wrapper around ``chonkie`` (MIT, ~15 MiB, 10
chunker types vs LangChain's ~80 MiB) is therefore still "builtin
content", not a reyn-core dependency: it ships as an independent MCP
server the ingest pipeline (P3) calls, exactly like any other bundled
server.

**No network / no bundled model.** ``chonkie``'s default tokenizer used
here is character-based (chonkie's own default), so this module never
downloads anything at import or call time -- it carries none of the
HuggingFace-fetch hazard FP-0057 line 55 recorded for the embedding
model. (A user who opts into a HF tokenizer via chonkie's own tokenizer
param would re-acquire that hazard themselves -- not this module's
default path.)

**``size`` and ``overlap`` are tool parameters, not constants** (proposal
R4): the 2026 default for persistent, quality-sensitive RAG is recursive
chunking at 256-512 tokens with 10-15% overlap -- deliberately NOT
FP-0057 line 51's 800-1024 figure, which that doc explicitly scopes to
"throwaway ephemeral" attachments, a different case. Defaults land at the
midpoint (chunk_size=400, overlap_ratio=0.125) but every call can
override both, because the builtin is a template people copy and tune --
the first thing a user wants to change must not be the hardest to find.

Overlap is implemented via chonkie's ``OverlapRefinery`` (context_size as
a fraction of chunk_size, suffix-merged into each chunk's ``.text`` so
consecutive chunks physically share a tail/head span) -- ``RecursiveChunker``
itself has no overlap parameter; this is chonkie's own documented two-step
shape (chunk, then refine), not a workaround.
"""
from __future__ import annotations

from typing import Any


def chunk_text(
    text: str,
    size: int = 400,
    overlap_ratio: float = 0.125,
    min_characters_per_chunk: int = 24,
) -> list[dict[str, Any]]:
    """Recursively chunk ``text`` into ``size``-token pieces with
    ``overlap_ratio`` (fraction of ``size``) of suffix overlap between
    consecutive chunks.

    Returns an ordered list of
    ``{"text", "token_count", "start_index", "end_index"}`` -- ``start_index``/
    ``end_index`` are offsets into the ORIGINAL text (pre-overlap-merge),
    letting a caller reconstruct provenance even though ``text`` itself
    may include a merged-in overlap span.
    """
    from chonkie import OverlapRefinery, RecursiveChunker  # noqa: PLC0415

    chunker = RecursiveChunker(
        chunk_size=size, min_characters_per_chunk=min_characters_per_chunk,
    )
    chunks = chunker.chunk(text)
    if overlap_ratio > 0 and len(chunks) > 1:
        refinery = OverlapRefinery(context_size=overlap_ratio, method="suffix")
        chunks = refinery.refine(chunks)
    return [
        {
            "text": c.text,
            "token_count": c.token_count,
            "start_index": c.start_index,
            "end_index": c.end_index,
        }
        for c in chunks
    ]


def build_server() -> Any:
    """Build the ``FastMCP`` server exposing :func:`chunk_text` as a tool."""
    from fastmcp import FastMCP  # noqa: PLC0415

    mcp = FastMCP("reyn-builtin-chunker")

    @mcp.tool
    def chunk(
        text: str,
        size: int = 400,
        overlap_ratio: float = 0.125,
        min_characters_per_chunk: int = 24,
    ) -> list[dict[str, Any]]:
        """Recursively chunk text into `size`-token pieces (default 400,
        the 256-512 2026 persistent-RAG default) with `overlap_ratio`
        (default 0.125 = 12.5%, within the 10-15% default band) of
        suffix overlap between consecutive chunks. Both are tunable per
        call -- there is no hardcoded chunk size. Returns
        [{"text", "token_count", "start_index", "end_index"}, ...]."""
        return chunk_text(
            text,
            size=size,
            overlap_ratio=overlap_ratio,
            min_characters_per_chunk=min_characters_per_chunk,
        )

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
