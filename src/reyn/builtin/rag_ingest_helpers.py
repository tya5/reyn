"""Small stdin/stdout JSON helpers for the builtin ``rag_ingest`` pipeline
(FP-0063 P3) -- local data munging the pipeline DSL's R1 expression language
(``docs/reference/runtime/pipeline-dsl.md``) cannot express itself: R1 has no
string-length/hash/zip-by-index primitives (its combinator set is
``map``/``filter``/``all``/``any``/``find``/``count``/``sum``/``join``/``get``/
``parse_json`` only). Rather than force those gaps through a clever R1
one-liner (or a reyn-core change), the ingest pipeline's ``shell`` steps
invoke this module as ``python3 -m reyn.builtin.rag_ingest_helpers
<subcommand>`` -- plain, testable, ruff/docstring-gated Python, same as the
rest of ``reyn.builtin`` (it ships alongside ``mcp_servers/`` as builtin
CONTENT, not a ``src/reyn/core`` change; the ingest pipeline is the only
caller).

Each subcommand reads one JSON value from stdin and prints one JSON value to
stdout -- the same convention the pipeline DSL's ``shell`` step already uses
(JSON-decoded stdin via the previous step's pipe data, JSON-decoded stdout
when it parses -- see ``src/reyn/tools/shell.py``). No reyn imports: this
module intentionally stays a free-standing script (like the MCP servers in
``mcp_servers/``) so a user who copies the pipeline (R2 -- "the builtin IS
the template people copy and tune") can copy this file alongside it with no
reyn-core import dependency to carry along.

Subcommands:

- ``list_files`` -- stdin ``{"input_path": str}`` -> stdout a JSON list of
  absolute file paths: the path itself if it names a file, or every file
  under it (recursively) whose extension is one of the owner's target
  formats (txt/md/pdf/xlsx/pptx/docx -- proposal 0063 line 20) if it names a
  directory.
- ``hash_chunks`` -- stdin ``{"source_path": str, "chunks": [{"text":
  str, "token_count": int, ...}, ...]}`` (the chunker MCP server's own
  per-chunk shape) -> stdout a JSON list of candidate chunk items:
  ``[{"id", "text", "content_hash", "chunk_index", "size_tokens",
  "est_tokens", "source_path"}, ...]``. ``id`` is a stable
  ``source_path::chunk_index`` key (C5's add/update/remove diff key);
  ``content_hash`` is the chunk TEXT's sha256 (C5's change-detection key);
  ``est_tokens`` is the chars/4 estimate (the SAME fallback heuristic
  ``EmbeddingProvider.estimate_tokens`` uses, e.g.
  ``src/reyn/data/embedding/litellm_provider.py``) -- used for the X2a/X5
  spend ESTIMATE the pipeline reports, since the real per-call
  ``total_tokens``/``cost_usd`` on ``embed``'s own envelope is not reachable
  from pipeline ``ctx`` today (``canonical_to_ctx_fields`` only exposes
  ``text``/``structured``, never ``meta`` -- see this arc's PR body).
- ``zip_vectors`` -- stdin ``{"items": [...], "vectors": [[float, ...],
  ...], "embedding_model": str}`` (same order, from a ``to_upsert`` list and
  ``embed``'s own vectors) -> stdout a JSON list of
  ``[{"id", "vector", "metadata": {...}}, ...]`` in the EXACT shape
  ``reyn.builtin.mcp_servers.vector_store_server``'s ``upsert`` tool expects
  -- pairing each item with its vector BY POSITION (R1 has no index-based
  zip) and stamping ``metadata.embedding_model`` from ``embedding_model``
  (the pipeline's OWN configured input -- see the module docstring note
  above on why not ``envelope.model``).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

# The owner's target format list (proposal 0063 line 20): txt/md/pdf/xlsx/
# pptx/docx. Lower-cased extension match, recursive directory walk.
_TARGET_EXTENSIONS = (".txt", ".md", ".pdf", ".xlsx", ".pptx", ".docx")


def _read_stdin_json() -> "dict":
    return json.load(sys.stdin)


def _print_json(value: object) -> None:
    print(json.dumps(value))


def list_files() -> None:
    """``list_files`` subcommand -- see module docstring."""
    payload = _read_stdin_json()
    input_path = os.path.abspath(payload["input_path"])
    if not os.path.isdir(input_path):
        _print_json([input_path])
        return
    found: list[str] = []
    for root, _dirs, files in os.walk(input_path):
        for name in files:
            if name.lower().endswith(_TARGET_EXTENSIONS):
                found.append(os.path.join(root, name))
    _print_json(sorted(found))


def hash_chunks() -> None:
    """``hash_chunks`` subcommand -- see module docstring."""
    payload = _read_stdin_json()
    source_path = payload["source_path"]
    items = []
    for index, chunk in enumerate(payload["chunks"]):
        text = chunk["text"]
        items.append({
            "id": f"{source_path}::{index}",
            "text": text,
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "chunk_index": index,
            "size_tokens": chunk.get("token_count", 0),
            "est_tokens": max(1, len(text) // 4),
            "source_path": source_path,
        })
    _print_json(items)


def zip_vectors() -> None:
    """``zip_vectors`` subcommand -- see module docstring."""
    payload = _read_stdin_json()
    items = payload["items"]
    vectors = payload["vectors"]
    embedding_model = payload["embedding_model"]
    parent_context = payload.get("parent_context")
    if len(items) != len(vectors):
        raise ValueError(
            f"zip_vectors: {len(items)} items but {len(vectors)} vectors -- "
            "embed's output order must match the items it was called with"
        )
    out = []
    for item, vector in zip(items, vectors, strict=True):
        out.append({
            "id": item["id"],
            "vector": vector,
            "metadata": {
                "source_path": item["source_path"],
                "source_type": "generic",
                "content_hash": item["content_hash"],
                "embedding_model": embedding_model,
                "chunk_index": item["chunk_index"],
                "size_tokens": item["size_tokens"],
                "parent_context": parent_context,
                "extra": {},
            },
        })
    _print_json(out)


_SUBCOMMANDS = {
    "list_files": list_files,
    "hash_chunks": hash_chunks,
    "zip_vectors": zip_vectors,
}


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in _SUBCOMMANDS:
        print(
            f"usage: python3 -m reyn.builtin.rag_ingest_helpers "
            f"{{{'|'.join(_SUBCOMMANDS)}}}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    _SUBCOMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
