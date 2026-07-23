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
permission gate prompts them before anything reaches config.

```
plugin_management__install(source={"kind": "builtin", "name": "rag"})
mcp__install_local(name="reyn_markitdown", command="uvx", args=["markitdown-mcp"])
```

`plugin_management__install` is **register-only** (#3209) -- it does NOT
install the plugin's Python dependencies for you. Do this next, via
`exec`, entirely from chat -- no operator keypress needed:

**1. Create the venv INSIDE the current project workspace** -- never under
`~/.reyn/...` (home dir). Your sandbox's write scope is the project
directory only; a home-dir path fails with "Operation not permitted", and a
shared home-dir path also collides across separate projects/sessions.

```bash
python3 -m venv ./.venv-rag
./.venv-rag/bin/pip install -r ~/.reyn/plugins/rag/requirements.txt
```

Windows: `python -m venv .venv-rag` then
`.venv-rag\Scripts\pip.exe install -r %USERPROFILE%\.reyn\plugins\rag\requirements.txt`.

**2. Point the two registered servers at that venv** -- edit
`.reyn/config/mcp.yaml`'s `mcp.servers.reyn_chunker.command` and
`mcp.servers.reyn_vector_store.command` (the entries
`plugin_management__install` just wrote) to the venv's OWN interpreter,
absolute path:

```yaml
mcp:
  servers:
    reyn_chunker:
      command: /abs/path/to/this/project/.venv-rag/bin/python   # Windows: ...\.venv-rag\Scripts\python.exe
    reyn_vector_store:
      command: /abs/path/to/this/project/.venv-rag/bin/python   # Windows: ...\.venv-rag\Scripts\python.exe
```

**Edit `command` ONLY -- leave `args` exactly as written.** `args` is
already the plugin's own absolute script path (e.g.
`~/.reyn/plugins/rag/scripts/chunker_server.py`) -- there is no
`-m reyn_chunker` module form; inventing one breaks spawn. And it is
`.venv-rag`, not a bare `venv/` -- the leading dot matters.

A probe/spawn against an unready or wrong-path venv fails fast with a clear
error -- reyn never falls back to a runtime fetch. Full detail
(troubleshooting, markitdown's own venv): `install-and-venv-setup.md`.

## Next steps -- which reference answers your question

Once installed:

1. **Embedding provider.** `rag_ingest` needs a working embedding provider,
   or every chunk it embeds is wasted spend. Have an API key? read
   `configure-embedding-provider.md`. No key / offline? read
   `configure-local-embedding-model.md`.
2. **Run it.** The exact `pipeline__run` calls -- copy these param names
   **verbatim**, a light model has generated `corpus_path`/`db_path`
   (ingest) and other plausible-sounding names unprompted:

   ```
   pipeline__run(name="rag_ingest.ingest", input={
     "input_path": "/abs/path/to/docs",   # required; absolute; folder or one file
     "output_db": "./rag/docs.sqlite",    # required; cwd-relative, zero-config
   })

   pipeline__run(name="rag_query.query", input={
     "query_text": "how does X work?",    # required
     "db": "./rag/docs.sqlite",           # required; SAME file output_db wrote.
                                           # EXACT name "db" -- not "db_path",
                                           # not "vector_store_path", not "corpus_path".
   })
   ```

   Full detail (absolute-vs-cwd-relative path rules, `top_k`/
   `embedding_model` defaults, the returned summary fields, and the
   `embedding_model` mismatch that silently ruins a corpus):
   `run-ingest-and-query-workflow.md`.
3. **Internals.** For the sqlite schema, incremental re-ingest, tuning, or
   swapping the vector-store/chunker/parser backend, read
   `corpus-internals-schema-tuning-and-backend-swap.md`.

Full setup + backend-swap guide: `docs/guide/for-users/build-a-rag-corpus.md`.
Config to copy: `docs/cookbook/configs/with-builtin-rag-mcp.yaml`.

## Bundled references

- [install-and-venv-setup.md](${CLAUDE_SKILL_DIR}/references/install-and-venv-setup.md)
  -- supplementary detail for the Prerequisites steps above: why the venv
  must be workspace-relative, troubleshooting, and markitdown's own
  fallback venv.
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
