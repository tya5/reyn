# Build a RAG corpus from your own documents

Point Reyn at a folder of documents (`txt` / `md` / `pdf` / `xlsx` / `pptx` / `docx`), get a **sqlite file you own** that Reyn can search by meaning. Two builtin pipelines do it: `rag_ingest.ingest` builds the store, `rag_query.query` searches it.

> **TL;DR**: `pip install "reyn[builtin-rag]"`, copy the MCP block from [`cookbook/configs/with-builtin-rag-mcp.yaml`](../../cookbook/configs/with-builtin-rag-mcp.yaml) into your `reyn.yaml` and uncomment it, then ask Reyn to ingest your folder. It ships **inert** — nothing runs until you enable those three servers yourself.

## Is this the RAG you want?

Reyn has **two**, and they are not interchangeable:

| | **this guide** (builtin user RAG) | **[semantic search](enable-semantic-search.md)** (in-core RAG) |
|---|---|---|
| Where the data lives | **a sqlite file you name** — yours to keep, copy, or hand to another tool | Reyn's own index under `.reyn/index/<source>/` |
| What you set up | 3 MCP servers (below) | an indexed source; no servers |
| Reads pdf/xlsx/pptx/docx | **yes** (via the markitdown server) | only what your indexing code chunked |
| Use it when | you have **a folder of documents** and want a portable corpus | you want Reyn to recall docs you already registered as a source |

If you just want Reyn to search docs you already index, you want [Enable semantic search](enable-semantic-search.md) — it needs no MCP servers. Come here when you have documents in real-world formats and want a store you own.

## Why it ships off

The three MCP servers are **inert by design**. Once a server appears under `mcp.servers.<name>` in any merged config, `reyn pipe run` auto-grants it — that auto-grant is justified only because *you* explicitly configured it. So Reyn ships the servers' **code** and never wires them in: **enabling them is your decision, made once, in your own config.**

Read what you are enabling before you do:

- **`reyn_markitdown`** reads **every file under the folder you point `rag_ingest` at**, and any `uri` it is handed.
- **`reyn_vector_store`** **writes** to whatever sqlite file `db_path` names.
- **`reyn_chunker`** reads no filesystem paths of its own.

## Setup

