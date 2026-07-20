---
name: build_and_query_rag_corpus
description: Make a folder of the operator's own documents (txt/md/pdf/xlsx/pptx/docx) searchable by meaning via a user-named sqlite vector store. Covers routing (this vs. `semantic_search`), installing the `rag` plugin, embedding-provider setup, the exact `rag_ingest`/`rag_query` pipeline calls, and corpus internals -- via bundled references. Read this before running the builtin `rag_ingest` / `rag_query` pipelines, or when the operator asks you to search documents that are NOT already in reyn's own semantic_search index.
---

# Build and query a RAG corpus

Two builtin pipelines do the work; this skill carries what their one-line
descriptions cannot -- **when to reach for them, how to turn them on, and
which bundled reference answers your next question.** Deep setup/tuning/
schema detail lives under `references/` (map at the end) -- read one when
your question maps to it, not all four unconditionally.

## First: is this even the right mechanism?

**Reyn has two different RAGs. Picking the wrong one wastes an ingest.**

| | **this skill** (builtin user RAG) | **`semantic_search`** (in-core RAG) |
|---|---|---|
| Store | an **external** sqlite file **you name** (`docs.sqlite`) | reyn's own index, `.reyn/index/<source>/` |
| Setup | you install the **`rag` plugin** + a markitdown MCP server (operator is prompted) | operator indexes a **source**; nothing else |
| Reach for it when | the operator points at **a folder/file of documents** and wants a corpus they own, keep, and can hand to another tool | the operator asks about docs **already indexed** as a reyn source |
| Formats | pdf/xlsx/pptx/docx + txt/md (via markitdown) | whatever the indexing code chunked |

If `semantic_search` already covers the question, **use it** -- it needs no
setup. Do not ingest a corpus just to answer one question about one file:
**read the file.**

## Prerequisites -- install them yourself

Neither the builtin `rag` plugin nor the third-party markitdown server ships
pre-installed, so enabling them is a decision, not a default. **The decision
is the operator's; making the request is yours** -- install and the
permission gate prompts them before anything reaches config. **Do not tell
the operator to hand-edit YAML.**

```
plugin_management__install(source={"kind": "builtin", "name": "rag"})
mcp__install_local(name="reyn_markitdown", command="uvx", args=["markitdown-mcp"])
```

The single `plugin_management__install` call installs **everything the rag
plugin ships** -- both MCP servers (`reyn_chunker` / `reyn_vector_store`),
the `rag_ingest` / `rag_query` pipelines, and this skill -- in one step:
copies the plugin to `~/.reyn/plugins/rag/`, materialises its dependencies
(chonkie/apsw/sqlite-vec) into a **dedicated per-plugin environment** (never
reyn's own env), and registers everything. **No `permissions:` block to
add** -- a server in the merged config is granted when the pipeline runs it,
and the registration step is **probed before it commits** (a failing server
is skipped, not half-written).

`rag_ingest` pre-flights all three servers and returns a **"blocked"**
message naming any unreachable one *before* spending on embeddings:

- **Not installed yet** (the common case): install it above, re-run.
- **Operator refused**: stop and relay it -- a refusal is an answer, not an
  error to route around. **Do not shell out, hand-roll an ingest, or re-ask.**
- **Materialisation failed**: `plugin_management__install` reports the failure
  inline -- **the operator's machine or network, not your call**; name what
  failed and let them.

**Never `pip install markitdown-mcp` beside Reyn** -- `uvx` fetches it into
an isolated environment instead. If `uvx` cannot reach PyPI (firewalled),
use a **separate venv + an absolute path** -- never Reyn's own venv:

```
python3 -m venv ~/.reyn-markitdown && ~/.reyn-markitdown/bin/pip install markitdown-mcp
mcp__install_local(name="reyn_markitdown", args=[],
                   command="/abs/path/.reyn-markitdown/bin/markitdown-mcp")
```

## Next steps -- which reference answers your question

Once installed:

1. **Embedding provider.** `rag_ingest` needs a working embedding provider,
   or every chunk it embeds is wasted spend. Have an API key? read
   `configure-embedding-provider.md`. No key / offline? read
   `configure-local-embedding-model.md`.
2. **Run it.** Read `run-ingest-and-query-workflow.md` for the exact
   `pipeline__run` calls, parameter names, and the `embedding_model`
   mismatch that silently ruins a corpus.
3. **Internals.** For the sqlite schema, incremental re-ingest, tuning, or
   swapping the vector-store/chunker/parser backend, read
   `corpus-internals-schema-tuning-and-backend-swap.md`.

Full setup + backend-swap guide: `docs/guide/for-users/build-a-rag-corpus.md`.
Config to copy: `docs/cookbook/configs/with-builtin-rag-mcp.yaml`.

## Bundled references

- [configure-embedding-provider.md](${CLAUDE_SKILL_DIR}/references/configure-embedding-provider.md)
  -- confirm a working embedding provider before your first `rag_ingest`
  call: the pre-flight curl check, and the API-key path (Case A, no proxy
  needed).
- [configure-local-embedding-model.md](${CLAUDE_SKILL_DIR}/references/configure-local-embedding-model.md)
  -- no embedding API key? run a local model behind a litellm proxy
  (Case B): start the server, register it in the proxy config, point reyn
  at the proxy, and how to pick which local model to use.
- [run-ingest-and-query-workflow.md](${CLAUDE_SKILL_DIR}/references/run-ingest-and-query-workflow.md)
  -- the exact `pipeline__run` calls for `rag_ingest.ingest` /
  `rag_query.query`, parameter names (absolute-vs-cwd-relative path rules),
  and the `embedding_model` mismatch that silently ruins a corpus.
- [corpus-internals-schema-tuning-and-backend-swap.md](${CLAUDE_SKILL_DIR}/references/corpus-internals-schema-tuning-and-backend-swap.md)
  -- the sqlite schema (three tables, why chunk text is never stored), why
  re-running ingest is cheap (content-hash incrementality), the
  `chunk_size`/`chunk_overlap_ratio` tuning knobs, and how to swap the
  vector-store/chunker/parser servers for different ones.
