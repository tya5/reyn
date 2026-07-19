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

**Each chunk carries its own ``content_hash`` / ``chunk_index`` /
``est_tokens`` (#2972).** These were previously computed by the ingest
pipeline shelling out to a bundled python helper, because the pipeline
DSL's R1 expression language has no hash / string-length / enumerate
primitive. That shell-out bound reyn to
whatever ``python3`` the ambient ``PATH`` resolved to (broken under
``pipx install reyn``, a non-activated venv, or any PATH whose ``python3``
is not reyn's own interpreter) -- i.e. reyn was assuming ownership of a
python runtime it does not own. The party that already holds the chunk text
is the right one to describe it, so the three derived fields are simply part
of what ``chunk`` RETURNS. No new tool, no new server, and the ingest
pipeline needs no python of its own.
"""
from __future__ import annotations

import hashlib
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

    Returns an ordered list of ``{"text", "token_count", "start_index",
    "end_index", "content_hash", "chunk_index", "est_tokens"}``.

    ``start_index``/``end_index`` are offsets into the ORIGINAL text
    (pre-overlap-merge), letting a caller reconstruct provenance even though
    ``text`` itself may include a merged-in overlap span.

    The three derived fields (#2972 -- see module docstring for why they live
    here rather than in a python shell-out):

    - ``content_hash`` -- sha256 of the chunk's TEXT. The ingest pipeline's
      C5 change-detection key: an unchanged chunk re-hashes identically and
      is skipped rather than re-embedded.
    - ``chunk_index`` -- this chunk's 0-based position in the returned order.
      Combined with the caller's own source path it forms the stable
      add/update/remove diff identity (R1 cannot enumerate a list).
    - ``est_tokens`` -- the chars/4 estimate of this chunk's embedding cost
      (the SAME fallback heuristic ``EmbeddingProvider.estimate_tokens``
      uses, e.g. ``reyn.data.embedding.litellm_provider``). It funds ONLY the
      pipeline's "tokens saved by dedup" figure, which is necessarily a
      counterfactual: the sole way to learn a SKIPPED chunk's true token
      count is to send it to the embedder -- i.e. to spend exactly what the
      skip exists to avoid. Deliberately NOT ``token_count``: that is
      chonkie's own tokenizer count (character-based by default here), which
      measures a different thing than the embedder would bill.
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
            "content_hash": hashlib.sha256(c.text.encode("utf-8")).hexdigest(),
            "chunk_index": index,
            "est_tokens": max(1, len(c.text) // 4),
        }
        for index, c in enumerate(chunks)
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
        [{"text", "token_count", "start_index", "end_index",
        "content_hash", "chunk_index", "est_tokens"}, ...] -- content_hash
        is the chunk text's sha256 (change-detection key), chunk_index its
        0-based position, est_tokens the chars/4 embedding-cost estimate."""
        return chunk_text(
            text,
            size=size,
            overlap_ratio=overlap_ratio,
            min_characters_per_chunk=min_characters_per_chunk,
        )

    return mcp


def _maybe_arm_diagnostic_traceback_dump() -> None:
    """DIAGNOSTIC (temporary, #3060 case-(b) probe): env-gated only.

    When ``REYN_CHUNKER_DIAG_DUMP_AFTER`` is set to a float number of seconds,
    arm ``faulthandler.dump_traceback_later`` so that if this server hangs
    during ``initialize()`` (e.g. a blocking network syscall under a
    ``network:false`` sandbox — glibc ``getaddrinfo``/DNS, an httpx/opentelemetry
    startup call, …), the FULL Python stack of EVERY thread is dumped to
    ``stderr`` after that delay, NAMING the exact frame that is blocked. The
    witness test forwards that stderr into the CI log.

    ``exit=False`` so the dump is observational — the process keeps running and
    the normal timeout/teardown still applies. Completely inert when the env var
    is unset or unparseable: production behaviour (including this module's
    "no network at import/call time" contract) is byte-for-byte unchanged, since
    ``faulthandler`` does no I/O beyond writing to the already-open ``stderr``
    fd on the timer.
    """
    import os  # noqa: PLC0415 — diagnostic-only, keep production import surface clean
    import sys  # noqa: PLC0415

    raw = os.environ.get("REYN_CHUNKER_DIAG_DUMP_AFTER")
    if not raw:
        return
    try:
        after_seconds = float(raw)
    except ValueError:
        return
    if after_seconds <= 0:
        return
    import faulthandler  # noqa: PLC0415 — stdlib, no network, diagnostic-gated

    faulthandler.dump_traceback_later(after_seconds, file=sys.stderr, exit=False)


def main() -> None:
    # DIAGNOSTIC probe (env-gated, no-op in production): a faulthandler delayed
    # traceback dump that names a blocked frame if the server hangs. NOTE: an
    # earlier fd-level stdout tee (REYN_CHUNKER_DIAG_TEE_STDOUT) was REMOVED — its
    # os.dup2(_, 1) is refused by the seccomp filter under network=False and
    # CRASHED the server, contaminating the very observation it was meant to make
    # (the true failure is server-healthy + handshake-incomplete). stdout is now
    # observed from the CLIENT side (sandbox-free) instead; see
    # reyn.mcp.client's REYN_MCP_DIAG_CAPTURE_HANDSHAKE probe.
    _maybe_arm_diagnostic_traceback_dump()
    build_server().run()


if __name__ == "__main__":
    main()
