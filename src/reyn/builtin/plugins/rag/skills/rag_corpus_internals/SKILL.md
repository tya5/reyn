---
name: rag_corpus_internals
description: The sqlite schema `rag_ingest`/`rag_query` write and read (three tables, why chunk text is never stored), why re-running ingest is cheap (content-hash incrementality), the `chunk_size`/`chunk_overlap_ratio` tuning knobs, and how to swap the vector-store/chunker/parser servers for different ones. Read this when you need to inspect the sqlite directly, decide whether to re-tune chunking, or replace a backend server -- not needed for a first ordinary ingest/query (see `build_and_query_rag_corpus` for that).
---

# RAG corpus internals -- schema, re-ingest, tuning, backend swap

Companion to `build_and_query_rag_corpus` (the entry-point skill for the
ordinary ingest/query workflow) and `configure_rag_embedding_provider`
(embedding setup). This skill covers what's underneath: the sqlite schema,
why re-ingest is cheap, the tuning knobs, and swapping servers.

## What ingest writes into the sqlite -- the three-table schema

The sqlite file `reyn_vector_store` writes is a plain database you can open
and inspect (`sqlite3 docs.sqlite '.schema'`). Ingest populates **three
tables**:

**`reyn_rag_chunks`** -- one row per chunk, all the metadata (but **not** the
chunk text -- there is no text column, by design):

| column | type | meaning |
|---|---|---|
| `rag_id` | `TEXT UNIQUE NOT NULL` | primary key. **Formula = `<source_path>::<chunk_index>`** -- the store derives it; a caller never sets it. |
| `source_path` | `TEXT NOT NULL` | the source file the chunk came from. |
| `source_type` | `TEXT NOT NULL DEFAULT 'generic'` | source kind. |
| `content_hash` | `TEXT NOT NULL` | sha256 of the chunk body -- the **change-detection key** (re-ingest compares this). |
| `embedding_model` | `TEXT NOT NULL` | the **resolved** model id that actually produced the vector (never a class alias like `standard`) -- stamped on every row. |
| `chunk_index` | `INTEGER NOT NULL DEFAULT 0` | 0-based position within `source_path`. |
| `size_tokens` | `INTEGER NOT NULL DEFAULT 0` | token count of the chunk. |
| `parent_context` | `TEXT` | the ingest root -- a scope tag for "which folder this ingest covered". |
| `extra` | `TEXT NOT NULL DEFAULT '{}'` | JSON blob for extension metadata. |

**`reyn_rag_config`** -- a single `dim INTEGER NOT NULL`: the embedding
**dimension**, fixed on the **first** upsert. A later upsert whose vector has
a different `dim` raises `VectorDimensionMismatchError` -- this is the
**mechanism** behind "one sqlite = one embedding model = one vector space"
(see `build_and_query_rag_corpus`; the model stamp in `embedding_model`
records *which* model, `dim` *enforces* that only one is ever mixed in).

**`reyn_rag_vectors`** -- a sqlite-vec virtual table,
`USING vec0(embedding float[<dim>])`, holding the vectors themselves. It is
kept in **positional (parallel-array) correspondence** with
`reyn_rag_chunks` by shared `rowid`: vector *i* belongs to chunk row *i*.

**How writes behave:**

- **Upsert = delete-then-insert on `rag_id`** (a "replace", so re-ingesting a
  chunk never duplicates it): the store deletes any existing row with the same
  `<source_path>::<chunk_index>` id, then inserts the new chunk row **and** its
  vector together.
- **Delete identifies a chunk by `(source_path, chunk_index)`** -- exactly the
  pair that composes `rag_id` -- so the add/update/remove diff (driven by
  `content_hash`) can target one chunk precisely.
- **Query** joins the two data tables on `rowid` and returns
  `[{id, distance, metadata}, ...]` -- `metadata` is these `reyn_rag_chunks`
  columns, minus the never-stored text.

## Re-running ingest is cheap -- and is how you update

Ingest is **incremental by `content_hash`**: re-run it on the same
`input_path` + `output_db` after documents change and unchanged chunks are
**skipped, not re-embedded** (`chunks_unchanged_skipped` /
`estimated_tokens_saved_by_dedup` report the saving); changed chunks are
replaced, and chunks whose file is gone are deleted. **Do not delete the
sqlite and start over** to "refresh" -- that re-pays full embedding cost for
documents that did not change.

## Tuning (only when the defaults underperform)

`chunk_size` (default 400) and `chunk_overlap_ratio` (default 0.125) are
inputs, not constants -- the defaults are the 2026 persistent-RAG band
(256-512 tokens, 10-15% overlap) and cover most corpora. Raise `chunk_size`
for dense prose whose ideas span paragraphs; lower it for reference material
queried by narrow fact. **Changing either re-chunks everything** -> a full
re-embed. Decide before the first big ingest, not after.

## Swapping the backend -- re-point the server

Want a different vector DB, chunker, or parser? Every server name is an input
with a default (`markitdown_server` / `chunker_server` / `vectorstore_server`),
so a drop-in swap needs **no file edit** -- just pass the new name (install it
first, same as `build_and_query_rag_corpus`'s "Prerequisites"):

```
pipeline__run(name="rag_ingest.ingest", input={
  "input_path": "/abs/docs", "output_db": "./docs.sqlite",
  "vectorstore_server": "my_qdrant",
})
```

The replacement must expose the same tool shapes (`upsert` / `query` /
`list_metadata` / `delete`). Need to change the *steps*, not just the server?
Copy `~/.reyn/plugins/rag/pipelines/rag_ingest.yaml` (+ `rag_query.yaml`,
present once the plugin is installed) into the operator's project and edit
it -- or promote your edited copy back as its own plugin
(`plugin_management__install(source={"kind": "local", "path": "..."})`). **This is the
intended extension mechanism, not a workaround**: reyn builds no adapter for
a user's RAG store (FP-0057 C2).

Full setup + backend-swap guide: `docs/guide/for-users/build-a-rag-corpus.md`.
Config to copy: `docs/cookbook/configs/with-builtin-rag-mcp.yaml`.
