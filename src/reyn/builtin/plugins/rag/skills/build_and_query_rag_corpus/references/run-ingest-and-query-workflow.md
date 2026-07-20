## Run the RAG ingest/query workflow

Companion to the router SKILL.md (routing + install) and
`configure-embedding-provider.md` (embedding setup) -- read those first if
you haven't installed the `rag` plugin or confirmed an embedding provider
yet. This file covers the two actual pipeline calls.

Both steps below run through `pipeline__run` -- the launch verb for a
**REGISTERED** pipeline, invoked by the name it was installed under
(`"rag_ingest.ingest"` / `"rag_query.query"`). This is **not**
`pipeline__run_inline` (an inline DSL definition instead of a name, for
defining a pipeline on the fly) -- passing `rag_ingest.ingest`'s
`name`/`input` shape to `pipeline__run_inline` fails; it expects a
`pipeline` body, not a `name`.

**1. Ingest** -- `input_path` **must be absolute**; the pipeline globs it
directly, and a relative pattern yields the wrong `source_path` column.
`output_db`, by contrast, is written by the `reyn_vector_store` server and
resolves **relative to the sandbox's default write grant = the directory you
ran `reyn` from**. A **cwd-relative `output_db` needs no config at all** --
keep it there unless the operator wants the store elsewhere (see below).

```
pipeline__run(name="rag_ingest.ingest", input={
  "input_path": "/abs/path/to/docs",        # a folder OR a single file
  "output_db": "./rag/docs.sqlite",         # zero-config: written under cwd
})
```

Returns a summary: `files_scanned` / `chunks_upserted` / `chunks_removed` /
`chunks_unchanged_skipped` / `embedding_model` / `tokens_embedded` /
`cost_usd` / `priced` / `estimated_tokens_saved_by_dedup`. **`cost_usd: null`
with `priced: false` means the model could not be priced -- report it as
"unknown", never as free.** (Ingest is incremental and cheap to re-run --
see `corpus-internals-schema-tuning-and-backend-swap.md`.)

**2. Query** -- the same `db` the ingest wrote. **The parameter name is
EXACTLY `db`** -- not `db_path` (the raw vector-store MCP tool's own arg
name, one layer down) and not `vector_store_path` (a plausible-sounding
name this pipeline has never accepted). A missing or misnamed `db` is not
silently ignored: `rag_query.query` returns a blocked message naming this
exact requirement, but passing the right name the first time saves a
round trip.

```
pipeline__run(name="rag_query.query", input={
  "query_text": "how does X work?",
  "db": "./rag/docs.sqlite",                 # EXACT param name: "db"
  "top_k": 5,                                # default 5
})
```

**Want the store outside cwd?** A **declared deviation, not the default**:
the operator must add a `write_paths` entry to `reyn_vector_store`'s config
(see `docs/cookbook/configs/with-builtin-rag-mcp.yaml`) -- not something
`mcp__install_local` can set. **Do not pass an absolute `output_db`/`db`
unless they already have**; relay a denial rather than retrying. The
alternative you can act on is a cwd-relative `output_db`.

Returns `[{id, distance, metadata}, ...]`, **nearest first**. `metadata`
carries `source_path` / `chunk_index` / `content_hash` / `embedding_model`.
**It does not carry the chunk text** -- the store has no column for it. To
quote a hit, read `metadata.source_path` with the ordinary file read op.
(Full schema: `corpus-internals-schema-tuning-and-backend-swap.md`.)

### One sqlite file = one embedding model

`embedding_model` defaults to `"standard"` on **both** pipelines. If you pass
it to one, **pass the same value to the other** -- a mismatch changes the
vector space, and the query either raises `VectorDimensionMismatchError` or,
at the same dimension, returns **quietly meaningless neighbours**. Different
model -> different sqlite file: to re-embed, ingest into a **new**
`output_db`; pointing a new model at the old file corrupts it. (Enforcing
mechanism, full schema, tuning knobs, and backend swap:
`corpus-internals-schema-tuning-and-backend-swap.md`.)

Full setup + backend-swap guide: `docs/guide/for-users/build-a-rag-corpus.md`.
Config to copy: `docs/cookbook/configs/with-builtin-rag-mcp.yaml`.