**1. Install the extra dependencies** (not part of Reyn's base install):

```bash
pip install "reyn[builtin-rag]"     # apsw + sqlite-vec + chonkie
pip install markitdown-mcp          # the document parser
```

> **Firewalled network?** The config's default `command: uvx` fetches `markitdown-mcp` from PyPI **on first run**. If PyPI is blocked, pre-install it as above and point `command` at the installed console script instead of `uvx`. `sqlite-vec` is **wheel-only** (no sdist), so it needs a mirror that serves wheels — an sdist-only internal mirror cannot install it, and musl/Alpine has no wheel at all. Reyn's own `python:3.12-slim` base image is glibc, so containers are unaffected.

**2. Copy the config.** Take the `permissions` + `mcp.servers` block from [`cookbook/configs/with-builtin-rag-mcp.yaml`](../../cookbook/configs/with-builtin-rag-mcp.yaml) into your `reyn.yaml` (or `reyn.local.yaml`) and uncomment it:

```yaml
permissions:
  mcp.reyn_vector_store: allow
  mcp.reyn_chunker: allow
  mcp.reyn_markitdown: allow

mcp:
  servers:
    reyn_vector_store:
      type: stdio
      command: reyn-rag-vector-store
    reyn_chunker:
      type: stdio
      command: reyn-rag-chunker
    reyn_markitdown:
      type: stdio
      command: uvx
      args: ["markitdown-mcp"]
```

**Why the `reyn-rag-*` console scripts and not `python -m ...`?** Both work — Reyn launches whatever `command` you write, as-is, in any language, and never rewrites it. **Preparing the runtime an MCP server needs is your job, not Reyn's.** The console scripts are *recommended* only because `pip` stamps their shebang with the absolute path of the interpreter they were installed into, so they always find Reyn. A bare `python3` is resolved from your `PATH` at launch, which is a *different* interpreter under `pipx install reyn`, a non-activated venv, or a `PATH` with another python first — there the server fails with `No module named reyn`. If you prefer the module form, give an absolute interpreter path and check it with `<that python> -c 'import reyn; print(reyn.__file__)'`.

## Use it

In `reyn chat`, just ask — *"ingest the documents in /abs/path/to/docs into a searchable store, then tell me what they say about X"*. Reyn reads its `build_and_query_rag_corpus` skill and drives both pipelines.

To run them yourself, outside a chat session:

```bash
reyn pipe run rag_ingest.ingest \
  --input '{"input_path": "/abs/path/to/docs", "output_db": "/abs/path/to/docs.sqlite"}'

reyn pipe run rag_query.query \
  --input '{"query_text": "how does X work?", "db": "/abs/path/to/docs.sqlite", "top_k": 5}'
```

`input_path` **must be absolute** — the pipeline globs it directly. It may be a folder or a single file.

The ingest reports what it did: `files_scanned`, `chunks_upserted`, `chunks_removed`, `chunks_unchanged_skipped`, the resolved `embedding_model`, and the spend (`tokens_embedded` / `cost_usd` / `priced`). A `cost_usd` of `null` with `priced: false` means the model has no price entry — the cost is **unknown**, not zero.

The query returns `[{id, distance, metadata}, ...]`, nearest first. `metadata` carries `source_path` / `chunk_index` / `content_hash` / `embedding_model` — **not the chunk text**; use `source_path` to go read the original.

### If a server isn't reachable

`rag_ingest` **pre-flights all three servers before spending anything on embeddings** and returns a message naming the one that failed plus a concrete remedy — rather than a bare `ImportError: No module named 'apsw'` from inside a subprocess. Follow the remedy it prints; it is written for exactly this situation.

## Keeping the corpus current

**Just re-run the ingest** on the same `input_path` and `output_db`. It is incremental by `content_hash`: unchanged chunks are skipped (never re-embedded), changed chunks are re-embedded and replaced, and chunks whose file disappeared are deleted. `estimated_tokens_saved_by_dedup` reports what the skip saved you.

**Don't delete the sqlite to "refresh" it** — that pays the full embedding cost again for documents that never changed.

## One sqlite file = one embedding model

Both pipelines take `embedding_model` (default `"standard"`). **If you set it on one, set the same value on the other.** A mismatch changes the vector space: you either get a `VectorDimensionMismatchError`, or — same dimension, different model — **quietly meaningless results with no error at all**. Different model → different sqlite file. To re-embed with a new model, ingest into a **new** `output_db`.

## Tuning chunking

`chunk_size` (default 400 tokens) and `chunk_overlap_ratio` (default 0.125) are pipeline inputs, not baked-in constants. The defaults are the 2026 persistent-RAG band (256–512 tokens, 10–15% overlap) and suit most corpora. Raise the size for dense prose whose ideas span paragraphs; lower it for reference material queried by narrow fact.

**Changing either re-chunks everything**, which changes every `content_hash` and re-embeds the whole corpus. Decide before your first large ingest.

Other inputs: `file_extensions` (which formats to pick up from a folder), `max_files` (default 10000), and the three `*_server` names below.

## Swapping the backend — copy the pipeline

Want Qdrant instead of sqlite-vec, a different chunker, or Docling instead of MarkItDown? **Copy `src/reyn/builtin/pipelines/rag_ingest.yaml` (+ `rag_query.yaml`) into your project and re-point the `*_server` inputs** at your replacement. This is the **intended extension mechanism, not a workaround**: Reyn deliberately builds no adapter for a user's RAG store, so the builtin pipeline *is* the template you copy.

Every server name is an input with a default, so a drop-in replacement exposing the same tools (`upsert` / `query` / `list_metadata` / `delete`) needs no file edit at all — just name it:

```bash
reyn pipe run rag_ingest.ingest --input '{
  "input_path": "/abs/docs", "output_db": "/abs/docs.sqlite",
  "vectorstore_server": "my_qdrant"}'
```

Read the pipeline before you copy it — it is written plainly, on purpose, because you are meant to read it.

## Related

- [`cookbook/configs/with-builtin-rag-mcp.yaml`](../../cookbook/configs/with-builtin-rag-mcp.yaml) — the config block to copy, with every tool signature
- [Enable semantic search](enable-semantic-search.md) — the *other* RAG: Reyn's own in-core index
- [Concepts: RAG](../../concepts/data-retrieval/rag.md) — embedding classes, cost, the in-core `IndexBackend`
- [Manage permissions](manage-permissions.md) — how the `mcp.<server>` grants above are evaluated
- [Write a pipeline](write-a-pipeline.md) — the DSL the two builtin pipelines are written in
