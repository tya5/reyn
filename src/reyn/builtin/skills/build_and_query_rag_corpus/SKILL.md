---
name: build_and_query_rag_corpus
description: Make a folder of the operator's own documents (txt/md/pdf/xlsx/pptx/docx) searchable by meaning -- ingest them into a user-named sqlite vector store, then query it for the top-k relevant chunks. Read this before running the builtin `rag_ingest` / `rag_query` pipelines, or when the operator asks you to search documents that are NOT already in reyn's own semantic_search index.
---

# Build and query a RAG corpus

Two builtin pipelines do the work; this skill is the part neither of their
one-line descriptions can carry -- **when to reach for them, what order they
go in, and the one mismatch that silently ruins a corpus** (proposal 0063 P4).

## First: is this even the right mechanism?

**Reyn has two different RAGs. Picking the wrong one wastes an ingest.**

| | **this skill** (builtin user RAG) | **`semantic_search`** (in-core RAG) |
|---|---|---|
| Store | an **external** sqlite file **you name** (`docs.sqlite`) | reyn's own index, `.reyn/index/<source>/` |
| Setup | operator enables **3 MCP servers** first | operator indexes a **source**; nothing else |
| Reach for it when | the operator points at **a folder/file of documents** and wants a corpus they own, keep, and can hand to another tool | the operator asks about docs **already indexed** as a reyn source |
| Formats | pdf/xlsx/pptx/docx + txt/md (via the markitdown MCP server) | whatever the indexing code chunked |

If `semantic_search` already covers the question, **use it** -- it needs no
setup. Reach here when the documents are not in it and the operator wants
their own portable store. Do not ingest a corpus just to answer one question
about one file: **read the file.**

## Prerequisites -- and you cannot satisfy them yourself

The three MCP servers this needs (`reyn_markitdown` / `reyn_chunker` /
`reyn_vector_store`) **ship INERT** -- deliberately unconfigured, so that
enabling them stays the operator's explicit decision. `mcp.servers` is
operator config: **you cannot enable them, and you should not try.**

**If they are not enabled, `rag_ingest` does not explode -- it pre-flights
them and returns a "blocked" message naming each unreachable server with a
concrete remedy, before spending anything on embeddings.** So a run that comes
back describing an unreachable server is the pipeline working as designed, not
a crash. When you get one:

- **Relay it to the operator as-is.** It already says which server and what to
  do; it is written for them, not for you.
- **Do not retry, and do not work around it** by shelling out or hand-rolling
  an ingest. The remedy is a config change only the operator can make. Point
  them at `docs/guide/for-users/build-a-rag-corpus.md`.

## The workflow

**1. Ingest** -- `input_path` **must be absolute**; the pipeline globs it
directly, and a relative pattern yields the wrong `source_path` column.
`output_db`, in contrast, is written by the `reyn_vector_store` MCP server
itself and resolves like any other write: **relative to the sandbox's
default write grant, which is the directory you ran `reyn` from.** A
**cwd-relative `output_db` needs no config at all** -- keep it there unless
the operator wants the store somewhere else (see below).

```
run_pipeline(name="rag_ingest.ingest", input={
  "input_path": "/abs/path/to/docs",        # a folder OR a single file
  "output_db": "./rag/docs.sqlite",         # zero-config: written under cwd
})
```

Returns a summary: `files_scanned` / `chunks_upserted` / `chunks_removed` /
`chunks_unchanged_skipped` / `embedding_model` / `tokens_embedded` /
`cost_usd` / `priced` / `estimated_tokens_saved_by_dedup`. **`cost_usd: null`
with `priced: false` means the model could not be priced -- report it as
"unknown", never as free.**

**2. Query** -- the same `db` the ingest wrote.

```
run_pipeline(name="rag_query.query", input={
  "query_text": "how does X work?",
  "db": "./rag/docs.sqlite",
  "top_k": 5,                                # default 5
})
```

**Want the store somewhere outside cwd instead** (an absolute path, or a
path outside the project)? That is supported, but it is a **declared
deviation, not the default**: the operator must add a `write_paths` entry
naming that location to the `reyn_vector_store` server's config (see
`docs/cookbook/configs/with-builtin-rag-mcp.yaml`). Without it, the sandbox
denies the write and the ingest fails with a raw sqlite error -- **do not
pass an absolute `output_db`/`db` unless the operator has already declared
`write_paths` for it.**

Returns `[{id, distance, metadata}, ...]`, **nearest first**. `metadata`
carries `source_path` / `chunk_index` / `content_hash` / `embedding_model`.
**It does not carry the chunk text** -- the store has no column for it. To
quote a hit, read `metadata.source_path` with the ordinary file read op.

## ⚠️ One sqlite file = one embedding model

`embedding_model` defaults to `"standard"` on **both** pipelines. If you pass
it to one, **pass the same value to the other** -- a mismatch changes the
vector space, and the query either raises `VectorDimensionMismatchError` or,
same dimension but a different model, returns **quietly meaningless
neighbours**. Different model -> different sqlite file. To re-embed an
existing corpus with a new model, ingest into a **new** `output_db`; pointing
a new model at the old file corrupts it.

## Re-running ingest is cheap -- and is how you update

Ingest is **incremental by `content_hash`** (add / update / remove). Re-run it
on the same `input_path` + `output_db` after the documents change: unchanged
chunks are **skipped, not re-embedded** (`chunks_unchanged_skipped` and
`estimated_tokens_saved_by_dedup` report what the skip saved), changed chunks
are re-embedded and replaced, and chunks whose file is gone are deleted. **Do
not delete the sqlite and start over** to "refresh" a corpus -- that pays full
embedding cost for documents that did not change.

## Tuning (only when the defaults underperform)

`chunk_size` (default 400) and `chunk_overlap_ratio` (default 0.125) are
inputs, not baked-in constants. The defaults are the 2026 persistent-RAG band
(256-512 tokens, 10-15% overlap) and cover most corpora. Raise `chunk_size`
for dense prose whose ideas span paragraphs; lower it for reference material
queried by narrow fact. **Changing either re-chunks everything** -> new
`content_hash` for every chunk -> a full re-embed of the corpus. Decide before
the first big ingest, not after.

## Swapping the backend -- copy the pipeline, re-point the server

Want a different vector DB, chunker, or parser? **Copy
`src/reyn/builtin/pipelines/rag_ingest.yaml` (+ `rag_query.yaml`) into the
operator's project and re-point the `*_server` inputs** (or the `mcp.servers`
entries they name) at the replacement. Every server name is an input with a
default (`markitdown_server` / `chunker_server` / `vectorstore_server`), so a
drop-in swap needs no edit at all -- just pass the new name:

```
run_pipeline(name="rag_ingest.ingest", input={
  "input_path": "/abs/docs", "output_db": "./docs.sqlite",
  "vectorstore_server": "my_qdrant",
})
```

The replacement must expose the same tool shapes (`upsert` / `query` /
`list_metadata` / `delete`). **This is the intended extension mechanism, not a
workaround**: reyn builds no adapter for a user's RAG store (FP-0057 C2), so
"copy the builtin and re-point it" is the supported path.

Full setup + backend-swap guide: `docs/guide/for-users/build-a-rag-corpus.md`.
Config to copy: `docs/cookbook/configs/with-builtin-rag-mcp.yaml`.
