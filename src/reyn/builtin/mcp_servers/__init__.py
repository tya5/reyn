"""Builtin MCP servers (FP-0063 P2 -- builtin turnkey user RAG).

Two thin bundled MCP servers that the ingest/query pipelines (P3, a later
phase) will call as external MCP vessels -- exactly like any other
operator-configured MCP server (FP-0057 C2: the user RAG store is EXTERNAL,
never in-core; reyn contributes no store/chunker code path of its own,
only these standalone scripts that happen to ship inside the reyn wheel):

- :mod:`reyn.builtin.mcp_servers.vector_store_server` -- wraps
  ``sqlite-vec`` (dual MIT/Apache-2.0, no bundled embedding model). Accepts
  externally pre-computed vectors (FP-0057 C1: reyn is the SOLE embedder),
  writes to a user-specified single sqlite file, and exposes generic
  upsert/query/list/delete ops -- the ingest pipeline (not this server)
  owns the ``content_hash`` add/update/remove diff (C5, settled at co-vet
  in the proposal).
- :mod:`reyn.builtin.mcp_servers.chunker_server` -- wraps ``chonkie``
  (MIT). ``size``/``overlap`` are tool parameters with 2026-default
  values (recursive, 256-512 tokens, 10-15% overlap), never hardcoded
  constants, so the first thing a template-copying user wants to tune is
  easy to find (R2/R4 in the proposal).

Neither server is wired into ``reyn.builtin.registry.build_builtin_config()``
-- doing so would make its ``mcp.servers.<name>`` key "configured" the
instant reyn is installed, which would let ``reyn pipe run``'s
trusted-by-configuration auto-grant (#2932) silently fire with no operator
decision anywhere in the chain (proposal R3 / architect co-vet F-D). Both
servers ship as pure INERT sample content instead: a commented config block
an operator must copy into their own ``reyn.yaml`` and explicitly enable --
see ``docs/cookbook/configs/with-builtin-rag-mcp.yaml``. This mirrors the
precedent set for builtin skills (``force_auto_invoke_false``, "A3
inert-ship" in ``reyn.builtin.registry``), adapted to MCP's structurally
different inertness mechanism: an ``mcp.servers`` entry has no ``enabled``
flag anywhere in the codebase today (#2932's auto-grant keys purely on
dict-key presence), so absence-from-merged-config is the only inert
posture available without inventing new core schema + auto-grant code --
which is out of scope for this arc's "zero reyn core change" target.

Both are runnable as ``python -m reyn.builtin.mcp_servers.<module>`` (stdio
transport, via ``fastmcp`` -- already a core dependency, see
``pyproject.toml``). The extra runtime deps they need
(``sqlite-vec``/``apsw``/``chonkie``) live in the ``builtin-rag`` optional
extra so a base ``pip install reyn`` is unaffected.
"""
from __future__ import annotations
