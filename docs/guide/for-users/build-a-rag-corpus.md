# Build a RAG corpus from your own documents

Point Reyn at a folder of documents (`txt` / `md` / `pdf` / `xlsx` / `pptx` / `docx`), get a **sqlite file you own** that Reyn can search by meaning. The builtin `rag` **plugin** does it: `rag_ingest.ingest` builds the store, `rag_query.query` searches it.

> **TL;DR**: ask Reyn to ingest your folder. Reyn installs the `rag` plugin (+ a third-party markitdown MCP server) itself via `plugin_management__install` — **it asks you before writing anything to your config**. Nothing runs until that install is approved.

## Is this the RAG you want?

Reyn has **two**, and they are not interchangeable:

| | **this guide** (builtin user RAG) | **[semantic search](enable-semantic-search.md)** (in-core RAG) |
|---|---|---|
| Where the data lives | **a sqlite file you name** — yours to keep, copy, or hand to another tool | Reyn's own index under `.reyn/index/<source>/` |
| What you set up | the `rag` plugin + a markitdown MCP server (Reyn installs them; you approve) | an indexed source; no servers |
| Reads pdf/xlsx/pptx/docx | **yes** (via the markitdown server) | only what your indexing code chunked |
| Use it when | you have **a folder of documents** and want a portable corpus | you want Reyn to recall docs you already registered as a source |

If you just want Reyn to search docs you already index, you want [Enable semantic search](enable-semantic-search.md) — it needs no plugin. Come here when you have documents in real-world formats and want a store you own.

## Why it ships off

Nothing in the `rag` plugin is installed by default. Once an MCP server appears under `mcp.servers.<name>` in any merged config, `reyn pipe run` auto-grants it — so a server must never land in your config without your say-so. Reyn therefore ships the plugin's **code** (inside the wheel) but never installs or wires it in on your behalf. Reyn can *install* it for you, but every write goes through the permission gate: **you are asked, and a refusal writes nothing.**

Read what you are enabling before you do:

- **`reyn_markitdown`** reads **every file under the folder you point `rag_ingest` at**, and any `uri` it is handed.
- **`reyn_vector_store`** **writes** to whatever sqlite file `db_path` names.
- **`reyn_chunker`** reads no filesystem paths of its own.

## Setup

**Ask Reyn to install the plugin.** In `reyn chat`, ask it to ingest a folder: it installs the `rag` plugin, **asking your permission before it writes** anything —

```
plugin_management__install(source={"kind": "builtin", "name": "rag"})
mcp__install_local(name="reyn_markitdown", command="uvx", args=["markitdown-mcp"])
```

`plugin_management__install` is **register-only**: it copies the plugin's files and registers **both MCP servers, both pipelines, and its RAG skill** together — no `permissions:` block to add, because a configured server is granted when the pipeline runs it. **Refuse and nothing is written.** It does **not** install the plugin's Python dependencies (chonkie/apsw/sqlite-vec/fastmcp) for you — that is a separate, deliberate next step:

> **Create the plugin's own venv, then point the servers at it** — Reyn's LLM does this in-sandbox, following the `build_and_query_rag_corpus` skill's SETUP steps, so you normally don't type these yourself:
>
> ```bash
> python3 -m venv ~/.reyn/plugins/rag/.venv
> ~/.reyn/plugins/rag/.venv/bin/pip install -r ~/.reyn/plugins/rag/requirements.txt
> ```
>
> Windows: the interpreter is at `Scripts\python.exe`, not `bin/python`:
>
> ```powershell
> python -m venv %USERPROFILE%\.reyn\plugins\rag\.venv
> %USERPROFILE%\.reyn\plugins\rag\.venv\Scripts\pip.exe install -r %USERPROFILE%\.reyn\plugins\rag\requirements.txt
> ```
>
> Then edit `.reyn/config/mcp.yaml`'s `mcp.servers.reyn_chunker.command` and `mcp.servers.reyn_vector_store.command` (the two entries `plugin_management__install` just wrote) to that venv's own interpreter, absolute path — see [`cookbook/configs/with-builtin-rag-mcp.yaml`](../../cookbook/configs/with-builtin-rag-mcp.yaml) for the exact shape. If you skip this (or the venv is incomplete), spawning either server **fails fast with a clear error** — Reyn never falls back to fetching the missing dependency at spawn time.

Each server is **probed before its registration is committed**: if a command does not start on your machine, that server is skipped rather than leaving a half-configured entry — do the venv setup above BEFORE registering, or re-run the install after fixing it.

> `sqlite-vec` is **wheel-only** (no sdist), so your venv's `pip install` needs a package index that serves wheels — an sdist-only internal mirror cannot install it, and musl/Alpine has no wheel at all. Reyn's own `python:3.12-slim` base image is glibc, so containers are unaffected.

> **Firewalled network?** `uvx` fetches `markitdown-mcp` from PyPI on first run, and your own `pip install -r requirements.txt` above fetches the `rag` plugin's deps from PyPI. If PyPI is blocked for `markitdown-mcp`, give it its **own venv** — never Reyn's — and point `command` at the absolute path:
>
> ```bash
> python3 -m venv ~/.reyn-markitdown
> ~/.reyn-markitdown/bin/pip install markitdown-mcp
> ```
>
> then use `command: /home/you/.reyn-markitdown/bin/markitdown-mcp` with `args: []`. Reyn starts whatever `command` names, as-is, so an absolute path to a script whose environment actually has the package is the reliable form.

**Why does the registered `reyn_chunker`/`reyn_vector_store` command need to be an absolute path to a venv interpreter (`.venv/bin/python` on macOS/Linux, `.venv\Scripts\python.exe` on Windows), not `python`?** `plugin_management__install` registers whatever `command` the plugin's own `.mcp.json` names, unmodified — pointing it at YOUR venv's absolute interpreter path is what makes spawning it independent of your ambient `PATH`'s `python3`, which is a *different* interpreter under `pipx install reyn`, a non-activated venv, or a `PATH` with another python first.

## Use it

In `reyn chat`, just ask — *"ingest the documents in /abs/path/to/docs into a searchable store, then tell me what they say about X"*. Reyn reads its `build_and_query_rag_corpus` skill (reading a bundled reference as needed) and drives both pipelines.

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

`rag_ingest` **pre-flights all three servers before spending anything on embeddings** and returns a message naming the one that failed plus a concrete remedy (`plugin_management__install(source={"kind": "builtin", "name": "rag"})` for the two builtin servers, `mcp__install_local(...)` for markitdown) — rather than a bare `ImportError: No module named 'apsw'` from inside a subprocess. Follow the remedy it prints; it is written for exactly this situation.

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

Want Qdrant instead of sqlite-vec, a different chunker, or Docling instead of MarkItDown? **Copy `~/.reyn/plugins/rag/pipelines/rag_ingest.yaml` (+ `rag_query.yaml`, present once the plugin is installed) into your project and re-point the `*_server` inputs** at your replacement. This is the **intended extension mechanism, not a workaround**: Reyn deliberately builds no adapter for a user's RAG store, so the builtin pipeline *is* the template you copy. Want to keep the edit reusable across projects? Promote it back as your own plugin: `plugin_management__install(source={"kind": "local", "path": "..."})`.

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
