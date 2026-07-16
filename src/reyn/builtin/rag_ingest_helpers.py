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

**``python3`` must be reyn's own interpreter -- a REAL constraint, not a
nicety.** The ingest pipeline reaches this module by shelling out to
``python3 -m reyn.builtin.rag_ingest_helpers``, and ``sandboxed_exec`` passes
the ambient ``PATH`` straight through (``sandboxed_exec.py``'s
``os.environ.get("PATH")``), so ``python3`` resolves however the operator's
shell would resolve it. ``sys.executable`` is NOT reachable from the pipeline
DSL, so the pipeline cannot ask for "the interpreter reyn is running under".
Consequence: a ``pipx install reyn``, a non-activated venv, or any environment
whose ``python3`` differs from reyn's will FAIL -- and the arc is therefore
not turnkey in those environments. The ``probe`` subcommand below exists so
that failure is caught by the ingest pipeline's step-0 pre-flight, with a
decision-enabling message, instead of surfacing later as an opaque
"path 'ctx.files_raw.structured' is absent". Closing the gap properly needs a
core/DSL decision (expose the interpreter, a ``python:`` step, or resolve
``python3`` -> ``sys.executable`` in ``sandboxed_exec``) and is tracked on the
FP-0063 umbrella; this module only makes the failure VISIBLE.

Subcommands:

- ``probe`` -- stdin ignored -> stdout ``[sys.executable, <reyn package dir>]``.
  Answers "is THIS ``python3`` an interpreter that can see reyn at all?" for
  the ingest pipeline's pre-flight. It does NOT (and cannot) detect the
  subtler case where ``python3`` sees a DIFFERENT reyn install than the one
  running the pipeline -- it reports the paths so a human can compare.
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
  ``src/reyn/data/embedding/litellm_provider.py``). ``est_tokens`` funds
  ONLY the pipeline's X5 "tokens saved by dedup" figure, which is
  necessarily a counterfactual: the sole way to learn a SKIPPED chunk's
  true token count is to send it to the embedder, i.e. to spend exactly
  what the skip exists to avoid. Tokens actually EMBEDDED are never
  estimated -- the pipeline reads ``embed``'s own metered
  ``total_tokens``/``cost_usd`` off its envelope meta instead.
- ``zip_vectors`` -- stdin ``{"items": [...], "vectors": [[float, ...],
  ...], "embedding_model": str}`` (same order, from a ``to_upsert`` list and
  ``embed``'s own vectors) -> stdout a JSON list of
  ``[{"id", "vector", "metadata": {...}}, ...]`` in the EXACT shape
  ``reyn.builtin.mcp_servers.vector_store_server``'s ``upsert`` tool expects
  -- pairing each item with its vector BY POSITION (R1 has no index-based
  zip) and stamping ``metadata.embedding_model`` from ``embedding_model``.
  The caller passes the RESOLVED model (``embed``'s ``envelope.model``),
  never a model-class alias: the column must name the model that actually
  produced the vectors beside it (FP-0057 C4).
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


def probe() -> None:
    """``probe`` subcommand -- see module docstring.

    Emits a JSON LIST (not an object) deliberately: a ``shell`` step whose
    stdout decodes to a dict renders as ``text`` with NO ``structured`` key
    (``shell_to_canonical``), whereas a list lands in ``ctx.<name>.structured``
    -- which is what makes "did this run at all?" a one-expression check for
    the caller. Same reason every other subcommand here emits a list.
    """
    import reyn  # noqa: PLC0415 -- probing THIS interpreter's reyn, if any
    _print_json([sys.executable, os.path.dirname(reyn.__file__)])


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
    "probe": probe,
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
